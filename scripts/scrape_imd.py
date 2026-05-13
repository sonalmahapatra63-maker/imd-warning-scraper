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
# Districts that trigger an email on Alert or Warning
ALERT_DISTRICTS = {"KHORDHA", "CUTTACK"}

# Stations that trigger an email (with their own severity thresholds below)
ALERT_STATIONS = {
    "Bhubaneshwar AP",
    "Cuttack",
    "Khordha",
    "Bhubaneshwar OUAT",
}

# Severity levels that trigger an email for districts
ALERT_SEVERITIES_DISTRICT = {"Alert", "Warning"}   # orange + red

# Bhubaneshwar AP / OUAT also trigger on Watch (yellow)
ALERT_STATIONS_WATCH = {"Bhubaneshwar AP", "Bhubaneshwar OUAT"}
ALERT_SEVERITIES_STATION_WATCH = {"Watch", "Alert", "Warning"}  # yellow + orange + red

# All other stations (Cuttack, Khordha) trigger on Alert / Warning only
ALERT_SEVERITIES_STATION = {"Alert", "Warning"}    # orange + red

# ── Email config (read from GitHub Actions secrets / env vars) ─
GMAIL_FROM    = os.getenv("GMAIL_FROM", "")
GMAIL_PASS    = os.getenv("GMAIL_APP_PASSWORD", "")
EMAIL_TO      = os.getenv("ALERT_EMAIL_TO", "")

# ── Supabase config ──────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────
# CLEAN OLD FILES  (keep .gitkeep, history, and PNGs)
# PNGs are overwritten in-place by load_page() — no delete needed.
# ─────────────────────────────────────────────────────────────

def clean_old_files():

    print("\n[scraper] Cleaning old files...")

    patterns = ["*.html", "*.csv", "*.json"]

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
    return HEX_TO_COLOR_NAME.get(normalize_color(hex_color), hex_color)


def marker_filename_to_color(href: str) -> str:
    match = re.search(r"map-marker-icon-png-(\w+)\.png", href, re.IGNORECASE)
    if not match:
        return ""
    colour_word = match.group(1).lower()
    return MARKER_FILENAME_TO_HEX.get(colour_word, "")


def parse_station_aria_label(aria_label: str):
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
# Screenshot uses a fixed filename so it is overwritten each run
# (git always sees it as changed and commits it).
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

        # Always write to a fixed filename so git detects it as modified
        screenshot_path = DATA_DIR / screenshot_name
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[scraper] Screenshot saved: {screenshot_path}")

        html = page.content()
        browser.close()

    return html


# ─────────────────────────────────────────────────────────────
# EXTRACTION
# ─────────────────────────────────────────────────────────────

