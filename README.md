# AI Job Agent

Autonomous job-application agent for remote, outside-IR35 DevOps/Platform contracts.
Each run it scrapes the configured boards, has Gemini score every new listing against
your profile, and — for strong matches — generates a **tailored resume (PDF) and cover
letter grounded in your Obsidian vault**, then either emails the application or leaves
a ready-to-send draft. Every application is logged back into the vault as a note.

```
scrape boards ──> dedupe (SQLite) ──> fetch full description
      ──> Gemini fit evaluation (score, IR35, remote, target-role check, apply email)
      ──> retrieve career facts from Obsidian vault
      ──> tailored resume.pdf + cover letter (never invents experience)
      ──> auto: email application | draft: save for review | alert: notify only
      ──> Notion pipeline card + Obsidian note + Discord notification
```

## Modes (`APPLY_MODE`)

| Mode    | What happens                                                                                                                                                                                    |
| ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `alert` | Notify on matches only (no LLM tailoring needed)                                                                                                                                                |
| `draft` | **Default.** Generates resume PDF + cover letter + application email into `data/applications/<job>/` for you to review and send                                                                 |
| `auto`  | Emails the application (resume PDF attached) when the listing contains an apply address; otherwise falls back to draft. Rate-limited by `MAX_APPLICATIONS_PER_RUN` / `MAX_APPLICATIONS_PER_DAY` |

