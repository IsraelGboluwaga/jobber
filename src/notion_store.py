"""Notion store: read existing rows (all statuses) for dedup, write new rows,
put the tailored CV on its own child page, drafted answers in the row body, and
roll over stale New rows.

Property schema (create the database with exactly these — see README):
  Company (title), Job title (rich_text w/ link), Country (select),
  Master match % (number), Tailored match % (number), Job type (select),
  Sponsorship (select), Relocation support (select), Viability (select),
  Fit note (rich_text), Status (select), Tailored CV (url), Has questions (select),
  Job ID (rich_text), Source (select), Posted date (date),
  Date first seen (date), Seniority (select), Salary (rich_text)
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, cast

from .acquire import Job

log = logging.getLogger(__name__)

ARCHIVED_STATUS = "Archived"
NEW_STATUS = "New"
APPLYING_STATUS = "Applying"
RICH_TEXT_LIMIT = 1900  # Notion caps a single rich_text content at 2000 chars


# --------------------------------------------------------------------------
# markdown -> Notion blocks (small, dependency-free converter)
# --------------------------------------------------------------------------

def _chunks(text: str, size: int = RICH_TEXT_LIMIT) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _rich(text: str, link: str | None = None) -> list[dict]:
    out = []
    for chunk in _chunks(text):
        rt: dict[str, Any] = {"type": "text", "text": {"content": chunk}}
        if link:
            rt["text"]["link"] = {"url": link}
        out.append(rt)
    return out


def _para(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich(text)}}


def _heading(text: str, level: int) -> dict:
    level = min(max(level, 1), 3)
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": _rich(text)}}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rich(text)}}


def markdown_to_blocks(md: str) -> list[dict]:
    """Minimal Markdown -> blocks: #/##/### headings, - / * bullets, blank-line
    separated paragraphs. Notion caps children at 100 per request; caller chunks."""
    blocks: list[dict] = []
    for raw_line in (md or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("### "):
            blocks.append(_heading(line[4:], 3))
        elif line.startswith("## "):
            blocks.append(_heading(line[3:], 2))
        elif line.startswith("# "):
            blocks.append(_heading(line[2:], 1))
        elif line.lstrip().startswith(("- ", "* ")):
            blocks.append(_bullet(line.lstrip()[2:]))
        else:
            blocks.append(_para(line))
    return blocks or [_para("(empty)")]


def _append_children(client, block_id: str, children: list[dict]) -> None:
    for i in range(0, len(children), 100):  # Notion: max 100 children per call
        client.blocks.children.append(block_id=block_id, children=children[i:i + 100])


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

class NotionStore:
    def __init__(self, cfg):
        from notion_client import Client

        token = cfg.secrets.notion_token
        self.database_id = cfg.secrets.notion_database_id
        if not token or not self.database_id:
            raise RuntimeError("NOTION_TOKEN and NOTION_DATABASE_ID must be set.")
        self.client = Client(auth=token)
        self.cfg = cfg
        self._data_source_id: str | None = None

    def _get_data_source_id(self) -> str:
        """Notion's 2025-09 API split each database into one or more data
        sources; querying rows now goes through data_sources, not databases."""
        if self._data_source_id is None:
            db = cast(dict, self.client.databases.retrieve(database_id=self.database_id))
            sources = db.get("data_sources") or []
            if not sources:
                raise RuntimeError(f"Notion database {self.database_id} has no data sources.")
            self._data_source_id = str(sources[0]["id"])
        return self._data_source_id

    # --- reads -----------------------------------------------------------
    def existing_job_ids(self) -> set[str]:
        """Every Job ID currently in the database, across ALL statuses."""
        ids: set[str] = set()
        cursor: str | None = None
        while True:
            resp = cast(dict, self.client.data_sources.query(
                data_source_id=self._get_data_source_id(),
                start_cursor=cursor,
                page_size=100,
            ))
            for row in resp.get("results", []):
                jid = _plain_text(row.get("properties", {}).get("Job ID"))
                if jid:
                    ids.add(jid)
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        log.info("Loaded %d existing Job IDs from Notion.", len(ids))
        return ids

    # --- writes ----------------------------------------------------------
    def insert(self, job: Job) -> str:
        """Create the row, its child tailored-CV page, and (if any) answers in
        the row body. Returns the row page id."""
        today = dt.datetime.now(tz=dt.UTC).date().isoformat()
        props = self._row_properties(job, today)
        row = cast(dict, self.client.pages.create(
            parent={"data_source_id": self._get_data_source_id()}, properties=props
        ))
        row_id = row["id"]

        # 1) tailored CV on its own dedicated child page, linked from the property
        if job.tailored_cv:
            cv_url = self._create_cv_page(row_id, job)
            if cv_url:
                self.client.pages.update(
                    page_id=row_id,
                    properties={"Tailored CV": {"url": cv_url}},
                )

        # 2) drafted answers in the row's own page body
        if job.answers:
            self._append_answers(row_id, job)

        return row_id

    def _create_cv_page(self, row_id: str, job: Job) -> str | None:
        title = f"Tailored CV — {job.company} — {job.title}"
        blocks = markdown_to_blocks(job.tailored_cv)
        try:
            page = cast(dict, self.client.pages.create(
                parent={"page_id": row_id},
                properties={"title": {"title": _rich(title)}},
                children=blocks[:100],
            ))
            if len(blocks) > 100:
                _append_children(self.client, page["id"], blocks[100:])
            return page.get("url")
        except Exception as exc:  # noqa: BLE001 - a failed CV page must not lose the whole row
            log.warning("Failed to create tailored-CV page for %s: %s", job.company, exc)
            return None

    def _append_answers(self, row_id: str, job: Job) -> None:
        blocks: list[dict] = [_heading("Drafted application answers", 2)]
        for qa in job.answers:
            blocks.append(_heading(qa.get("question", "Question"), 3))
            blocks.append(_para(qa.get("answer", "")))
        try:
            _append_children(self.client, row_id, blocks)
        except Exception as exc:  # noqa: BLE001 - drafted answers are optional; never fail the row
            log.warning("Failed to append answers for %s: %s", job.company, exc)

    def _row_properties(self, job: Job, today: str) -> dict:
        props: dict[str, Any] = {
            "Company": {"title": _rich(job.company or "Unknown")},
            "Job ID": {"rich_text": _rich(job.job_id)},
            "Master match %": {"number": int(job.master_match)},
            "Tailored match %": {"number": int(job.tailored_match)},
            "Status": {"select": {"name": NEW_STATUS}},
            "Has questions": {"select": {"name": "yes" if job.has_questions else "no"}},
            "Date first seen": {"date": {"start": today}},
        }
        if job.title:
            props["Job title"] = {"rich_text": _rich(job.title, link=job.url or None)}
        _set_select(props, "Country", job.country)
        _set_select(props, "Job type", job.job_type)
        _set_select(props, "Sponsorship", job.sponsorship)
        _set_select(props, "Relocation support", job.relocation)
        _set_select(props, "Viability", job.viability)
        _set_select(props, "Source", job.source)
        _set_select(props, "Seniority", job.seniority)
        if job.fit_note:
            props["Fit note"] = {"rich_text": _rich(job.fit_note)}
        if job.salary:
            props["Salary"] = {"rich_text": _rich(job.salary)}
        if job.posted_date:
            props["Posted date"] = {"date": {"start": job.posted_date}}
        return props

    # --- rollover --------------------------------------------------------
    def rollover(self) -> int:
        """Archive New rows whose Date first seen is older than the configured
        window. Never touch Applying. Returns count archived."""
        days = int(self.cfg.rollover.get("archive_new_after_days", 14))
        cutoff = (dt.datetime.now(tz=dt.UTC).date() - dt.timedelta(days=days)).isoformat()
        archived = 0
        cursor: str | None = None
        while True:
            resp = cast(dict, self.client.data_sources.query(
                data_source_id=self._get_data_source_id(),
                filter={
                    "and": [
                        {"property": "Status", "select": {"equals": NEW_STATUS}},
                        {"property": "Date first seen", "date": {"before": cutoff}},
                    ]
                },
                start_cursor=cursor,
                page_size=100,
            ))
            for row in resp.get("results", []):
                self.client.pages.update(
                    page_id=row["id"],
                    properties={"Status": {"select": {"name": ARCHIVED_STATUS}}},
                )
                archived += 1
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        if archived:
            log.info("Rollover: archived %d stale New rows (older than %d days).", archived, days)
        return archived


def _set_select(props: dict, name: str, value: str) -> None:
    if value:
        props[name] = {"select": {"name": str(value)[:100]}}


def _plain_text(prop: dict | None) -> str:
    if not prop:
        return ""
    parts = prop.get("rich_text") or prop.get("title") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()
