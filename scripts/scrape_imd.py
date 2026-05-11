import os
import re
import csv
import json
import sys

from pathlib import Path
from collections import Counter
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

STATE_ID = os.getenv("IMD_STATE_ID", "10")

DISTRICT_URL = (
    "https://mausam.imd.gov.in/imd_latest/"
    f"contents/districtwisewarnings_mc.php?id={STATE_ID}"
)

STATION_URL = (
    "https://mausam.imd.gov.in/imd_latest/"
    f"contents/stationwise-nowcast-warning_mc.php?id={STATE_ID}"
)

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

WARNING_COLOR_MAP = {
    "#008000": "No Warning",
    "#ffff00": "Watch",
    "#ffa500": "Alert",
    "#ff0000": "Warning",
}


# ─────────────────────────────────────────────────────────────
# CLEAN OLD FILES
# ─────────────────────────────────────────────────────────────

def clean_old_files():

    print("\n[scraper] Cleaning old files...")

    patterns = [
        "*.html",
        "*.png",
        "*.csv",
        "*.json",
    ]

    keep_files = {
        ".gitkeep",
        "warnings_history.jsonl",
    }

    deleted = 0

    for pattern in patterns:

        for file in DATA_DIR.glob(pattern):

            if file.name in keep_files:
                continue

            try:
                file.unlink()
                deleted += 1

            except Exception as e:
                print(f"[scraper] Failed deleting {file.name}: {e}")

    print(f"[scraper] Deleted {deleted} old files")


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def normalize_color(color: str) -> str:

    if not color:
        return ""

    color = color.strip().lower()

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

    for k, v in WARNING_COLOR_MAP.items():

        if color == k.lower():
            return v

    return f"Unknown ({color})"


# ─────────────────────────────────────────────────────────────
# PAGE LOADER
# ─────────────────────────────────────────────────────────────

def load_page(url: str, screenshot_name: str):

    print(f"\n[scraper] Loading: {url}")

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
            viewport={
                "width": 1400,
                "height": 1200,
            },
            locale="en-IN",
        )

        page = context.new_page()

        try:

            page.goto(
                url,
                wait_until="networkidle",
                timeout=90000
            )

        except PlaywrightTimeout:

            print("[scraper] WARNING: timeout occurred")

        try:

            page.wait_for_selector(
                "svg",
                timeout=40000
            )

            page.wait_for_timeout(8000)

        except PlaywrightTimeout:

            print("[scraper] WARNING: SVG not detected")

        screenshot_path = DATA_DIR / screenshot_name

        page.screenshot(
            path=str(screenshot_path),
            full_page=True
        )

        html = page.content()

        browser.close()

    return html


# ─────────────────────────────────────────────────────────────
# EXTRACTION
# ─────────────────────────────────────────────────────────────

def extract_map_records(html: str, record_type: str):

    soup = BeautifulSoup(html, "html.parser")

    records = []

    # =========================================================
    # SVG PATHS
    # =========================================================

    svg_paths = soup.select("svg path")

    for path in svg_paths:

        label = (
            path.get("aria-label")
            or path.get("title")
            or ""
        ).strip()

        fill = (
            path.get("fill")
            or path.get("stroke")
            or ""
        ).strip()

        if label and fill:

            records.append({
                "type": record_type,
                "name": label,
                "warning_color": fill,
                "severity": color_to_severity(fill),
            })

    # =========================================================
    # SVG CIRCLES / STATIONS
    # =========================================================

    svg_circles = soup.select("svg circle")

    for circle in svg_circles:

        label = (
            circle.get("aria-label")
            or circle.get("title")
            or ""
        ).strip()

        fill = (
            circle.get("fill")
            or circle.get("stroke")
            or ""
        ).strip()

        if label and fill:

            records.append({
                "type": record_type,
                "name": label,
                "warning_color": fill,
                "severity": color_to_severity(fill),
            })

    # =========================================================
    # EMBEDDED JSON
    # =========================================================

    scripts = soup.find_all("script")

    for script in scripts:

        text = script.string or script.get_text()

        if not text:
            continue

        patterns = [
            r"dataProvider\s*[:=]\s*(\[[\s\S]*?\])",
            r"chart\.data\s*=\s*(\[[\s\S]*?\])",
            r"polygonSeries\.data\s*=\s*(\[[\s\S]*?\])",
            r"imageSeries\.data\s*=\s*(\[[\s\S]*?\])",
            r"pointSeries\.data\s*=\s*(\[[\s\S]*?\])",
        ]

        for pattern in patterns:

            matches = re.finditer(pattern, text)

            for match in matches:

                raw_json = match.group(1)

                try:

                    items = json.loads(raw_json)

                    for item in items:

                        name = (
                            item.get("title")
                            or item.get("name")
                            or item.get("district")
                            or item.get("station")
                            or item.get("id")
                            or ""
                        )

                        color = (
                            item.get("color")
                            or item.get("fill")
                            or item.get("warningColor")
                            or ""
                        )

                        if name:

                            records.append({
                                "type": record_type,
                                "name": name,
                                "warning_color": color,
                                "severity": color_to_severity(color),
                            })

                except Exception:
                    pass

    # =========================================================
    # REMOVE DUPLICATES
    # =========================================================

    unique = {}

    for item in records:
        unique[item["name"]] = item

    return list(unique.values())


