import os
import re
import csv
import json
import sys
import smtplib

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from supabase import create_client, Client

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

# ── Alert thresholds ──────────────────────────────────────────
ALERT_DISTRICTS = {"KHORDHA", "CUTTACK"}

ALERT_STATIONS = {
    "Bhubaneshwar AP",
    "Cuttack",
    "Khordha",
    "Bhubaneshwar OUAT",
}

ALERT_SEVERITIES_DISTRICT     = {"Alert", "Warning"}
ALERT_STATIONS_WATCH          = {"Bhubaneshwar AP", "Bhubaneshwar OUAT"}
ALERT_SEVERITIES_STATION_WATCH = {"Watch", "Alert", "Warning"}
ALERT_SEVERITIES_STATION      = {"Alert", "Warning"}

# ── Email ─────────────────────────────────────────────────────
GMAIL_FROM = os.getenv("GMAIL_FROM", "")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD", "")
EMAIL_TO   = os.getenv("ALERT_EMAIL_TO", "")

# ── Supabase ──────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://odrvhelastdyozjejqss.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

DISTRICT_TABLE = "district_warnings"
STATION_TABLE  = "station_warnings"

# ── Colour maps ───────────────────────────────────────────────
HEX_TO_COLOR_NAME = {
    "#008000": "green",
    "#ffff00": "yellow",
    "#ffa500": "orange",
    "#ff0000": "red",
}

WARNING_COLOR_MAP = {
    "#008000": "No Warning",
    "#ffff00": "Watch",
    "#ffa500": "Alert",
    "#ff0000": "Warning",
}

MARKER_FILENAME_TO_HEX = {
    "green":  "#008000",
    "yellow": "#ffff00",
    "orange": "#ffa500",
    "red":    "#ff0000",
}

# IST scan schedule — must mirror pg_cron / Apps Script schedule
_SCAN_TIMES_IST = [
    (1,15),(4,15),(7,15),(10,15),(13,15),(16,15),(19,15),(22,15)
]


# ─────────────────────────────────────────────────────────────
# CLEAN OLD FILES
# PNGs excluded — overwritten in-place each run.
# ─────────────────────────────────────────────────────────────

def clean_old_files():
    print("\n[scraper] Cleaning old files...")
    keep    = {".gitkeep", "warnings_history.jsonl"}
    deleted = 0
    for pattern in ["*.html", "*.csv", "*.json"]:
        for f in DATA_DIR.glob(pattern):
            if f.name in keep:
                continue
            try:
                f.unlink()
                deleted += 1
            except Exception as e:
                print(f"[scraper] Could not delete {f.name}: {e}")
    print(f"[scraper] Deleted {deleted} old file(s)")


# ─────────────────────────────────────────────────────────────
# COLOUR HELPERS
# ─────────────────────────────────────────────────────────────

def normalize_color(color: str) -> str:
    if not color:
        return ""
    color = color.strip().lower()
    m = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", color)
    if m:
        r, g, b = map(int, m.groups())
        return "#{:02x}{:02x}{:02x}".format(r, g, b)
    return color


def color_to_severity(color: str) -> str:
    c = normalize_color(color)
    for k, v in WARNING_COLOR_MAP.items():
        if c == k.lower():
            return v
    return f"Unknown ({c})"


def hex_to_color_name(hex_color: str) -> str:
    return HEX_TO_COLOR_NAME.get(normalize_color(hex_color), hex_color)


def marker_filename_to_color(href: str) -> str:
    m = re.search(r"map-marker-icon-png-(\w+)\.png", href, re.IGNORECASE)
    if not m:
        return ""
    return MARKER_FILENAME_TO_HEX.get(m.group(1).lower(), "")


# ─────────────────────────────────────────────────────────────
# STATION ARIA-LABEL PARSER
# ─────────────────────────────────────────────────────────────

