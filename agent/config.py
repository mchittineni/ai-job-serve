import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Config:
    """Merged view of config.yaml (non-secret) and environment variables (secrets/overrides)."""

    def __init__(self, path: str | None = None):
        cfg_path = Path(path or os.getenv("CONFIG_PATH") or ROOT / "config.yaml")
        self.raw: dict = {}
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                self.raw = yaml.safe_load(f) or {}

        self.data_dir = Path(os.getenv("DATA_DIR") or ROOT / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = self.data_dir / "applications"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # --- search ---
    @property
    def boards(self) -> list[dict]:
        return self.raw.get("boards", [])

    @property
    def keywords(self) -> list[str]:
        return [k.lower() for k in self.raw.get("search", {}).get("keywords", [])]

    @property
    def requirements(self) -> dict:
        return self.raw.get("search", {}).get("requirements", {})

    @property
    def target_roles(self) -> list[str]:
        return self.raw.get("search", {}).get("target_roles", [])

    # --- agent behaviour ---
    @property
    def apply_mode(self) -> str:
        """alert = notify only | draft = generate materials, human sends | auto = email applications."""
        mode = (os.getenv("APPLY_MODE") or self.raw.get("agent", {}).get("mode") or "draft").lower()
        return mode if mode in ("alert", "draft", "auto") else "draft"

    @property
    def min_score(self) -> int:
        return int(os.getenv("MIN_SCORE") or self.raw.get("agent", {}).get("min_score", 70))

    @property
    def max_applications_per_run(self) -> int:
        return int(
            os.getenv("MAX_APPLICATIONS_PER_RUN")
            or self.raw.get("agent", {}).get("max_applications_per_run", 3)
        )

    @property
    def max_applications_per_day(self) -> int:
        return int(
            os.getenv("MAX_APPLICATIONS_PER_DAY")
            or self.raw.get("agent", {}).get("max_applications_per_day", 10)
        )

    # --- job sources ---
    @property
    def reed_api_key(self) -> str | None:
        return os.getenv("REED_API_KEY")

    @property
    def adzuna_app_id(self) -> str | None:
        return os.getenv("ADZUNA_APP_ID")

    @property
    def adzuna_app_key(self) -> str | None:
        return os.getenv("ADZUNA_APP_KEY")

    # --- Gemini ---
    @property
    def gemini_api_key(self) -> str | None:
        return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    @property
    def gemini_model(self) -> str:
        return os.getenv("GEMINI_MODEL") or self.raw.get("agent", {}).get("model", "gemini-2.5-pro")

    # --- candidate profile ---
    @property
    def profile(self) -> dict:
        return self.raw.get("profile", {})

    @property
    def resume_path(self) -> Path:
        return Path(
            os.getenv("RESUME_PATH")
            or self.raw.get("profile", {}).get("master_resume")
            or ROOT / "profile" / "master-resume.md"
        )

    # --- Obsidian ---
    @property
    def vault_path(self) -> Path | None:
        raw = os.getenv("OBSIDIAN_VAULT_PATH") or self.raw.get("obsidian", {}).get("vault_path")
        if not raw:
            return None
        p = Path(raw).expanduser()
        return p if p.exists() else None

    @property
    def vault_knowledge_folders(self) -> list[str]:
        return self.raw.get("obsidian", {}).get("knowledge_folders", [])

    @property
    def vault_applications_folder(self) -> str:
        return self.raw.get("obsidian", {}).get("applications_folder", "Applications")

    @property
    def vault_write_enabled(self) -> bool:
        return _env_bool(
            "OBSIDIAN_WRITE_NOTES", bool(self.raw.get("obsidian", {}).get("write_application_notes", True))
        )

    # --- Notion job tracking ---
    @property
    def notion_api_key(self) -> str | None:
        return os.getenv("NOTION_API_KEY")

    @property
    def notion_database_id(self) -> str | None:
        return os.getenv("NOTION_DATABASE_ID") or self.raw.get("notion", {}).get("database_id") or None

    @property
    def notion_parent_page_id(self) -> str | None:
        return os.getenv("NOTION_PARENT_PAGE_ID") or self.raw.get("notion", {}).get("parent_page_id") or None

    # --- notifications ---
    @property
    def discord_webhook(self) -> str | None:
        return os.getenv("DISCORD_WEBHOOK_URL")

    # --- email (auto-apply channel) ---
    @property
    def smtp(self) -> dict | None:
        host = os.getenv("SMTP_HOST")
        if not host:
            return None
        return {
            "host": host,
            "port": int(os.getenv("SMTP_PORT", "587")),
            "user": os.getenv("SMTP_USER"),
            "password": os.getenv("SMTP_PASSWORD"),
            "from_addr": os.getenv("SMTP_FROM") or os.getenv("SMTP_USER"),
            "bcc": os.getenv("SMTP_BCC"),  # optional self-copy of every application
        }
