"""Obsidian vault integration.

Read side:  career notes ground the resume/cover-letter tailoring so the model
            can only re-emphasize real experience, never invent it.
Write side: one application note per job (Dataview-friendly frontmatter) so the
            vault doubles as the application pipeline tracker.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .models import Job

STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "will", "are", "our", "this", "that",
    "have", "from", "they", "their", "role", "team", "work", "working", "who", "what",
    "about", "into", "been", "were", "which", "would", "should", "could", "them",
    "than", "then", "there", "these", "those", "over", "such", "per", "day", "must",
    "job", "jobs", "apply", "candidate", "experience", "skills", "required", "years",
}

SKIP_DIRS = {".obsidian", ".trash", "templates", "Templates"}


@dataclass
class Fact:
    source: str   # note name (vault-relative)
    heading: str
    text: str


def _terms(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9+#./-]{2,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def _chunk_note(text: str) -> list[tuple[str, str]]:
    """Split a markdown note into (heading, body) chunks."""
    chunks: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in _strip_frontmatter(text).splitlines():
        if line.startswith("#"):
            if buf and "".join(buf).strip():
                chunks.append((heading, "\n".join(buf).strip()))
            heading = line.lstrip("#").strip()
            buf = []
        else:
            buf.append(line)
    if buf and "".join(buf).strip():
        chunks.append((heading, "\n".join(buf).strip()))
    return chunks


class Knowledge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.vault = cfg.vault_path
        self._chunks: list[Fact] | None = None

    # ---------- read side ----------

    def load_master_resume(self) -> str:
        """Master resume from RESUME_PATH; falls back to a vault note named like 'master resume'."""
        path = self.cfg.resume_path
        if path.exists():
            return path.read_text(encoding="utf-8")
        if self.vault:
            for p in self.vault.rglob("*.md"):
                if "master" in p.stem.lower() and ("resume" in p.stem.lower() or "cv" in p.stem.lower()):
                    return p.read_text(encoding="utf-8")
        return ""

    def profile_summary(self) -> str:
        """Short candidate summary used to prime the job evaluator."""
        prof = self.cfg.profile
        lines = []
        if prof.get("name"):
            lines.append(f"Candidate: {prof['name']}")
        if prof.get("headline"):
            lines.append(f"Headline: {prof['headline']}")
        resume = self.load_master_resume()
        if resume:
            lines.append("Master resume (excerpt):")
            lines.append(resume[:2500])
        return "\n".join(lines)

    def _load_chunks(self) -> list[Fact]:
        if self._chunks is not None:
            return self._chunks
        chunks: list[Fact] = []
        if self.vault:
            folders = self.cfg.vault_knowledge_folders
            roots = [self.vault / f for f in folders] if folders else [self.vault]
            for root in roots:
                if not root.exists():
                    continue
                for p in sorted(root.rglob("*.md")):
                    if any(part in SKIP_DIRS for part in p.parts):
                        continue
                    # never feed the agent's own application notes back in as facts
                    if self.cfg.vault_applications_folder in p.parts:
                        continue
                    try:
                        text = p.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    rel = str(p.relative_to(self.vault))
                    for heading, body in _chunk_note(text):
                        chunks.append(Fact(source=rel, heading=heading, text=body))
        self._chunks = chunks
        print(f"[knowledge] loaded {len(chunks)} fact chunks"
              f" from {'vault' if self.vault else 'no vault (profile only)'}")
        return chunks

    def retrieve_facts(self, job_text: str, top_n: int = 8, max_chars: int = 6000) -> list[Fact]:
        """Keyword-overlap retrieval: rank vault chunks against the job description."""
        chunks = self._load_chunks()
        if not chunks:
            return []
        job_terms = _terms(job_text)
        scored: list[tuple[float, Fact]] = []
        for fact in chunks:
            fact_terms = _terms(fact.heading + " " + fact.text)
            overlap = job_terms & fact_terms
            if not overlap:
                continue
            score = len(overlap) + 2 * len(job_terms & _terms(fact.heading))
            scored.append((score, fact))
        scored.sort(key=lambda x: x[0], reverse=True)

        picked, used = [], 0
        for _, fact in scored[: top_n * 2]:
            if used + len(fact.text) > max_chars:
                continue
            picked.append(fact)
            used += len(fact.text)
            if len(picked) >= top_n:
                break
        return picked

    # ---------- write side ----------

    def write_application_note(self, job: Job, evaluation, package,
                               status: str, materials_dir: Path | None) -> Path | None:
        """Log the application into the vault (or locally when no vault is configured)."""
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        safe = re.sub(r"[^\w\s-]", "", f"{job.company} - {job.title}").strip()[:80]
        filename = f"{date} {safe}.md"

        if self.vault and self.cfg.vault_write_enabled:
            folder = self.vault / self.cfg.vault_applications_folder
        else:
            folder = self.cfg.output_dir / "notes"
        folder.mkdir(parents=True, exist_ok=True)

        matched = ", ".join(evaluation.matched_skills[:10]) if evaluation else ""
        note = f"""---
type: job-application
company: "{job.company}"
role: "{job.title}"
rate: "{job.rate}"
source: {job.source}
url: "{job.url}"
score: {evaluation.fit_score if evaluation else ""}
status: {status}
applied: {date if status == "applied" else ""}
follow_up: ""
tags: [job-application]
---

# {job.title} — {job.company}

**Status:** {status} | **Fit score:** {evaluation.fit_score if evaluation else "n/a"}/100 | **Rate:** {job.rate}
**Listing:** {job.url}
**Materials:** `{materials_dir}`

## Why it matched
{evaluation.summary if evaluation else ""}

- **Matched skills:** {matched}
- **Gaps:** {", ".join(evaluation.missing_skills[:6]) if evaluation else ""}
- **Red flags:** {", ".join(evaluation.red_flags) if evaluation and evaluation.red_flags else "none noted"}

## Cover letter sent
{package.cover_letter if package else "_not generated_"}

## Follow-up checklist
- [ ] Confirm application received
- [ ] Follow up after 3 working days
- [ ] Log interview / outcome here
"""
        path = folder / filename
        path.write_text(note, encoding="utf-8")
        print(f"[knowledge] application note -> {path}")
        return path