def extract_district_records(html: str) -> list:
    """
    Extract district warning records.
    issued_at and valid_upto are populated later from station data
    (see enrich_district_issued_at).
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
# FIX: Copy issued_at / valid_upto from station → district
# Strategy: take the most common issued_at + valid_upto from
# station records and apply it to all district rows (since the
# district page does not carry time info itself).
# ─────────────────────────────────────────────────────────────

def enrich_district_issued_at(district_records: list, station_records: list) -> list:
    """
    Populate issued_at and valid_upto on district records using
    the consensus value from station records.
    """

    station_issued = [r["issued_at"] for r in station_records if r.get("issued_at")]
    station_valid  = [r["valid_upto"] for r in station_records if r.get("valid_upto")]

    if not station_issued:
        print("[scraper] No station issued_at found — district columns stay empty")
        return district_records

    # Use the most common value (handles edge-case where a few stations differ)
    common_issued = Counter(station_issued).most_common(1)[0][0]
    common_valid  = Counter(station_valid).most_common(1)[0][0] if station_valid else ""

    print(f"[scraper] Enriching districts: issued_at={common_issued}  valid_upto={common_valid}")

    for r in district_records:
        r["issued_at"]  = common_issued
        r["valid_upto"] = common_valid

    return district_records


# ─────────────────────────────────────────────────────────────
# ALERT CHECK
# ─────────────────────────────────────────────────────────────

def check_alerts(district_records: list, station_records: list) -> dict:

    # KHORDHA or CUTTACK at Alert / Warning level
    triggered_districts = [
        r for r in district_records
        if r["name"].upper() in ALERT_DISTRICTS
        and r["severity"] in ALERT_SEVERITIES_DISTRICT
    ]

    # Bhubaneshwar AP / OUAT: Watch + Alert + Warning
    # Cuttack / Khordha stations: Alert + Warning only
    triggered_stations = [
        r for r in station_records
        if r["name"] in ALERT_STATIONS
        and (
            (
                r["name"] in ALERT_STATIONS_WATCH
                and r["severity"] in ALERT_SEVERITIES_STATION_WATCH
            )
            or (
                r["name"] not in ALERT_STATIONS_WATCH
                and r["severity"] in ALERT_SEVERITIES_STATION
            )
        )
    ]

    return {
        "should_alert":   bool(triggered_districts or triggered_stations),
        "districts":      triggered_districts,
        "stations":       triggered_stations,
    }


# ─────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────

def _alert_rows_to_html_table(records: list, title: str) -> str:
    """Render a list of alert records as a styled HTML table section."""
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
            f"<td style='padding:6px 10px;border-bottom:1px solid #ddd;'>{r['name']}</td>"
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


def build_email_plain(alert_info: dict, timestamp_human: str) -> str:
    lines = [
        f"IMD Warning Alert — {timestamp_human}",
        "=" * 60,
        "",
        "⚠️  High-severity warnings detected:",
        "",
    ]
    for section_label, section_records in [
        ("DISTRICT ALERTS", alert_info["districts"]),
        ("STATION ALERTS",  alert_info["stations"]),
    ]:
        if section_records:
            lines.append(section_label)
            lines.append("-" * 40)
            for r in section_records:
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
        "Attachments: district map PNG, station map PNG",
    ]
    return "\n".join(lines)


def build_email_html(alert_info: dict, timestamp_human: str) -> str:
    district_table = _alert_rows_to_html_table(alert_info["districts"], "🗺️ District Alerts")
    station_table  = _alert_rows_to_html_table(alert_info["stations"],  "📍 Station Alerts")

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:0;">
  <div style="max-width:680px;margin:20px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 8px rgba(0,0,0,.12);overflow:hidden;">

    <!-- Header -->
    <div style="background:#b30000;padding:18px 24px;">
      <h2 style="margin:0;color:#fff;font-size:18px;">
        🚨 IMD HIGH ALERT &nbsp;—&nbsp; {timestamp_human}
      </h2>
    </div>

    <!-- Body -->
    <div style="padding:20px 24px;">
      <p style="margin:0 0 12px;color:#333;">
        High-severity weather warnings have been detected for monitored locations
        in Odisha. Details are shown below.
      </p>

      {district_table}
      {station_table}

      <hr style="margin:24px 0;border:none;border-top:1px solid #eee;">
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
        print("[scraper] Email env vars not set — skipping email alert")
        return

    recipients = [addr.strip() for addr in EMAIL_TO.split(",") if addr.strip()]

    subject = f"🚨 IMD HIGH ALERT — {timestamp_human}"

    # ── Build multipart/alternative message (plain + HTML) ───────────────
    msg = MIMEMultipart("mixed")
    msg["From"]    = GMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject

    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(build_email_plain(alert_info, timestamp_human), "plain"))
    alt_part.attach(MIMEText(build_email_html(alert_info, timestamp_human),  "html"))
    msg.attach(alt_part)

    # ── Attachments: 2 PNGs only ─────────────────────────────────────────
    attachments = [
        DATA_DIR / f"district_warning_{STATE_ID}.png",
        DATA_DIR / f"station_warning_{STATE_ID}.png",
    ]

    for attach_path in attachments:
        if not attach_path.exists():
            print(f"[scraper] Attachment not found (skipping): {attach_path.name}")
            continue
        with open(attach_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{attach_path.name}"',
        )
        msg.attach(part)
        print(f"[scraper] Attached: {attach_path.name}")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_FROM, GMAIL_PASS)
            server.sendmail(GMAIL_FROM, recipients, msg.as_string())
        print(f"[scraper] Alert email sent to: {', '.join(recipients)}")
    except Exception as e:
        print(f"[scraper] ERROR sending email: {e}")


# ─────────────────────────────────────────────────────────────
# SUPABASE UPLOAD
# Clears both tables (state_id-scoped) then inserts fresh rows.
# Tables always reflect the current IMD snapshot.
# ─────────────────────────────────────────────────────────────

def build_supabase_rows(records: list, meta: dict) -> list:
    rows = []
    for r in records:
        rows.append({
            "scraped_at":    meta["scraped_at"],
            "state_id":      meta["state_id"],
            "type":          r.get("type", ""),
            "name":          r.get("name", ""),
            "warning_color": r.get("warning_color", ""),
            "severity":      r.get("severity", ""),
            "issued_at":     r.get("issued_at") or None,
            "valid_upto":    r.get("valid_upto") or None,
        })
    return rows


def upload_to_supabase(district_records: list, station_records: list, meta: dict):
    """
    Clear both tables, then insert fresh rows for this scrape run.
    Each run represents the current live IMD snapshot — no stale rows.
    Requires DELETE permission on both tables in Supabase RLS policy.
    """

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
            # ── Step 1: Clear existing rows ───────────────────────────────
            # Filter on state_id so only this state's rows are removed;
            # safe to run even if table is already empty.
            sb.table(table).delete().eq("state_id", STATE_ID).execute()
            print(f"[scraper] Supabase: cleared existing rows in '{table}' for state_id={STATE_ID}")

            # ── Step 2: Insert fresh rows ─────────────────────────────────
            rows = build_supabase_rows(records, meta)

            if not rows:
                print(f"[scraper] Supabase: no rows to insert for '{table}'")
                continue

            batch_size = 100
            total_inserted = 0
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                sb.table(table).insert(batch).execute()
                total_inserted += len(batch)

            print(f"[scraper] Supabase: inserted {total_inserted} rows into '{table}'")

        except Exception as e:
            print(f"[scraper] Supabase ERROR on table '{table}': {e}")


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

    # 1. Delete old output files (PNGs are excluded — overwritten in-place)
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

    # ── FIX: Copy issued_at / valid_upto from station → district ─────────
    district_records = enrich_district_issued_at(district_records, station_records)

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

    # ── DISTRICT FILES ────────────────────────────────────────────────────
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

    # ── SUPABASE UPLOAD (clear + insert fresh snapshot) ──────────────────
    upload_to_supabase(district_records, station_records, meta)

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
