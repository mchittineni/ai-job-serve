"""Email application channel used in APPLY_MODE=auto.

Contract roles on JobServe/CWJobs are usually agency-mediated, and agencies list
an apply email — email with a PDF resume attached is the one channel that works
reliably and doesn't fight per-site application forms.
"""
import asyncio
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .config import Config
from .models import Job
from .tailor import ApplicationPackage

# Strict shape check for the LLM-extracted address: one plain address, no
# display names, whitespace, or header-injection characters.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _send_sync(smtp_cfg: dict, to_addr: str, subject: str, body: str,
               attachments: list[Path]) -> None:
    msg = EmailMessage()
    msg["From"] = smtp_cfg["from_addr"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    if smtp_cfg.get("bcc"):
        msg["Bcc"] = smtp_cfg["bcc"]
    msg.set_content(body)

    for path in attachments:
        if not path.exists():
            continue
        data = path.read_bytes()
        if path.suffix == ".pdf":
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=path.name)
        else:
            msg.add_attachment(data, maintype="text", subtype="plain", filename=path.name)

    port = smtp_cfg["port"]
    if port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(smtp_cfg["host"], port, timeout=30)
    else:
        server = smtplib.SMTP(smtp_cfg["host"], port, timeout=30)
    with server:
        if port != 465:
            server.starttls()
        if smtp_cfg.get("user"):
            server.login(smtp_cfg["user"], smtp_cfg["password"])
        server.send_message(msg)


async def send_application(cfg: Config, job: Job, package: ApplicationPackage,
                           to_addr: str, materials_dir: Path) -> None:
    """Raises on failure so the caller can downgrade the job to draft status."""
    smtp_cfg = cfg.smtp
    if not smtp_cfg:
        raise RuntimeError("SMTP not configured (set SMTP_HOST/SMTP_USER/SMTP_PASSWORD)")
    to_addr = (to_addr or "").strip()
    if not EMAIL_RE.match(to_addr):
        raise ValueError(f"refusing to send: extracted apply address looks invalid ({to_addr!r})")

    resume_pdf = materials_dir / "resume.pdf"
    await asyncio.to_thread(
        _send_sync, smtp_cfg, to_addr, package.email_subject, package.email_body, [resume_pdf],
    )
    print(f"[apply] emailed application for '{job.title}' to {to_addr}")
