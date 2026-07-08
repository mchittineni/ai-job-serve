"""Generate a job-tailored resume + cover letter, grounded strictly in real facts.

The resume PDF is rendered with the Chromium instance Playwright already ships,
so no extra PDF dependency is needed.
"""

import re
from pathlib import Path

import markdown as md
from playwright.async_api import Browser
from pydantic import BaseModel, Field

from .config import Config
from .evaluator import JobEvaluation
from .knowledge import Fact
from .llm import LLM, LLMError, LLMQuotaError
from .models import Job


class ApplicationPackage(BaseModel):
    resume_markdown: str = Field(description="Complete tailored resume in clean markdown")
    cover_letter: str = Field(description="Cover letter body, 200-320 words, plain text paragraphs")
    email_subject: str = Field(description="Subject line for an application email")
    email_body: str = Field(
        description="Short professional email body wrapping the cover letter, ready to send, ending with the candidate's sign-off"
    )
    tailoring_notes: list[str] = Field(
        description="What was emphasized/reordered and why (for the human's audit trail)"
    )


TAILOR_SYSTEM = """You tailor resumes and cover letters for a specific job listing.

ACCURACY IS NON-NEGOTIABLE. You are given the candidate's master resume plus verified
career facts from their personal knowledge base (Obsidian vault). You may:
- reorder and re-emphasize existing experience to match the listing
- rephrase bullet points using the listing's terminology where it is truthful
- select which projects/achievements to feature

You may NEVER:
- invent employers, job titles, dates, certifications, degrees, metrics, or tools
- upgrade seniority, extend durations, or claim skills absent from the source material
- copy requirements from the listing into the resume as if they were experience

If the source material lacks something the listing wants, leave it out — the
missing_skills gap is handled honestly in the cover letter ("quick to pick up X
given my Y") rather than faked in the resume.

Resume format: name/contact header, professional summary (3 lines max), core skills,
experience (reverse chronological, quantified bullets), then education/certs.
Cover letter: specific to this company and role, references 2-3 concrete achievements
from the facts, confirms the logistics the listing cares about (e.g. outside-IR35,
fully remote, availability), no filler."""


RESUME_CSS = """
@page { size: A4; margin: 14mm 16mm; }
* { box-sizing: border-box; }
body { font-family: 'Helvetica Neue', Arial, sans-serif; color: #1c1e21;
       font-size: 10.2pt; line-height: 1.45; margin: 0; }
h1 { font-size: 19pt; margin: 0 0 2px; letter-spacing: .5px; }
h2 { font-size: 11pt; text-transform: uppercase; letter-spacing: 1.2px;
     border-bottom: 1.5px solid #2c3e50; padding-bottom: 2px; margin: 14px 0 6px; }
h3 { font-size: 10.8pt; margin: 10px 0 2px; }
p { margin: 4px 0; }
ul { margin: 4px 0 8px; padding-left: 16px; }
li { margin: 2px 0; }
a { color: #2c3e50; text-decoration: none; }
strong { color: #000; }
"""


class Tailor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.llm = LLM(cfg)

    async def build_package(
        self, job: Job, evaluation: JobEvaluation, master_resume: str, facts: list[Fact]
    ) -> ApplicationPackage | None:
        if not self.llm.enabled or not master_resume.strip():
            print("[tailor] skipped (needs GEMINI_API_KEY and a master resume)")
            return None

        fact_block = (
            "\n\n".join(f"[{f.source} :: {f.heading or 'note'}]\n{f.text}" for f in facts)
            or "(no additional vault facts retrieved)"
        )
        profile = self.cfg.profile
        contact = ", ".join(
            filter(
                None,
                [
                    profile.get("name"),
                    profile.get("email"),
                    profile.get("phone"),
                    profile.get("location"),
                    profile.get("linkedin"),
                    profile.get("website"),
                ],
            )
        )

        user_msg = f"""JOB LISTING
Title: {job.title}
Company/Agency: {job.company} | Rate: {job.rate} | Source: {job.source}
Evaluator summary: {evaluation.summary}
Matched skills: {", ".join(evaluation.matched_skills)}
Gaps to address honestly in the letter: {", ".join(evaluation.missing_skills) or "none"}

Listing text:
{job.full_text[:6000]}

CANDIDATE CONTACT LINE (use verbatim in the resume header): {contact or "(take from master resume)"}

MASTER RESUME (the only permitted source of experience):
{master_resume[:12000]}

VERIFIED CAREER FACTS FROM THE CANDIDATE'S OBSIDIAN VAULT:
{fact_block}
"""
        try:
            return await self.llm.generate_structured(
                system=TAILOR_SYSTEM,
                user=user_msg,
                schema=ApplicationPackage,
            )
        except LLMQuotaError:
            raise  # defer the job: nothing recorded yet, so a later run redoes it with quota
        except LLMError as e:
            print(f"[tailor] {e}")
            return None

    async def save_package(self, browser: Browser, job: Job, package: ApplicationPackage) -> Path:
        """Write resume.md/.pdf + cover letter into data/applications/<job>/."""
        safe = re.sub(r"[^\w-]+", "-", f"{job.company}-{job.title}").strip("-")[:70].lower()
        out = self.cfg.output_dir / f"{safe}-{job.id[:8]}"
        out.mkdir(parents=True, exist_ok=True)

        (out / "resume.md").write_text(package.resume_markdown, encoding="utf-8")
        (out / "cover-letter.txt").write_text(package.cover_letter, encoding="utf-8")
        (out / "email.txt").write_text(
            f"Subject: {package.email_subject}\n\n{package.email_body}", encoding="utf-8"
        )
        (out / "tailoring-notes.md").write_text(
            "\n".join(f"- {n}" for n in package.tailoring_notes), encoding="utf-8"
        )

        html = (
            "<meta charset='utf-8'>"
            f"<style>{RESUME_CSS}</style>"
            f"{md.markdown(package.resume_markdown, extensions=['extra'])}"
        )
        # JS disabled: the HTML is model-generated content, rendering only needs CSS
        page = await browser.new_page(java_script_enabled=False)
        try:
            await page.set_content(html, wait_until="load")
            await page.pdf(path=str(out / "resume.pdf"), format="A4", print_background=True)
        finally:
            await page.close()

        print(f"[tailor] materials -> {out}")
        return out
