# Azure OpenAI Retirement Scraper

## What it does

- Scrapes Microsoft's Learn page for the Azure OpenAI model retirement schedule:
  https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule
- Extracts the **Azure OpenAI** section table (under "Foundry Models sold directly by Azure").
- Produces a CSV with a **Type** column (always `Azure OpenAI`).
- Persists a local JSON snapshot for change detection between runs.
- Writes an RSS feed with items for **new rows** or **field changes** (e.g., Retirement date changes).

THIS IS THE RSS FEED URL you want if you just want the info: `https://conoro.github.io/azure-ai-model-retirements-rss/rss.xml`

## Using in Slack
- make sure the built-in RSS app is installed in your workspace
- add the RSS feed URL to a channel using `/feed add https://conoro.github.io/azure-ai-model-retirements-rss/rss.xml`


# These steps only needed if running it yourself
## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python scrape_ms_retirements.py
```

## Outputs

- **CSV:**    `output/current_models.csv`
- **RSS:**    `output/rss.xml`
- **State:**  `data/snapshot.json`

## Notes

- First run creates a baseline snapshot and a single RSS item noting the baseline.
- Subsequent runs include items for NEW rows and for any field updates among:
  Lifecycle status, Retirement date, Replacement model.
- Item links point at the `#azure-openai` anchor on the schedule page.

## GitHub Actions

Add this file to your repo: `.github/workflows/retirements.yml` (included here). It runs the scraper twice per day
(06:00 and 18:00 UTC), then commits any changes to:

- `output/current_models.csv`
- `output/rss.xml`
- `data/snapshot.json`

Make sure your repository settings allow workflows to create commits:

- No extra secrets are needed; it uses the default `GITHUB_TOKEN` with `contents: write` permission.
