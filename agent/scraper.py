"""Job sources. Three board types, set per-board in config.yaml:

type: reed  — Reed.co.uk jobseeker API (free key). Most reliable: plain JSON,
              no bot detection. Needs REED_API_KEY.
type: rss   — any RSS feed (e.g. a JobServe saved-search feed). No browser
              needed for the listing; detail pages still fetched via Playwright.
type: html  — Playwright scraping (default). Boards behind aggressive bot
              protection (CWJobs/Totaljobs) may reset connections regardless.
"""

import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext

from .config import Config
from .models import Job

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_SELECTORS = {
    "card": ".job-item, .job-search-result, [data-testid='job-card'], article[class*='job']",
    "title": ".job-title, h2, h3, [class*='title']",
    "company": ".company-name, [class*='company'], [data-testid*='company']",
    "rate": ".salary, .job-salary, [class*='salary'], [class*='rate']",
    "link": "a[href*='/job/'], a[href*='/jobs/'], a[href*='JobDetail'], a[href]",
    "description": "[class*='job-description'], [class*='jobDescription'], [id*='description'], main, article",
}

MAX_DESCRIPTION_CHARS = 8000
REED_API = "https://www.reed.co.uk/api/1.0"


async def new_context(browser: Browser) -> BrowserContext:
    return await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="en-GB",
        timezone_id="Europe/London",
    )


def _text(card, selector: str) -> str:
    el = card.select_one(selector)
    return el.get_text(strip=True) if el else ""


# ---------- type: html (Playwright) ----------


async def scrape_board(context: BrowserContext, board: dict) -> list[Job]:
    """Scrape every search URL for one board config entry."""
    name = board.get("name", "unknown")
    selectors = {**DEFAULT_SELECTORS, **board.get("selectors", {})}
    base_url = board.get("base_url", "")
    jobs: list[Job] = []

    page = await context.new_page()
    try:
        for url in board.get("search_urls", []):
            print(f"[scrape] {name}: {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)  # let JS-rendered cards settle
                soup = BeautifulSoup(await page.content(), "html.parser")
            except Exception as e:
                print(f"[scrape] {name} failed to load {url}: {e}")
                continue

            for card in soup.select(selectors["card"]):
                title = _text(card, selectors["title"]) or "Unknown Title"
                company = _text(card, selectors["company"]) or "Direct / Agency"
                rate = _text(card, selectors["rate"]) or "Not stated"
                link_el = card.select_one(selectors["link"])
                href = link_el.get("href", "") if link_el else ""
                if not href:
                    continue
                jobs.append(
                    Job(
                        title=title,
                        company=company,
                        rate=rate,
                        url=urljoin(base_url or url, href),
                        source=name,
                        snippet=card.get_text(" ", strip=True)[:1500],
                    )
                )
    finally:
        await page.close()

    print(f"[scrape] {name}: {len(jobs)} cards")
    return jobs


# ---------- type: rss ----------


async def scrape_rss(board: dict) -> list[Job]:
    """Parse RSS 2.0 (or Atom) feeds — e.g. a JobServe saved-search feed."""
    name = board.get("name", "rss")
    jobs: list[Job] = []
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        for url in board.get("search_urls", []):
            print(f"[scrape] {name} (rss): {url}")
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except (httpx.HTTPError, ET.ParseError) as e:
                print(f"[scrape] {name} feed failed: {e}")
                continue

            items = list(root.iter("item")) or list(root.iter("{http://www.w3.org/2005/Atom}entry"))
            for item in items:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if not link:  # Atom puts the URL in <link href="...">
                    link_el = item.find("{http://www.w3.org/2005/Atom}link")
                    link = link_el.get("href", "") if link_el is not None else ""
                if not link:
                    continue
                desc_html = item.findtext("description") or ""
                snippet = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True)
                jobs.append(
                    Job(
                        title=title or "Unknown Title",
                        company="See listing",
                        rate="See listing",
                        url=link,
                        source=name,
                        snippet=snippet[:1500],
                    )
                )
    print(f"[scrape] {name}: {len(jobs)} feed items")
    return jobs


# ---------- type: reed (API) ----------


