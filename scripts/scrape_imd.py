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
ALERT_DISTRICT = "KHORDHA"

ALERT_STATIONS = {
    "Bhubaneshwar AP",
    "Cuttack",
    "Khordha",
    "Bhubaneshwar OUAT",
}

ALERT_SEVERITIES = {"Warning"}   # red only

# ── Email config (read from GitHub Actions secrets / env vars) ─
GMAIL_FROM    = os.getenv("GMAIL_FROM", "")          # your Gmail address
GMAIL_PASS    = os.getenv("GMAIL_APP_PASSWORD", "")  # 16-char App Password
EMAIL_TO      = os.getenv("ALERT_EMAIL_TO", "")      # recipient address(es), comma-separated

# ── Colour maps ───────────────────────────────────────────────
# Human-readable colour name used in the CSV warning_color column
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


# ─────────────────────────────────────────────────────────────
# CLEAN OLD FILES  (delete everything except .gitkeep & history)
# ─────────────────────────────────────────────────────────────

def clean_old_files():

    print("\n[scraper] Cleaning old files...")

    patterns = ["*.html", "*.png", "*.csv", "*.json"]

    keep_files = {".gitkeep", "warnings_history.jsonl"}

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

    rgb_match = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", color)

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


def hex_to_color_name(hex_color: str) -> str:
    """Return human-readable color name (green/yellow/orange/red) for the CSV column."""
    return HEX_TO_COLOR_NAME.get(normalize_color(hex_color), hex_color)


def marker_filename_to_color(href: str) -> str:
    match = re.search(r"map-marker-icon-png-(\w+)\.png", href, re.IGNORECASE)
    if not match:
        return ""
    colour_word = match.group(1).lower()
    return MARKER_FILENAME_TO_HEX.get(colour_word, "")


def parse_station_aria_label(aria_label: str):
    """
    Parse the aria-label on a station marker <g> element.

    Returns a dict: name, warning_text, issued_at, valid_upto.
    """

    name_match = re.match(r"^([^<]+)", aria_label)
    name = name_match.group(1).strip() if name_match else aria_label.strip()

    warning_match = re.search(
        r"\b(No Warning|Watch|Alert|Warning)\b", aria_label
    )
    warning_text = warning_match.group(1) if warning_match else "Unknown"

    time_match = re.search(
        r"Time of issue:\s*([\d-]+)\s*(?:</br>|<br\s*/?>|\s)([\d]+)\s*Hrs",
        aria_label,
        re.IGNORECASE,
    )
    issued_at = (
        f"{time_match.group(1)} {time_match.group(2)}" if time_match else ""
    )

    valid_match = re.search(
        r"Valid upto:\s*([\d]+)\s*Hrs", aria_label, re.IGNORECASE
    )
    valid_upto = valid_match.group(1) if valid_match else ""

    return {
        "name":         name,
        "warning_text": warning_text,
        "issued_at":    issued_at,
        "valid_upto":   valid_upto,
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
            viewport={"width": 1400, "height": 1200},
            locale="en-IN",
        )

        page = context.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=90000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: timeout occurred")

        try:
            page.wait_for_selector("svg", timeout=40000)
            page.wait_for_timeout(8000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: SVG not detected")

        screenshot_path = DATA_DIR / screenshot_name
        page.screenshot(path=str(screenshot_path), full_page=True)

        html = page.content()
        browser.close()

    return html


# ─────────────────────────────────────────────────────────────
# EXTRACTION
# ─────────────────────────────────────────────────────────────

def extract_district_records(html: str) -> list:
    """
    Extract district warning records.

    Adds issued_at and valid_upto columns (empty for districts,
    which don't carry time information on the map page) so that
    district_warnings_latest.csv has the same schema as the
    station CSV.
    """

    soup    = BeautifulSoup(html, "html.parser")
    records = []
    seen    = set()

    # ── Primary: amcharts-map-area SVG paths ─────────────────────────────
    for path in soup.select("svg path.amcharts-map-area"):

        label = (path.get("aria-label") or "").strip()
        fill  = (path.get("fill") or "").strip()

        if label and fill and label not in seen:
            seen.add(label)
            hex_color = normalize_color(fill)
            records.append({
                "type":          "district",
                "name":          label,
                "warning_color": hex_to_color_name(hex_color),
                "severity":      color_to_severity(fill),
                "issued_at":     "",
                "valid_upto":    "",
            })

    # ── Fallback: any SVG path with aria-label + fill ────────────────────
    if not records:
        for path in soup.select("svg path"):

            label = (
                path.get("aria-label") or path.get("title") or ""
            ).strip()

            fill = (
                path.get("fill") or path.get("stroke") or ""
            ).strip()

            if label and fill and label not in seen:
                seen.add(label)
                hex_color = normalize_color(fill)
                records.append({
                    "type":          "district",
                    "name":          label,
                    "warning_color": hex_to_color_name(hex_color),
                    "severity":      color_to_severity(fill),
                    "issued_at":     "",
                    "valid_upto":    "",
                })

    print(f"[scraper] District records extracted: {len(records)}")
    return records


def extract_station_records(html: str) -> list:
    """
    Extract station warning records from the station nowcast page.
    """

    soup    = BeautifulSoup(html, "html.parser")
    records = []
    seen    = set()

    # ── Primary: amcharts-map-image elements ─────────────────────────────
    for img_el in soup.select("svg image.amcharts-map-image"):

        href = (
            img_el.get("xlink:href") or img_el.get("href") or ""
        ).strip()

        color_hex = marker_filename_to_color(href)

        parent = img_el.parent
        aria   = (parent.get("aria-label") or "").strip() if parent else ""

        if not aria:
            continue

        parsed = parse_station_aria_label(aria)
        name   = parsed["name"]

        if not name or name in seen:
            continue

        seen.add(name)

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
            "warning_color": hex_to_color_name(color_hex),
            "severity":      color_to_severity(color_hex) if color_hex else parsed["warning_text"],
            "issued_at":     parsed["issued_at"],
            "valid_upto":    parsed["valid_upto"],
        })

    # ── Fallback: embedded JSON data arrays ──────────────────────────────
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
                                item.get("title") or item.get("name")
                                or item.get("station") or item.get("id") or ""
                            )
                            color = (
                                item.get("color") or item.get("fill")
                                or item.get("warningColor") or ""
                            )
                            if name and name not in seen:
                                seen.add(name)
                                hex_color = normalize_color(color)
                                records.append({
                                    "type":          "station",
                                    "name":          name,
                                    "warning_color": hex_to_color_name(hex_color),
                                    "severity":      color_to_severity(color),
                                    "issued_at":     "",
                                    "valid_upto":    "",
                                })
                    except Exception:
                        pass

    print(f"[scraper] Station records extracted: {len(records)}")
    return records


