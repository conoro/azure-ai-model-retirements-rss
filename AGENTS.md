# AGENTS.md

## Two copies of `rss.xml` — edit the right one

- `output/rss.xml` is the **source**, written by the scraper.
- `docs/rss.xml` is the **published** copy served from GitHub Pages at `https://conoro.github.io/azure-ai-model-retirements-rss/rss.xml` — this URL is what subscribers (Slack `/feed add`, etc.) consume.

The workflow copies `output/rss.xml` → `docs/rss.xml` after each scrape. Don't hand-edit `docs/rss.xml`; it will be overwritten. To change the feed, change the scraper.

## CI commits state files back to `main`

`.github/workflows/retirements.yml` runs twice daily (06:00, 18:00 UTC) and pushes two commits to `main` with `[skip ci]`:
1. `docs/rss.xml`
2. `output/current_models.csv`, `output/rss.xml`, `data/snapshot.json`

Implications:
- Pull before any local work — bot commits land frequently.
- Running the scraper locally dirties `output/` and `data/snapshot.json`. Don't commit those by hand unless you intend to.
- `data/snapshot.json` is **state**, not config. Resetting it forces the next run to emit a baseline RSS item and treat every row as new.

## Source page is fragile — Microsoft restructures it without warning

In April 2026 Microsoft moved the per-model tables off `concepts/model-retirements` (which is now policy-only) onto a new `concepts/model-retirement-schedule` page, dropped the modality tabs (Text/Audio/Image/Embedding), reorganized by **provider** instead, and removed the "Deprecation date" column. The scraper now targets `<h3 id="azure-openai">` on the schedule page.

If the scraper fails with "Could not locate '#azure-openai' section": fetch the page, find where the Azure OpenAI table moved, and update `MS_URL_BASE` and the selector in `parse_azure_openai_schedule`.

## `requests` mojibakes the page if you don't force UTF-8

Microsoft Learn returns `Content-Type: text/html` with no charset. `requests` then defaults to ISO-8859-1 (per RFC 2616) and corrupts UTF-8 characters like the em-dash. `fetch_page()` sets `r.encoding = "utf-8"` explicitly — keep it.

## Snapshot key is `(Type, Model, Version)` — changing `Type` semantics breaks diffing

If you ever change what goes in the `Type` column (e.g., to scrape multiple providers, or rename "Azure OpenAI"), every existing snapshot entry becomes a phantom "removed" and every new entry becomes a phantom "new" → flood of bogus RSS items. Add a migration in `load_snapshot()` like the existing `LEGACY_TYPES` rewrite.

## `uv` in CI, `pip` in README — both work

CI uses `uv venv` + `uv pip install -r requirements.txt` + `uv run`. The README documents plain `pip`. Either is fine locally; match CI when reproducing CI behavior.
