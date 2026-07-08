import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Job:
    title: str
    company: str
    rate: str
    url: str
    source: str
    snippet: str = ""
    description: str = ""
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def id(self) -> str:
        # Rate text is volatile ("Competitive" vs a number), so key on title/company/url.
        raw = f"{self.title}|{self.company}|{self.url}".lower().replace(" ", "")
        return hashlib.md5(raw.encode()).hexdigest()

    @property
    def full_text(self) -> str:
        return self.description or self.snippet

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "company": self.company,
            "rate": self.rate,
            "url": self.url,
            "source": self.source,
            "snippet": self.snippet[:500],
            "scraped_at": self.scraped_at,
        }
