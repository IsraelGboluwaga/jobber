"""ONE-TIME / manual helper: build data/master_cv.json from a Google Doc.

NOT called by the daily run. Run it yourself whenever you update your resume,
review the printed JSON, and commit data/master_cv.json by hand.

It fetches the Google Doc's plain-text EXPORT endpoint (not the /edit page, which
is a JS app that returns an editor shell), structures the text into the schema
with a single LLM pass (fine here — one-time, off the hot path), writes the file,
and prints it for review.

Usage:
    export CV_DOC_ID=<google-doc-id>   # or pass --doc-id
    export DEEPSEEK_API_KEY=...
    python scripts/import_cv.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import MASTER_CV_PATH, load_config
from src.llm import complete, make_client

EXPORT_TMPL = "https://docs.google.com/document/d/{doc_id}/export?format=txt"

SCHEMA_HINT = {
    "contact": {"name": "", "email": "", "phone": "", "location": "",
                "links": {"github": "", "linkedin": "", "website": ""}},
    "summary": "",
    "experience": [{"company": "", "title": "", "location": "", "start": "",
                    "end": "", "highlights": [""], "stack": [""]}],
    "skills": [""],
    "education": [{"institution": "", "degree": "", "start": "", "end": ""}],
}


def fetch_doc_text(doc_id: str) -> str:
    url = EXPORT_TMPL.format(doc_id=doc_id)
    resp = requests.get(url, timeout=30)
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"Google Doc returned {resp.status_code}. Set the doc's sharing to "
            "'Anyone with the link can view' and re-run."
        )
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or "<html" in text[:200].lower():
        raise SystemExit("Export did not return plain text — check the doc id and sharing.")
    return text


def structure_cv(text: str, cfg) -> dict:
    client = make_client(cfg)
    system = (
        "You convert a resume's plain text into strict JSON. Use ONLY facts present "
        "in the text — never invent. Output JSON only, matching this schema exactly:\n"
        + json.dumps(SCHEMA_HINT)
    )
    raw = complete(system, text[:12000], max_tokens=2000, client=client, cfg=cfg)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("\n") + 1:] if "\n" in raw else raw
    return json.loads(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="One-time: Google Doc -> master_cv.json")
    parser.add_argument("--doc-id", default=os.environ.get("CV_DOC_ID"),
                        help="Google Doc id (or set CV_DOC_ID).")
    parser.add_argument("--out", default=str(MASTER_CV_PATH))
    args = parser.parse_args()

    if not args.doc_id:
        raise SystemExit("Provide --doc-id or set CV_DOC_ID.")

    cfg = load_config()
    print(f"Fetching Google Doc {args.doc_id} (export=txt)...", file=sys.stderr)
    text = fetch_doc_text(args.doc_id)
    print(f"Structuring {len(text)} chars into JSON via the LLM...", file=sys.stderr)
    cv = structure_cv(text, cfg)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(cv, fh, indent=2, ensure_ascii=False)

    print(f"\nWrote {args.out}. Review it below, then commit by hand:\n", file=sys.stderr)
    print(json.dumps(cv, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
