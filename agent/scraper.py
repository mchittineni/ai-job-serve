"""Job sources. Four board types, set per-board in config.yaml:

type: adzuna   — Adzuna aggregator API (free key). Covers many UK boards,
                 including Totaljobs/CWJobs content, as plain JSON.
                 Needs ADZUNA_APP_ID + ADZUNA_APP_KEY.
type: linkedin — LinkedIn's unauthenticated guest endpoint (no key). Unofficial:
                 rate-limits aggressively, so keep queries modest; backs off on 429.
type: rss      — any RSS feed (e.g. a JobServe saved-search feed). No browser
                 needed for the listing; detail pages still fetched via Playwright.
type: html     — Playwright scraping (default). Boards behind aggressive bot
                 protection (CWJobs/Totaljobs) may reset connections regardless.
"""

import asyncio
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
ADZUNA_API = "https://api.adzuna.com/v1/api/jobs"
LINKEDIN_GUEST_API = "https://www.linkedin.com/jobs-guest/jobs/api"


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
            if "REPLACE_WITH" in url:
                print(f"[scrape] {name}: skipped — placeholder URL still in config.yaml")
                continue
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


# ---------- type: adzuna (API) ----------


def _adzuna_rate(result: dict) -> str:
    lo, hi = result.get("salary_min"), result.get("salary_max")
    if lo and hi:
        return f"{lo:,.0f}-{hi:,.0f} GBP"
    if lo or hi:
        return f"{(lo or hi):,.0f} GBP"
    return "Not stated"


async def scrape_adzuna(board: dict, app_id: str, app_key: str) -> list[Job]:
    """Adzuna aggregator API: one search per query, paginated (50 results/page)."""
    name = board.get("name", "adzuna")
    country = board.get("country", "gb")
    pages = int(board.get("pages", 1))
    base_params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": 50,
        **board.get("params", {}),
    }
    jobs: list[Job] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for query in board.get("queries", []):
            print(f"[scrape] {name} (api): {query!r}")
            for page_no in range(1, pages + 1):
                try:
                    resp = await client.get(
                        f"{ADZUNA_API}/{country}/search/{page_no}",
                        params={**base_params, "what": query},
                    )
                    resp.raise_for_status()
                    results = resp.json().get("results", [])
                except (httpx.HTTPError, ValueError) as e:
                    print(f"[scrape] {name} query {query!r} page {page_no} failed: {e}")
                    break

                for r in results:
                    url = r.get("redirect_url")
                    if not url:
                        continue
                    # snippets can carry <strong> highlight markup — strip it
                    snippet = BeautifulSoup(r.get("description") or "", "html.parser")
                    jobs.append(
                        Job(
                            title=r.get("title") or "Unknown Title",
                            company=(r.get("company") or {}).get("display_name") or "Direct / Agency",
                            rate=_adzuna_rate(r),
                            url=url,
                            source=name,
                            snippet=snippet.get_text(" ", strip=True)[:1500],
                        )
                    )
                if len(results) < base_params["results_per_page"]:
                    break
    print(f"[scrape] {name}: {len(jobs)} results")
    return jobs


# ---------- type: linkedin (guest endpoint) ----------


def _linkedin_card_to_job(card, name: str) -> Job | None:
    link_el = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
    href = link_el.get("href", "") if link_el else ""
    m = re.search(r"/jobs/view/[^?]*?(\d{6,})", href)
    if not m:
        return None
    location = _text(card, ".job-search-card__location")
    return Job(
        title=_text(card, ".base-search-card__title, h3") or "Unknown Title",
        company=_text(card, ".base-search-card__subtitle, h4") or "Direct / Agency",
        rate=_text(card, ".job-search-card__salary-info") or "Not stated",
        # canonical URL: the raw href carries per-request tracking params that would break dedupe
        url=f"https://www.linkedin.com/jobs/view/{m.group(1)}",
        source=name,
        snippet=card.get_text(" ", strip=True)[:1500] + (f" | Location: {location}" if location else ""),
    )


async def scrape_linkedin(board: dict) -> list[Job]:
    """LinkedIn guest search endpoint: HTML cards, 25 per page, no login.

    Unofficial and aggressively rate-limited (especially from datacenter IPs, so
    expect thinner results on CI runners) — paced politely, stops a query on 429.
    """
    name = board.get("name", "linkedin")
    pages = int(board.get("pages", 1))
    base_params = {**board.get("params", {})}
    jobs: list[Job] = []
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        for query in board.get("queries", []):
            print(f"[scrape] {name} (guest): {query!r}")
            for page_no in range(pages):
                params = {**base_params, "keywords": query, "start": page_no * 25}
                try:
                    resp = await client.get(f"{LINKEDIN_GUEST_API}/seeMoreJobPostings/search", params=params)
                    if resp.status_code == 429:
                        print(f"[scrape] {name} rate-limited (429) — skipping rest of {query!r}")
                        break
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    print(f"[scrape] {name} query {query!r} page {page_no} failed: {e}")
                    break

                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select("div.base-card") or soup.select("li")
                page_jobs = [j for j in (_linkedin_card_to_job(c, name) for c in cards) if j]
                jobs.extend(page_jobs)
                if not page_jobs:  # empty page = end of results (or a soft block)
                    break
                await asyncio.sleep(1.5)  # pace requests to stay under the rate limit
    print(f"[scrape] {name}: {len(jobs)} cards")
    return jobs


async def _fetch_linkedin_description(job: Job) -> str:
    """Full description via the guest job-posting endpoint (no login needed)."""
    m = re.search(r"/jobs/view/(\d+)", job.url)
    if not m:
        return ""
    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(f"{LINKEDIN_GUEST_API}/jobPosting/{m.group(1)}")
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            el = soup.select_one(".show-more-less-html__markup, .description__text")
            text = el.get_text("\n", strip=True) if el else ""
            return text[:MAX_DESCRIPTION_CHARS]
    except httpx.HTTPError as e:
        print(f"[scrape] linkedin detail failed for {job.url}: {e}")
        return ""


# ---------- dispatch ----------


def _board_for(job: Job, cfg: Config) -> dict:
    return next((b for b in cfg.boards if b.get("name") == job.source), {})


async def scrape_all(context: BrowserContext, cfg: Config) -> list[Job]:
    jobs: list[Job] = []
    for board in cfg.boards:
        btype = board.get("type", "html")
        try:
            if btype == "adzuna":
                if cfg.adzuna_app_id and cfg.adzuna_app_key:
                    jobs.extend(await scrape_adzuna(board, cfg.adzuna_app_id, cfg.adzuna_app_key))
                else:
                    print(f"[scrape] {board.get('name')}: skipped (ADZUNA_APP_ID/ADZUNA_APP_KEY not set)")
            elif btype == "linkedin":
                jobs.extend(await scrape_linkedin(board))
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
    """Full description for one job — guest API for LinkedIn, Playwright for everything else."""
    board = _board_for(job, cfg)
    if board.get("type") == "linkedin":
        text = await _fetch_linkedin_description(job)
        if text:
            return text
        # fall through to Playwright — public postings usually render before the authwall

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
