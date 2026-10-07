"""Data access for dbo.ScrapedProducts. Every SQL statement in the project lives here."""

import logging
from decimal import Decimal

from app.db import get_connection
from app.scraper import BASE_URL, STORE_NAME

logger = logging.getLogger(__name__)

# Upsert key = (StoreName, ProductUrl). Same product scraped again -> UPDATE, never a duplicate row.
# COALESCE(?, SKU): a blank value from a fast (no --details) run must not erase a good SKU
# that an earlier detailed run already stored.
UPDATE_SQL = """
UPDATE dbo.ScrapedProducts
SET StoreUrl     = ?,
    CategoryName = ?,
    ProductName  = ?,
    SKU          = COALESCE(?, SKU),
    ImageUrl     = ?,
    Price        = ?,
    RegularPrice = ?,
    DiscountPct  = ?,
    OnSale       = ?,
    StockStatus  = ?,
    StockDetail  = COALESCE(?, StockDetail),
    ScrapedAt    = SYSUTCDATETIME()
WHERE StoreName = ? AND ProductUrl = ?
"""

INSERT_SQL = """
INSERT INTO dbo.ScrapedProducts
    (StoreName, StoreUrl, CategoryName, ProductName, ProductUrl, SKU, ImageUrl,
     Price, RegularPrice, DiscountPct, OnSale, StockStatus, StockDetail, ScrapedAt)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME())
"""

SELECT_COLUMNS = """
    Id AS id, StoreName AS store_name, StoreUrl AS store_url,
    CategoryName AS category_name, ProductName AS product_name,
    ProductUrl AS product_url, SKU AS sku, ImageUrl AS image_url,
    Price AS price, RegularPrice AS regular_price, DiscountPct AS discount_pct,
    OnSale AS on_sale, StockStatus AS stock_status, StockDetail AS stock_detail,
    ScrapedAt AS scraped_at
"""


def _text(value, limit):
    """Trim to the column width (SQL Server raises an error on overflow). '' -> None."""
    if value is None:
        return None
    value = str(value).strip()[:limit]
    return value or None


def _money(value):
    """Scraper gives float or ''. Send Decimal so DECIMAL columns get exact values."""
    if value in ("", None):
        return None
    return Decimal(str(value))


def _prepare(row):
    """Scraper dict -> validated values in column-width limits. None = unusable row."""
    name = _text(row.get("product_name"), 300)
    url = _text(row.get("product_url"), 500)
    if not name or not url:          # both columns are NOT NULL
        return None
    return {
        "category": _text(row.get("category"), 150),
        "name": name,
        "url": url,
        "sku": _text(row.get("sku"), 100),
        "image": _text(row.get("image"), 500),
        "price": _money(row.get("price")),
        "regular_price": _money(row.get("regular_price")),
        "discount_pct": _money(row.get("discount_pct")),
        "on_sale": bool(row.get("on_sale")),
        "stock_status": _text(row.get("stock_status"), 20) or "Unknown",
        "stock_detail": _text(row.get("stock_detail"), 100),
    }


def upsert_products(rows):
    """Insert new products, update existing ones. One transaction: all or nothing.
    Returns (inserted, updated, skipped)."""
    inserted = updated = skipped = 0
    with get_connection() as conn:          # commits on success, rolls back on error
        cur = conn.cursor()
        for row in rows:
            p = _prepare(row)
            if p is None:
                skipped += 1
                continue
            cur.execute(
                UPDATE_SQL,
                BASE_URL, p["category"], p["name"], p["sku"], p["image"],
                p["price"], p["regular_price"], p["discount_pct"], p["on_sale"],
                p["stock_status"], p["stock_detail"],
                STORE_NAME, p["url"],
            )
            if cur.rowcount == 0:           # nothing matched -> it's a new product
                cur.execute(
                    INSERT_SQL,
                    STORE_NAME, BASE_URL, p["category"], p["name"], p["url"],
                    p["sku"], p["image"], p["price"], p["regular_price"],
                    p["discount_pct"], p["on_sale"], p["stock_status"], p["stock_detail"],
                )
                inserted += 1
            else:
                updated += 1
    return inserted, updated, skipped


def list_products(limit=50, offset=0, category=None, on_sale=None):
    """Read products. The SQL *text* only ever contains fixed fragments;
    every user-supplied value travels as a parameter (no SQL injection)."""
    where, params = [], []
    if category:
        where.append("CategoryName LIKE ?")
        params.append(f"%{category}%")
    if on_sale is not None:
        where.append("OnSale = ?")
        params.append(1 if on_sale else 0)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    sql = (f"SELECT {SELECT_COLUMNS} FROM dbo.ScrapedProducts {clause} "
           "ORDER BY Id OFFSET ? ROWS FETCH NEXT ? ROWS ONLY")
    params += [offset, limit]

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, *params)
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, r)) for r in cur.fetchall()]