def parse_station_aria_label(aria_label: str) -> dict:
    soup_label = BeautifulSoup(aria_label, "html.parser")
    lines = [
        line.strip()
        for line in soup_label.get_text(separator="\n").splitlines()
        if line.strip()
    ]

    name         = lines[0] if lines else aria_label.strip()
    warn_m       = re.search(r"\b(No Warning|Watch|Alert|Warning)\b", aria_label)
    warning_text = warn_m.group(1) if warn_m else "Unknown"

    time_m    = re.search(
        r"Time of issue:\s*([\d-]+)\s*(?:</br>|<br\s*/?>|\s)([\d]+)\s*Hrs",
        aria_label, re.IGNORECASE,
    )
    issued_at  = f"{time_m.group(1)} {time_m.group(2)}" if time_m else ""

    valid_m    = re.search(r"Valid upto:\s*([\d]+)\s*Hrs", aria_label, re.IGNORECASE)
    valid_upto = valid_m.group(1) if valid_m else ""

    rain_description      = ""
    thunderstorm_desc     = ""
    lightning_probability = ""

    for line in lines[1:]:
        ll = line.lower()
        if "rain" in ll and not rain_description:
            rain_description = line
        elif ("thunderstorm" in ll or "kmph" in ll) and not thunderstorm_desc:
            thunderstorm_desc = line
        elif "lightning probability" in ll and not lightning_probability:
            lightning_probability = line

    return {
        "name":                  name,
        "warning_text":          warning_text,
        "issued_at":             issued_at,
        "valid_upto":            valid_upto,
        "rain_description":      rain_description,
        "thunderstorm_desc":     thunderstorm_desc,
        "lightning_probability": lightning_probability,
    }


# ─────────────────────────────────────────────────────────────
# BALLOON TEXT PARSER  (district popup)
# ─────────────────────────────────────────────────────────────

def parse_district_balloon(raw_text: str) -> dict:
    """
    Parse the amCharts balloon popup text that appears when a
    district with cursor="pointer" is clicked on the district map.

    Typical balloon text (after stripping HTML) looks like:
        SUNDARGARH
        Thunderstorm with lightning very likely
        Heavy to very heavy rain: likely
        Maximum wind speed: 40-50 kmph gusting to 60 kmph
        Lightning probability: High
        Time of issue: 2026-05-14 0830 Hrs
        Valid upto: 1730 Hrs

    Returns a dict with the parsed fields.
    """
    if not raw_text:
        return {
            "balloon_text":          "",
            "rain_description":      "",
            "thunderstorm_desc":     "",
            "lightning_probability": "",
            "issued_at":             "",
            "valid_upto":            "",
        }

    soup  = BeautifulSoup(raw_text, "html.parser")
    lines = [
        line.strip()
        for line in soup.get_text(separator="\n").splitlines()
        if line.strip()
    ]

    rain_description      = ""
    thunderstorm_desc     = ""
    lightning_probability = ""
    issued_at             = ""
    valid_upto            = ""

    for line in lines:
        ll = line.lower()
        if ("rain" in ll or "rainfall" in ll) and not rain_description:
            rain_description = line
        elif ("thunderstorm" in ll or "lightning very likely" in ll
              or "wind speed" in ll or "kmph" in ll) and not thunderstorm_desc:
            thunderstorm_desc = line
        elif "lightning probability" in ll and not lightning_probability:
            lightning_probability = line
        elif "time of issue" in ll and not issued_at:
            # e.g. "Time of issue: 2026-05-14 0830 Hrs"
            m = re.search(
                r"Time of issue:\s*([\d-]+)\s*([\d]+)\s*Hrs",
                line, re.IGNORECASE,
            )
            if m:
                issued_at = f"{m.group(1)} {m.group(2)}"
        elif "valid upto" in ll and not valid_upto:
            m = re.search(r"Valid upto:\s*([\d]+)\s*Hrs", line, re.IGNORECASE)
            if m:
                valid_upto = m.group(1)

    # Store the full cleaned balloon text as one string (for debugging/CSV)
    balloon_text = " | ".join(lines)

    return {
        "balloon_text":          balloon_text,
        "rain_description":      rain_description,
        "thunderstorm_desc":     thunderstorm_desc,
        "lightning_probability": lightning_probability,
        "issued_at":             issued_at,
        "valid_upto":            valid_upto,
    }


# ─────────────────────────────────────────────────────────────
# PAGE LOADERS
# ─────────────────────────────────────────────────────────────