def _reed_rate(result: dict) -> str:
    lo, hi = result.get("minimumSalary"), result.get("maximumSalary")
    cur = result.get("currency") or "GBP"
    if lo and hi:
        return f"{lo:,.0f}-{hi:,.0f} {cur}"
    if lo or hi:
        return f"{(lo or hi):,.0f} {cur}"
    return "Not stated"


async def scrape_reed(board: dict, api_key: str) -> list[Job]:
    """Reed jobseeker API: one search per configured query, JSON results."""
    name = board.get("name", "reed")
    jobs: list[Job] = []
    base_params = {"resultsToTake": 100, **board.get("params", {})}
    async with httpx.AsyncClient(timeout=30, auth=(api_key, "")) as client:
        for query in board.get("queries", []):
            print(f"[scrape] {name} (api): {query!r}")
            try:
                resp = await client.get(f"{REED_API}/search", params={**base_params, "keywords": query})
                resp.raise_for_status()
                results = resp.json().get("results", [])
            except (httpx.HTTPError, ValueError) as e:
                print(f"[scrape] {name} query {query!r} failed: {e}")
                continue

            for r in results:
                job_id = r.get("jobId")
                jobs.append(
                    Job(
                        title=r.get("jobTitle") or "Unknown Title",
                        company=r.get("employerName") or "Direct / Agency",
                        rate=_reed_rate(r),
                        url=r.get("jobUrl") or f"https://www.reed.co.uk/jobs/{job_id}",
                        source=name,
                        snippet=(r.get("jobDescription") or "")[:1500],
                    )
                )
    print(f"[scrape] {name}: {len(jobs)} results")
    return jobs


async def _fetch_reed_description(job: Job, api_key: str) -> str:
    """Full description via the Reed job-details endpoint (search results are truncated)."""
    m = re.search(r"/(\d+)/?(?:[?#]|$)", job.url)
    if not m:
        return ""
    try:
        async with httpx.AsyncClient(timeout=30, auth=(api_key, "")) as client:
            resp = await client.get(f"{REED_API}/jobs/{m.group(1)}")
            resp.raise_for_status()
            html = resp.json().get("jobDescription") or ""
            return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)[:MAX_DESCRIPTION_CHARS]
    except (httpx.HTTPError, ValueError) as e:
        print(f"[scrape] reed detail failed for {job.url}: {e}")
        return ""


# ---------- dispatch ----------


def _board_for(job: Job, cfg: Config) -> dict:
    return next((b for b in cfg.boards if b.get("name") == job.source), {})


async def scrape_all(context: BrowserContext, cfg: Config) -> list[Job]:
    jobs: list[Job] = []
    for board in cfg.boards:
        btype = board.get("type", "html")
        try:
            if btype == "reed":
                if cfg.reed_api_key:
                    jobs.extend(await scrape_reed(board, cfg.reed_api_key))
                else:
                    print(f"[scrape] {board.get('name')}: skipped (REED_API_KEY not set)")
            elif btype == "rss":
                jobs.extend(await scrape_rss(board))
            else:
                jobs.extend(await scrape_board(context, board))
        except Exception as e:
            print(f"[scrape] board {board.get('name')} errored: {e}")
    # de-dupe within the run
    unique: dict[str, Job] = {}
    for job in jobs:
        unique.setdefault(job.id, job)
    return list(unique.values())


async def fetch_description(context: BrowserContext, job: Job, cfg: Config) -> str:
    """Full description for one job — API for Reed, Playwright for everything else."""
    board = _board_for(job, cfg)
    if board.get("type") == "reed" and cfg.reed_api_key:
        return await _fetch_reed_description(job, cfg.reed_api_key)

    selectors = {**DEFAULT_SELECTORS, **board.get("selectors", {})}
    page = await context.new_page()
    try:
        await page.goto(job.url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1500)
        soup = BeautifulSoup(await page.content(), "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        el = soup.select_one(selectors["description"])
        text = el.get_text("\n", strip=True) if el else soup.get_text("\n", strip=True)
        return text[:MAX_DESCRIPTION_CHARS]
    except Exception as e:
        print(f"[scrape] detail fetch failed for {job.url}: {e}")
        return ""
    finally:
        await page.close()
