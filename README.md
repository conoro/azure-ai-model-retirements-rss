# Azure OpenAI "Current models" Retirement Scraper

## What it does

- Scrapes Microsoft's Learn page for Azure OpenAI model retirements:
  https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-retirements
- Extracts ONLY the **Current models** tables (ignores Fine-tuned and Default) across:
  - Text (Text generation)
  - Audio
  - Image and video
  - Embedding
- Produces a combined CSV with a **Type** column.
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
# Or focus only on Text models:
python scrape_ms_retirements.py --only text
```

## Outputs

- **CSV:**    `/mnt/data/ms_model_retirements/output/current_models.csv`
- **RSS:**    `/mnt/data/ms_model_retirements/output/rss.xml`
- **State:**  `/mnt/data/ms_model_retirements/data/snapshot.json`

## Notes

- First run creates a baseline snapshot and a single RSS item noting the baseline.
- Subsequent runs include items for NEW rows and for any field updates among:
  Lifecycle status, Retirement date, Replacement model.
- The feed uses the page's tab query param in item links (e.g., `?tabs=text`) based on the row Type.

## GitHub Actions

Add this file to your repo: `.github/workflows/retirements.yml` (included here). It runs the scraper twice per day
(06:00 and 18:00 UTC), then commits any changes to:

- `output/current_models.csv`
- `output/rss.xml`
- `data/snapshot.json`

Make sure your repository settings allow workflows to create commits:

- No extra secrets are needed; it uses the default `GITHUB_TOKEN` with `contents: write` permission.
