"""Runtime configuration, read from environment variables.

On an installed system these come from /etc/sysadmin-ia/sysadmin-ia.env,
loaded by the systemd unit (EnvironmentFile=). For development you can export
them in your shell or put them in a `.env` file next to this package.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent  # the project root (contains app/)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (dev convenience). Does not override existing vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(APP_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: os.environ.get("SAI_HOST", "0.0.0.0"))
    # HTTPS port when a TLS cert/key is configured; otherwise the (only) plain-HTTP port.
    port: int = field(default_factory=lambda: int(os.environ.get("SAI_PORT", "8765")))
    # Additional plain-HTTP port served alongside HTTPS. 0 disables it.
    http_port: int = field(default_factory=lambda: int(os.environ.get("SAI_HTTP_PORT") or "0"))
    tls_cert: str = field(default_factory=lambda: os.environ.get("SAI_TLS_CERT", ""))
    tls_key: str = field(default_factory=lambda: os.environ.get("SAI_TLS_KEY", ""))

    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("SAI_DATA_DIR", str(APP_DIR / "data")))
    )
    app_dir: Path = APP_DIR
    service_name: str = field(
        default_factory=lambda: os.environ.get("SAI_SERVICE_NAME", "sysadmin-ia")
    )
    secret_key: str = field(
        default_factory=lambda: os.environ.get("SAI_SECRET_KEY") or secrets.token_hex(32)
    )
    session_hours: int = field(default_factory=lambda: int(os.environ.get("SAI_SESSION_HOURS", "12")))

    # Claude
    # Used once, to create the first "Claude" AI connection from older installs; after that,
    # models, keys and providers are managed in the AI connections module (ai_connections.py).
    model: str = field(default_factory=lambda: os.environ.get("SAI_MODEL") or "claude-opus-5-5")
    effort: str = field(default_factory=lambda: os.environ.get("SAI_EFFORT") or "high")
    max_tokens: int = field(default_factory=lambda: int(os.environ.get("SAI_MAX_TOKENS", "64000")))
    # "default" = server-side refusal fallback routing; empty string disables it.
    fallbacks: str = field(default_factory=lambda: os.environ.get("SAI_FALLBACKS", "default"))

    # Tool execution
    command_timeout: int = field(
        default_factory=lambda: int(os.environ.get("SAI_COMMAND_TIMEOUT", "300"))
    )
    max_tool_output: int = field(
        default_factory=lambda: int(os.environ.get("SAI_MAX_TOOL_OUTPUT", "30000"))
    )
    # When true, a user may switch the chat to "auto-approve" mode.
    allow_auto_approve: bool = field(default_factory=lambda: _bool("SAI_ALLOW_AUTO_APPROVE", True))
    # When true, admins get a web console (a shell on the server as the service user).
    allow_console: bool = field(default_factory=lambda: _bool("SAI_ALLOW_CONSOLE", True))

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert and self.tls_key)

    @property
    def http_enabled(self) -> bool:
        """True if any listener serves plain HTTP."""
        return not self.tls_enabled or self.http_port > 0

    def listeners(self) -> list[tuple[int, bool]]:
        """(port, use_tls) pairs to serve."""
        if not self.tls_enabled:
            return [(self.port, False)]
        out = [(self.port, True)]
        if self.http_port and self.http_port != self.port:
            out.append((self.http_port, False))
        return out

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sysadmin-ia.sqlite3"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.backup_dir.mkdir(parents=True, exist_ok=True)
