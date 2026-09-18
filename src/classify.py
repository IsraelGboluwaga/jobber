"""Keyword/rule classifier for the categorical fields.

No LLM. Every field is derived from explicit signals in the job text. For
`sponsorship` and `relocation` we write yes/no ONLY on an explicit signal, and
`undefined` otherwise — never inferred yes from silence.

An optional LLM fallback (`llm_label`) exists for wording too varied for regex,
but it only produces labels; the keep/drop DECISION always lives in viability.
"""
from __future__ import annotations

import logging
import re

from .acquire import Job

log = logging.getLogger(__name__)


def _has(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


# --- job_type -------------------------------------------------------------
_REMOTE = [r"\bfully remote\b", r"\b100% remote\b", r"\bremote[- ]first\b", r"\bremote\b"]
_HYBRID = [r"\bhybrid\b", r"\bpartially remote\b", r"\bdays (?:in|per week) (?:in )?(?:the )?office\b"]
_ONSITE = [r"\bon[- ]?site\b", r"\bin[- ]office\b", r"\brelocate to\b", r"\bbased in\b"]


def classify_job_type(text: str, geo_tokens: set[str]) -> str:
    if _has(text, _HYBRID):
        return "hybrid"
    if "remote" in geo_tokens or _has(text, _REMOTE):
        return "remote"
    if _has(text, _ONSITE):
        return "onsite"
    return "onsite"  # conservative default: unstated => treat as onsite


# --- remote_scope ---------------------------------------------------------
_WORLDWIDE = [r"\bremote,? *worldwide\b", r"\bwork from anywhere\b", r"\bfully distributed\b",
              r"\bglobally remote\b", r"\bremote \(?global\)?\b", r"\banywhere in the world\b"]
_EMEA = [r"\bEMEA\b", r"\bremote \(?EMEA\)?\b", r"\beurope,? middle east\b"]
_COUNTRY_LOCK = [
    r"\bmust be (?:legally )?authoriz(?:e|s)ed to work in ([A-Za-z .]+)",
    r"\b([A-Za-z .]+?)[- ]based only\b",
    r"\bmust (?:reside|be located) in ([A-Za-z .]+)",
    r"\b(US|U\.S\.|United States) only\b",
    r"\bwork authorization in ([A-Za-z .]+) required\b",
]
_REGION_LOCK = [r"\bremote \(?([A-Za-z ]+ only)\)?", r"\btime ?zones?\b.*\bonly\b"]


def classify_remote_scope(text: str, job_type: str) -> str:
    if job_type != "remote":
        return "n/a"
    if _has(text, _WORLDWIDE):
        return "worldwide"
    if _has(text, _EMEA):
        return "emea"
    if _has(text, _COUNTRY_LOCK):
        return "country-locked"
    if _has(text, _REGION_LOCK):
        return "region-locked"
    return "unclear"


# --- sponsorship ----------------------------------------------------------
_SPONSOR_YES = [
    r"\bvisa sponsorship (?:is )?(?:available|provided|offered)\b",
    r"\bwe (?:can |will |do )?sponsor(?:ship)?\b",
    r"\bsponsorship available\b",
    r"\bwill provide visa\b",
]
_SPONSOR_NO = [
    r"\bno visa sponsorship\b",
    r"\bunable to sponsor\b",
    r"\bcannot (?:provide|offer) sponsorship\b",
    r"\bsponsorship (?:is )?not available\b",
    r"\bwe do(?:es)? not sponsor\b",
    r"\bmust (?:already )?have (?:the )?(?:legal )?right to work\b",
    r"\bmust be (?:legally )?authoriz(?:e|s)ed to work\b(?!.*sponsor)",
]


def classify_sponsorship(text: str) -> str:
    if _has(text, _SPONSOR_YES):
        return "yes"
    if _has(text, _SPONSOR_NO):
        return "no"
    return "undefined"


# --- relocation -----------------------------------------------------------
_RELOC_YES = [r"\brelocation (?:assistance|support|package|allowance)\b",
              r"\bwe(?:'ll| will) help you relocate\b", r"\brelocation (?:is )?provided\b"]
_RELOC_NO = [r"\bno relocation\b", r"\brelocation (?:is )?not (?:available|provided|offered)\b"]


def classify_relocation(text: str) -> str:
    if _has(text, _RELOC_YES):
        return "yes"
    if _has(text, _RELOC_NO):
        return "no"
    return "undefined"


# --- seniority ------------------------------------------------------------
def classify_seniority(title: str, text: str, levels: list[str]) -> str:
    blob = f"{title} {text[:400]}".lower()
    # Prefer the most senior configured level that appears.
    order = ["principal", "staff", "lead", "senior", "mid", "junior"]
    present = [lvl for lvl in order if lvl in blob]
    for lvl in present:
        if lvl in [x.lower() for x in levels]:
            return lvl
    return present[0] if present else ""


# --------------------------------------------------------------------------

def classify(job: Job, cfg, llm_client=None) -> Job:
    """Populate categorical fields on `job` in place and return it."""
    text = f"{job.title}\n{job.description}"
    job.job_type = classify_job_type(text, job.geo_tokens)
    job.remote_scope = classify_remote_scope(text, job.job_type)
    job.sponsorship = classify_sponsorship(text)
    job.relocation = classify_relocation(text)
    job.seniority = classify_seniority(job.title, job.description, cfg.search.get("seniority", []))
    _derive_geo(job, cfg)

    # Optional LLM label fallback ONLY where regex was inconclusive. Labels only;
    # never a keep/drop decision. Kept off unless a client is supplied.
    if llm_client is not None and (job.remote_scope == "unclear" or job.sponsorship == "undefined"):
        _llm_label(job, llm_client, cfg)
    return job


_COUNTRY_HINTS = {
    "united kingdom": ("UK", "UK"), "uk": ("UK", "UK"), "england": ("UK", "UK"),
    "london": ("UK", "UK"), "scotland": ("UK", "UK"),
    "ireland": ("Ireland", "Ireland"), "dublin": ("Ireland", "Ireland"),
    "germany": ("Germany", "EU"), "berlin": ("Germany", "EU"),
    "netherlands": ("Netherlands", "EU"), "amsterdam": ("Netherlands", "EU"),
    "france": ("France", "EU"), "spain": ("Spain", "EU"), "portugal": ("Portugal", "EU"),
    "canada": ("Canada", "Canada"), "toronto": ("Canada", "Canada"),
    "united states": ("US", "US"), "usa": ("US", "US"), "new york": ("US", "US"),
    "san francisco": ("US", "US"), "remote us": ("US", "US"),
    "australia": ("Australia", "Australia"), "sydney": ("Australia", "Australia"),
    "melbourne": ("Australia", "Australia"),
    "new zealand": ("New Zealand", "New Zealand"), "auckland": ("New Zealand", "New Zealand"),
    "wellington": ("New Zealand", "New Zealand"),
    "singapore": ("Singapore", "Singapore"),
    "united arab emirates": ("UAE", "Middle East"), "uae": ("UAE", "Middle East"),
    "dubai": ("UAE", "Middle East"), "abu dhabi": ("UAE", "Middle East"),
    "india": ("India", "India"), "bangalore": ("India", "India"), "bengaluru": ("India", "India"),
    "nigeria": ("Nigeria", "Africa"), "kenya": ("Kenya", "Africa"),
    "south africa": ("South Africa", "Africa"),
}


def _derive_geo(job: Job, cfg) -> None:
    """Set job.country / job.region from location tokens (best effort)."""
    blob = " ".join(job.geo_tokens)
    for hint, (country, region) in _COUNTRY_HINTS.items():
        if hint in blob:
            job.country = job.country or country
            job.region = job.region or region
    # If still unknown but a country-lock was detected in text, capture it.
    if not job.country:
        m = re.search(r"authoriz(?:e|s)ed to work in ([A-Za-z .]+)", job.description, re.IGNORECASE)
        if m:
            token = m.group(1).strip(" .").lower()
            for hint, (country, region) in _COUNTRY_HINTS.items():
                if hint in token:
                    job.country, job.region = country, region
                    break


def _llm_label(job: Job, llm_client, cfg) -> None:
    """Cheap best-effort label extraction for varied wording. Labels only."""
    from .llm import complete

    system = (
        "You extract labels from a job description. Reply with exactly two lines:\n"
        "remote_scope: <worldwide|emea|region-locked|country-locked|unclear|n/a>\n"
        "sponsorship: <yes|no|undefined>\n"
        "Use 'undefined' unless sponsorship is explicitly stated. Do not explain."
    )
    try:
        out = complete(system, job.description[:3000], max_tokens=40, client=llm_client, cfg=cfg)
    except Exception as exc:  # noqa: BLE001 - labels are optional; never fail the pipeline
        log.debug("LLM label fallback failed: %s", exc)
        return
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip().lower()
        if key == "remote_scope" and job.remote_scope == "unclear" and val:
            job.remote_scope = val
        elif key == "sponsorship" and job.sponsorship == "undefined" and val in ("yes", "no"):
            job.sponsorship = val


def classify_all(jobs: list[Job], cfg, llm_client=None) -> list[Job]:
    return [classify(j, cfg, llm_client) for j in jobs]
