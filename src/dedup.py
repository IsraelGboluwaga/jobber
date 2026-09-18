"""Stable job id + cross-status dedup.

job_id = the board's native id when present, else a stable hash of
normalize(company) + normalize(title) + canonical_url.

Dedup collapses a board listing and its direct-ATS equivalent to the direct one,
and skips anything already seen in Notion in ANY status (New/Applying/Applied/
Uninterested/Archived). That cross-status memory is what stops dismissed jobs
from reappearing.
"""
from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import urlsplit, urlunsplit

from .acquire import DIRECT_SOURCE_TYPE, Job

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w ]", "", (s or "").lower())).strip()


def canonical_url(url: str) -> str:
    """Drop query/fragment and trailing slash so the same posting hashes equally."""
    if not url:
        return ""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def stable_job_id(job: Job, native_id: str | None = None) -> str:
    if native_id:
        return str(native_id)
    basis = f"{_norm(job.company)}|{_norm(job.title)}|{canonical_url(job.url)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _collapse_key(job: Job) -> str:
    """Key used to collapse a board listing and its direct equivalent."""
    return f"{_norm(job.company)}|{_norm(job.title)}"


def assign_ids(jobs: list[Job]) -> list[Job]:
    for job in jobs:
        if not job.job_id:
            job.job_id = stable_job_id(job)
    return jobs


def collapse_duplicates(jobs: list[Job]) -> list[Job]:
    """Within this run, collapse company+title duplicates, preferring direct
    (ATS) over board rows. Also de-dupes identical job_ids."""
    best: dict[str, Job] = {}
    for job in jobs:
        key = _collapse_key(job)
        existing = best.get(key)
        if existing is None:
            best[key] = job
            continue
        # Prefer direct over board; otherwise keep the one with a longer description.
        existing_direct = existing.source_type == DIRECT_SOURCE_TYPE
        job_direct = job.source_type == DIRECT_SOURCE_TYPE
        if job_direct and not existing_direct or job_direct == existing_direct and len(job.description) > len(existing.description):
            best[key] = job
    # second pass: unique job_id
    by_id: dict[str, Job] = {}
    for job in best.values():
        by_id.setdefault(job.job_id, job)
    collapsed = list(by_id.values())
    log.info("Collapsed %d -> %d after in-run dedup.", len(jobs), len(collapsed))
    return collapsed


def drop_already_seen(jobs: list[Job], seen_ids: set[str]) -> list[Job]:
    fresh = [j for j in jobs if j.job_id not in seen_ids]
    log.info("Dropped %d already-in-Notion; %d genuinely new.",
             len(jobs) - len(fresh), len(fresh))
    return fresh
