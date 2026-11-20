# fixpartch-scraper

Scrapes every product title and price from fixpart.ch into SQLite, resumably.

Each category listing embeds a schema.org `ItemList` in JSON-LD, so one GET returns
25 complete products (title, price, currency, availability, brand, MPN, URL). No
browser, no product-page crawl, no HTML selectors.

## Run

```bash
export FIXPART_COOKIE='<cookie header from a browser session>'
uv run fixpart.py --db fixpart.db --rate 1.5 --concurrency 2
```

The site rate-limits at roughly 100 requests/minute per IP and answers `429` above
it. 1.5 req/s (90/min) is the highest rate measured clean; a `429` parks the run for
30 s. A `302` to `/captcha` means the browser session expired — solve it once by hand
and copy the cookie again.

## Resume

Progress is durable: `products` is keyed by SKU, `combos` records finished
brand/category listings. Re-running the same command skips what is done.

```bash
uv run fixpart.py --db fixpart.db                 # continue
uv run fixpart.py --db fixpart.db --export products.csv
```

## Schema

```sql
CREATE TABLE products (
  sku TEXT PRIMARY KEY, title TEXT, price TEXT, currency TEXT,
  availability TEXT, brand TEXT, mpn TEXT, url TEXT
);
CREATE TABLE combos (
  brand TEXT, category TEXT, done INTEGER DEFAULT 0,
  PRIMARY KEY (brand, category)
);
```

## Scope

53,250 listings = 142 brands × 375 categories, paginated at 25 products/page.
The crawl is deliberately serialised at ~90 pages/min to stay under the limiter.
