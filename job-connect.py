"""Autonomous job application agent — entry point.

Pipeline per run (designed for a cron schedule):
  1. scrape configured job boards
  2. skip anything already tracked (SQLite)
  3. fetch the full description, evaluate fit with Gemini
  4. for strong matches: retrieve career facts from the Obsidian vault,
     generate a tailored resume (PDF) + cover letter grounded in those facts
  5. APPLY_MODE=auto  -> email the application when a contact address exists
     APPLY_MODE=draft -> save the ready-to-send package for human review
     APPLY_MODE=alert -> notify only (original behaviour)
  6. log an application note back into the Obsidian vault + Discord

Configuration: config.yaml (search/boards/profile) + environment (secrets).
"""

import asyncio
import traceback

from playwright.async_api import async_playwright

from agent import applier, notifier, scraper
from agent.config import Config
from agent.evaluator import Evaluator
from agent.knowledge import Knowledge
from agent.llm import LLMQuotaError
from agent.notion_tracker import NotionTracker
from agent.tailor import Tailor
from agent.tracker import Tracker


async def process_job(
    job, *, cfg, context, browser, evaluator, knowledge, tailor, tracker, notion, run_stats, applications_sent
) -> int:
    """Handle one new job end-to-end. Returns applications sent for this job (0 or 1)."""
    job.description = await scraper.fetch_description(context, job, cfg)
    evaluation = await evaluator.evaluate(job)

    print(
        f"[eval] {job.title} @ {job.company}: score={evaluation.fit_score} "
        f"ir35={evaluation.ir35_status} remote={evaluation.remote_status} "
        f"apply={evaluation.should_apply}"
    )

    if not evaluation.should_apply or evaluation.fit_score < cfg.min_score:
        reason = "; ".join(evaluation.red_flags) or f"score {evaluation.fit_score} < {cfg.min_score}"
        tracker.record(job, "skipped", evaluation.fit_score, reason, job.to_dict())
        run_stats["skipped"] += 1
        return 0

    if cfg.apply_mode == "alert":
        tracker.record(job, "evaluated", evaluation.fit_score, "alert mode", job.to_dict())
        notion_url = await notion.track(job, evaluation, "alert")
        await notifier.notify(cfg, job, evaluation, "alert", notion_url=notion_url)
        run_stats["alert"] += 1
        return 0

    # --- tailor materials grounded in the Obsidian vault ---
    facts = knowledge.retrieve_facts(job.full_text)
    master_resume = knowledge.load_master_resume()
    package = await tailor.build_package(job, evaluation, master_resume, facts)

    if package is None:
        tracker.record(
            job, "evaluated", evaluation.fit_score, "match found but tailoring unavailable", job.to_dict()
        )
        notion_url = await notion.track(job, evaluation, "alert")
        await notifier.notify(
            cfg,
            job,
            evaluation,
            "alert",
            notion_url=notion_url,
            detail="Strong match, but no tailored materials (check GEMINI_API_KEY / master resume).",
        )
        run_stats["alert"] += 1
        return 0

    materials_dir = await tailor.save_package(browser, job, package)

    # --- apply or draft ---
    daily_room = tracker.applications_today() < cfg.max_applications_per_day
    run_room = applications_sent < cfg.max_applications_per_run
    can_auto = cfg.apply_mode == "auto" and evaluation.apply_email and cfg.smtp and daily_room and run_room

    status, detail, sent = "draft", "", 0
    if can_auto:
        try:
            await applier.send_application(cfg, job, package, evaluation.apply_email, materials_dir)
            status, sent = "applied", 1
            detail = f"Sent to {evaluation.apply_email}"
        except Exception as e:
            detail = f"Email send failed ({e}) — saved as draft"
            print(f"[apply] {detail}")
    elif cfg.apply_mode == "auto":
        if not evaluation.apply_email:
            detail = "No apply email in listing — apply manually via the URL"
        elif not cfg.smtp:
            detail = "SMTP not configured — saved as draft"
        elif not (daily_room and run_room):
            detail = "Application rate limit reached — saved as draft"

    tracker.record(
        job, status, evaluation.fit_score, detail, {**job.to_dict(), "materials": str(materials_dir)}
    )
    note_path = knowledge.write_application_note(job, evaluation, package, status, materials_dir)
    notion_url = await notion.track(job, evaluation, status, materials_dir, package)
    await notifier.notify(
        cfg,
        job,
        evaluation,
        status,
        materials_dir=materials_dir,
        note_path=note_path,
        notion_url=notion_url,
        detail=detail,
    )
    run_stats[status] += 1
    return sent


async def run() -> None:
    cfg = Config()
    tracker = Tracker(cfg.data_dir)
    knowledge = Knowledge(cfg)
    evaluator = Evaluator(cfg, knowledge.profile_summary())
    tailor = Tailor(cfg)
    notion = NotionTracker(cfg)

    print(
        f"[run] mode={cfg.apply_mode} min_score={cfg.min_score} "
        f"model={cfg.gemini_model if cfg.gemini_api_key else 'heuristic (no API key)'} "
        f"vault={'yes' if cfg.vault_path else 'no'} "
        f"notion={'yes' if notion.enabled else 'no'}"
    )

    run_stats = {"new": 0, "applied": 0, "draft": 0, "alert": 0, "skipped": 0, "deferred": 0, "error": 0}
    applications_sent = 0

    async with async_playwright() as p:
        # HTTP/1.1 fallback: some boards' bot protection resets HTTP/2 from headless clients
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-http2", "--disable-blink-features=AutomationControlled"],
        )
        context = await scraper.new_context(browser)
        try:
            jobs = await scrape_new_jobs(context, cfg, tracker)
            run_stats["new"] = len(jobs)
            print(f"[run] {len(jobs)} new jobs to evaluate")

            for index, job in enumerate(jobs):
                try:
                    applications_sent += await process_job(
                        job,
                        cfg=cfg,
                        context=context,
                        browser=browser,
                        evaluator=evaluator,
                        knowledge=knowledge,
                        tailor=tailor,
                        tracker=tracker,
                        notion=notion,
                        run_stats=run_stats,
                        applications_sent=applications_sent,
                    )
                except LLMQuotaError as e:
                    # Gemini quota is gone for this run. Stop WITHOUT recording the
                    # remaining jobs — they stay unseen and a later run re-evaluates
                    # them with real quota instead of burning them on the heuristic.
                    run_stats["deferred"] = len(jobs) - index
                    print(f"[run] {e}")
                    print(f"[run] deferring {run_stats['deferred']} remaining jobs to a later run")
                    break
                except Exception:
                    run_stats["error"] += 1
                    print(f"[run] error on '{job.title}':\n{traceback.format_exc()}")
                    tracker.record(job, "error", None, traceback.format_exc()[-500:], job.to_dict())
        finally:
            await browser.close()

    await notifier.notify_run_summary(cfg, run_stats)
    tracker.close()


async def scrape_new_jobs(context, cfg: Config, tracker: Tracker) -> list:
    jobs = await scraper.scrape_all(context, cfg)
    return [j for j in jobs if not tracker.seen(j.id)]


if __name__ == "__main__":
    asyncio.run(run())
