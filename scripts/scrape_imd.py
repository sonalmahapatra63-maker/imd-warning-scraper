"""
IMD District-Wise Nowcast Warning Scraper
==========================================

Cloud-ready Playwright scraper for IMD amCharts district warning maps.

Features:
- Works in GitHub Actions
- Handles JavaScript-rendered SVG maps
- Multiple fallback extraction methods
- Saves JSON + CSV + historical JSONL
- Uploads raw HTML snapshots for debugging

Run locally:
    pip install -r requirements.txt
    python -m playwright install chromium
    python scripts/scrape_imd.py

GitHub Actions:
    Uses environment variable:
        IMD_STATE_ID=10
"""

import os
import sys
import json
import re
import csv

from datetime import datetime, timezone
from pathlib import Path
from collections import Counter

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from bs4 import BeautifulSoup


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

STATE_ID = os.getenv("IMD_STATE_ID", "10")

BASE_URL = (
    f"https://mausam.imd.gov.in/imd_latest/"
    f"contents/districtwisewarnings_mc.php?id={STATE_ID}"
)

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


# IMD standard warning colors
WARNING_COLOR_MAP = {
    "#008000": "No Warning",
    "#ffff00": "Watch",
    "#ffa500": "Alert",
    "#ff0000": "Warning",
}


# ─────────────────────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────────────────────

def normalize_color(color: str) -> str:
    if not color:
        return ""

    color = color.strip().lower()

    # Convert rgb() → hex if needed
    rgb_match = re.match(
        r"rgb\((\d+),\s*(\d+),\s*(\d+)\)",
        color
    )

    if rgb_match:
        r, g, b = map(int, rgb_match.groups())
        return "#{:02x}{:02x}{:02x}".format(r, g, b)

    return color


def color_to_severity(color: str) -> str:
    color = normalize_color(color)

    for known_color, severity in WARNING_COLOR_MAP.items():
        if color == known_color.lower():
            return severity

    return f"Unknown ({color})"


# ─────────────────────────────────────────────────────────────
# HTML Extraction Logic
# ─────────────────────────────────────────────────────────────

def extract_warnings_from_html(html: str) -> list[dict]:

    soup = BeautifulSoup(html, "html.parser")

    records = []

    # =========================================================
    # Strategy 1
    # SVG PATHS
    # =========================================================

    svg_paths = soup.select("svg path")

    for path in svg_paths:

        district = (
            path.get("aria-label")
            or path.get("title")
            or ""
        ).strip()

        fill = (
            path.get("fill")
            or path.get("stroke")
            or ""
        ).strip()

        if district and fill:

            records.append({
                "district": district,
                "warning_color": fill,
                "severity": color_to_severity(fill),
            })

    # =========================================================
    # Strategy 2
    # Embedded amCharts JSON
    # =========================================================

    if not records:

        scripts = soup.find_all("script")

        for script in scripts:

            text = script.string or script.get_text()

            if not text:
                continue

            patterns = [
                r"dataProvider\s*[:=]\s*(\[[\s\S]*?\])",
                r"polygonSeries\.data\s*=\s*(\[[\s\S]*?\])",
                r"chart\.data\s*=\s*(\[[\s\S]*?\])",
            ]

            for pattern in patterns:

                match = re.search(pattern, text)

                if not match:
                    continue

                raw_json = match.group(1)

                try:
                    items = json.loads(raw_json)

                    for item in items:

                        district = (
                            item.get("title")
                            or item.get("name")
                            or item.get("district")
                            or item.get("id")
                            or ""
                        )

                        color = (
                            item.get("color")
                            or item.get("fill")
                            or item.get("warningColor")
                            or ""
                        )

                        if district:

                            records.append({
                                "district": district,
                                "warning_color": color,
                                "severity": color_to_severity(color),
                            })

                except Exception:
                    pass

    # =========================================================
    # Strategy 3
    # HTML TABLES
    # =========================================================

    if not records:

        tables = soup.find_all("table")

        for table in tables:

            rows = table.find_all("tr")

            for row in rows:

                cells = row.find_all(["td", "th"])

                if len(cells) < 2:
                    continue

                district = cells[0].get_text(strip=True)

                warning_cell = cells[1]

                style = warning_cell.get("style", "")

                color_match = re.search(
                    r"background(?:-color)?\s*:\s*([#\w]+)",
                    style,
                    re.IGNORECASE
                )

                color = (
                    color_match.group(1)
                    if color_match
                    else warning_cell.get_text(strip=True)
                )

                if district.lower() in [
                    "district",
                    "sl.no",
                    "#",
                ]:
                    continue

                records.append({
                    "district": district,
                    "warning_color": color,
                    "severity": color_to_severity(color),
                })

    # Remove duplicates
    unique = {}

    for item in records:
        unique[item["district"]] = item

    return list(unique.values())


