import logging
from datetime import datetime

import pyodbc
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app import jobs, repository
from app.db import get_connection

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Supplement Store Scraper API",
    description="Scrapes supplementstores.lk and stores the products in SQL Server.",
    version="1.0.0",
)


# ---------- request / response models (the API's public contract) ----------
class ScrapeRequest(BaseModel):
    category: str | None = Field(
        default=None,
        description="Category name or slug, e.g. 'Protein'. Leave empty to scrape every category.",
    )
    details: bool = Field(
        default=False,
        description="Also open each product page for SKU, stock text and gallery. "
                    "Much slower: one extra request per product.",
    )


class ProductOut(BaseModel):
    id: int
    store_name: str
    store_url: str | None
    category_name: str | None
    product_name: str
    product_url: str
    sku: str | None
    image_url: str | None
    price: float | None
    regular_price: float | None
    discount_pct: float | None
    on_sale: bool
    stock_status: str
    stock_detail: str | None
    scraped_at: datetime


# ---------- routes ----------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/health")
def health():
    """Liveness: is the process up? (No DB involved.)"""
    return {"status": "ok"}


@app.get("/db-check")
def db_check():
    """Readiness: can the app reach the database?"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DB_NAME(), @@VERSION")
            db_name, version = cursor.fetchone()
        return {"connected": True, "database": db_name,
                "server_version": version.splitlines()[0]}
    except pyodbc.Error:
        logger.exception("Database connection failed")
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/scrape", status_code=202)
def start_scrape(background_tasks: BackgroundTasks, body: ScrapeRequest | None = None):
    """Start a scrape in the background. Returns immediately; poll /scrape/status."""
    body = body or ScrapeRequest()
    try:
        job = jobs.start_job(body.category, body.details)
    except jobs.JobAlreadyRunning:
        raise HTTPException(status_code=409, detail="A scrape is already running")
    background_tasks.add_task(jobs.run_job, job)
    return {"job_id": job.job_id, "status": job.status,
            "check_progress": "/scrape/status"}


@app.get("/scrape/status")
def scrape_status():
    """Progress of the current (or most recent) scrape."""
    return jobs.get_status() or {"status": "idle"}


@app.get("/products", response_model=list[ProductOut])
def get_products(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    category: str | None = Query(None, description="Substring match on the category name"),
    on_sale: bool | None = Query(None, description="true = only discounted items"),
):
    """Read stored products (paged)."""
    try:
        return repository.list_products(limit=limit, offset=offset,
                                        category=category, on_sale=on_sale)
    except pyodbc.Error:
        logger.exception("Could not read products")
        raise HTTPException(status_code=503, detail="Database unavailable")