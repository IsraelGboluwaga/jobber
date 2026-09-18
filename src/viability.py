"""Geo/salary/source viability rules: drop or rank each role.

Runs after classification+dedup and BEFORE any LLM tailoring, so dead-end roles
cost no tokens. Every decision is a deterministic rule carrying a human-readable
reason. The model is never the judge here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .acquire import DIRECT_SOURCE_TYPE, Job

log = logging.getLogger(__name__)

TIER_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Dropped:
    job: Job
    reason: str


def _lc(items) -> set[str]:
    return {str(x).strip().lower() for x in (items or [])}


def _fx_to_usd(job: Job, fx: dict) -> tuple[float | None, float | None]:
    factor = fx.get((job.currency or "").upper())
    if factor is None:
        return None, None
    lo = job.salary_min * factor if job.salary_min else None
    hi = job.salary_max * factor if job.salary_max else None
    return lo, hi


def _region_matches(region: str, welcome: set[str]) -> bool:
    return (region or "").lower() in welcome


def _africa_eligible(job: Job, signals: set[str]) -> bool:
    """Remote role whose scope makes it Africa-eligible."""
    if job.job_type != "remote":
        return False
    if job.remote_scope in ("worldwide", "emea"):
        return True
    # explicit signal words appearing in geo tokens / scope
    hay = " ".join(job.geo_tokens) + " " + job.remote_scope + " " + job.region.lower()
    return any(sig in hay for sig in signals)


def evaluate(jobs: list[Job], cfg) -> tuple[list[Job], list[Dropped]]:
    geo = cfg.geo
    salary = cfg.salary
    exclude = _lc(geo.get("exclude_countries"))
    welcome = _lc(geo.get("welcome_regions"))
    onsite_dead = _lc(geo.get("onsite_dead_regions"))
    africa_signals = _lc(geo.get("remote_africa_signals"))
    unlisted_rule = str(geo.get("unlisted_region", "low")).lower()
    hard_sponsor = bool(geo.get("hard_sponsorship_filter"))
    fx = {k.upper(): float(v) for k, v in (salary.get("fx") or {}).items()}
    min_usd = float(salary.get("min_usd", 0))
    max_usd = float(salary.get("max_usd", 1e12))

    survivors: list[Job] = []
    dropped: list[Dropped] = []

    for job in jobs:
        country_l = (job.country or "").lower()
        region_l = (job.region or "").lower()

        # --- HARD DROPS ---------------------------------------------------
        if country_l in exclude or region_l in exclude:
            dropped.append(Dropped(job, f"Excluded country ({job.country or job.region})"))
            continue

        if region_l in onsite_dead and job.job_type in ("hybrid", "onsite"):
            dropped.append(Dropped(job, f"{job.region} {job.job_type}: won't hire/sponsor from NG"))
            continue

        if (job.job_type == "remote" and job.remote_scope in ("country-locked", "region-locked")
                and not _africa_eligible(job, africa_signals) and not _region_matches(job.region, welcome)):
            dropped.append(Dropped(job, f"Remote locked to a place I can't work ({job.remote_scope})"))
            continue

        lo, hi = _fx_to_usd(job, fx)
        if lo is not None or hi is not None:
            top = hi if hi is not None else lo
            bottom = lo if lo is not None else hi
            assert top is not None and bottom is not None
            if top < min_usd or bottom > max_usd:
                dropped.append(Dropped(job, f"Salary ~${int(bottom):,}-${int(top):,} USD outside band"))
                continue

        if hard_sponsor and job.sponsorship == "no":
            dropped.append(Dropped(job, "Sponsorship explicitly no (hard filter on)"))
            continue

        # --- LABEL SURVIVORS ---------------------------------------------
        tier, note = _label(job, welcome, onsite_dead, africa_signals, unlisted_rule)
        if tier is None:  # unlisted_region == drop
            dropped.append(Dropped(job, note))
            continue
        job.viability = tier
        job.fit_note = note
        survivors.append(job)

    log.info("Viability: %d survivors, %d dropped.", len(survivors), len(dropped))
    return survivors, dropped


def _label(job: Job, welcome: set[str], onsite_dead: set[str],
           africa_signals: set[str], unlisted_rule: str) -> tuple[str | None, str]:
    """Return (tier, fit_note). tier None means 'drop' (only for unlisted+drop)."""
    region = job.region or job.country or "unknown"
    region_l = region.lower()

    # high: Africa-eligible remote, OR welcome region with sponsorship yes
    if _africa_eligible(job, africa_signals):
        return "high", f"Remote, {job.remote_scope} scope — Africa-eligible"
    if region_l in welcome and job.sponsorship == "yes":
        return "high", f"{region}, sponsorship stated"

    # medium: welcome region, sponsorship undefined
    if region_l in welcome and job.sponsorship == "undefined":
        return "medium", f"{region}, sponsorship not stated"

    # low: welcome region sponsorship no; OR US/Canada remote with genuinely global scope
    if region_l in welcome and job.sponsorship == "no":
        return "low", f"{region}, no sponsorship — long odds"
    if region_l in onsite_dead and job.job_type == "remote" and job.remote_scope == "worldwide":
        return "low", f"{region} remote, global scope, no sponsorship signal"

    # --- unlisted region: in none of the lists, not Africa-eligible ------
    if unlisted_rule == "drop":
        return None, f"Unlisted region ({region}) — dropped per geo.unlisted_region"
    return "low", f"Unlisted region ({region}) — surfaced last per geo.unlisted_region"


def rank(jobs: list[Job], cfg) -> list[Job]:
    """Order by viability tier, then source_type (direct first if prefer_direct),
    then deterministic match %. Caller keeps the top N."""
    prefer_direct = bool(cfg.source.get("prefer_direct", True))

    def key(job: Job):
        tier = TIER_ORDER.get(job.viability, 3)
        direct_rank = 0 if (prefer_direct and job.source_type == DIRECT_SOURCE_TYPE) else 1
        return (tier, direct_rank, -int(job.master_match))

    return sorted(jobs, key=key)
