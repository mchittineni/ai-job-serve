from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext

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
    "description": "[class*='job-description'], [class*='jobDescription'], "
    "[id*='description'], main, article",
}

MAX_DESCRIPTION_CHARS = 8000


async def new_context(browser: Browser) -> BrowserContext:
    return await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})


def _text(card, selector: str) -> str:
    el = card.select_one(selector)
    return el.get_text(strip=True) if el else ""


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


async def scrape_all(context: BrowserContext, boards: list[dict]) -> list[Job]:
    jobs: list[Job] = []
    for board in boards:
        try:
            jobs.extend(await scrape_board(context, board))
        except Exception as e:
            print(f"[scrape] board {board.get('name')} errored: {e}")
    # de-dupe within the run
    unique: dict[str, Job] = {}
    for job in jobs:
        unique.setdefault(job.id, job)
    return list(unique.values())


async def fetch_description(context: BrowserContext, job: Job, boards: list[dict]) -> str:
    """Load the job's detail page and pull the full description text for evaluation/tailoring."""
    selectors = DEFAULT_SELECTORS
    for board in boards:
        if board.get("name") == job.source:
            selectors = {**DEFAULT_SELECTORS, **board.get("selectors", {})}
            break

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
