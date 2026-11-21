# fixpartch-scraper

Collects product titles and prices from FixPart.ch brand/category listings into
SQLite. Coverage and the total accessible catalogue size are not yet established.

Listing pages embed a schema.org `ItemList` in JSON-LD, typically with 25 products
(title, price, currency, availability, brand, MPN, URL). Extraction requires a SKU,
title, currency and valid price; unexpected pages remain pending.

## Run

```bash
export FIXPART_COOKIE='<cookie header from a browser session>'
uv run fixpart.py --db fixpart.db
```

To get volume early, measure every brand page once (~16 min) and crawl the biggest
catalogues first instead of alphabetically:

```bash
uv run fixpart.py --db fixpart.db --order-by-size
```

Sizes are cached in `brand_sizes`, so later runs reuse them.

Defaults are 1.6 request starts/second and two workers, including discovery.
These are operating settings, not a verified site allowance or a guarantee against
throttling. A 429 pauses future requests using Retry-After when supplied, otherwise
the existing 30-second fallback. Already in-flight requests cannot be recalled.
Captcha responses stop the run with progress preserved. No captcha automation,
proxy rotation or access-control bypass is included. Keep credentials out of git.

## Resume

Products and the next-page cursor are committed in one transaction per page.
Re-running skips verified complete listings and resumes partial ones at their
saved page. Server errors, extraction failures and page-capped tests do not
mark a listing complete. Existing product prices are refreshed on rediscovery.

**Upgrading older databases:** products are retained, but the old `combos` table's
completion flags are ignored because earlier versions could mark failed or
cancelled work complete. The new `progress` table starts unverified; old pages
must be revisited. Back up your database before upgrading.

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
CREATE TABLE progress (
  brand TEXT, category TEXT, next_page INTEGER NOT NULL, last_sku TEXT,
  done INTEGER NOT NULL, max_page INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (brand, category)
);
```

## Scope

Discovery follows all advertised alphabet/number pages, not just the default
A-brand directory and featured brands. A live audit found 1,512 brand slugs across
27 directory pages and 375 category slugs, giving 567,000 candidate combinations.
Many combinations do not exist; others have multiple pages or overlapping products.
These counts do not establish a unique-product total or prove exhaustive coverage.

## Tests

```bash
uv run --with httpx --with orjson python -m unittest -v
```

Tests use in-memory SQLite and HTTPX MockTransport; they do not contact FixPart.
