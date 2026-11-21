# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28", "orjson>=3.10"]
# ///
"""Scrape every FixPart.ch product title + price into SQLite, resumably.

Each listing page embeds a schema.org ItemList (25 products) with name, price,
currency, availability, brand and URL, so one GET yields 25 complete rows.
Pagination is server-side (?page=N, max from data-page).

Products and per-page progress are committed together in fixpart.db.
Old `combos` flags are not trusted: historical versions marked failures done.
Captcha responses stop the run once they are unbroken; a lone gated listing stays pending.
"""

import argparse
import asyncio
import csv
import os
import re
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, parse_qs

import httpx
import orjson

BASE = "https://fixpart.ch"
BRANDS_PATH = "/de/marken"
CATEGORIES_PATH = "/de/produktkategorien"
LISTING = "/de/ersatzteile/{brand}/{category}?page={page}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36",
}
FIELDS = ("sku", "title", "price", "currency", "availability", "brand", "mpn", "url")
# the gate also answers HTTP 200, and a cookie that mostly works is gated on odd listings,
# so one gate is skipped and only an unbroken run of them means the wall is total
MAX_CONSECUTIVE_GATES = 20


class Page(HTMLParser):
    def __init__(self, html: str, *, url=BASE):
        super().__init__()
        self.url = url
        self.links = []
        self.popular = []
        self.pages = []
        self.scripts = []
        self.script = None
        self.is_listing = False
        self.listing_depth = 0
        self.filter_depth = 0
        self.counter_text = []
        self.product_cards = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "div":
            if self.listing_depth:
                self.listing_depth += 1
            elif "productlist__wrapper" in classes:
                self.listing_depth = 1
                self.is_listing = True
            if self.listing_depth and attrs.get("id") == "filters":
                self.filter_depth = self.listing_depth
        if self.listing_depth and "productlist__item" in classes:
            self.product_cards += 1
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
            if "blocklist__item--brand" in classes:
                self.popular.append(attrs["href"])
        if attrs.get("data-page"):
            self.pages.append(int(attrs["data-page"]))
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.script = ""

    def handle_data(self, data):
        if self.filter_depth:
            self.counter_text.append(data)
        if self.script is not None:
            self.script += data

    def handle_endtag(self, tag):
        if tag == "div" and self.listing_depth:
            if self.listing_depth == self.filter_depth:
                self.filter_depth = 0
            self.listing_depth -= 1
        if tag == "script" and self.script is not None:
            self.scripts.append(self.script)
            self.script = None


def extract_items(html: str) -> tuple[list[dict], int]:
    """Return the products in this page's ItemList and the highest page number on it."""
    if 'id="confirmIsHuman"' in html:
        # the gate answers HTTP 200, so only the body tells it apart from a real page
        raise RuntimeError("Captcha required; run stopped with progress preserved")
    page = Page(html)
    verified_empty = (
        " ".join("".join(page.counter_text).split()) == "Filter 0 Ergebnisse"
        and page.product_cards == 0
        and max(page.pages, default=1) == 1
    )
    rows = []
    for blob in page.scripts:
        data = orjson.loads(blob)
        if data.get("@type") != "ItemList":
            continue
        for entry in data["itemListElement"]:
            item = entry["item"]
            offer = item["offers"]
            required = (item.get("sku"), item.get("name"), offer.get("priceCurrency"))
            if not all(isinstance(value, str) and value.strip() for value in required):
                raise ValueError("Product is missing SKU, title, or currency")
            try:
                price = Decimal(str(offer["price"]))
            except (KeyError, InvalidOperation) as exc:
                raise ValueError("Product price is missing or malformed") from exc
            if not price.is_finite() or price < 0:
                raise ValueError("Product price must be finite and nonnegative")
            rows.append(
                {
                    "sku": item.get("sku", ""),
                    "title": item.get("name", ""),
                    "price": str(price),
                    "currency": offer.get("priceCurrency", ""),
                    "availability": (offer.get("availability") or "").rsplit("/", 1)[-1],
                    "brand": (item.get("brand") or {}).get("name", ""),
                    "mpn": item.get("mpn", ""),
                    "url": item.get("url", ""),
                }
            )
        if not rows and not verified_empty:
            raise ValueError("Empty ItemList has no explicit zero-result confirmation")
        if rows and verified_empty:
            raise ValueError("Zero-result counter contradicts product data")
        return rows, max(page.pages, default=1)
    if verified_empty:
        return [], 1
    raise ValueError("Expected catalogue ItemList is absent; page remains pending")


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(path: str, pid: int | None = None) -> bool:
    """One crawler per database. A lock held by a dead PID is reclaimed."""
    pid = pid or os.getpid()
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(pid).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                holder = int(open(path, encoding="utf-8").read().strip())
            except (OSError, ValueError):
                holder = 0
            if process_alive(holder):
                return False
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    return False


