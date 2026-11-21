# Repository Guidelines

Guidance for agents and humans working on this scraper. Read this before changing code.

## What this is

Collects product titles and prices from FixPart.ch brand/category listings into SQLite.
It is not a browser automation project: every listing page ships a schema.org `ItemList`
in JSON-LD, so one HTTP GET yields up to 25 complete rows.

## Running it

```bash
export FIXPART_COOKIE='<cookie header from a browser session that passed the captcha>'
uv run fixpart.py --db fixpart.db --rate 1.6 --concurrency 2
uv run fixpart.py --db fixpart.db --export products.csv
```

- The cookie is the only credential. Get it from a real browser session's request headers;
  never commit it, never log it.
- Defaults are already 1.6 req/s and 2 workers. Do not raise them without new measurements.
- `--export` never takes the crawl lock, so it is safe while a crawl is running.
- `--order-by-size` measures every brand page once (cached in `brand_sizes`) and crawls the
  largest catalogues first. Use it when the goal is product volume sooner rather than full
  alphabetical coverage first.
- `--max-pages N` limits pages per listing; a page-capped listing stays pending and resumes
  at the saved page.
- `--brands a,b --categories x,y` narrows a run for smoke tests.

## Invariants — do not break these

1. **No captcha automation.** Never click, solve, or outsource the captcha. If a captcha
   appears, stop and let a human pass it in a browser.
2. **Rate ceiling.** ~100 requests/minute per IP is the measured limit; 1.6 req/s is the
   operating point. A 429 parks all requests for 30 s (or `Retry-After`).
3. **Never delete product rows.** Resume by skipping verified work, not by clearing tables.
4. **Completion is earned.** A listing is `done` only when every advertised page has been
   fetched, or the site explicitly shows an empty result (`Filter 0 Ergebnisse`), or a
   later page is an empty listing shell. Server errors, missing `ItemList`, repeated pages,
   and page caps leave a listing pending.
5. **Never filter by the brand page's category grid.** It is a curated subset: Beko shows
   12 categories while `beko/abdeckkappe` alone has 748 products.
6. **One crawler per database.** `fixpart.lock` holds the PID; a stale lock is reclaimed
   only when that PID is gone.

## Architecture

| Piece | Location | Notes |
|---|---|---|
| Discovery | `discover()` | `/de/marken` + 27 `?letter=` pages → brand slugs; `/de/produktkategorien` → 375 categories |
| Listing URL | `LISTING` | `/de/ersatzteile/{brand}/{category}?page=N`, 25 products/page |
| Pagination | `scrape_combo()` | last page from `data-page`; the advertised maximum is persisted in `progress.max_page` |
| Extraction | `extract_items()` | JSON-LD `ItemList`; falls back to an explicit empty page or an empty listing shell |
| Storage | `open_db()` | `products(sku PK)`, `progress(brand, category, next_page, last_sku, done, max_page)` |
| Pacing | `Guard` | global spacing + cooldown; every request, including discovery, goes through it |
| Lock | `acquire_lock()` | PID file, stale-safe |

Ordering: popular brands first (from the directory's featured grid), then the rest. Coverage
is unchanged by the order.

Write rows and the page checkpoint in one SQLite transaction per page, so a crash can never
leave products saved without their cursor (or the reverse).

## Tests

```bash
uv run --with httpx --with orjson python -m unittest
```

Tests use `httpx.MockTransport` and in-memory SQLite; they never contact FixPart. Add a
failing test before fixing a bug, and run the full suite before committing.

## Known limits

- The public catalogue is reached through 1,512 brands × 375 categories = 567,000 listings.
  At 96 req/min the full sweep takes ~4 days.
- Category-only pages do not paginate server-side: every `?page=N` returns the first 12
  products. Do not build a crawler on them.
- Brand pages advertise totals that include supplier stock which is not in any listing;
  treat those numbers as marketing, not coverage.
- Some listings claim results (`Filter 1 Ergebnis`) but render nothing. They stay pending by
  design rather than being counted as complete.

## Data files

`fixpart.db`, `products.csv` and `fixpart.lock` are runtime artifacts. The committed
`products.csv` is a small example; do not commit multi-megabyte exports.
