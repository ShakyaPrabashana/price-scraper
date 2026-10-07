"""Background scrape job: state tracking + the runner that ties scraper and repository together.

Why a job? A full scrape takes minutes. An HTTP request that stays open that
long gets killed by browsers/proxies. So POST /scrape starts the work, returns
immediately, and the client polls GET /scrape/status.

State is kept in memory: fine for one process, lost on restart. A production
system would use a task queue (Celery/RQ/Arq) and store job state in the DB.
"""

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app import repository, scraper

logger = logging.getLogger(__name__)


class JobAlreadyRunning(Exception):
    pass


class NoMatchingCategory(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ScrapeJob:
    job_id: str
    category: str | None
    details: bool
    started_at: str
    status: str = "running"        # running | completed | completed_with_errors | failed
    finished_at: str | None = None
    current_category: str | None = None
    categories_total: int = 0
    categories_done: int = 0
    products_scraped: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


_lock = threading.Lock()          # guards _current: the worker thread writes, request threads read
_current: ScrapeJob | None = None


def start_job(category: str | None, details: bool) -> ScrapeJob:
    """Register a new job, or refuse if one is already running (never two scrapes at once)."""
    global _current
    with _lock:
        if _current is not None and _current.status == "running":
            raise JobAlreadyRunning(_current.job_id)
        _current = ScrapeJob(job_id=uuid.uuid4().hex[:12], category=category,
                             details=details, started_at=_now())
        return _current


def get_status() -> dict | None:
    with _lock:
        return asdict(_current) if _current else None


def _update(job: ScrapeJob, **changes) -> None:
    with _lock:
        for key, value in changes.items():
            setattr(job, key, value)


def _add(job: ScrapeJob, **increments) -> None:
    with _lock:
        for key, amount in increments.items():
            setattr(job, key, getattr(job, key) + amount)


def _add_error(job: ScrapeJob, message: str) -> None:
    with _lock:
        job.errors.append(message)


def run_job(job: ScrapeJob) -> None:
    """Runs in a worker thread (FastAPI BackgroundTasks). Must never raise."""
    try:
        categories = scraper.get_categories()
        if job.category:
            categories = scraper.filter_categories(categories, job.category)
        if not categories:
            raise NoMatchingCategory(job.category or "no categories discovered")
        _update(job, categories_total=len(categories))

        seen: set[str] = set()
        for cat in categories:
            _update(job, current_category=cat["name"])
            try:
                rows = scraper.scrape_category(cat, details=job.details, skip_urls=seen)
                inserted, updated, skipped = repository.upsert_products(rows)  # saved per category
            except Exception as exc:  # noqa: BLE001 - one bad category must not kill the whole run
                logger.exception("Category %s failed", cat["name"])
                # Full details go to the log; the API response only gets the error type.
                _add_error(job, f"{cat['name']}: {type(exc).__name__}")
            else:
                seen.update(r["product_url"] for r in rows)
                _add(job, products_scraped=len(rows), inserted=inserted,
                     updated=updated, skipped=skipped)
            finally:
                _add(job, categories_done=1)

        with _lock:
            if not job.errors:
                job.status = "completed"
            elif job.categories_done > len(job.errors):
                job.status = "completed_with_errors"
            else:
                job.status = "failed"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Scrape job %s failed", job.job_id)
        _add_error(job, f"job: {type(exc).__name__}")
        _update(job, status="failed")
    finally:
        _update(job, current_category=None, finished_at=_now())
        logger.info("Scrape job %s finished: %s", job.job_id, get_status())