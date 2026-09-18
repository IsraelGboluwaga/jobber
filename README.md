# Jobber — daily job-search pipeline

A lightweight **scheduled pipeline** (no web app, no server, no UI) that runs once
each morning on GitHub Actions and:

1. Finds ~10 postings matching your criteria across the consumer job boards
   (LinkedIn / Indeed / Glassdoor / Google, via JobSpy).
2. Deduplicates, drops roles unlikely to hire from Nigeria, and ranks the rest.
3. Tailors your CV to each new posting (DeepSeek via the OpenAI-compatible API).
4. Drafts answers to each posting's application questions, if any.
5. Writes one row per job into a Notion database, with the tailored CV on its own
   linked page and any drafted answers in the row's page body.
6. Sends a single ntfy.sh push when the run finishes.

You review and apply manually. **The pipeline never auto-applies.** Disabling the
GitHub Actions workflow stops the entire system.

---

## How it fits together

```
acquire → max-age filter → classify → dedup (ids + collapse) → drop already-seen
  → score (master_match) → viability (drop/label) → rank + clamp → budget preflight
  → tailor (LLM, kept N only) → score (tailored_match) → Notion (row + CV page + answers)
  → rollover → ntfy
```

- **`data/master_cv.json`** is the single source of truth and the one input you
  cannot regenerate. The daily run reads only this file and never fetches your CV
  over the network. `scripts/import_cv.py` builds it from a Google Doc (one-time).
- The **viability filter runs before any LLM call**, so dead-end roles cost no
  tokens.
- Only **new** rows are ever tailored; existing rows are never re-tailored.

---

## Setup

### 0. Python (uv)

