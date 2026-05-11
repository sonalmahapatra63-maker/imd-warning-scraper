# IMD District-Wise Warning Scraper

Automatically scrapes **district-level nowcast warnings** from the India Meteorological Department (IMD) website and commits the data to this repository — no local PC required.

## How it works

```
GitHub Actions (cron: every 3 hours)
        │
        ▼
  Playwright (headless Chromium)
        │  renders the amCharts JS map fully
        ▼
  BeautifulSoup parser
        │  extracts district → warning colour → severity
        ▼
  Saves to data/
        │
        ▼
  git commit & push  ←  data lives right here in the repo
```

## Output files (auto-updated in `data/`)

| File | Description |
|------|-------------|
| `data/warnings_latest.json` | Latest scrape — full JSON with metadata |
| `data/warnings_latest.csv`  | Latest scrape as CSV (easy to open in Excel) |
| `data/warnings_history.jsonl` | Every scrape appended as one JSON object per line |

### `warnings_latest.json` structure
```json
{
  "meta": {
    "scraped_at": "2026-05-11 06:00 UTC",
    "url": "https://mausam.imd.gov.in/imd_latest/contents/districtwisewarnings_mc.php?id=10",
    "state_id": "10"
  },
  "count": 30,
  "districts": [
    { "district": "Khurda",  "warning_color": "#ff0000", "severity": "Warning" },
    { "district": "Puri",    "warning_color": "#ffa500", "severity": "Alert"   },
    { "district": "Cuttack", "warning_color": "#008000", "severity": "No warning" }
  ]
}
```

## Warning severity levels (IMD colour code)

| Colour | Code | Level |
|--------|------|-------|
| 🟢 Green  | `#008000` | No warning |
| 🟡 Yellow | `#ffff00` | Watch |
| 🟠 Orange | `#ffa500` | Alert |
| 🔴 Red    | `#ff0000` | Warning |

## Scraping a different state

Change `IMD_STATE_ID` in `.github/workflows/scrape_imd.yml`:

```yaml
env:
  IMD_STATE_ID: '10'   # Change this number for another Met Centre
```

Known state IDs (from the IMD URL parameter):

| ID | Met Centre |
|----|------------|
| 10 | Bhubaneswar (Odisha) |
| 1  | Delhi |
| 2  | Mumbai |
| 3  | Chennai |
| 4  | Kolkata |
| 5  | Bangalore |
| 6  | Hyderabad |
| 7  | Ahmedabad |
| 8  | Chandigarh |
| 9  | Lucknow |

## Run manually

You can trigger a scrape any time from **GitHub → Actions → IMD District-Wise Warning Scraper → Run workflow**.

## Run locally (for testing)

```bash
pip install -r requirements.txt
playwright install chromium
python scripts/scrape_imd.py
```

## Debugging

If the scraper extracts 0 districts, the raw HTML snapshot is uploaded as a **GitHub Actions artifact** (kept for 3 days). Download it and inspect the page structure to update the parser in `scripts/scrape_imd.py`.

## License

MIT
