"""Notion pipeline tracking: one database page per job the agent acts on.

The Notion database is the human-facing pipeline board — the agent writes rows
with Status Alert/Draft/Applied, and you move them through Interview/Offer/
Rejected by hand. SQLite stays the agent's private dedupe memory; Obsidian
notes remain optional.

Configure either NOTION_DATABASE_ID (existing database, shared with the
integration) or NOTION_PARENT_PAGE_ID (the agent creates a "Job Applications"
database there on first run and caches the id under data/).
"""
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import Config
from .models import Job

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

STATUS_LABEL = {"alert": "Alert", "draft": "Draft", "applied": "Applied", "error": "Error"}

DB_SCHEMA = {
    "Name": {"title": {}},
    "Company": {"rich_text": {}},
    "Status": {"select": {"options": [
        {"name": "Alert", "color": "yellow"},
        {"name": "Draft", "color": "blue"},
        {"name": "Applied", "color": "green"},
        {"name": "Interview", "color": "purple"},
        {"name": "Offer", "color": "pink"},
        {"name": "Rejected", "color": "red"},
        {"name": "Error", "color": "orange"},
    ]}},
    "Fit Score": {"number": {}},
    "Rate": {"rich_text": {}},
    "Source": {"select": {}},
    "IR35": {"select": {}},
    "Remote": {"select": {}},
    "Matched Skills": {"multi_select": {}},
    "URL": {"url": {}},
    "Applied": {"date": {}},
    "Follow Up": {"date": {}},
    "Materials": {"rich_text": {}},
}


def _chunks(text: str, limit: int = 2000) -> list[str]:
    text = (text or "").strip()
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [" "]


def _rich(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": c}} for c in _chunks(text)]


def _para(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich(text)}}


def _heading(text: str) -> dict:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rich(text)}}


def _todo(text: str) -> dict:
    return {"object": "block", "type": "to_do", "to_do": {"rich_text": _rich(text), "checked": False}}


def _select_name(value: str) -> str:
    # Notion select/multi_select option names may not contain commas
    return re.sub(r",", " ", value).strip()[:100] or "n/a"


class NotionTracker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.enabled = bool(cfg.notion_api_key
                            and (cfg.notion_database_id or cfg.notion_parent_page_id))
        self._db_id: str | None = cfg.notion_database_id
        self._db_id_cache: Path = cfg.data_dir / "notion_db_id.txt"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.cfg.notion_api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def _database_id(self, client: httpx.AsyncClient) -> str:
        if self._db_id:
            return self._db_id
        if self._db_id_cache.exists():
            self._db_id = self._db_id_cache.read_text().strip()
            return self._db_id
        resp = await client.post(f"{API}/databases", headers=self._headers(), json={
            "parent": {"type": "page_id", "page_id": self.cfg.notion_parent_page_id},
            "title": [{"type": "text", "text": {"content": "Job Applications"}}],
            "properties": DB_SCHEMA,
        })
        resp.raise_for_status()
        self._db_id = resp.json()["id"]
        self._db_id_cache.write_text(self._db_id)
        print(f"[notion] created 'Job Applications' database ({self._db_id})")
        return self._db_id

    async def _find_page(self, client: httpx.AsyncClient, db_id: str, url: str) -> str | None:
        resp = await client.post(f"{API}/databases/{db_id}/query", headers=self._headers(), json={
            "filter": {"property": "URL", "url": {"equals": url}},
            "page_size": 1,
        })
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0]["id"] if results else None

    def _properties(self, job: Job, evaluation, status: str,
                    materials_dir: Path | None) -> dict:
        props = {
            "Name": {"title": _rich(f"{job.title} — {job.company}"[:200])},
            "Company": {"rich_text": _rich(job.company)},
            "Status": {"select": {"name": STATUS_LABEL.get(status, "Alert")}},
            "Rate": {"rich_text": _rich(job.rate)},
            "Source": {"select": {"name": _select_name(job.source)}},
            "URL": {"url": job.url},
        }
        if evaluation:
            props["Fit Score"] = {"number": evaluation.fit_score}
            props["IR35"] = {"select": {"name": _select_name(evaluation.ir35_status)}}
            props["Remote"] = {"select": {"name": _select_name(evaluation.remote_status)}}
            skills = list(dict.fromkeys(_select_name(s) for s in evaluation.matched_skills))[:10]
            props["Matched Skills"] = {"multi_select": [{"name": s} for s in skills]}
        if status == "applied":
            props["Applied"] = {"date": {"start": datetime.now(timezone.utc).date().isoformat()}}
        if materials_dir:
            props["Materials"] = {"rich_text": _rich(str(materials_dir))}
        return props

    def _body(self, job: Job, evaluation, package) -> list[dict]:
        blocks = [_heading("Why it matched"),
                  _para(evaluation.summary if evaluation else job.snippet[:1500])]
        if evaluation:
            if evaluation.missing_skills:
                blocks.append(_para(f"Gaps: {', '.join(evaluation.missing_skills[:8])}"))
            if evaluation.red_flags:
                blocks.append(_para(f"Red flags: {', '.join(evaluation.red_flags)}"))
            if evaluation.apply_email:
                blocks.append(_para(f"Apply contact: {evaluation.apply_email}"
                                    + (f" ({evaluation.recruiter_name})" if evaluation.recruiter_name else "")))
        if package:
            blocks.append(_heading("Cover letter"))
            for paragraph in package.cover_letter.split("\n\n"):
                if paragraph.strip():
                    blocks.append(_para(paragraph.strip()))
        blocks += [
            _heading("Follow-up"),
            _todo("Confirm application received"),
            _todo("Follow up after 3 working days"),
            _todo("Log interview / outcome"),
        ]
        return blocks[:100]  # Notion caps children per request

    async def track(self, job: Job, evaluation, status: str,
                    materials_dir: Path | None = None, package=None) -> str | None:
        """Upsert the job's page (keyed on URL). Never raises — tracking must not break the run."""
        if not self.enabled:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                db_id = await self._database_id(client)
                props = self._properties(job, evaluation, status, materials_dir)
                page_id = await self._find_page(client, db_id, job.url)
                if page_id:
                    resp = await client.patch(f"{API}/pages/{page_id}",
                                              headers=self._headers(),
                                              json={"properties": props})
                else:
                    resp = await client.post(f"{API}/pages", headers=self._headers(), json={
                        "parent": {"database_id": db_id},
                        "properties": props,
                        "children": self._body(job, evaluation, package),
                    })
                resp.raise_for_status()
                page_url = resp.json().get("url")
                print(f"[notion] {'updated' if page_id else 'created'} "
                      f"'{job.title}' ({STATUS_LABEL.get(status, status)}) -> {page_url}")
                return page_url
        except httpx.HTTPStatusError as e:
            print(f"[notion] API error {e.response.status_code}: {e.response.text[:300]}")
        except httpx.HTTPError as e:
            print(f"[notion] request failed: {e}")
        return None
