"""Environment and path configuration. Import `settings` from here."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    demo: bool = field(default_factory=lambda: _bool(os.getenv("DEMO"), False))
    data_dir: Path = field(default_factory=lambda: (ROOT / os.getenv("DATA_DIR", "./data")).resolve())
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8765")))
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY") or None)
    classify_model: str = field(default_factory=lambda: os.getenv("CLASSIFY_MODEL", "claude-haiku-4-5"))
    escalate_model: str = field(default_factory=lambda: os.getenv("ESCALATE_MODEL", "claude-sonnet-5"))
    escalate_below: float = field(default_factory=lambda: float(os.getenv("ESCALATE_BELOW_CONFIDENCE", "0.6")))
    credentials_path: Path = field(default_factory=lambda: ROOT / os.getenv("GOOGLE_CREDENTIALS", "credentials.json"))
    token_path: Path = field(default_factory=lambda: ROOT / os.getenv("GOOGLE_TOKEN", "token.json"))

    @property
    def db_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / ("demo.db" if self.demo else "cleanup.db")

    @property
    def mode(self) -> str:
        return "demo" if self.demo else "live"


settings = Config()
