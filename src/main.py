"""Orchestrates one run.

Pipeline: acquire -> max-age filter -> classify -> assign ids + collapse dups ->
drop already-seen (Notion) -> score master_match -> viability (drop/label) ->
rank + clamp -> budget preflight -> tailor (LLM) -> score tailored_match ->
write to Notion (row + child CV page + answers) -> rollover -> notify.

Cost/safety guards are enforced here: hard_max clamp before the LLM loop,
viability before any LLM call, budget preflight, explicit max_tokens (in llm.py),
thinking off (llm.py), token logging to stdout.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

from . import dedup, notify, score, viability
from .acquire import Job, acquire_all
from .classify import classify_all
from .config import MASTER_CV_PATH, load_config
from .llm import make_client, reset_usage, usage
from .tailor import tailor

log = logging.getLogger("jobber")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _load_master_cv() -> dict:
    if not MASTER_CV_PATH.exists():
        raise FileNotFoundError(
            f"{MASTER_CV_PATH} not found. Run scripts/import_cv.py once to build it "
            "(or copy data/master_cv.example.json and edit)."
        )
    with open(MASTER_CV_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _filter_max_age(jobs: list[Job], max_age_days: int) -> list[Job]:
    if not max_age_days:
        return jobs
    cutoff = dt.datetime.now(tz=dt.UTC).date() - dt.timedelta(days=max_age_days)
    kept = []
    for j in jobs:
        if not j.posted_date:
            kept.append(j)  # missing date does not drop the row
            continue
        try:
            if dt.date.fromisoformat(j.posted_date) >= cutoff:
                kept.append(j)
        except ValueError:
            kept.append(j)
    log.info("Max-age filter (%dd): %d -> %d.", max_age_days, len(jobs), len(kept))
    return kept


def _budget_preflight(cfg, n_rows: int) -> None:
    per = int(cfg.llm.get("rough_tokens_per_job", 4000))
    ceiling = int(cfg.llm.get("budget_ceiling_tokens", 80000))
    estimate = n_rows * per
    log.info("Budget preflight: %d rows x %d = ~%d tokens (ceiling %d).",
             n_rows, per, estimate, ceiling)
    if estimate > ceiling:
        raise RuntimeError(
            f"Estimated {estimate} tokens exceeds ceiling {ceiling}. Aborting before "
            "any LLM call. Lower search.count or raise llm.budget_ceiling_tokens."
        )


def run(dry_run: bool = False, config_path: str | None = None) -> int:
    cfg = load_config(config_path)
    master_cv = _load_master_cv()
    cv_text = score.cv_to_text(master_cv)

    # 1. acquire
    jobs = acquire_all(cfg)

    # 2. max-age filter
    jobs = _filter_max_age(jobs, int(cfg.search.get("max_age_days", 30)))

    # 3. classify (deterministic; no LLM client passed => no token spend)
    jobs = classify_all(jobs, cfg)

    # 4. ids + in-run collapse
    jobs = dedup.assign_ids(jobs)
    jobs = dedup.collapse_duplicates(jobs)

    # 5. drop already-seen in Notion (cross-status). Skippable only in dry-run
    #    when Notion creds are absent.
    store = None
    seen_ids: set[str] = set()
    if not dry_run or (cfg.secrets.notion_token and cfg.secrets.notion_database_id):
        try:
            from .notion_store import NotionStore

            store = NotionStore(cfg)
            seen_ids = store.existing_job_ids()
        except Exception as exc:
            if dry_run:
                log.warning("Dry-run: could not read Notion for dedup (%s); continuing.", exc)
            else:
                raise
    jobs = dedup.drop_already_seen(jobs, seen_ids)

    # 6. deterministic master_match (used for ranking)
    for j in jobs:
        j.master_match = score.coverage_score(cv_text, f"{j.title}\n{j.description}")

    # 7. viability: drop dead-ends, label + fit-note survivors  (BEFORE any LLM)
    survivors, dropped = viability.evaluate(jobs, cfg)

    # 8. rank + clamp to hard_max, then keep top N=count
    ranked = viability.rank(survivors, cfg)
    count = int(cfg.search.get("count", 10))
    hard_max = int(cfg.search.get("hard_max", 15))
    keep_n = min(count, hard_max)
    ranked = ranked[:hard_max]        # absolute ceiling
    kept = ranked[:keep_n]            # only these are tailored

    _print_plan(kept, dropped, dry_run)

    # 9. budget preflight (only the kept rows reach the LLM)
    _budget_preflight(cfg, len(kept))

    # 10. tailor (LLM) — only the kept N. Never re-tailors existing rows.
    reset_usage()
    if kept:
        try:
            client = make_client(cfg)
        except Exception as exc:
            if dry_run:
                log.warning("Dry-run: no LLM client (%s); skipping tailoring.", exc)
                client = None
            else:
                raise
        if client is not None:
            for j in kept:
                tailor(j, master_cv, cfg, client)
                j.tailored_match = score.coverage_score(
                    j.tailored_cv or cv_text, f"{j.title}\n{j.description}"
                )

    # 11. write + rollover + notify
    new_count = 0
    if dry_run:
        log.info("Dry-run: writing nothing to Notion, sending no notification.")
    else:
        assert store is not None  # guaranteed above: not dry_run => raise on failed init
        for j in kept:
            store.insert(j)
            new_count += 1
        store.rollover()

    u = usage()
    log.info("LLM usage this run: calls=%d prompt=%d completion=%d total=%d",
             u["calls"], u["prompt"], u["completion"], u["total"])

    if not dry_run:
        notify.notify_success(cfg.ntfy_topic, new_count)
    log.info("Done. %d new roles%s.", len(kept) if dry_run else new_count,
             " (dry-run, not written)" if dry_run else " written to Notion")
    return 0


def _print_plan(kept: list[Job], dropped, dry_run: bool) -> None:
    print("\n" + "=" * 70)
    print(f"PLAN — {len(kept)} to tailor, {len(dropped)} dropped"
          + (" (DRY RUN)" if dry_run else ""))
    print("=" * 70)
    print("\nKEEP (tailor + write):")
    for j in kept:
        print(f"  [{j.viability:<6}] {j.master_match:3d}% {j.source_type:<6} "
              f"{j.company} — {j.title} ({j.country or '?'}) :: {j.fit_note}")
    print("\nDROPPED (never tailored):")
    for d in dropped:
        print(f"  {d.job.company} — {d.job.title} ({d.job.country or '?'}) :: {d.reason}")
    print("=" * 70 + "\n")


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description="Daily job-search pipeline.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run everything but write nothing to Notion and send no notification.")
    parser.add_argument("--config", default=None, help="Path to config.yaml.")
    args = parser.parse_args(argv)

    cfg_for_fail = None
    try:
        cfg_for_fail = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - best-effort so failure notification still has a topic
        log.debug("Could not preload config for failure notification: %s", exc)

    try:
        return run(dry_run=args.dry_run, config_path=args.config)
    except Exception as exc:
        log.exception("Run failed")
        if not args.dry_run and cfg_for_fail is not None:
            notify.notify_failure(cfg_for_fail.ntfy_topic, str(exc)[:200])
        return 1


if __name__ == "__main__":
    sys.exit(main())
