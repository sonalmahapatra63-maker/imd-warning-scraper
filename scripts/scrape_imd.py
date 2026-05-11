"""
IMD District-Wise Nowcast Warning Scraper
==========================================
Uses Playwright (headless Chromium) to fully render the amCharts-based
IMD warning page, then extracts district-level warning data.

Run locally:
    pip install -r requirements.txt
    playwright install chromium
    python scripts/scrape_imd.py

Or set env vars:
    IMD_STATE_ID=10 python scripts/scrape_imd.py
"""

import os
import sys
import json
import re
import csv
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from bs4 import BeautifulSoup


# ── Configuration ─────────────────────────────────────────────────────────────

STATE_ID   = os.getenv("IMD_STATE_ID", "10")   # 10 = Odisha (Bhubaneswar Met Centre)
BASE_URL   = f"https://mausam.imd.gov.in/imd_latest/contents/districtwisewarnings_mc.php?id={STATE_ID}"
DATA_DIR   = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Warning colour → severity mapping (IMD standard)
WARNING_COLOR_MAP = {
    "#008000": "No warning",   # green
    "#ffff00": "Watch",        # yellow
    "#ffa500": "Alert",        # orange
    "#ff0000": "Warning",      # red
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_color(color: str) -> str:
    """Lowercase and normalise a CSS colour string."""
    return color.strip().lower()


def color_to_severity(color: str) -> str:
    color = normalize_color(color)
    for k, v in WARNING_COLOR_MAP.items():
        if color == k.lower() or color == k:
            return v
    return f"Unknown ({color})"


def extract_warnings_from_html(html: str) -> list[dict]:
    """
    Parse the rendered HTML to extract district warnings.

    The IMD page embeds an amCharts SVG map.  Each district <path> or <area>
    element carries an aria-label with the district name and a fill colour
    that encodes the warning level.  We also fall back to scraping the
    legend table if a tabular form is present.
    """
    soup = BeautifulSoup(html, "html.parser")
    records = []

    # ── Strategy 1: SVG path elements with aria-label + fill ─────────────
    svg_paths = soup.select("svg path[aria-label]")
    for path in svg_paths:
        name  = path.get("aria-label", "").strip()
        fill  = path.get("fill", "")
        if not name or not fill:
            continue
        records.append({
            "district": name,
            "warning_color": fill,
            "severity": color_to_severity(fill),
        })

    # ── Strategy 2: amCharts data embedded in <script> as JSON ───────────
    if not records:
        scripts = soup.find_all("script")
        for script in scripts:
            text = script.string or ""
            # Look for dataProvider array
            m = re.search(r"dataProvider\s*[:=]\s*(\[.*?\])", text, re.DOTALL)
            if m:
                try:
                    items = json.loads(m.group(1))
                    for item in items:
                        name  = item.get("title") or item.get("name") or item.get("id", "")
                        color = item.get("color") or item.get("fill") or ""
                        if name:
                            records.append({
                                "district": name,
                                "warning_color": color,
                                "severity": color_to_severity(color),
                            })
                except json.JSONDecodeError:
                    pass

    # ── Strategy 3: HTML table rows (some Met Centre pages render a table) ─
    if not records:
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    district_cell = cells[0]
                    warning_cell  = cells[1]
                    district = district_cell.get_text(strip=True)
                    # Try to get colour from background-color style
                    style = warning_cell.get("style", "")
                    color_match = re.search(r"background(?:-color)?\s*:\s*([#\w]+)", style)
                    color = color_match.group(1) if color_match else warning_cell.get_text(strip=True)
                    if district and district.lower() not in ("district", "sl.no", "#"):
                        records.append({
                            "district": district,
                            "warning_color": color,
                            "severity": color_to_severity(color),
                        })

    return records


def scrape(state_id: str = STATE_ID) -> dict:
    """Launch Playwright, load the page, extract data."""
    url = f"https://mausam.imd.gov.in/imd_latest/contents/districtwisewarnings_mc.php?id={state_id}"
    print(f"[scraper] Target URL: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
        )
        page = context.new_page()

        print("[scraper] Loading page …")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: page load timed out — continuing with partial content")

        # Wait for the amCharts SVG map to render
        try:
            page.wait_for_selector("svg path", timeout=30_000)
            print("[scraper] SVG map detected — waiting an extra 3 s for full render …")
            page.wait_for_timeout(3_000)
        except PlaywrightTimeout:
            print("[scraper] SVG did not appear in time — page may be using a different structure")

        html = page.content()
        browser.close()

    return {"url": url, "html": html}


def save_snapshot(html: str, timestamp: str) -> Path:
    path = DATA_DIR / f"snapshot_{timestamp}.html"
    path.write_text(html, encoding="utf-8")
    print(f"[scraper] HTML snapshot saved → {path}")
    return path


def save_json(records: list[dict], meta: dict) -> Path:
    output = {
        "meta": meta,
        "count": len(records),
        "districts": records,
    }
    path = DATA_DIR / "warnings_latest.json"
    path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[scraper] JSON saved → {path}  ({len(records)} districts)")
    return path


def save_csv(records: list[dict], meta: dict) -> Path:
    path = DATA_DIR / "warnings_latest.csv"
    fieldnames = ["scraped_at", "state_id", "district", "warning_color", "severity"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            writer.writerow({
                "scraped_at": meta["scraped_at"],
                "state_id":   meta["state_id"],
                **r,
            })
    print(f"[scraper] CSV saved  → {path}")
    return path


def append_history(records: list[dict], meta: dict) -> Path:
    """Append this run's records to a cumulative JSONL history file."""
    path = DATA_DIR / "warnings_history.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"meta": meta, "districts": records}, ensure_ascii=False) + "\n")
    print(f"[scraper] History appended → {path}")
    return path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ts      = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ts_nice = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    result  = scrape(STATE_ID)
    html    = result["html"]

    # Always save the raw HTML snapshot (uploaded as GitHub artifact)
    save_snapshot(html, ts)

    records = extract_warnings_from_html(html)
    print(f"[scraper] Extracted {len(records)} district records")

    if not records:
        print(
            "[scraper] ⚠  No district data found.\n"
            "           The page might be using JS-injected SVG that requires\n"
            "           a longer wait, or the structure has changed.\n"
            "           Check the snapshot HTML artifact for debugging."
        )
        sys.exit(1)

    meta = {
        "scraped_at": ts_nice,
        "url":        result["url"],
        "state_id":   STATE_ID,
    }

    save_json(records, meta)
    save_csv(records, meta)
    append_history(records, meta)

    print("\n[scraper] ✅  Done.")
    print(f"           Severity breakdown:")
    from collections import Counter
    for severity, count in Counter(r["severity"] for r in records).most_common():
        print(f"             {count:3d}  {severity}")


if __name__ == "__main__":
    main()
