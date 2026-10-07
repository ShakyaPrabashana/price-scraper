import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Resolve .env relative to the project root, not the current working directory.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"yes", "true", "1"}


@dataclass(frozen=True)  # frozen = immutable, settings shouldn't change at runtime
class DBSettings:
    driver: str
    server: str
    database: str
    trusted_connection: bool
    user: str | None
    password: str | None
    encrypt: str
    trust_server_certificate: str
    timeout: int

    @classmethod
    def from_env(cls) -> "DBSettings":
        trusted = _as_bool(os.getenv("DB_TRUSTED_CONNECTION", "no"))
        return cls(
            driver=_require("DB_DRIVER"),
            server=_require("DB_SERVER"),
            database=_require("DB_NAME"),
            trusted_connection=trusted,
            # Only demand SQL credentials when we are NOT using Windows auth
            user=None if trusted else _require("DB_USER"),
            password=None if trusted else _require("DB_PASSWORD"),
            encrypt=os.getenv("DB_ENCRYPT", "yes"),
            trust_server_certificate=os.getenv("DB_TRUST_SERVER_CERTIFICATE", "no"),
            timeout=int(os.getenv("DB_TIMEOUT", "15")),
        )


@dataclass(frozen=True)
class AppSettings:
    """How the web server itself is launched. Not secrets, but still config."""

    host: str
    port: int
    reload: bool

    @classmethod
    def from_env(cls) -> "AppSettings":
        return cls(
            host=os.getenv("APP_HOST", "127.0.0.1"),
            port=int(os.getenv("APP_PORT", "8000")),
            reload=_as_bool(os.getenv("APP_RELOAD", "no")),
        )