# ─────────────────────────────────────────────────────────────
# SAVE FUNCTIONS
# ─────────────────────────────────────────────────────────────

def save_snapshot(name: str, html: str, timestamp: str):

    path = DATA_DIR / f"{name}_{timestamp}.html"

    path.write_text(
        html,
        encoding="utf-8"
    )


def save_json(filename: str, records: list, meta: dict):

    path = DATA_DIR / filename

    output = {
        "meta": meta,
        "count": len(records),
        "records": records,
    }

    path.write_text(
        json.dumps(
            output,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


def save_csv(filename: str, records: list, meta: dict):

    path = DATA_DIR / filename

    fieldnames = [
        "scraped_at",
        "state_id",
        "type",
        "name",
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

        for r in records:

            writer.writerow({
                "scraped_at": meta["scraped_at"],
                "state_id": meta["state_id"],
                **r,
            })


def append_history(records: list, meta: dict):

    path = DATA_DIR / "warnings_history.jsonl"

    with open(
        path,
        "a",
        encoding="utf-8"
    ) as f:

        f.write(
            json.dumps({
                "meta": meta,
                "records": records,
            }, ensure_ascii=False)
            + "\n"
        )


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():

    clean_old_files()

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")

    timestamp_human = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    # DISTRICT PAGE

    district_html = load_page(
        DISTRICT_URL,
        f"district_warning_{STATE_ID}.png"
    )

    save_snapshot(
        "district_snapshot",
        district_html,
        timestamp
    )

    district_records = extract_map_records(
        district_html,
        "district"
    )

    # STATION PAGE

    station_html = load_page(
        STATION_URL,
        f"station_warning_{STATE_ID}.png"
    )

    save_snapshot(
        "station_snapshot",
        station_html,
        timestamp
    )

    station_records = extract_map_records(
        station_html,
        "station"
    )

    # COMBINED

    all_records = district_records + station_records

    print(f"\n[scraper] District records : {len(district_records)}")
    print(f"[scraper] Station records  : {len(station_records)}")
    print(f"[scraper] Total records    : {len(all_records)}")

    if not all_records:

        print("\n[scraper] ERROR: no data extracted")
        sys.exit(1)

    meta = {
        "scraped_at": timestamp_human,
        "state_id": STATE_ID,
        "district_url": DISTRICT_URL,
        "station_url": STATION_URL,
    }

    # DISTRICT FILES

    save_json(
        "district_warnings_latest.json",
        district_records,
        meta
    )

    save_csv(
        "district_warnings_latest.csv",
        district_records,
        meta
    )

    # STATION FILES

    save_json(
        "station_warnings_latest.json",
        station_records,
        meta
    )

    save_csv(
        "station_warnings_latest.csv",
        station_records,
        meta
    )

    # COMBINED FILES

    save_json(
        "warnings_latest.json",
        all_records,
        meta
    )

    save_csv(
        "warnings_latest.csv",
        all_records,
        meta
    )

    append_history(all_records, meta)

    print("\n[scraper] Severity Summary")

    counts = Counter(
        r["severity"]
        for r in all_records
    )

    for severity, count in counts.items():

        print(f"  {severity}: {count}")

    print("\n[scraper] SUCCESS")


if __name__ == "__main__":
    main()