# ─────────────────────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────────────────────

def scrape(state_id: str = STATE_ID) -> dict:

    url = (
        "https://mausam.imd.gov.in/imd_latest/"
        f"contents/districtwisewarnings_mc.php?id={state_id}"
    )

    print(f"[scraper] Target URL: {url}")

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ]
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={
                "width": 1400,
                "height": 1200,
            },
            locale="en-IN",
        )

        page = context.new_page()

        print("[scraper] Loading page...")

        try:

            page.goto(
                url,
                wait_until="networkidle",
                timeout=90_000
            )

        except PlaywrightTimeout:

            print(
                "[scraper] WARNING: "
                "Page load timeout. Continuing..."
            )

        # Wait for SVG render
        try:

            page.wait_for_selector(
                "svg path",
                timeout=40_000
            )

            print(
                "[scraper] SVG detected. "
                "Waiting for full render..."
            )

            page.wait_for_timeout(8000)

        except PlaywrightTimeout:

            print(
                "[scraper] WARNING: "
                "SVG not detected."
            )

        # Screenshot for debugging
        screenshot_path = (
            DATA_DIR / f"screenshot_{state_id}.png"
        )

        page.screenshot(
            path=str(screenshot_path),
            full_page=True
        )

        html = page.content()

        browser.close()

    return {
        "url": url,
        "html": html,
    }


# ─────────────────────────────────────────────────────────────
# Save Functions
# ─────────────────────────────────────────────────────────────

def save_snapshot(html: str, timestamp: str):

    path = DATA_DIR / f"snapshot_{timestamp}.html"

    path.write_text(
        html,
        encoding="utf-8"
    )

    print(f"[scraper] Snapshot saved → {path}")

    return path


def save_json(records: list[dict], meta: dict):

    path = DATA_DIR / "warnings_latest.json"

    output = {
        "meta": meta,
        "count": len(records),
        "districts": records,
    }

    path.write_text(
        json.dumps(
            output,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print(f"[scraper] JSON saved → {path}")

    return path


def save_csv(records: list[dict], meta: dict):

    path = DATA_DIR / "warnings_latest.csv"

    fieldnames = [
        "scraped_at",
        "state_id",
        "district",
        "warning_color",
        "severity",
    ]

    with open(
        path,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames
        )

        writer.writeheader()

        for record in records:

            writer.writerow({
                "scraped_at": meta["scraped_at"],
                "state_id": meta["state_id"],
                **record,
            })

    print(f"[scraper] CSV saved → {path}")

    return path


def append_history(records: list[dict], meta: dict):

    path = DATA_DIR / "warnings_history.jsonl"

    with open(
        path,
        "a",
        encoding="utf-8"
    ) as f:

        f.write(
            json.dumps({
                "meta": meta,
                "districts": records,
            }, ensure_ascii=False)
            + "\n"
        )

    print(f"[scraper] History appended → {path}")

    return path


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")

    timestamp_human = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    result = scrape(STATE_ID)

    html = result["html"]

    # Save raw snapshot
    save_snapshot(html, timestamp)

    # Extract data
    records = extract_warnings_from_html(html)

    print(
        f"[scraper] Extracted "
        f"{len(records)} district records"
    )

    if not records:

        print(
            "\n[scraper] ERROR: "
            "No district data found.\n"
            "Check snapshot HTML + screenshot.\n"
        )

        sys.exit(1)

    meta = {
        "scraped_at": timestamp_human,
        "url": result["url"],
        "state_id": STATE_ID,
    }

    save_json(records, meta)

    save_csv(records, meta)

    append_history(records, meta)

    print("\n[scraper] ✅ Completed Successfully\n")

    print("[scraper] Severity Summary:")

    severity_counts = Counter(
        r["severity"]
        for r in records
    )

    for severity, count in severity_counts.items():

        print(f"  {severity}: {count}")


if __name__ == "__main__":
    main()
