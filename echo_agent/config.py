"""Runtime configuration, loaded from environment variables and .env files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OPENAI_URL = "https://api.openai.com/v1"


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines without overriding the real environment."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Config:
    """Everything Echo needs to know about where it runs and what it talks to."""

    home: Path
    provider: str = "ollama"
    model: str = "llama3.2"
    base_url: str = DEFAULT_OLLAMA_URL
    api_key: str = ""
    embed_model: str = ""
    temperature: float = 0.3
    max_tool_steps: int = 12
    llm_retries: int = 2
    llm_retry_delay: float = 1.0
    auto_memory: bool = True
    docs_dir_name: str = "docs"
    outbox_dir_name: str = "outbox"
    archive_dir_name: str = "archive"
    db_name: str = "echo.db"

    @classmethod
    def from_env(cls) -> "Config":
        home = Path(os.environ.get("ECHO_HOME", "~/.echo")).expanduser()
        load_dotenv(Path.cwd() / ".env")
        load_dotenv(home / ".env")

        provider = os.environ.get("ECHO_PROVIDER", "ollama").strip().lower()
        if provider in {"openai-compatible", "openai_compatible", "compatible"}:
            provider = "openai"
        if provider not in {"ollama", "openai"}:
            raise ValueError(
                "ECHO_PROVIDER must be 'ollama' or 'openai' (got %r)" % provider
            )

        base_url = os.environ.get("ECHO_BASE_URL", "").strip()
        if not base_url:
            base_url = DEFAULT_OLLAMA_URL if provider == "ollama" else DEFAULT_OPENAI_URL

        try:
            temperature = float(os.environ.get("ECHO_TEMPERATURE", "0.3"))
        except ValueError:
            temperature = 0.3
        try:
            max_steps = int(os.environ.get("ECHO_MAX_TOOL_STEPS", "12"))
        except ValueError:
            max_steps = 12
        try:
            retries = int(os.environ.get("ECHO_LLM_RETRIES", "2"))
        except ValueError:
            retries = 2
        try:
            retry_delay = float(os.environ.get("ECHO_LLM_RETRY_DELAY", "1"))
        except ValueError:
            retry_delay = 1.0

        return cls(
            home=home,
            provider=provider,
            model=os.environ.get("ECHO_MODEL", "llama3.2").strip() or "llama3.2",
            base_url=base_url.rstrip("/"),
            api_key=os.environ.get("ECHO_API_KEY", "").strip(),
            embed_model=os.environ.get("ECHO_EMBED_MODEL", "").strip(),
            temperature=temperature,
            max_tool_steps=max(1, max_steps),
            llm_retries=max(0, retries),
            llm_retry_delay=max(0.0, retry_delay),
            auto_memory=_truthy(os.environ.get("ECHO_AUTO_MEMORY", "1")),
        )

    @property
    def docs_dir(self) -> Path:
        return self.home / self.docs_dir_name

    @property
    def outbox_dir(self) -> Path:
        return self.home / self.outbox_dir_name

    @property
    def archive_dir(self) -> Path:
        return self.home / self.archive_dir_name

    @property
    def db_path(self) -> Path:
        return self.home / self.db_name

    def ensure_dirs(self) -> None:
        for path in (self.home, self.docs_dir, self.outbox_dir, self.archive_dir):
            path.mkdir(parents=True, exist_ok=True)

    def summary(self) -> str:
        lines = [
            "home:     %s" % self.home,
            "provider: %s" % self.provider,
            "model:    %s" % self.model,
            "endpoint: %s" % self.base_url,
            "embed:    %s" % (self.embed_model or "(keyword search only)"),
            "retries:  %d (backoff %.1fs)" % (self.llm_retries, self.llm_retry_delay),
            "docs:     %s" % self.docs_dir,
            "outbox:   %s" % self.outbox_dir,
        ]
        return "\n".join(lines)