Start in `draft`, sanity-check a few generated packages, then switch to `auto`.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime deps + pre-commit + ruff
python -m playwright install chromium
pre-commit install            # ruff lint/format, prettier, hygiene checks on commit
cp .env.example .env          # fill in secrets
```

1. **Fill in `profile/master-resume.md`** — this is the single source of truth the
   agent tailors from. It will never claim anything not written here or in the vault.
2. **Point `OBSIDIAN_VAULT_PATH` at your vault.** The agent mines it for career facts
   (projects, achievements, metrics) to make each resume specific _and accurate_, and
   writes one note per application into `Applications/` with Dataview-friendly
   frontmatter (`status`, `score`, `company`, `follow_up`, …) so the vault doubles as
   your pipeline tracker. Scope fact-mining with `obsidian.knowledge_folders` in
   `config.yaml` if you don't want the whole vault read.
3. **Edit `config.yaml`** — profile contact details, hard requirements, job sources,
   and stack keywords.
4. Run it: `python job-connect.py`

### Job sources (`boards` in config.yaml)

| Type   | Board            | Notes                                                                                                                                                        |
| ------ | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `reed` | Reed.co.uk API   | **Primary.** Free key at [reed.co.uk/developers](https://www.reed.co.uk/developers) → `REED_API_KEY`. Clean JSON, no bot detection.                          |
| `rss`  | JobServe (et al) | Save a search on jobserve.com, click the RSS icon on the results page, paste the feed URL.                                                                   |
| `html` | Anything else    | Playwright scraping. Boards with aggressive bot protection (CWJobs/Totaljobs) reset headless connections (`ERR_HTTP2_PROTOCOL_ERROR`) — prefer `reed`/`rss`. |

Example Dataview query for a pipeline dashboard note:

```dataview
TABLE company, role, rate, score, status, applied, follow_up
FROM "Applications"
WHERE type = "job-application"
SORT applied DESC
```

## Notion job tracking

The primary pipeline board lives in Notion: the agent upserts one card per job
(keyed on the listing URL) with **Status** (`Alert`/`Draft`/`Applied`), **Fit Score**,
**Rate**, **IR35/Remote**, **Matched Skills**, apply contact, and the cover letter in
the page body plus a follow-up checklist. You move cards through
`Interview` → `Offer` / `Rejected` by hand — since the SQLite dedupe means each job is
processed exactly once, later runs never touch a card whose status you've changed.

Setup (2 minutes):

1. Create an internal integration at [notion.so/my-integrations](https://www.notion.so/my-integrations)
   and copy the token → `NOTION_API_KEY`.
2. Pick a Notion page for tracking, open its `…` menu → _Connections_ → add your
   integration, and set `NOTION_PARENT_PAGE_ID` (the 32-char id from the page URL).
   The agent creates a **Job Applications** database there on its first run.
   (Alternatively point `NOTION_DATABASE_ID` at an existing database that has the
   same property names — see `DB_SCHEMA` in `agent/notion_tracker.py`.)
3. On GitHub: add `NOTION_API_KEY` as a secret and `NOTION_DATABASE_ID` /
   `NOTION_PARENT_PAGE_ID` as repository **variables**.

Tip: add a _Board_ view grouped by **Status** in Notion and you have a kanban of your
whole job search. Notion tracking is best-effort — if the API call fails the run
continues, and the SQLite dedupe DB remains the agent's source of truth.

## CI/CD & scheduled runs — GitHub Actions (primary)

> ⚠️ **Keep this repository private.** The master resume, generated application
> materials, and the state DB (all committed) contain personal contact details.

Two workflows under `.github/workflows/`:

- **`ci.yml`** — on every push/PR: installs deps, compiles all modules, validates
  `config.yaml`, and smoke-tests imports + the master resume.
- **`job-agent.yml`** — runs the agent every 30 min on UK weekday work hours
  (GitHub cron is UTC), and on demand via _Actions → Job Agent → Run workflow_,
  where you can override the apply mode per run.

Setup:

1. **Secrets** (_Settings → Secrets and variables → Actions → Secrets_):
   `GEMINI_API_KEY`, `REED_API_KEY`, `DISCORD_WEBHOOK_URL`, `NOTION_API_KEY`, and for
   auto mode `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_BCC`.
2. **Variables** (same page, _Variables_ tab): `APPLY_MODE` = `alert` | `draft` |
   `auto` (defaults to `draft` if unset), plus `NOTION_DATABASE_ID` or
   `NOTION_PARENT_PAGE_ID` for the Notion board.
3. Push to `main`. Done — no server needed.

How state works on Actions: runners are ephemeral, so after each run the workflow
**commits `data/` back to the repo** (`[skip ci]`) — that's the dedupe DB, generated
resumes/cover letters, and application notes. Materials are also uploaded as a run
artifact (14-day retention). A `concurrency` group stops overlapping runs from
double-applying. Since the Actions runner has no Obsidian vault, cloud runs are
grounded in `profile/master-resume.md` — keep it current (you can also drop extra
fact files into `profile/` and reference them from the resume).

For **full Obsidian-vault grounding**, run locally on a schedule (`launchd`/`cron`)
with `OBSIDIAN_VAULT_PATH` set — local and Actions runs share the same committed
`data/jobs.db`, so pull before local runs to avoid duplicate applications.

### Alternative: Railway (optional)

`railway.json` + `nixpacks.toml` are kept for a Railway cron deployment. If you use
it, attach a volume and set `DATA_DIR=/data` so state survives deploys.

## Environment variables

See `.env.example`. The important ones: `GEMINI_API_KEY` (evaluation + tailoring;
without it the agent degrades to keyword alerts), `APPLY_MODE`, `OBSIDIAN_VAULT_PATH`,
`DISCORD_WEBHOOK_URL`, and `SMTP_*` for the auto-apply email channel (`SMTP_BCC`
yourself to keep copies of everything sent).

## Safety rails

- **Nothing is ever fabricated**: the tailoring prompt hard-forbids inventing
  employers, dates, certs, metrics, or skills; it can only reorder/rephrase what's in
  your master resume and vault. `tailoring-notes.md` in each package is the audit trail.
- Auto-apply only triggers at/above `MIN_SCORE`, only when a real apply email exists in
  the listing, and is capped per run and per day.
- Every action is recorded in SQLite and (optionally) your vault, so re-runs are
  idempotent — the agent never applies to the same job twice.
- Scraping note: keep the cron cadence modest and selectors polite; job boards' terms
  vary and aggressive scraping can get your IP blocked.