This project uses [uv](https://docs.astral.sh/uv/). Dependencies live in
`pyproject.toml` and are pinned in `uv.lock` (both committed).

```bash
uv sync                   # creates .venv and installs the locked deps
cp .env.example .env      # fill in real values for local runs
```

Run anything with `uv run …` (it uses the project venv automatically). You don't
activate a venv or `pip install` manually.

### 1. Master CV (one-time)

Your Google Doc is your human-readable copy; the runtime stays offline and
deterministic. Build the JSON from it once:

1. Share the Doc as **"Anyone with the link can view"** (the export endpoint needs
   this; on 401/403 the script tells you to fix sharing).
2. Grab the doc id from `https://docs.google.com/document/d/<DOC_ID>/edit`.
3. Run:
   ```bash
   export CV_DOC_ID=<DOC_ID>
   export DEEPSEEK_API_KEY=sk-...
   uv run python scripts/import_cv.py     # prints the JSON for review
   ```
4. Review the printed JSON, then **commit `data/master_cv.json` by hand.**

Re-run this whenever you update your resume. (`data/master_cv.example.json` shows
the schema if you'd rather write it directly.)

### 2. DeepSeek key

Create a key at <https://platform.deepseek.com> and set `DEEPSEEK_API_KEY`. The
client is OpenAI-compatible and pointed at `https://api.deepseek.com` (see
`config.yaml → llm.base_url`). Model is `deepseek-flash`; thinking mode is sent
explicitly disabled. To swap providers later (e.g. `gpt-5-mini`), change only
`llm.provider` / `llm.model` / `llm.base_url` — no code change.

### 3. Notion database + integration

1. Create a new **database** (full-page) in Notion with **exactly these
   properties** (name and type must match):

   | Property | Type |
   |---|---|
   | Company | Title |
   | Job title | Text |
   | Country | Select |
   | Master match % | Number |
   | Tailored match % | Number |
   | Job type | Select |
   | Sponsorship | Select |
   | Relocation support | Select |
   | Viability | Select |
   | Fit note | Text |
   | Status | Select (New / Applying / Applied / Uninterested / Archived) |
   | Tailored CV | URL |
   | Has questions | Select (yes / no) |
   | Job ID | Text |
   | Source | Select |
   | Posted date | Date |
   | Date first seen | Date |
   | Seniority | Select |
   | Salary | Text |

   Select options are created automatically the first time a value appears, so
   you don't have to pre-fill them (except that you may want to add `Applied` and
   `Uninterested` yourself for manual use).

2. Create an **internal integration** at
   <https://www.notion.so/my-integrations>, copy its token (`NOTION_TOKEN`).
3. **Scope the token to this one database only:** open the database → `•••` →
   *Connections* → add your integration. Do **not** share your whole workspace
   with it.
4. Copy the database id from its URL (the 32-char id before `?v=`) into
   `NOTION_DATABASE_ID`.
5. Keep your default view filtered to `Status is New or Applying` — Applied,
   Uninterested, and Archived rows stay on record but drop out of sight.

**Exporting a tailored CV:** each job's tailored CV lives on its own child page,
linked from the row's `Tailored CV` column. Open it → `•••` → *Export* → PDF /
HTML / Markdown. Single-page export works on the Notion free plan.

### 4. ntfy topic

The topic is `jobba-alert` (set in `config.yaml → notify.ntfy_topic`; the
`NTFY_TOPIC` env/secret overrides it). Subscribe to
`https://ntfy.sh/jobba-alert` in the ntfy app or web. You'll get one push per run
(and a high-priority push if a run fails). The topic is effectively public, so
treat it as a notification channel, not a secret.

### 5. GitHub Actions secrets

In the repo: *Settings → Secrets and variables → Actions → New repository secret*,
add all four:

- `DEEPSEEK_API_KEY`
- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`
- `NTFY_TOPIC`

Nothing secret is ever committed. `.env` is gitignored. The workflow injects these
as env vars. Leave GitHub's built-in failure email on — it's the backstop under
ntfy.

---

## Running

- **Locally, safe (writes nothing, sends nothing):**
  ```bash
  uv run python -m src.main --dry-run
  ```
  Prints the full plan: every kept role with its viability label, and **every
  dropped role with the reason**. Tailoring runs if a DeepSeek key is present;
  otherwise it's skipped with a warning.

- **Locally, for real:**
  ```bash
  uv run python -m src.main
  ```

- **In CI:** runs daily at 04:00 UTC (05:00 Africa/Lagos). Trigger manually from
  the Actions tab (`workflow_dispatch`).

---

## Configuration (`config.yaml`)

All tunables live in `config.yaml` (committed; no secrets). Highlights:

- `search.*` — titles, locations, count (10), `hard_max` (15 absolute ceiling),
  seniority, `max_age_days`, which boards to query.
- `geo.*` — `exclude_countries` (hard drop), `welcome_regions` (conceptual buckets
  the viability step labels against), `onsite_dead_regions`,
  `remote_africa_signals`, and `unlisted_region` (`low` | `drop`) for regions in
  none of the lists. **Two layers:** `search.locations` is the concrete strings
  JobSpy actually queries; `geo.welcome_regions` is the conceptual buckets — keep
  every welcome region reachable from a `search.locations` entry (e.g. Middle East
  → United Arab Emirates), or it's never fetched.
- `salary.*` — USD band and a **manually refreshed** FX map.
- `source.prefer_direct` — rank postings whose apply URL resolves to a company
  ATS (Greenhouse/Lever/Ashby) above plain board listings.
- `llm.*` — provider/model/base_url, token caps, and the budget guard
  (`rough_tokens_per_job`, `budget_ceiling_tokens`).
- `rollover.archive_new_after_days` — stale `New` rows move to `Archived` (never
  `Applying`).

---

## Viability rules (the "don't waste my time" gate)

Deterministic, reproducible, every decision carries a human-readable reason. The
model is never the judge here.

**Hard drops:** excluded country (India); US/Canada onsite/hybrid; remote roles
locked to a place you can't work from; salary present and outside the USD band
(missing salary does *not* drop).

**Labels on survivors:**
- `high` — Africa-eligible remote (worldwide/EMEA/Africa signals), or a welcome
  region with sponsorship stated `yes`.
- `medium` — welcome region, sponsorship undefined.
- `low` — welcome region with sponsorship `no`; a US/Canada remote role with a
  genuinely global scope; or an unlisted region when `geo.unlisted_region: low`.

Ranking: viability tier → source type (direct above board) → deterministic
match %. Only the top `search.count` survive to tailoring.

---

## Cost & safety guards

1. Explicit `max_tokens` on every LLM call.
2. Thinking mode off.
3. LLM only for new rows; existing rows never re-tailored.
4. Job count clamped to `search.hard_max` before the LLM loop.
5. Bounded retries (≤3), transient-only, capped backoff, never on 4xx.
6. Concurrency group + 10-minute job timeout.
7. Pre-flight budget check aborts the run if `rows × rough_per_job` exceeds the
   ceiling.
8. Token usage logged to stdout (visible in the Actions log).
9. Viability filter runs before tailoring — a dropped role never reaches an LLM
   call.

---

## Non-goals

No auto-apply, no form submission, no logging into boards. No pre-rendered PDFs
(the tailored CV is Markdown; render a PDF on demand from the Notion page only for
jobs you choose to apply to). No database beyond Notion, no frontend, no hosted
service.
