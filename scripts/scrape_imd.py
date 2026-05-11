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

# Station markers use image filenames instead of fill colors.
# Map the filename colour word → canonical hex so severity lookup still works.
MARKER_FILENAME_TO_HEX = {
    "green":  "#008000",
    "yellow": "#ffff00",
    "orange": "#ffa500",
    "red":    "#ff0000",
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


def marker_filename_to_color(href: str) -> str:
    """
    Extract the warning colour hex from an amCharts marker image filename.

    The station nowcast page uses coloured pin images instead of SVG fill
    colours.  The filename pattern is:
        nowcast_marker/map-marker-icon-png-<colour>.png
    where <colour> is one of: green, yellow, orange, red.

    Returns the canonical hex string (e.g. '#008000') or '' if not matched.
    """
    match = re.search(r"map-marker-icon-png-(\w+)\.png", href, re.IGNORECASE)
    if not match:
        return ""
    colour_word = match.group(1).lower()
    return MARKER_FILENAME_TO_HEX.get(colour_word, "")


def parse_station_aria_label(aria_label: str):
    """
    Parse the aria-label attribute on a station marker <g> element.

    amCharts sets aria-label to the full tooltip HTML, e.g.:
        "Angul <p>No Warning </br></br> Time of issue: 2026-05-11</br>
         2200 Hrs</br> Valid upto: 0100 Hrs </p>"

    Returns a dict with:
        name        – station name (plain text before the first HTML tag)
        warning_text – e.g. "No Warning", "Watch", "Alert", "Warning"
        issued_at   – "2026-05-11 2200" or ""
        valid_upto  – "0100" or ""
    """

    # Station name is everything before the first HTML tag
    name_match = re.match(r"^([^<]+)", aria_label)
    name = name_match.group(1).strip() if name_match else aria_label.strip()

    # Warning level text (appears literally in the tooltip)
    warning_match = re.search(
        r"\b(No Warning|Watch|Alert|Warning)\b",
        aria_label
    )
    warning_text = warning_match.group(1) if warning_match else "Unknown"

    # "Time of issue: 2026-05-11</br>2200 Hrs"
    time_match = re.search(
        r"Time of issue:\s*([\d-]+)\s*(?:</br>|<br\s*/?>|\s)([\d]+)\s*Hrs",
        aria_label,
        re.IGNORECASE,
    )
    issued_at = (
        f"{time_match.group(1)} {time_match.group(2)}" if time_match else ""
    )

    # "Valid upto: 0100 Hrs"
    valid_match = re.search(
        r"Valid upto:\s*([\d]+)\s*Hrs",
        aria_label,
        re.IGNORECASE,
    )
    valid_upto = valid_match.group(1) if valid_match else ""

    return {
        "name": name,
        "warning_text": warning_text,
        "issued_at": issued_at,
        "valid_upto": valid_upto,
    }


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

def extract_district_records(html: str) -> list:
    """
    Extract district warning records from the district nowcast page.

    Districts are rendered as SVG <path> elements with class
    'amcharts-map-area'.  The warning colour is in the 'fill' attribute
    and the district name is in 'aria-label'.
    """

    soup = BeautifulSoup(html, "html.parser")
    records = []
    seen = set()

    # ── Primary: SVG paths with amcharts-map-area class ──────────────────
    for path in soup.select("svg path.amcharts-map-area"):

        label = (path.get("aria-label") or "").strip()
        fill  = (path.get("fill") or "").strip()

        if label and fill and label not in seen:
            seen.add(label)
            records.append({
                "type":          "district",
                "name":          label,
                "warning_color": normalize_color(fill),
                "severity":      color_to_severity(fill),
                "issued_at":     "",
                "valid_upto":    "",
            })

    # ── Fallback: any SVG path with aria-label + fill (original logic) ───
    if not records:
        for path in soup.select("svg path"):

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

            if label and fill and label not in seen:
                seen.add(label)
                records.append({
                    "type":          "district",
                    "name":          label,
                    "warning_color": normalize_color(fill),
                    "severity":      color_to_severity(fill),
                    "issued_at":     "",
                    "valid_upto":    "",
                })

    print(f"[scraper] District records extracted: {len(records)}")
    return records


def extract_station_records(html: str) -> list:
    """
    Extract station warning records from the station nowcast page.

    KEY DIFFERENCE from districts:
    Stations are rendered as SVG <image class="amcharts-map-image"> elements
    inside a parent <g> element whose aria-label contains the full tooltip:

        aria-label="Angul <p>No Warning </br></br>
                    Time of issue: 2026-05-11</br>2200 Hrs</br>
                    Valid upto: 0100 Hrs </p>"

    The warning colour is NOT in a fill attribute — it is encoded in the
    image filename:
        nowcast_marker/map-marker-icon-png-green.png   → #008000 (No Warning)
        nowcast_marker/map-marker-icon-png-yellow.png  → #ffff00 (Watch)
        nowcast_marker/map-marker-icon-png-orange.png  → #ffa500 (Alert)
        nowcast_marker/map-marker-icon-png-red.png     → #ff0000 (Warning)
    """

    soup = BeautifulSoup(html, "html.parser")
    records = []
    seen = set()

    # ── Primary: amcharts-map-image elements ─────────────────────────────
    for img_el in soup.select("svg image.amcharts-map-image"):

        href = (
            img_el.get("xlink:href")
            or img_el.get("href")
            or ""
        ).strip()

        # Colour comes from the marker image filename
        color_hex = marker_filename_to_color(href)

        # Name and tooltip are on the parent <g>
        parent = img_el.parent
        aria   = (parent.get("aria-label") or "").strip() if parent else ""

        if not aria:
            continue

        parsed   = parse_station_aria_label(aria)
        name     = parsed["name"]

        if not name or name in seen:
            continue

        seen.add(name)

        # If we couldn't get colour from filename, try warning text as fallback
        if not color_hex:
            text_to_color = {
                "No Warning": "#008000",
                "Watch":      "#ffff00",
                "Alert":      "#ffa500",
                "Warning":    "#ff0000",
            }
            color_hex = text_to_color.get(parsed["warning_text"], "")

        records.append({
            "type":          "station",
            "name":          name,
            "warning_color": color_hex,
            "severity":      color_to_severity(color_hex) if color_hex else parsed["warning_text"],
            "issued_at":     parsed["issued_at"],
            "valid_upto":    parsed["valid_upto"],
        })

    # ── Fallback: embedded JSON data arrays (original logic) ─────────────
    if not records:
        print("[scraper] WARNING: amcharts-map-image not found, trying JSON fallback")
        scripts = soup.find_all("script")

        for script in scripts:
            text = script.string or script.get_text()
            if not text:
                continue

            patterns = [
                r"dataProvider\s*[:=]\s*(\[[\s\S]*?\])",
                r"chart\.data\s*=\s*(\[[\s\S]*?\])",
                r"imageSeries\.data\s*=\s*(\[[\s\S]*?\])",
                r"pointSeries\.data\s*=\s*(\[[\s\S]*?\])",
            ]

            for pattern in patterns:
                for match in re.finditer(pattern, text):
                    try:
                        items = json.loads(match.group(1))
                        for item in items:
                            name = (
                                item.get("title")
                                or item.get("name")
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
                            if name and name not in seen:
                                seen.add(name)
                                records.append({
                                    "type":          "station",
                                    "name":          name,
                                    "warning_color": normalize_color(color),
                                    "severity":      color_to_severity(color),
                                    "issued_at":     "",
                                    "valid_upto":    "",
                                })
                    except Exception:
                        pass

    print(f"[scraper] Station records extracted: {len(records)}")
    return records


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
        "meta":    meta,
        "count":   len(records),
        "records": records,
    }

    path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def save_csv(filename: str, records: list, meta: dict, extra_fields: list = None):

    path = DATA_DIR / filename

    base_fields = [
        "scraped_at",
        "state_id",
        "type",
        "name",
        "warning_color",
        "severity",
    ]

    fieldnames = base_fields + (extra_fields or [])

    with open(path, "w", newline="", encoding="utf-8") as f:

        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        for r in records:
            row = {
                "scraped_at": meta["scraped_at"],
                "state_id":   meta["state_id"],
                **r,
            }
            writer.writerow(row)


def append_history(records: list, meta: dict):

    path = DATA_DIR / "warnings_history.jsonl"

    with open(path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps({"meta": meta, "records": records}, ensure_ascii=False)
            + "\n"
        )


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():

    clean_old_files()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    timestamp_human = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── DISTRICT PAGE ────────────────────────────────────────────────────

    district_html = load_page(
        DISTRICT_URL,
        f"district_warning_{STATE_ID}.png"
    )

    save_snapshot("district_snapshot", district_html, timestamp)

    district_records = extract_district_records(district_html)

    # ── STATION PAGE ─────────────────────────────────────────────────────

    station_html = load_page(
        STATION_URL,
        f"station_warning_{STATE_ID}.png"
    )

    save_snapshot("station_snapshot", station_html, timestamp)

    station_records = extract_station_records(station_html)

    # ── SUMMARY ──────────────────────────────────────────────────────────

    all_records = district_records + station_records

    print(f"\n[scraper] District records : {len(district_records)}")
    print(f"[scraper] Station records  : {len(station_records)}")
    print(f"[scraper] Total records    : {len(all_records)}")

    if not all_records:
        print("\n[scraper] ERROR: no data extracted")
        sys.exit(1)

    meta = {
        "scraped_at":   timestamp_human,
        "state_id":     STATE_ID,
        "district_url": DISTRICT_URL,
        "station_url":  STATION_URL,
    }

    # ── DISTRICT FILES ───────────────────────────────────────────────────

    save_json("district_warnings_latest.json", district_records, meta)
    save_csv("district_warnings_latest.csv",   district_records, meta)

    # ── STATION FILES (include issued_at / valid_upto columns) ───────────

    save_json("station_warnings_latest.json", station_records, meta)
    save_csv(
        "station_warnings_latest.csv",
        station_records,
        meta,
        extra_fields=["issued_at", "valid_upto"],
    )

    # ── COMBINED FILES ───────────────────────────────────────────────────

    save_json("warnings_latest.json", all_records, meta)
    save_csv(
        "warnings_latest.csv",
        all_records,
        meta,
        extra_fields=["issued_at", "valid_upto"],
    )

    append_history(all_records, meta)

    # ── SEVERITY SUMMARY ─────────────────────────────────────────────────

    print("\n[scraper] Severity Summary")

    for label, records in [("District", district_records), ("Station", station_records)]:
        counts = Counter(r["severity"] for r in records)
        print(f"\n  {label}:")
        for severity, count in sorted(counts.items()):
            print(f"    {severity}: {count}")

    print("\n[scraper] SUCCESS")


if __name__ == "__main__":
    main()
