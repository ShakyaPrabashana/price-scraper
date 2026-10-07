"""
SupplementStores.lk scraper.

Pure scraping logic: NO database code and NO web-framework code lives here.
It takes a category and returns plain dicts. That keeps it testable (feed it
saved HTML), reusable (CLI, scheduler or API can call it) and replaceable
(a second store gets its own module that returns the same dict shape).

Site notes (WooCommerce + Woodmart theme, server-rendered HTML):
  * categories:  /product-category/<slug>/          (auto-discovered from / and /shop/)
  * pagination:  /product-category/<slug>/page/N/   (stop when there is no "next" link)
  * listing tile -> name, url, image, price, sale badge, stock badge
  * product page (optional "details" pass) -> SKU, stock text, gallery, description
"""

import logging
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

STORE_NAME = "SupplementStores.lk"
BASE_URL = "https://supplementstores.lk"
REQUEST_DELAY = 0.5      # seconds between requests: be polite to the site
MAX_PAGES = 200          # safety cap so a broken "next" link can never loop forever

CAT_LINK_RE = re.compile(r"^/product-category/([a-z0-9\-]+)/?$")


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
    })
    # Retry transient failures (rate limit / server hiccups) with growing pauses
    retry = Retry(total=3, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",))
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = _build_session()


def get(url, **kwargs):
    return SESSION.get(url, timeout=25, **kwargs)


# --------------------------------------------------------------------------
# Category discovery
# --------------------------------------------------------------------------
def get_categories():
    """Auto-discover every /product-category/<slug>/ link from the homepage
    and the /shop/ page. Returns [{"name":..., "url":..., "slug":...}, ...]."""
    seen = {}
    for path in ("/", "/shop/"):
        try:
            resp = get(BASE_URL + path)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("Could not load %s to discover categories: %s", path, e)
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            parsed = urlparse(a["href"])
            m = CAT_LINK_RE.match(parsed.path)
            if not m:
                continue
            slug = m.group(1)
            url = f"{BASE_URL}/product-category/{slug}/"
            name = a.get_text(strip=True)
            # Prefer a real link-text name over a blank one (some links wrap only an <img>)
            if slug not in seen or (not seen[slug]["name"] and name):
                seen[slug] = {"name": name or slug.replace("-", " ").title(),
                              "url": url, "slug": slug}
    return list(seen.values())


def filter_categories(categories, wanted):
    """Keep only the category whose name or slug equals `wanted` (case-insensitive)."""
    wanted = wanted.strip().lower()
    return [c for c in categories if wanted in (c["name"].lower(), c["slug"].lower())]


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------
def _clean_price(text):
    """'Rs.7,500.00' -> 7500.0 ; also handles bare numbers with commas."""
    if not text:
        return None
    m = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
    return float(m.group()) if m else None


def _currency_symbol(text):
    if not text:
        return ""
    m = re.match(r"[^\d]+", text.strip())
    return m.group().strip() if m else ""


def _best_image_url(img_tag):
    """Woodmart lazy-loads images: the real URL is in data-src / data-lazy-src,
    while `src` is often a tiny inline placeholder or lazy.svg."""
    if img_tag is None:
        return ""
    for attr in ("data-src", "data-lazy-src", "data-large_image"):
        val = img_tag.get(attr)
        if val and "lazy.svg" not in val:
            return urljoin(BASE_URL, val)
    srcset = img_tag.get("data-srcset") or img_tag.get("srcset")
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first and "lazy.svg" not in first:
            return urljoin(BASE_URL, first)
    src = img_tag.get("src")
    if src and "lazy.svg" not in src:
        return urljoin(BASE_URL, src)
    return ""


def parse_product_tile(node, category_name):
    """Parse one `li.product` / `div.product-grid-item` tile into a normalized dict."""
    title_a = (node.select_one("h3.wd-entities-title a")
               or node.select_one(".woocommerce-loop-product__title")
               or node.select_one("a.product-title")
               or node.find("a", href=re.compile(r"/product/")))
    product_name = title_a.get_text(strip=True) if title_a else ""
    product_url = urljoin(BASE_URL, title_a["href"]) if title_a and title_a.get("href") else ""

    img_tag = node.find("img")
    image = _best_image_url(img_tag)

    price_span = node.select_one("span.price")
    price = regular_price = None
    currency = ""
    if price_span:
        ins = price_span.select_one("ins .woocommerce-Price-amount, ins")
        del_ = price_span.select_one("del .woocommerce-Price-amount, del")
        if ins and del_:
            price = _clean_price(ins.get_text())
            regular_price = _clean_price(del_.get_text())
            currency = _currency_symbol(ins.get_text())
        else:
            amt = price_span.select_one(".woocommerce-Price-amount") or price_span
            price = _clean_price(amt.get_text())
            regular_price = price
            currency = _currency_symbol(amt.get_text())

    onsale_tag = node.select_one("span.onsale")
    discount_pct = None
    if onsale_tag:
        m = re.search(r"-?(\d+(?:\.\d+)?)\s*%", onsale_tag.get_text())
        if m:
            discount_pct = float(m.group(1))
    if discount_pct is None and price and regular_price and regular_price > price:
        discount_pct = round((regular_price - price) / regular_price * 100, 1)

    node_classes = " ".join(node.get("class", []))
    sold_out_badge = node.find(string=re.compile(r"sold out", re.I))
    stock_status = "Out of Stock" if ("outofstock" in node_classes or sold_out_badge) else "In Stock"

    return {
        "category": category_name,
        "product_name": product_name,
        "product_url": product_url,
        "sku": "",
        "image": image,
        "all_images": image,
        "currency": currency,
        "price": price if price is not None else "",
        "regular_price": regular_price if regular_price is not None else "",
        "discount_pct": discount_pct if discount_pct is not None else "",
        "on_sale": bool(discount_pct and discount_pct > 0),
        "stock_status": stock_status,
        "stock_detail": "",
        "short_description": "",
    }


# --------------------------------------------------------------------------
# Crawling
# --------------------------------------------------------------------------
def get_category_products(category_url, category_name):
    """Paginate through a category's /page/N/ URLs until there is no next page."""
    all_rows = []
    page = 1
    while page <= MAX_PAGES:
        url = category_url if page == 1 else urljoin(category_url, f"page/{page}/")
        resp = get(url)
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        tiles = soup.select("li.product") or soup.select("div.product-grid-item")
        if not tiles:
            break
        all_rows.extend(parse_product_tile(t, category_name) for t in tiles)

        # Stop once WooCommerce's own pagination says there's no next page
        if not soup.select_one("a.next.page-numbers, .next.page-numbers"):
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return all_rows


def enrich_with_details(row):
    """Optional deep pass: visit the product page for SKU, stock text, gallery, description."""
    if not row["product_url"]:
        return row
    try:
        resp = get(row["product_url"])
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Detail fetch failed for %s: %s", row["product_url"], e)
        return row
    soup = BeautifulSoup(resp.text, "html.parser")

    sku_tag = soup.select_one("span.sku")
    if sku_tag:
        row["sku"] = sku_tag.get_text(strip=True)

    stock_tag = soup.select_one("p.stock")
    if stock_tag:
        row["stock_detail"] = stock_tag.get_text(strip=True)

    desc_tag = soup.select_one(".woocommerce-product-details__short-description")
    if desc_tag:
        row["short_description"] = desc_tag.get_text(" ", strip=True)

    gallery_imgs = soup.select(
        ".woocommerce-product-gallery__image img, "
        ".woocommerce-product-gallery img"
    )
    urls = []
    for img in gallery_imgs:
        u = img.get("data-large_image") or _best_image_url(img)
        if u and u not in urls:
            urls.append(u)
    if urls:
        row["image"] = urls[0]
        row["all_images"] = " | ".join(urls)

    cats = soup.select("span.posted_in a")
    if cats:
        row["category"] = ", ".join(c.get_text(strip=True) for c in cats)

    return row


def scrape_category(category, details=False, skip_urls=frozenset()):
    """Scrape ONE category and return its product dicts.

    skip_urls: product URLs already handled (products often appear under a
    parent and a child category; we only want them once per run).
    """
    rows = get_category_products(category["url"], category["name"])

    unique = {}
    for r in rows:
        url = r["product_url"]
        if url and url not in skip_urls:
            unique.setdefault(url, r)
    rows = list(unique.values())

    if details:
        for row in rows:
            enrich_with_details(row)
            time.sleep(REQUEST_DELAY)
    return rows