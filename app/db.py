import logging
from contextlib import contextmanager

import pyodbc

from app.config import DBSettings

logger = logging.getLogger(__name__)
settings = DBSettings.from_env()


def _escape(value: str) -> str:
    """ODBC values containing ; { } = or spaces are wrapped in braces; '}' is escaped as '}}'."""
    return "{" + value.replace("}", "}}") + "}"


def build_connection_string(s: DBSettings) -> str:
    parts = [
        f"DRIVER={{{s.driver}}}",
        f"SERVER={s.server}",
        f"DATABASE={_escape(s.database)}",   # braces handle the space in "Uni Project"
    ]
    if s.trusted_connection:
        parts.append("Trusted_Connection=yes")   # use the current Windows login
    else:
        parts.append(f"UID={s.user}")
        parts.append(f"PWD={_escape(s.password)}")
    parts.append(f"Encrypt={s.encrypt}")
    parts.append(f"TrustServerCertificate={s.trust_server_certificate}")
    return ";".join(parts) + ";"


@contextmanager
def get_connection():
    """Yield a connection; commit on success, rollback on error, always close."""
    conn = pyodbc.connect(build_connection_string(settings), timeout=settings.timeout)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    # Smoke test: run `python -m app.db` to verify DB connectivity
    # BEFORE involving any web framework.
    with get_connection() as conn:
        row = conn.cursor().execute("SELECT DB_NAME(), SUSER_SNAME()").fetchone()
        print(f"Connected to database '{row[0]}' as '{row[1]}'")