"""Config + secret loading.

config.yaml holds all tunables and is committed. Secrets come from environment
variables only (loaded from .env locally via python-dotenv; injected by GitHub
Actions in CI). Nothing here ever writes a secret to disk.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # optional locally, absent/irrelevant in CI
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a convenience only
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
MASTER_CV_PATH = REPO_ROOT / "data" / "master_cv.json"


@dataclass
class Secrets:
    """Runtime secrets, read from the environment only."""

    deepseek_api_key: str = ""
    notion_token: str = ""
    notion_database_id: str = ""
    ntfy_topic: str = ""

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            notion_token=os.environ.get("NOTION_TOKEN", ""),
            notion_database_id=os.environ.get("NOTION_DATABASE_ID", ""),
            ntfy_topic=os.environ.get("NTFY_TOPIC", ""),
        )


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)
    secrets: Secrets = field(default_factory=Secrets)

    # convenience accessors ------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {}) or {}

    @property
    def search(self) -> dict[str, Any]:
        return self.section("search")

    @property
    def geo(self) -> dict[str, Any]:
        return self.section("geo")

    @property
    def salary(self) -> dict[str, Any]:
        return self.section("salary")

    @property
    def source(self) -> dict[str, Any]:
        return self.section("source")

    @property
    def llm(self) -> dict[str, Any]:
        return self.section("llm")

    @property
    def rollover(self) -> dict[str, Any]:
        return self.section("rollover")

    @property
    def ntfy_topic(self) -> str:
        # Env wins over the config.yaml placeholder.
        return self.secrets.ntfy_topic or self.section("notify").get("ntfy_topic", "")


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config(raw=raw, secrets=Secrets.from_env())