def release_lock(path: str, pid: int | None = None) -> None:
    pid = pid or os.getpid()
    try:
        if int(open(path, encoding="utf-8").read().strip()) == pid:
            os.remove(path)
    except (OSError, ValueError):
        pass


def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS products ("
        "sku TEXT PRIMARY KEY, title TEXT, price TEXT, currency TEXT, "
        "availability TEXT, brand TEXT, mpn TEXT, url TEXT)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS brand_sizes ("
        "brand TEXT PRIMARY KEY, products INTEGER NOT NULL, checked_at INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS progress ("
        "brand TEXT, category TEXT, next_page INTEGER NOT NULL, last_sku TEXT, "
        "done INTEGER NOT NULL, max_page INTEGER NOT NULL DEFAULT 1, "
        "PRIMARY KEY (brand, category))"
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(progress)")}
    if "max_page" not in columns:
        db.execute("ALTER TABLE progress ADD COLUMN max_page INTEGER NOT NULL DEFAULT 1")
    db.commit()
    return db


def save_rows(db: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    before = db.total_changes
    db.executemany(
        "INSERT OR IGNORE INTO products (sku, title, price, currency, availability, brand, mpn, url) "
        "VALUES (:sku, :title, :price, :currency, :availability, :brand, :mpn, :url)",
        rows,
    )
    inserted = db.total_changes - before
    db.executemany(
        "UPDATE products SET title=:title, price=:price, currency=:currency, "
        "availability=:availability, brand=:brand, mpn=:mpn, url=:url WHERE sku=:sku",
        rows,
    )
    return inserted


def export(db: sqlite3.Connection, path: str) -> None:
    encoding = "utf-8-sig" if path.endswith(".csv") else "utf-8"
    with open(path, "w", encoding=encoding, newline="") as fh:
        if path.endswith(".csv"):
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(FIELDS)
        else:
            writer = None
        for record in db.execute(f"SELECT {', '.join(FIELDS)} FROM products ORDER BY rowid"):
            if writer:
                writer.writerow(record)
            else:
                fh.write(orjson.dumps(dict(zip(FIELDS, record))).decode() + "\n")
    total = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    print(f"exported {total:,} products -> {path}", flush=True)


class Guard:
    """Spaces requests at --rate per second and parks everyone when nginx says 429."""

    def __init__(self, rate: float):
        self.interval = 1 / rate
        self.next_at = 0.0
        self.blocked_until = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                delay = max(self.next_at, self.blocked_until) - now
                if delay <= 0:
                    self.next_at = now + self.interval
                    return
                await asyncio.sleep(delay)

    def block(self, seconds: float) -> None:
        self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)


async def get(client: httpx.AsyncClient, url: str, guard: Guard, state: dict) -> httpx.Response | None:
    for _ in range(6):
        await guard.wait()
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            print(f"network error ({exc.__class__.__name__}), leaving listing pending", flush=True)
            return None
        if response.status_code == 429:
            delay = 30
            retry_after = response.headers.get("retry-after")
            if retry_after:
                from email.utils import parsedate_to_datetime
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = max(0, parsedate_to_datetime(retry_after).timestamp() - time.time())
            print(f"HTTP 429: global cooldown {delay}s", flush=True)
            guard.block(delay)
            continue
        if "captcha" in urlsplit(urljoin(url, response.headers.get("location", ""))).path:
            raise RuntimeError("Captcha required; run stopped with progress preserved")
        return response
    return None


def parse_cookie(raw: str) -> dict[str, str]:
    jar = {}
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            jar[name] = value
    return jar


def listing_slugs(page: Page) -> set[str]:
    slugs = set()
    for link in page.links:
        url = urlsplit(urljoin(page.url, link))
        parts = url.path.strip("/").split("/")
        if url.netloc == "fixpart.ch" and len(parts) == 3 and parts[:2] == ["de", "ersatzteile"]:
            slugs.add(parts[2])
    return slugs


BRAND_SIZE = re.compile(r"mehr als\s+([\d\u2019'.,\s]+?)\s*Produkte", re.I)


def parse_brand_size(html: str) -> int | None:
    """Read the brand page's stated catalogue size, e.g. 'mehr als 4’604 Produkte'."""
    match = BRAND_SIZE.search(html)
    if not match:
        return None
    digits = "".join(ch for ch in match.group(1) if ch.isdigit())
    return int(digits) if digits else None


def save_brand_size(db: sqlite3.Connection, brand: str, products: int) -> None:
    db.execute(
        "INSERT OR REPLACE INTO brand_sizes (brand, products, checked_at) VALUES (?, ?, ?)",
        (brand, products, int(time.time())),
    )
    db.commit()


def read_brand_sizes(db: sqlite3.Connection) -> dict[str, int]:
    return dict(db.execute("SELECT brand, products FROM brand_sizes"))


async def measure_brands(client, guard, db, brands: list[str]) -> dict[str, int]:
    sizes = read_brand_sizes(db)
    missing = [b for b in brands if b not in sizes]
    for index, brand in enumerate(missing, 1):
        response = await get(client, f"{BASE}/de/ersatzteile/{brand}", guard, {"db": db})
        if response is None or response.status_code != 200:
            continue
        size = parse_brand_size(response.text) or 0
        save_brand_size(db, brand, size)
        sizes[brand] = size
        if index % 100 == 0:
            print(f"measured {index}/{len(missing)} brands", flush=True)
    return sizes


def order_pending(brands, categories, popular, done, sizes=None):
    """Biggest catalogues first when sizes are known, otherwise popular brands first."""
    if sizes:
        ordered = sorted(brands, key=lambda b: (-sizes.get(b, 0), b))
    else:
        ordered = [b for b in brands if b in popular] + [b for b in brands if b not in popular]
    return [(b, c) for b in ordered for c in categories if (b, c) not in done]


async def discover(client: httpx.AsyncClient, *, guard=None) -> tuple[list[str], list[str], set[str]]:
    guard = guard if guard is not None else Guard(1.6)
    async def load(url):
        response = await get(client, url, guard, {})
        if response is None:
            raise RuntimeError("Discovery request failed; cannot establish scope")
        response.raise_for_status()
        return Page(response.text, url=str(response.url))

    categories = listing_slugs(await load(BASE + CATEGORIES_PATH))
    directory = await load(BASE + BRANDS_PATH)
    brands = listing_slugs(directory)
    popular = {
        urlsplit(urljoin(directory.url, link)).path.rstrip("/").split("/")[-1]
        for link in directory.popular
    }
    letter_urls = {
        urljoin(directory.url, link).split("#")[0] for link in directory.links
        if urlsplit(urljoin(directory.url, link)).netloc == "fixpart.ch"
        and urlsplit(urljoin(directory.url, link)).path == BRANDS_PATH
        and "letter" in parse_qs(urlsplit(link).query)
    }
    if not letter_urls:
        raise ValueError("Brand directory has no alphabet navigation")
    for url in sorted(letter_urls):
        brands.update(listing_slugs(await load(url)))
    if not brands or not categories:
        raise ValueError("Empty discovery is not a complete catalogue")
    return sorted(brands), sorted(categories), popular


async def scrape_combo(
    client: httpx.AsyncClient,
    brand: str,
    category: str,
    guard: Guard,
    state: dict,
    args,
) -> None:
    checkpoint = state["db"].execute(
        "SELECT next_page, last_sku, done, max_page FROM progress WHERE brand=? AND category=?",
        (brand, category),
    ).fetchone()
    if checkpoint and checkpoint[2]:
        return
    page, previous_first_sku = checkpoint[:2] if checkpoint else (1, None)
    expected_last_page = checkpoint[3] if checkpoint else 1
    while True:
        url = BASE + LISTING.format(brand=brand, category=category, page=page)
        response = await get(client, url, guard, state)
        if response is None:
            return
        destination = urlsplit(urljoin(url, response.headers.get("location", "")))
        is_parent_redirect = (
            page == 1 and response.status_code in (301, 302)
            and destination.netloc == "fixpart.ch"
            and destination.path == f"/de/ersatzteile/{brand}"
        )
        if response.status_code == 404 or is_parent_redirect:
            if page != 1:
                raise ValueError("Listing disappeared before advertised final page")
            rows, max_page = [], 1
        else:
            response.raise_for_status()
            try:
                rows, max_page = extract_items(response.text)
            except ValueError:
                # The final page of a long listing renders cards without JSON-LD.
                if page > 1 and Page(response.text).is_listing:
                    rows, max_page = [], expected_last_page
                else:
                    raise
        if not rows:
            with state["db"]:
                state["db"].execute(
                    "INSERT OR REPLACE INTO progress "
                    "(brand, category, next_page, last_sku, done, max_page) VALUES (?, ?, ?, ?, 1, ?)",
                    (brand, category, page, previous_first_sku, expected_last_page),
                )
            return
        if page < expected_last_page and max_page < expected_last_page:
            raise ValueError("Pagination disappeared or contradicted the saved final page")
        expected_last_page = max(expected_last_page, max_page)
        if rows[0]["sku"] == previous_first_sku:
            raise ValueError("Empty or repeated listing before verified completion")
        previous_first_sku = rows[0]["sku"]

        async with state["lock"]:
            done = page >= expected_last_page
            with state["db"]:
                inserted = save_rows(state["db"], rows)
                state["db"].execute(
                    "INSERT OR REPLACE INTO progress "
                    "(brand, category, next_page, last_sku, done, max_page) VALUES (?, ?, ?, ?, ?, ?)",
                    (brand, category, page + 1, previous_first_sku, int(done), expected_last_page),
                )
            state["rows"] += inserted
            state["pages"] += 1
            if state["rows"] // args.report > state["last_report"]:
                state["last_report"] = state["rows"] // args.report
                elapsed = time.time() - state["start"]
                print(
                    f"{state['rows']:,} rows | {state['pages']:,} pages | "
                    f"{state['rows'] / elapsed:,.0f} rows/s | {brand}/{category} p{page}/{max_page}",
                    flush=True,
                )
        page += 1
        if done or (args.max_pages and page > args.max_pages):
            return


async def run(args) -> None:
    db = open_db(args.db)
    if args.export:
        export(db, args.export)
        db.close()
        return

    lock = os.path.join(os.path.dirname(os.path.abspath(args.db)), args.lock)
    if not acquire_lock(lock):
        print("another collector already holds the lock; exiting", flush=True)
        db.close()
        return

    done = {row for row in db.execute("SELECT brand, category FROM progress WHERE done = 1")}
    cookie = args.cookie or os.environ.get("FIXPART_COOKIE", "")
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(
        headers=HEADERS,
        cookies=parse_cookie(cookie),
        timeout=httpx.Timeout(args.timeout),
        limits=limits,
        http2=False,
        follow_redirects=False,
        trust_env=True,
    ) as client:
        guard = Guard(args.rate)
        brands, categories, popular = await discover(client, guard=guard)
        if args.categories:
            wanted = {c.strip() for c in args.categories.split(",")}
            categories = [c for c in categories if c in wanted]
        if args.brands:
            wanted = {b.strip() for b in args.brands.split(",")}
            brands = [b for b in brands if b in wanted]
        sizes = None
        if args.order_by_size:
            sizes = await measure_brands(client, guard, db, brands)
            top = sorted(sizes.items(), key=lambda item: -item[1])[:5]
            print(f"brand sizes: " + ", ".join(f"{b}={n}" for b, n in top), flush=True)
        pending = order_pending(brands, categories, popular, done, sizes=sizes)
        print(
            f"{len(pending):,} listings pending "
            f"({len(brands):,} brands x {len(categories):,} categories), "
            f"concurrency {args.concurrency}, rate {args.rate}/s",
            flush=True,
        )
        if not pending:
            db.close()
            return

        state = {
            "db": db,
            "rows": 0,
            "pages": 0,
            "lock": asyncio.Lock(),
            "start": time.time(),
            "last_report": 0,
        }
        await crawl_pending(client, pending, guard=guard, state=state, args=args)
        elapsed = time.time() - state["start"]
        total = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        verified = set(db.execute("SELECT brand, category FROM progress WHERE done = 1"))
        remaining = sum(pair not in verified for pair in pending)
        print(
            f"batch done: +{state['rows']:,} new rows, {state['pages']:,} pages "
            f"in {elapsed / 60:.1f} min, {total:,} products in {args.db}; "
            f"{remaining:,} listings still pending",
            flush=True,
        )
    db.close()


async def crawl_pending(client, pending, *, guard, state, args):
    listings = iter(pending)
    gates = 0
    async def worker():
        nonlocal gates
        for brand, category in listings:
            try:
                await scrape_combo(client, brand, category, guard, state, args)
                gates = 0
            except RuntimeError:
                gates += 1
                if gates >= MAX_CONSECUTIVE_GATES:
                    raise
                print(f"{brand}/{category} gated, left pending", flush=True)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                print(f"{brand}/{category} remains pending: {exc}", flush=True)

    async with asyncio.TaskGroup() as group:
        for _ in range(args.concurrency):
            group.create_task(worker())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="fixpart.db")
    parser.add_argument("--lock", default="fixpart.lock")
    parser.add_argument("--export", default="", help="dump the DB to .csv or .jsonl and exit")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--rate", type=float, default=1.6, help="requests per second across all tasks")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--report", type=int, default=10_000)
    parser.add_argument("--cookie", default="", help="Cookie header; defaults to $FIXPART_COOKIE")
    parser.add_argument("--order-by-size", action="store_true", help="crawl the biggest catalogues first")
    parser.add_argument("--brands", default="", help="comma separated, for a smoke test")
    parser.add_argument("--categories", default="", help="comma separated, for a smoke test")
    parser.add_argument("--max-pages", type=int, default=0, help="0 = all pages")
    args = parser.parse_args()
    if args.concurrency <= 0 or args.rate <= 0 or args.timeout <= 0 or args.report <= 0:
        parser.error("concurrency, rate, timeout, and report must be positive")
    if args.max_pages < 0:
        parser.error("max-pages must be nonnegative")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
