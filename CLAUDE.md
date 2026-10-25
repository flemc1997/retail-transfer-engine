# Retail transfer engine – project brief

A local Streamlit app that suggests inter-store stock transfers for a four-store fashion retailer, lets the user approve them, and produces a picking list. It is a **portfolio project, purely for show**. A separate live system runs the real business; nothing here feeds it.

The owner of this repo (Cillian) is a data scientist / data engineer who will walk interviewers through this code. Claude writes the code; Cillian must be able to explain every part of it.

## Hard rules

- **No real data in the repo, ever.** Real data lives outside the repo in the folder named by the `RETAIL_DATA_DIR` environment variable (set in a gitignored `.env`). The repo ships a synthetic data generator instead.
- `.gitignore` must cover `.env`, `*.duckdb`, `*.csv.gz`, `*.xlsx`, `data/local/`, `backups/`.
- Never copy real store names, style names or business notes into the repo. The legacy engine in `$RETAIL_DATA_DIR/legacy-engine/` is **reference only**: port the logic, not its names, notes or config values that identify the business.
- Keep code plain and readable: small functions, type hints, docstrings saying what each rule does in business words. Prefer clarity over cleverness.

## How we work (agreed)

Build one module at a time, in the order below. After each module:
1. Explain what was built in a few short paragraphs (what, why, where the key logic is).
2. Ask Cillian 2–3 questions to check his understanding. Wait for answers before starting the next module.
3. Suggest one small change for Cillian to make himself (he writes it; Claude reviews).

Ask one question at a time when something needs deciding. Cillian is on a Mac, using VS Code, Python 3.12+.

## Scope

In: upload weekly export → checks → append to history; transfer suggestions (moves + tidy-ups); approve / partial / reject; picking list download (Excel); one trading page (sales vs last year and last week by store and category, slow movers, follow-up check on whether last week's approved transfers happened).

Out (live-system features, not needed here): Sunday freeze, mid-week pushes, stores page, norm review, Black Friday, sales chart, region-split tidy-ups, size top-ups.

Colour variants: in scope only as the legacy split rule (agreed in module 3). For the style-colours on a split list, the colour becomes `colour · variant` (from the export's `Style Variant Name`), so uploads match the seeded history. The split list names real products, so it lives only in the local config outside the repo.

## Data

One DuckDB file: `$RETAIL_DATA_DIR/retail.duckdb`.

- `history` – one row per year, week, store, style, colour, size: `yr, wk, store, dept, brand, cat, num, sty, col, size, q (units), v (sales value incl. VAT), md (markdown %), st (closing stock), gp (gross profit), rem (remaining qty: invoiced / in transit, not yet booked in)`. The master keeps every export column, because the export is deleted after upload and this is the only copy.
- `norms` – `store, sty, col, size, norm` (latest snapshot; a norm > 0 means the line is auto-replenished).
- `batches` – the transfer suggestions generated for each week, with a stable run id.
- `decisions` – approved / partial (with sizes) / rejected, reason or note, per run id.

Seeding: `$RETAIL_DATA_DIR/seed/history.csv.gz` and `seed/norms.csv` load the initial tables (one-off script).

### Weekly upload (safety net, agreed)

1. Parse the Power BI export (format: see `load_export` in the legacy engine). Blank stock (`Stock EOH QTY`) is stored as 0, matching recent history (agreed in module 3). Real store names, the excluded store and the variant split list come from the local config at `$RETAIL_DATA_DIR/config.json` (set `RETAIL_CONFIG` to it); `config/config.example.json` holds made-up values in the same shape. Category = Power BI's `Style Master Category Name` for every style; each upload also gives that style the same category in its earlier weeks (history was relabelled once with `scripts/relabel_categories.py`).
2. Check: exactly one week, all four stores, expected columns present; total sales ≥ ~60% of the previous week and of the same week last year. Show a preview (week, stores, sales, vs LY) with a Confirm button.
3. Copy `retail.duckdb` to `$RETAIL_DATA_DIR/backups/` (keep the last 4).
4. In one transaction: delete rows for that year/week/store, insert the new rows, refresh norms.
5. Only after success, delete the uploaded export file.

## Transfer rules (port from legacy `analyse()`, transfer section)

Always whole style-colour runs (every size the sender holds). Skip any style-colour with a norm in any store.

**Moves.** Rate = units/week over the last 4 weeks; cover = stock ÷ rate.
- Receiver: rate ≥ 0.5/wk and cover < 4 weeks.
- Donor (most stock first): not a receiver, ≥ 3 units, cover ≥ 12 weeks (no sales = infinite cover).
- Match each donor to the fastest receiver whose cover after the move would be ≤ 12 weeks; update that receiver's stock before the next donor.

**Tidy-ups.** A style-colour with no norm, not in a move, held in ≥ 2 stores with no store above 5 units, first stocked ≥ 6 weeks ago, accessories only if the chain total is small, and slow chain-wide (≥ 12 weeks cover at the 8-week rate, or no sales). Receiver = most units sold in 8 weeks (ties: most units held, then most sizes); every other holder sends its whole run there.

All thresholds live in `config/config.example.json`.

Known issues from the legacy engine, both fixed: the week helper assumed 52 weeks a year (now ISO weeks, `weeks.py`, module 3); moves had no "new-in" rule for donors (now `donor_min_weeks_since_first_stocked`, module 4; measured chain-wide, a per-store version is a possible follow-up).

Agreed in module 4:
- Stock on the way (`rem`) counts towards a receiver's cover: receiver cover = (stock + on the way) ÷ rate. Donors only send booked-in stock.
- Accessories = a list of categories (`accessory_categories`), tidied only when the chain holds ≤ 12 units (legacy value).
- Ties are broken alphabetically by store code so results are repeatable (the legacy engine used config order or an unstable sort; on week 42 of 2026 that changed 3 of 25 moves, all exact ties).

Known limitation (found in module 7): the receiver rate counts sales, and sales stop at zero stock, so a store that sold out more than `rate_weeks` ago has rate 0 and can't receive, even if it's the store that most needs the line (censored demand). A possible follow-up: measure the rate only over weeks the store had stock.

## Build order

1. Project setup: `pyproject.toml` or `requirements.txt` (streamlit, pandas, duckdb, openpyxl, pytest), `.gitignore`, `.env.example`, settings loader.
2. Storage: DuckDB schema, seed script, backup helper.
3. Upload: export parser, checks, preview, transactional append, delete.
4. Transfer rules as pure functions (input: a stock-and-rate table + config; output: suggestion rows) with pytest tests on small hand-made tables.
5. Approval screen + picking list download.
6. Trading page + follow-up check.
7. Synthetic data generator (four stores, seasonality, size curves, normed and slow lines, so every rule fires).
8. README first draft – Cillian rewrites it in his own words.