# ─────────────────────────────────────────────────────────────
# ALERT CHECK
# ─────────────────────────────────────────────────────────────

def check_alerts(district_records: list, station_records: list) -> dict:
    """
    Return a dict describing whether an alert email should be sent,
    and which specific records triggered it.
    """

    triggered_districts = [
        r for r in district_records
        if r["name"].upper() == ALERT_DISTRICT
        and r["severity"] in ALERT_SEVERITIES
    ]

    triggered_stations = [
        r for r in station_records
        if r["name"] in ALERT_STATIONS
        and r["severity"] in ALERT_SEVERITIES
    ]

    return {
        "should_alert":   bool(triggered_districts or triggered_stations),
        "districts":      triggered_districts,
        "stations":       triggered_stations,
    }


# ─────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────

def build_email_body(alert_info: dict, timestamp_human: str) -> str:

    lines = [
        f"IMD Warning Alert — {timestamp_human}",
        "=" * 60,
        "",
        "⚠️  The following high-severity warnings were detected:",
        "",
    ]

    if alert_info["districts"]:
        lines.append("DISTRICT ALERTS")
        lines.append("-" * 40)
        for r in alert_info["districts"]:
            lines.append(
                f"  • {r['name']}  |  {r['severity'].upper()} ({r['warning_color']})"
            )
            if r.get("issued_at"):
                lines.append(f"    Issued at : {r['issued_at']}")
            if r.get("valid_upto"):
                lines.append(f"    Valid upto: {r['valid_upto']} Hrs")
        lines.append("")

    if alert_info["stations"]:
        lines.append("STATION ALERTS")
        lines.append("-" * 40)
        for r in alert_info["stations"]:
            lines.append(
                f"  • {r['name']}  |  {r['severity'].upper()} ({r['warning_color']})"
            )
            if r.get("issued_at"):
                lines.append(f"    Issued at : {r['issued_at']}")
            if r.get("valid_upto"):
                lines.append(f"    Valid upto: {r['valid_upto']} Hrs")
        lines.append("")

    lines += [
        "=" * 60,
        "Source: IMD Nowcast Warning system",
        "Scraped automatically by GitHub Actions.",
    ]

    return "\n".join(lines)


