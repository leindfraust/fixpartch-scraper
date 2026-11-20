# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx[http2]>=0.28", "orjson>=3.10"]
# ///
"""Scrape every FixPart.ch product title + price into SQLite, resumably.

Each listing page embeds a schema.org ItemList (25 products) with name, price,
currency, availability, brand and URL, so one GET yields 25 complete rows.
Pagination is server-side (?page=N, max from data-page).

Progress lives in fixpart.db: rows in `products` (sku is the key) and finished
listings in `combos`, so a restart continues where the last one stopped.
A 302 to /captcha means the hand-passed session died; a 302 anywhere else is
just an unknown brand/category slug and is skipped.
"""

import argparse
import asyncio
import csv
import os
import re
import sqlite3
import sys
import time

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
LD_JSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
LISTING_LINK = re.compile(r'href="https://fixpart\.ch/de/ersatzteile/([a-z0-9-]+)"')
MAX_PAGE = re.compile(r'data-page="(\d+)"')
FIELDS = ("sku", "title", "price", "currency", "availability", "brand", "mpn", "url")


def extract_items(html: str) -> tuple[list[dict], int]:
    """Return the products in this page's ItemList and the highest page number on it."""
    rows = []
    for blob in LD_JSON.findall(html):
        try:
            data = orjson.loads(blob)
        except orjson.JSONDecodeError:
            continue
        if data.get("@type") != "ItemList":
            continue
        for entry in data.get("itemListElement", []):
            item = entry.get("item")
            if not item:
                continue
            offer = item.get("offers") or {}
            rows.append(
                {
                    "sku": item.get("sku", ""),
                    "title": item.get("name", ""),
                    "price": offer.get("price", ""),
                    "currency": offer.get("priceCurrency", ""),
                    "availability": (offer.get("availability") or "").rsplit("/", 1)[-1],
                    "brand": (item.get("brand") or {}).get("name", ""),
                    "mpn": item.get("mpn", ""),
                    "url": item.get("url", ""),
                }
            )
        break
    pages = [int(n) for n in MAX_PAGE.findall(html)]
    return rows, (max(pages) if pages else 1)


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
        "CREATE TABLE IF NOT EXISTS combos ("
        "brand TEXT, category TEXT, done INTEGER DEFAULT 0, PRIMARY KEY (brand, category))"
    )
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
    return db.total_changes - before


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
            start = max(time.monotonic(), self.next_at, self.blocked_until)
            self.next_at = start + self.interval
        delay = start - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def block(self, seconds: float) -> None:
        self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)


async def get(client: httpx.AsyncClient, url: str, guard: Guard, state: dict) -> httpx.Response | None:
    for _ in range(6):
        await guard.wait()
        response = await client.get(url)
        if response.status_code == 429:
            print("429 from nginx: pausing 30s", flush=True)
            guard.block(30)
            continue
        if response.status_code == 302 and "captcha" in (response.headers.get("location") or ""):
            state["db"].commit()
            sys.exit("session challenged again (302 -> /captcha); solve the captcha and re-copy the cookie")
        return response
    return None


def parse_cookie(raw: str) -> dict[str, str]:
    jar = {}
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            jar[name] = value
    return jar


async def discover(client: httpx.AsyncClient) -> tuple[list[str], list[str]]:
    categories = sorted(set(LISTING_LINK.findall((await client.get(BASE + CATEGORIES_PATH)).text)))
    brand_page = (await client.get(BASE + BRANDS_PATH)).text
    brands = sorted(set(LISTING_LINK.findall(brand_page)) - set(categories))
    return brands, categories


async def scrape_combo(
    client: httpx.AsyncClient,
    brand: str,
    category: str,
    guard: Guard,
    state: dict,
    args,
) -> None:
    page = 1
    previous_first_sku = None
    max_page = 1
    try:
        while page <= max_page:
            url = BASE + LISTING.format(brand=brand, category=category, page=page)
            response = await get(client, url, guard, state)
            if response is None:
                return
            if response.status_code != 200:
                break
            rows, max_page = extract_items(response.text)
            if not rows or rows[0]["sku"] == previous_first_sku:
                break
            previous_first_sku = rows[0]["sku"]

            async with state["lock"]:
                state["rows"] += save_rows(state["db"], rows)
                state["db"].commit()
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
            if args.max_pages and page > args.max_pages:
                break
    finally:
        async with state["lock"]:
            state["db"].execute(
                "INSERT OR REPLACE INTO combos (brand, category, done) VALUES (?, ?, 1)",
                (brand, category),
            )
            state["db"].commit()


async def run(args) -> None:
    db = open_db(args.db)
    if args.export:
        export(db, args.export)
        return

    done = {row for row in db.execute("SELECT brand, category FROM combos WHERE done = 1")}
    cookie = args.cookie or os.environ.get("FIXPART_COOKIE", "")
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(
        headers=HEADERS,
        cookies=parse_cookie(cookie),
        timeout=httpx.Timeout(args.timeout),
        limits=limits,
        http2=True,
        follow_redirects=False,
        trust_env=True,
    ) as client:
        for _ in range(10):
            probe = await client.get(BASE + CATEGORIES_PATH)
            if probe.status_code == 200:
                break
            if probe.status_code == 302 and "captcha" in (probe.headers.get("location") or ""):
                sys.exit("session challenged (302 -> /captcha); solve the captcha and re-copy the cookie")
            print(f"startup: HTTP {probe.status_code}, retrying in 60s", flush=True)
            await asyncio.sleep(60)
        else:
            sys.exit(f"cannot reach {BASE} after 10 tries (last HTTP {probe.status_code})")
        brands, categories = await discover(client)
        if args.categories:
            wanted = {c.strip() for c in args.categories.split(",")}
            categories = [c for c in categories if c in wanted]
        if args.brands:
            wanted = {b.strip() for b in args.brands.split(",")}
            brands = [b for b in brands if b in wanted]
        pending = [(b, c) for b in brands for c in categories if (b, c) not in done]
        print(
            f"{len(pending):,} listings pending "
            f"({len(done):,} already done of {len(brands) * len(categories):,}), "
            f"concurrency {args.concurrency}, rate {args.rate}/s",
            flush=True,
        )
        if not pending:
            return

        state = {
            "db": db,
            "rows": 0,
            "pages": 0,
            "lock": asyncio.Lock(),
            "start": time.time(),
            "last_report": 0,
        }
        guard = Guard(args.rate)
        await asyncio.gather(
            *(scrape_combo(client, b, c, guard, state, args) for b, c in pending)
        )
        elapsed = time.time() - state["start"]
        total = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        print(
            f"batch done: +{state['rows']:,} new rows, {state['pages']:,} pages "
            f"in {elapsed / 60:.1f} min, {total:,} products in {args.db}",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="fixpart.db")
    parser.add_argument("--export", default="", help="dump the DB to .csv or .jsonl and exit")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--rate", type=float, default=8, help="requests per second across all tasks")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--report", type=int, default=10_000)
    parser.add_argument("--cookie", default="", help="Cookie header; defaults to $FIXPART_COOKIE")
    parser.add_argument("--brands", default="", help="comma separated, for a smoke test")
    parser.add_argument("--categories", default="", help="comma separated, for a smoke test")
    parser.add_argument("--max-pages", type=int, default=0, help="0 = all pages")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
