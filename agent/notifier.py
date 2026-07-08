from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import Config
from .models import Job

STATUS_STYLE = {
    "applied": ("✅ APPLIED", 3066993),    # green
    "draft":   ("📝 DRAFT READY", 3447003),  # blue
    "alert":   ("🚨 NEW MATCH", 15844367),   # gold
    "error":   ("⚠️ NEEDS ATTENTION", 15158332),  # red
}


async def notify(cfg: Config, job: Job, evaluation, status: str,
                 materials_dir: Path | None = None, note_path: Path | None = None,
                 notion_url: str | None = None, detail: str = "") -> None:
    label, color = STATUS_STYLE.get(status, STATUS_STYLE["alert"])

    if not cfg.discord_webhook:
        print(f"[notify] {label}: {job.title} @ {job.company} "
              f"(score {getattr(evaluation, 'fit_score', '?')}) {detail}")
        return

    fields = [
        {"name": "Rate", "value": job.rate or "n/a", "inline": True},
        {"name": "Source", "value": job.source, "inline": True},
    ]
    if evaluation:
        fields += [
            {"name": "Fit score", "value": f"{evaluation.fit_score}/100", "inline": True},
            {"name": "IR35 / Remote",
             "value": f"{evaluation.ir35_status} / {evaluation.remote_status}", "inline": True},
        ]
        if evaluation.matched_skills:
            fields.append({"name": "Matched stack",
                           "value": ", ".join(evaluation.matched_skills[:10]), "inline": False})
    if materials_dir:
        fields.append({"name": "Materials", "value": f"`{materials_dir}`", "inline": False})
    if note_path:
        fields.append({"name": "Obsidian note", "value": f"`{note_path.name}`", "inline": False})
    if notion_url:
        fields.append({"name": "Notion", "value": f"[open card]({notion_url})", "inline": False})
    if detail:
        fields.append({"name": "Detail", "value": detail[:1000], "inline": False})

    embed = {
        "title": f"{label}: {job.title}",
        "description": (evaluation.summary if evaluation else job.snippet[:300]) or "",
        "url": job.url,
        "color": color,
        "fields": fields,
        "footer": {"text": f"job agent • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"},
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(cfg.discord_webhook, json={"embeds": [embed]})
    except httpx.HTTPError as e:
        print(f"[notify] Discord webhook failed: {e}")


async def notify_run_summary(cfg: Config, stats: dict) -> None:
    line = (f"Run complete — new: {stats.get('new', 0)}, applied: {stats.get('applied', 0)}, "
            f"drafts: {stats.get('draft', 0)}, alerts: {stats.get('alert', 0)}, "
            f"skipped: {stats.get('skipped', 0)}, errors: {stats.get('error', 0)}")
    print(f"[notify] {line}")
    if cfg.discord_webhook and (stats.get("applied") or stats.get("draft") or stats.get("error")):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(cfg.discord_webhook, json={"content": f"🧾 {line}"})
        except httpx.HTTPError:
            pass
