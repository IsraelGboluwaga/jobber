"""Acquisition: find postings on the consumer boards (via JobSpy) and normalize
every posting to one schema.

A board listing whose direct apply URL points at a company ATS host is tagged
source_type="direct" so viability can rank it above plain board rows.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

log = logging.getLogger(__name__)

BOARD_SOURCE_TYPE = "board"
DIRECT_SOURCE_TYPE = "direct"

# Hosts that indicate a posting's apply URL resolves to a company ATS.
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com")


@dataclass
class Job:
    """One posting, normalized. Fields beyond acquisition (match %, viability,
    tailored_cv, answers) are filled in later stages and default to empty."""

    company: str
    title: str
    url: str
    source: str                       # board/ATS name: linkedin, greenhouse:monzo, ...
    source_type: str                  # "direct" | "board"
    description: str = ""
    apply_url: str = ""
    country: str = ""
    region: str = ""
    job_type: str = ""                # remote | hybrid | onsite (from classify)
    remote_scope: str = "unclear"     # worldwide | emea | region-locked | country-locked | unclear
    posted_date: str | None = None  # ISO date string
    seniority: str = ""
    sponsorship: str = "undefined"    # yes | no | undefined
    relocation: str = "undefined"     # yes | no | undefined
    salary: str = ""                  # human-readable, when present
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str = ""
    questions: list[str] = field(default_factory=list)
    job_id: str = ""                  # set by dedup

    # later stages
    master_match: int = 0
    tailored_match: int = 0
    viability: str = ""               # high | medium | low
    fit_note: str = ""
    geo_tokens: set[str] = field(default_factory=set)  # location/region hints for viability
    tailored_cv: str = ""             # markdown
    answers: list[dict[str, str]] = field(default_factory=list)  # [{question, answer}]

    @property
    def has_questions(self) -> bool:
        return bool(self.questions)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _iso(value: Any) -> str | None:
    """Best-effort coercion of assorted date shapes to an ISO date string."""
    if value in (None, "", "NaT"):
        return None
    if isinstance(value, (int, float)):  # epoch millis (Ashby/Lever style)
        try:
            secs = value / 1000 if value > 1e12 else value
            return datetime.fromtimestamp(secs, UTC).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    s = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s[: len(fmt) + 6], fmt).date().isoformat()  # noqa: DTZ007 - only the date is kept, tz is irrelevant
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def _is_ats_url(url: str) -> bool:
    return any(host in (url or "").lower() for host in ATS_HOSTS)


# --------------------------------------------------------------------------
# consumer boards via JobSpy
# --------------------------------------------------------------------------

def acquire_boards(cfg) -> list[Job]:
    """Query JobSpy across configured titles x locations. Capped small."""
    search = cfg.search
    try:
        from jobspy import scrape_jobs
    except ImportError:  # pragma: no cover
        log.warning("python-jobspy not installed — skipping consumer boards.")
        return []

    titles = search.get("titles", [])
    locations = search.get("locations", [])
    boards = search.get("boards", ["indeed", "linkedin", "google"])
    max_age_days = int(search.get("max_age_days", 30))
    hours_old = max_age_days * 24
    # Keep each query small; downstream clamps to hard_max anyway.
    per_query = max(3, int(search.get("count", 10)))

    jobs: list[Job] = []
    for title in titles:
        for location in locations:
            is_remote = location.strip().lower() == "remote"
            try:
                df = scrape_jobs(
                    site_name=boards,
                    search_term=title,
                    google_search_term=f"{title} jobs {location}",
                    location=None if is_remote else location,
                    is_remote=is_remote,
                    results_wanted=per_query,
                    hours_old=hours_old,
                    country_indeed=search.get("country_indeed", "United Kingdom"),
                    linkedin_fetch_description=True,
                    verbose=0,
                )
            except Exception as exc:  # noqa: BLE001 - jobspy raises assorted network errors
                log.warning("JobSpy query failed for %r @ %r: %s", title, location, exc)
                continue
            jobs.extend(_jobs_from_dataframe(df, fallback_location=location))
    log.info("Acquired %d raw board postings.", len(jobs))
    return jobs


def _jobs_from_dataframe(df, fallback_location: str) -> list[Job]:
    if df is None or len(df) == 0:
        return []
    out: list[Job] = []
    for row in df.to_dict("records"):
        def g(key: str, default: Any = "", row: dict[str, Any] = row) -> Any:
            val = row.get(key, default)
            if val is None or (isinstance(val, float) and math.isnan(val)):
                return default
            return val

        direct = str(g("job_url_direct") or "")
        listing = str(g("job_url") or "")
        # Prefer the direct apply URL when present.
        url = direct or listing
        source_type = DIRECT_SOURCE_TYPE if (direct and _is_ats_url(direct)) else BOARD_SOURCE_TYPE
        salary_min = g("min_amount", None)
        salary_max = g("max_amount", None)
        currency = str(g("currency") or "")
        out.append(
            Job(
                company=str(g("company") or "Unknown"),
                title=str(g("title") or ""),
                url=url or listing,
                apply_url=direct or listing,
                source=str(g("site") or "board"),
                source_type=source_type,
                description=str(g("description") or ""),
                posted_date=_iso(g("date_posted", None)),
                salary_min=float(salary_min) if _num(salary_min) else None,
                salary_max=float(salary_max) if _num(salary_max) else None,
                currency=currency,
                salary=_salary_str(salary_min, salary_max, currency, g("interval")),
                geo_tokens=_geo_tokens(str(g("location") or fallback_location), bool(g("is_remote"))),
            )
        )
    return out


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)) and v > 0


def _salary_str(lo: Any, hi: Any, currency: str, interval: Any) -> str:
    if not (_num(lo) or _num(hi)):
        return ""
    lo_s = f"{int(lo):,}" if _num(lo) else "?"
    hi_s = f"{int(hi):,}" if _num(hi) else "?"
    unit = f"/{interval}" if interval else ""
    return f"{currency} {lo_s}–{hi_s}{unit}".strip()


def _geo_tokens(location: str, is_remote: bool) -> set[str]:
    tokens = {t.strip().lower() for t in re.split(r"[,/|]", location or "") if t.strip()}
    if is_remote:
        tokens.add("remote")
    return tokens


def acquire_all(cfg) -> list[Job]:
    """All postings from the consumer boards. Classification, dedup, viability,
    scoring happen in later stages.

    A board listing whose direct apply URL resolves to a company ATS
    (Greenhouse/Lever/Ashby) is still tagged source_type="direct" so the
    viability step can rank it above plain board rows — no curated company list
    is needed for that."""
    return acquire_boards(cfg)