def load_page(url: str, screenshot_name: str) -> str:
    """
    Generic page loader for the station page.
    Navigates, waits for SVG, takes screenshot, returns HTML.
    """
    print(f"\n[scraper] Loading: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1400, "height": 1200},
            locale="en-IN",
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=90000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: page load timed out — continuing anyway")

        try:
            page.wait_for_selector("svg", timeout=40000)
            page.wait_for_timeout(8000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: SVG not detected — continuing anyway")

        screenshot_path = DATA_DIR / screenshot_name
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[scraper] Screenshot saved → {screenshot_path.name}")

        html = page.content()
        browser.close()

    return html


def load_district_page(url: str, screenshot_name: str) -> tuple[str, dict]:
    """
    Loads the district warning page and captures balloon popup text
    for every district that has cursor="pointer" (i.e. districts that
    have detailed warning text when clicked — e.g. SUNDARGARH, KENDUJHAR,
    MAYURBHANJ during active weather events).

    Returns:
        html         — full page HTML (for static colour extraction)
        balloon_map  — dict of { district_name_upper: parsed_balloon_dict }
    """
    print(f"\n[scraper] Loading district page (with balloon capture): {url}")

    balloon_map: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1400, "height": 1200},
            locale="en-IN",
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=90000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: district page load timed out — continuing anyway")

        try:
            page.wait_for_selector("svg", timeout=40000)
            page.wait_for_timeout(8000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: SVG not detected on district page")

        # ── Balloon capture: click each cursor="pointer" district ─────────
        # These are the districts that IMD has assigned detailed warning text.
        # The balloon div is injected by amCharts on hover/click.
        try:
            clickable_paths = page.locator('svg path[cursor="pointer"]')
            count = clickable_paths.count()
            print(f"[scraper] Found {count} clickable district(s) with popup balloons")

            for i in range(count):
                try:
                    path_el = clickable_paths.nth(i)

                    # Get district name from aria-label before clicking
                    aria = path_el.get_attribute("aria-label") or ""
                    district_name = aria.strip().upper()

                    # Click the district to trigger the balloon
                    path_el.scroll_into_view_if_needed()
                    path_el.click()

                    # Wait for the amCharts balloon div to appear
                    balloon_selector = ".amcharts-balloon-div"
                    try:
                        page.wait_for_selector(
                            balloon_selector,
                            state="visible",
                            timeout=3000,
                        )
                        page.wait_for_timeout(500)  # let content fully render

                        balloon_el = page.locator(balloon_selector)
                        if balloon_el.is_visible():
                            raw_html = balloon_el.inner_html()
                            parsed   = parse_district_balloon(raw_html)
                            if district_name:
                                balloon_map[district_name] = parsed
                                print(
                                    f"[scraper]   ✔ Balloon captured for {district_name}: "
                                    f"{parsed['balloon_text'][:80]}..."
                                )
                        else:
                            print(f"[scraper]   ✘ Balloon not visible for {district_name}")

                    except PlaywrightTimeout:
                        print(f"[scraper]   ✘ No balloon appeared for {district_name}")

                    # Click away (on a neutral area) to dismiss the balloon
                    page.mouse.click(10, 10)
                    page.wait_for_timeout(300)

                except Exception as e:
                    print(f"[scraper]   ✘ Error clicking district {i}: {e}")

        except Exception as e:
            print(f"[scraper] Balloon capture failed (non-fatal): {e}")

        # ── Screenshot after all clicks ───────────────────────────────────
        screenshot_path = DATA_DIR / screenshot_name
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[scraper] Screenshot saved → {screenshot_path.name}")

        html = page.content()
        browser.close()

    print(f"[scraper] Balloon map captured for {len(balloon_map)} district(s): "
          f"{list(balloon_map.keys())}")
    return html, balloon_map


# ─────────────────────────────────────────────────────────────
# EXTRACTION — DISTRICTS
# Accepts balloon_map to enrich records with popup detail text.
# ─────────────────────────────────────────────────────────────

def extract_district_records(html: str, balloon_map: dict = None) -> list:
    """
    Extract district warning records from the SVG map HTML.
    balloon_map (optional): { DISTRICT_NAME_UPPER: parsed_balloon_dict }
    Districts found in balloon_map get rain/storm/lightning fields populated.
    issued_at / valid_upto are also taken from balloon data if available,
    otherwise filled in later by enrich_district_issued_at().
    """
    soup       = BeautifulSoup(html, "html.parser")
    records    = []
    seen       = set()
    balloon_map = balloon_map or {}

    def _make_record(label: str, fill: str) -> dict:
        name      = label.strip()
        name_key  = name.upper()
        hex_color = normalize_color(fill)
        balloon   = balloon_map.get(name_key, {})

        return {
            "type":                  "district",
            "name":                  name,
            "warning_color":         hex_to_color_name(hex_color),
            "severity":              color_to_severity(fill),
            # Time info — from balloon if available, else filled by enrich step
            "issued_at":             balloon.get("issued_at", ""),
            "valid_upto":            balloon.get("valid_upto", ""),
            # Detailed warning text from balloon popup (empty if no popup)
            "balloon_text":          balloon.get("balloon_text", ""),
            "rain_description":      balloon.get("rain_description", ""),
            "thunderstorm_desc":     balloon.get("thunderstorm_desc", ""),
            "lightning_probability": balloon.get("lightning_probability", ""),
        }

    # Primary: amcharts-map-area SVG paths
    for path in soup.select("svg path.amcharts-map-area"):
        label = (path.get("aria-label") or "").strip()
        fill  = (path.get("fill") or "").strip()
        if label and fill and label.strip() not in seen:
            seen.add(label.strip())
            records.append(_make_record(label, fill))

    # Fallback: any SVG path with aria-label + fill
    if not records:
        for path in soup.select("svg path"):
            label = (path.get("aria-label") or path.get("title") or "").strip()
            fill  = (path.get("fill") or path.get("stroke") or "").strip()
            if label and fill and label not in seen:
                seen.add(label)
                records.append(_make_record(label, fill))

    print(f"[scraper] District records extracted: {len(records)}")
    balloon_hits = sum(1 for r in records if r.get("balloon_text"))
    print(f"[scraper] Districts with balloon data: {balloon_hits}")
    return records


# ─────────────────────────────────────────────────────────────
# EXTRACTION — STATIONS  (unchanged)
# ─────────────────────────────────────────────────────────────

def extract_station_records(html: str) -> list:

    soup    = BeautifulSoup(html, "html.parser")
    records = []
    seen    = set()

    TEXT_TO_COLOR = {
        "No Warning": "#008000",
        "Watch":      "#ffff00",
        "Alert":      "#ffa500",
        "Warning":    "#ff0000",
    }

    for img_el in soup.select("svg image.amcharts-map-image"):
        href      = (img_el.get("xlink:href") or img_el.get("href") or "").strip()
        color_hex = marker_filename_to_color(href)
        parent    = img_el.parent
        aria      = (parent.get("aria-label") or "").strip() if parent else ""
        if not aria:
            continue
        parsed = parse_station_aria_label(aria)
        name   = parsed["name"]
        if not name or name in seen:
            continue
        seen.add(name)
        if not color_hex:
            color_hex = TEXT_TO_COLOR.get(parsed["warning_text"], "")
        records.append({
            "type":                  "station",
            "name":                  name,
            "warning_color":         hex_to_color_name(color_hex),
            "severity":              color_to_severity(color_hex) if color_hex else parsed["warning_text"],
            "issued_at":             parsed["issued_at"],
            "valid_upto":            parsed["valid_upto"],
            "balloon_text":          "",   # stations don't have balloon popups
            "rain_description":      parsed.get("rain_description", ""),
            "thunderstorm_desc":     parsed.get("thunderstorm_desc", ""),
            "lightning_probability": parsed.get("lightning_probability", ""),
        })

    # Fallback: embedded JSON in <script> tags
    if not records:
        print("[scraper] WARNING: primary extraction failed — trying JSON fallback")
        for script in soup.find_all("script"):
            text = script.string or script.get_text()
            if not text:
                continue
            for pattern in [
                r"dataProvider\s*[:=]\s*(\[[\s\S]*?\])",
                r"chart\.data\s*=\s*(\[[\s\S]*?\])",
                r"imageSeries\.data\s*=\s*(\[[\s\S]*?\])",
                r"pointSeries\.data\s*=\s*(\[[\s\S]*?\])",
            ]:
                for m in re.finditer(pattern, text):
                    try:
                        for item in json.loads(m.group(1)):
                            name  = (item.get("title") or item.get("name") or
                                     item.get("station") or item.get("id") or "")
                            color = (item.get("color") or item.get("fill") or
                                     item.get("warningColor") or "")
                            if name and name not in seen:
                                seen.add(name)
                                hex_color = normalize_color(color)
                                records.append({
                                    "type":                  "station",
                                    "name":                  name,
                                    "warning_color":         hex_to_color_name(hex_color),
                                    "severity":              color_to_severity(color),
                                    "issued_at":             "",
                                    "valid_upto":            "",
                                    "balloon_text":          "",
                                    "rain_description":      "",
                                    "thunderstorm_desc":     "",
                                    "lightning_probability": "",
                                })
                    except Exception:
                        pass

    print(f"[scraper] Station records extracted: {len(records)}")
    return records


# ─────────────────────────────────────────────────────────────
# ENRICH DISTRICTS
# For districts WITHOUT balloon data, copy the consensus
# issued_at / valid_upto from station records.
# Districts WITH balloon data already have this from the popup.
# ─────────────────────────────────────────────────────────────

def enrich_district_issued_at(district_records: list, station_records: list) -> list:
    issued_vals = [r["issued_at"] for r in station_records if r.get("issued_at")]
    valid_vals  = [r["valid_upto"] for r in station_records if r.get("valid_upto")]

    if not issued_vals:
        print("[scraper] No station issued_at found — district time columns stay empty")
        return district_records

    common_issued = Counter(issued_vals).most_common(1)[0][0]
    common_valid  = Counter(valid_vals).most_common(1)[0][0] if valid_vals else ""
    print(f"[scraper] Station consensus → issued_at={common_issued}  valid_upto={common_valid}")

    for r in district_records:
        # Only fill from station data if the balloon didn't already provide it
        if not r.get("issued_at"):
            r["issued_at"] = common_issued
        if not r.get("valid_upto"):
            r["valid_upto"] = common_valid

    return district_records


# ─────────────────────────────────────────────────────────────
# ALERT CHECK
# ─────────────────────────────────────────────────────────────

def check_alerts(district_records: list, station_records: list) -> dict:
    triggered_districts = [
        r for r in district_records
        if r["name"].upper() in ALERT_DISTRICTS
        and r["severity"] in ALERT_SEVERITIES_DISTRICT
    ]
    triggered_stations = [
        r for r in station_records
        if r["name"] in ALERT_STATIONS
        and (
            (r["name"] in ALERT_STATIONS_WATCH
             and r["severity"] in ALERT_SEVERITIES_STATION_WATCH)
            or
            (r["name"] not in ALERT_STATIONS_WATCH
             and r["severity"] in ALERT_SEVERITIES_STATION)
        )
    ]
    return {
        "should_alert": bool(triggered_districts or triggered_stations),
        "districts":    triggered_districts,
        "stations":     triggered_stations,
    }


# ─────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────

def _alert_rows_to_html_table(records: list, title: str) -> str:
    if not records:
        return ""
    COLOR_BADGE = {
        "red":    ("#ff0000", "#fff"),
        "orange": ("#ffa500", "#000"),
        "yellow": ("#ffff00", "#000"),
        "green":  ("#008000", "#fff"),
    }
    rows_html = ""
    for r in records:
        bg, fg = COLOR_BADGE.get(r.get("warning_color", "").lower(), ("#cccccc", "#000"))
        badge = (
            f'<span style="background:{bg};color:{fg};padding:2px 8px;'
            f'border-radius:4px;font-weight:bold;font-size:12px;">'
            f'{r["severity"].upper()}</span>'
        )
        rows_html += (
            f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;font-weight:600;'>{r['name']}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;text-align:center;'>{badge}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;'>{r.get('issued_at','—')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;'>{r.get('valid_upto','—')} Hrs</td>"
            f"</tr>"
        )
    return f"""
    <h3 style="margin:16px 0 6px;color:#b30000;">{title}</h3>
    <table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;">
      <thead>
        <tr style="background:#1B3A6B;color:#fff;">
          <th style="padding:7px 10px;text-align:left;">Name</th>
          <th style="padding:7px 10px;">Severity</th>
          <th style="padding:7px 10px;text-align:left;">Issued At</th>
          <th style="padding:7px 10px;text-align:left;">Valid Upto</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>"""


def _weather_details_html_table(records: list, title: str) -> str:
    """Render weather detail rows for both district and station records."""
    detail_records = [
        r for r in records
        if r.get("rain_description") or r.get("thunderstorm_desc")
        or r.get("lightning_probability") or r.get("balloon_text")
    ]
    if not detail_records:
        return ""
    rows_html = ""
    for r in detail_records:
        rain  = (r.get("rain_description")      or "—").strip()
        storm = (r.get("thunderstorm_desc")      or "—").strip()
        light = (r.get("lightning_probability")  or "—").strip()
        rows_html += (
            f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;font-weight:600;'>{r['name']}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;font-size:12px;'>🌧️ {rain}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;font-size:12px;'>⛈️ {storm}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;font-size:12px;'>⚡ {light}</td>"
            f"</tr>"
        )
    return f"""
    <h3 style="margin:16px 0 6px;color:#b30000;">{title}</h3>
    <table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;">
      <thead>
        <tr style="background:#1B3A6B;color:#fff;">
          <th style="padding:7px 10px;text-align:left;">Name</th>
          <th style="padding:7px 10px;text-align:left;">Rain</th>
          <th style="padding:7px 10px;text-align:left;">Storm / Wind</th>
          <th style="padding:7px 10px;text-align:left;">Lightning</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>"""


def _build_trigger_footer_html() -> str:
    dist_list     = ", ".join(sorted(ALERT_DISTRICTS))
    dist_sev      = ", ".join(sorted(ALERT_SEVERITIES_DISTRICT))
    watch_stn     = ", ".join(sorted(ALERT_STATIONS_WATCH))
    watch_sev     = ", ".join(sorted(ALERT_SEVERITIES_STATION_WATCH))
    other_stn     = ", ".join(sorted(ALERT_STATIONS - ALERT_STATIONS_WATCH))
    other_sev     = ", ".join(sorted(ALERT_SEVERITIES_STATION))
    scan_times_str = ", ".join(f"{h:02d}:{m:02d}" for h, m in _SCAN_TIMES_IST)
    row_style = "padding:4px 8px;border-bottom:1px solid #eee;font-size:12px;"
    lbl_style = "color:#555;width:200px;vertical-align:top;padding-right:8px;"
    return f"""
    <div style="background:#f9f9f9;border:1px solid #e0e0e0;border-radius:6px;
                padding:12px 16px;margin-top:4px;">
      <p style="margin:0 0 8px;font-size:12px;font-weight:bold;color:#444;">
        📋 Email Send Condition
      </p>
      <table style="border-collapse:collapse;width:100%;">
        <tr style="{row_style}">
          <td style="{lbl_style}">Districts monitored</td>
          <td style="font-size:12px;">{dist_list}</td>
        </tr>
        <tr style="{row_style}">
          <td style="{lbl_style}">District triggers on</td>
          <td style="font-size:12px;">{dist_sev}</td>
        </tr>
        <tr style="{row_style}">
          <td style="{lbl_style}">{watch_stn}</td>
          <td style="font-size:12px;">triggers on {watch_sev}</td>
        </tr>
        <tr style="{row_style}">
          <td style="{lbl_style}">{other_stn}</td>
          <td style="font-size:12px;">triggers on {other_sev}</td>
        </tr>
        <tr style="padding:4px 8px;font-size:12px;">
          <td style="{lbl_style}">Scan schedule (IST)</td>
          <td style="font-size:12px;color:#555;">
            {scan_times_str} Hrs &nbsp;via Supabase pg_cron → GitHub Actions
          </td>
        </tr>
      </table>
    </div>"""


def build_email_plain(alert_info: dict, timestamp_human: str) -> str:
    lines = [
        f"IMD Warning Alert — {timestamp_human}",
        "=" * 60, "",
        "⚠️  High-severity warnings detected:", "",
    ]
    for section_label, section_records, include_details in [
        ("DISTRICT ALERTS", alert_info["districts"], True),
        ("STATION ALERTS",  alert_info["stations"],  True),
    ]:
        if section_records:
            lines += [section_label, "-" * 40]
            for r in section_records:
                lines.append(
                    f"  • {r['name']}  |  {r['severity'].upper()} ({r['warning_color']})"
                )
                if r.get("issued_at"):
                    lines.append(f"    Issued at : {r['issued_at']}")
                if r.get("valid_upto"):
                    lines.append(f"    Valid upto: {r['valid_upto']} Hrs")
                if include_details:
                    if r.get("rain_description"):
                        lines.append(f"    Rain      : {r['rain_description']}")
                    if r.get("thunderstorm_desc"):
                        lines.append(f"    Thunderst.: {r['thunderstorm_desc']}")
                    if r.get("lightning_probability"):
                        lines.append(f"    Lightning : {r['lightning_probability']}")
            lines.append("")

    scan_times_str = ", ".join(f"{h:02d}:{m:02d}" for h, m in _SCAN_TIMES_IST)
    lines += [
        "=" * 60,
        "EMAIL SEND CONDITION",
        "-" * 40,
        "Districts monitored : " + ", ".join(sorted(ALERT_DISTRICTS)),
        "  → Email sent when severity is: " + ", ".join(sorted(ALERT_SEVERITIES_DISTRICT)),
        "",
        "Stations monitored  : " + ", ".join(sorted(ALERT_STATIONS)),
        "  → Bhubaneshwar AP / OUAT: email on Watch, Alert, or Warning",
        "  → Cuttack / Khordha    : email on Alert or Warning only",
        "",
        f"Scan schedule (IST) : {scan_times_str} Hrs",
        "  via Supabase pg_cron → Edge Function → GitHub Actions",
        "=" * 60,
        "Source: IMD Nowcast Warning system",
        "Attachments: district map PNG, station map PNG",
    ]
    return "\n".join(lines)


def build_email_html(alert_info: dict, timestamp_human: str) -> str:
    district_table         = _alert_rows_to_html_table(alert_info["districts"], "🗺️ District Alerts")
    station_table          = _alert_rows_to_html_table(alert_info["stations"],  "📍 Station Alerts")
    district_details_table = _weather_details_html_table(alert_info["districts"], "🌩️ District Weather Details")
    station_details_table  = _weather_details_html_table(alert_info["stations"],  "🌩️ Station Weather Details")
    trigger_html           = _build_trigger_footer_html()

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:0;">
  <div style="max-width:720px;margin:20px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 8px rgba(0,0,0,.12);overflow:hidden;">
    <div style="background:#b30000;padding:18px 24px;">
      <h2 style="margin:0;color:#fff;font-size:18px;">🚨 IMD HIGH ALERT</h2>
      <p style="margin:4px 0 0;color:#ffdada;font-size:12px;">Scraped at {timestamp_human}</p>
    </div>
    <div style="padding:20px 24px;">
      <p style="margin:0 0 12px;color:#333;">
        High-severity weather warnings detected for monitored locations in Odisha.
      </p>
      {district_table}
      {district_details_table}
      {station_table}
      {station_details_table}
      <hr style="margin:24px 0;border:none;border-top:1px solid #eee;">
      {trigger_html}
      <hr style="margin:16px 0;border:none;border-top:1px solid #eee;">
      <p style="font-size:11px;color:#888;margin:0;">
        Source: IMD Nowcast Warning system &nbsp;|&nbsp;
        Scraped automatically by GitHub Actions.<br>
        Attachments: district map PNG &bull; station map PNG
      </p>
    </div>
  </div>
</body>
</html>"""


def send_alert_email(alert_info: dict, timestamp_human: str):
    if not GMAIL_FROM or not GMAIL_PASS or not EMAIL_TO:
        print("[scraper] Email env vars not set — skipping alert email")
        return

    recipients = [a.strip() for a in EMAIL_TO.split(",") if a.strip()]

    all_triggered      = alert_info["districts"] + alert_info["stations"]
    severities_present = {r["severity"] for r in all_triggered}
    if "Warning" in severities_present:
        severity_label = "⛔ WARNING"
    elif "Alert" in severities_present:
        severity_label = "🚨 ALERT"
    else:
        severity_label = "⚠️ WATCH"

    location_names = [r["name"] for r in all_triggered]
    locations_str  = ", ".join(location_names[:3])
    if len(location_names) > 3:
        locations_str += f" +{len(location_names) - 3} more"

    subject = f"IMD {severity_label} — {locations_str}"

    msg = MIMEMultipart("mixed")
    msg["From"]    = GMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject

    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(build_email_plain(alert_info, timestamp_human), "plain"))
    alt_part.attach(MIMEText(build_email_html(alert_info, timestamp_human),  "html"))
    msg.attach(alt_part)

    for attach_path in [
        DATA_DIR / f"district_warning_{STATE_ID}.png",
        DATA_DIR / f"station_warning_{STATE_ID}.png",
    ]:
        if not attach_path.exists():
            print(f"[scraper] Attachment not found (skipping): {attach_path.name}")
            continue
        with open(attach_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{attach_path.name}"')
        msg.attach(part)
        print(f"[scraper] Attached: {attach_path.name}")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_FROM, GMAIL_PASS)
            server.sendmail(GMAIL_FROM, recipients, msg.as_string())
        print(f"[scraper] Alert email sent → {', '.join(recipients)}")
    except Exception as e:
        print(f"[scraper] ERROR sending email: {e}")


# ─────────────────────────────────────────────────────────────
# SUPABASE UPLOAD
# Clears state_id rows then inserts fresh snapshot.
# balloon_text + detail fields included for district rows.
# ─────────────────────────────────────────────────────────────

def build_supabase_rows(records: list, meta: dict) -> list:
    return [
        {
            "scraped_at":            meta["scraped_at"],
            "state_id":              meta["state_id"],
            "type":                  r.get("type", ""),
            "name":                  r.get("name", ""),
            "warning_color":         r.get("warning_color", ""),
            "severity":              r.get("severity", ""),
            "issued_at":             r.get("issued_at") or None,
            "valid_upto":            r.get("valid_upto") or None,
            "balloon_text":          r.get("balloon_text") or None,
            "rain_description":      r.get("rain_description") or None,
            "thunderstorm_desc":     r.get("thunderstorm_desc") or None,
            "lightning_probability": r.get("lightning_probability") or None,
        }
        for r in records
    ]


def upload_to_supabase(district_records: list, station_records: list, meta: dict):
    if not SUPABASE_KEY:
        print("[scraper] SUPABASE_KEY not set — skipping Supabase upload")
        return
    try:
        sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"[scraper] Supabase client error: {e}")
        return

    for table, records in [
        (DISTRICT_TABLE, district_records),
        (STATION_TABLE,  station_records),
    ]:
        try:
            sb.table(table).delete().eq("state_id", STATE_ID).execute()
            print(f"[scraper] Supabase: cleared '{table}' for state_id={STATE_ID}")

            rows = build_supabase_rows(records, meta)
            if not rows:
                print(f"[scraper] Supabase: no rows for '{table}' — skipping")
                continue

            total = 0
            for i in range(0, len(rows), 100):
                sb.table(table).insert(rows[i:i + 100]).execute()
                total += len(rows[i:i + 100])
            print(f"[scraper] Supabase: {total} rows → '{table}'")
        except Exception as e:
            print(f"[scraper] Supabase ERROR on '{table}': {e}")


# ─────────────────────────────────────────────────────────────
# FILE SAVERS
# ─────────────────────────────────────────────────────────────

def save_snapshot(name: str, html: str, timestamp: str):
    (DATA_DIR / f"{name}_{timestamp}.html").write_text(html, encoding="utf-8")


def save_json(filename: str, records: list, meta: dict):
    (DATA_DIR / filename).write_text(
        json.dumps({"meta": meta, "count": len(records), "records": records},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def save_csv(filename: str, records: list, meta: dict, extra_fields: list = None):
    fieldnames = [
        "scraped_at", "state_id", "type", "name",
        "warning_color", "severity",
    ] + (extra_fields or [])
    with open(DATA_DIR / filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow({"scraped_at": meta["scraped_at"],
                             "state_id":   meta["state_id"], **r})


def append_history(records: list, meta: dict):
    with open(DATA_DIR / "warnings_history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"meta": meta, "records": records}, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    clean_old_files()

    timestamp       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    timestamp_human = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── DISTRICT PAGE (with balloon popup capture) ────────────────────────
    district_html, balloon_map = load_district_page(
        DISTRICT_URL, f"district_warning_{STATE_ID}.png"
    )
    save_snapshot("district_snapshot", district_html, timestamp)
    district_records = extract_district_records(district_html, balloon_map)

    # ── STATION PAGE ──────────────────────────────────────────────────────
    station_html = load_page(STATION_URL, f"station_warning_{STATE_ID}.png")
    save_snapshot("station_snapshot", station_html, timestamp)
    station_records = extract_station_records(station_html)

    # ── Enrich districts without balloon data from station consensus ──────
    district_records = enrich_district_issued_at(district_records, station_records)

    all_records = district_records + station_records

    print(f"\n[scraper] District : {len(district_records)} records")
    print(f"[scraper] Station  : {len(station_records)} records")
    print(f"[scraper] Total    : {len(all_records)} records")

    if not all_records:
        print("\n[scraper] ERROR: no data extracted — aborting")
        sys.exit(1)

    meta = {
        "scraped_at":   timestamp_human,
        "state_id":     STATE_ID,
        "district_url": DISTRICT_URL,
        "station_url":  STATION_URL,
    }

    # ── District files ────────────────────────────────────────────────────
    save_json("district_warnings_latest.json", district_records, meta)
    save_csv(
        "district_warnings_latest.csv", district_records, meta,
        extra_fields=[
            "issued_at", "valid_upto",
            "balloon_text", "rain_description",
            "thunderstorm_desc", "lightning_probability",
        ],
    )

    # ── Station files ─────────────────────────────────────────────────────
    save_json("station_warnings_latest.json", station_records, meta)
    save_csv(
        "station_warnings_latest.csv", station_records, meta,
        extra_fields=[
            "issued_at", "valid_upto",
            "rain_description", "thunderstorm_desc", "lightning_probability",
        ],
    )

    # ── Combined files ────────────────────────────────────────────────────
    save_json("warnings_latest.json", all_records, meta)
    save_csv(
        "warnings_latest.csv", all_records, meta,
        extra_fields=[
            "issued_at", "valid_upto",
            "balloon_text", "rain_description",
            "thunderstorm_desc", "lightning_probability",
        ],
    )

    append_history(all_records, meta)

    # ── Supabase ──────────────────────────────────────────────────────────
    upload_to_supabase(district_records, station_records, meta)

    # ── Severity summary ──────────────────────────────────────────────────
    print("\n[scraper] Severity Summary")
    for label, recs in [("District", district_records), ("Station", station_records)]:
        counts = Counter(r["severity"] for r in recs)
        print(f"  {label}: " + "  ".join(f"{s}={c}" for s, c in sorted(counts.items())))

    # ── Alert email ───────────────────────────────────────────────────────
    alert_info = check_alerts(district_records, station_records)
    if alert_info["should_alert"]:
        print("\n[scraper] 🚨 HIGH ALERT — sending email...")
        send_alert_email(alert_info, timestamp_human)
    else:
        print("\n[scraper] No high-severity alerts")

    print("\n[scraper] SUCCESS")


if __name__ == "__main__":
    main()