def send_alert_email(alert_info: dict, timestamp_human: str):
    """
    Send a Gmail alert with both PNG screenshots as attachments.
    Requires GMAIL_FROM, GMAIL_APP_PASSWORD, and ALERT_EMAIL_TO env vars.
    """

    if not GMAIL_FROM or not GMAIL_PASS or not EMAIL_TO:
        print("[scraper] Email env vars not set — skipping email alert")
        return

    recipients = [addr.strip() for addr in EMAIL_TO.split(",") if addr.strip()]

    subject = f"🚨 IMD HIGH ALERT — {timestamp_human}"
    body    = build_email_body(alert_info, timestamp_human)

    msg = MIMEMultipart()
    msg["From"]    = GMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "plain"))

    # Attach both PNG screenshots
    png_files = [
        DATA_DIR / f"district_warning_{STATE_ID}.png",
        DATA_DIR / f"station_warning_{STATE_ID}.png",
    ]

    for png_path in png_files:
        if png_path.exists():
            with open(png_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename={png_path.name}",
            )
            msg.attach(part)
            print(f"[scraper] Attached: {png_path.name}")
        else:
            print(f"[scraper] Screenshot not found (skipping attachment): {png_path.name}")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_FROM, GMAIL_PASS)
            server.sendmail(GMAIL_FROM, recipients, msg.as_string())
        print(f"[scraper] Alert email sent to: {', '.join(recipients)}")
    except Exception as e:
        print(f"[scraper] ERROR sending email: {e}")


# ─────────────────────────────────────────────────────────────
# SAVE FUNCTIONS
# ─────────────────────────────────────────────────────────────

def save_snapshot(name: str, html: str, timestamp: str):

    path = DATA_DIR / f"{name}_{timestamp}.html"
    path.write_text(html, encoding="utf-8")


def save_json(filename: str, records: list, meta: dict):

    path   = DATA_DIR / filename
    output = {"meta": meta, "count": len(records), "records": records}
    path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")


def save_csv(filename: str, records: list, meta: dict, extra_fields: list = None):
    """
    Write a CSV.

    district_warnings_latest.csv now shares the same schema as
    station_warnings_latest.csv, including issued_at and valid_upto.

    warning_color is stored as a human-readable name
    (green / yellow / orange / red) rather than a hex code.
    """

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
            json.dumps({"meta": meta, "records": records}, ensure_ascii=False) + "\n"
        )


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():

    # 1. Delete all old output files before writing new ones
    clean_old_files()

    timestamp       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    timestamp_human = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── DISTRICT PAGE ────────────────────────────────────────────────────
    district_html = load_page(DISTRICT_URL, f"district_warning_{STATE_ID}.png")
    save_snapshot("district_snapshot", district_html, timestamp)
    district_records = extract_district_records(district_html)

    # ── STATION PAGE ─────────────────────────────────────────────────────
    station_html = load_page(STATION_URL, f"station_warning_{STATE_ID}.png")
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

    # ── DISTRICT FILES (now includes issued_at / valid_upto) ─────────────
    save_json("district_warnings_latest.json", district_records, meta)
    save_csv(
        "district_warnings_latest.csv",
        district_records,
        meta,
        extra_fields=["issued_at", "valid_upto"],
    )

    # ── STATION FILES ─────────────────────────────────────────────────────
    save_json("station_warnings_latest.json", station_records, meta)
    save_csv(
        "station_warnings_latest.csv",
        station_records,
        meta,
        extra_fields=["issued_at", "valid_upto"],
    )

    # ── COMBINED FILES ────────────────────────────────────────────────────
    save_json("warnings_latest.json", all_records, meta)
    save_csv(
        "warnings_latest.csv",
        all_records,
        meta,
        extra_fields=["issued_at", "valid_upto"],
    )

    append_history(all_records, meta)

    # ── SEVERITY SUMMARY ──────────────────────────────────────────────────
    print("\n[scraper] Severity Summary")
    for label, records in [("District", district_records), ("Station", station_records)]:
        counts = Counter(r["severity"] for r in records)
        print(f"\n  {label}:")
        for severity, count in sorted(counts.items()):
            print(f"    {severity}: {count}")

    # ── ALERT CHECK & EMAIL ───────────────────────────────────────────────
    alert_info = check_alerts(district_records, station_records)

    if alert_info["should_alert"]:
        print("\n[scraper] 🚨 HIGH ALERT detected — sending email...")
        send_alert_email(alert_info, timestamp_human)
    else:
        print("\n[scraper] No high-severity alerts for monitored locations")

    print("\n[scraper] SUCCESS")


if __name__ == "__main__":
    main()
