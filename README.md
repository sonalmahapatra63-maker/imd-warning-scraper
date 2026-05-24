# IMD Warning Scraper — TPCODL Districts

Automated scraper for [IMD Nowcast Warnings](https://mausam.imd.gov.in/) (Odisha — State ID 10).  
Monitors all **8 TPCODL coverage districts**, runs every 15 minutes via GitHub Actions + Supabase pg_cron, saves data to CSV, uploads to Supabase, and sends a structured Gmail alert (with PDF report) when any district escalates to **orange (Alert)** or **red (Warning)**.

---

## 📁 Repository Structure

```
imd-warning-scraper/
├── .github/
│   └── workflows/
│       └── scrape_imd.yml          # GitHub Actions workflow
├── data/                           # Auto-generated output (committed each run)
│   ├── district_warnings_latest.csv
│   ├── district_warning_10.png     # Screenshot — full district map
│   └── district_hover_<name>_10.png  # Hover screenshots (escalation runs only)
├── scripts/
│   └── scrape_imd.py               # Main scraper
├── requirements.txt
└── README.md
```

---

## 📊 Data Schema

### Supabase table: `district_warnings`

| Column | Type | Description |
|---|---|---|
| `scraped_at` | text | UTC timestamp, e.g. `2026-05-19 09:03 UTC` |
| `state_id` | int4 | IMD state ID (always `10` for Odisha) |
| `type` | text | Always `district` |
| `name` | text | District name in CAPS, e.g. `KHORDHA` |
| `warning_color` | text | `green` / `yellow` / `orange` / `red` |
| `severity` | text | `No Warning` / `Watch` / `Alert` / `Warning` |
| `issued_at` | text | Warning issue time, e.g. `2026-05-19 1900` |
| `valid_upto` | text | Valid until time in Hrs, e.g. `2200` |
| `balloon_text` | text | Raw balloon hover text from IMD map |
| `rain_description` | text | Full rain line extracted from balloon |
| `thunderstorm_desc` | text | Full thunderstorm/wind line from balloon |
| `lightning_probability` | text | Full lightning line from balloon |

> **Note:** `issued_at` and weather fields are populated from balloon hover extraction (not the district map HTML), which gives accurate real-time values.

### Severity / Colour mapping

| Color | Severity | Rank | Action |
|---|---|---|---|
| 🟢 `green` | No Warning | 0 | No adverse weather |
| 🟡 `yellow` | Watch | 1 | Be updated |
| 🟠 `orange` | Alert | 2 | Be prepared — **email triggered** |
| 🔴 `red` | Warning | 3 | Take action — **email triggered** |

---

## 🏭 Monitored Districts (TPCODL Coverage)

| District | Circle(s) | Divisions |
|---|---|---|
| ANUGUL | DHENKANAL | ANED ANGUL, TED CHAINPAL |
| CUTTACK | CUTTACK, BBSR-1, BBSR-2 | AED ATHAGARH, BCDD-II BBSR, CDD-I/II Cuttack, CED Cuttack, KHED Khurda, SED SALIPUR |
| DHENKANAL | DHENKANAL | DED DHENKANAL, TED CHAINPAL |
| JAGATSINGHPUR | PARADEEP | JED JAGATSINGHPUR, PAED PARADEEP |
| KENDRAPARA | PARADEEP | KED-I KENDRAPARA, KED-II MARSHAGHAI |
| KHORDHA | BBSR1, BBSR2 | BCDD-I/II BBSR, BED BBSR, KHED KHORDHA, NYED NAYAGARH, NED NIMAPARA, BAED BALUGAON |
| NAYAGARH | BBSR2 | NYED NAYAGARH, KHED KHORDHA, BAED BALUGAON |
| PURI | BBSR2, BBSR1 | PED PURI, BED BBSR, KHED KHORDHA, NED NIMAPARA |

---

## ⏰ Schedule

The scraper runs **every 15 minutes** via dual-repo redundancy triggered by Supabase pg_cron:

| Repo | Account | Trigger times (every hour) |
|---|---|---|
| `imd-warning-scraper` | sonalmahapatra63-maker | :03 and :33 |
| `imd-scraper-b` | second account | :18 and :48 |

Both repos run identical code and write all 8 rows to the same Supabase table on every run.

```
Supabase pg_cron
  ├── imd-repo-a  → :03 and :33 every hour
  └── imd-repo-b  → :18 and :48 every hour
          ↓
  Supabase Edge Function: trigger-imd-scraper
          ↓
  GitHub Actions: workflow_dispatch → scrape_imd.py
```

You can also trigger a manual run anytime from **Actions → Run workflow**.

---

## 🗄️ Supabase Setup

### One-time table creation

```sql
CREATE TABLE district_warnings (
    id                    bigserial PRIMARY KEY,
    scraped_at            text,
    state_id              int4,
    type                  text,
    name                  text,
    warning_color         text,
    severity              text,
    issued_at             text,
    valid_upto            text,
    balloon_text          text,
    rain_description      text,
    thunderstorm_desc     text,
    lightning_probability text,
    UNIQUE (state_id, name)
);
```

> The `UNIQUE(state_id, name)` constraint is required — the scraper uses upsert on conflict to update all 8 rows every run.

### Add missing columns (if upgrading from an older schema)

```sql
ALTER TABLE district_warnings
  ADD COLUMN IF NOT EXISTS rain_description      text,
  ADD COLUMN IF NOT EXISTS thunderstorm_desc     text,
  ADD COLUMN IF NOT EXISTS lightning_probability text,
  ADD COLUMN IF NOT EXISTS balloon_text          text;
```

### Useful queries

```sql
-- Current state of all 8 TPCODL districts
SELECT name, warning_color, severity, issued_at, valid_upto,
       rain_description, thunderstorm_desc, lightning_probability, scraped_at
FROM district_warnings WHERE state_id = 10 ORDER BY name;

-- Manually set a district to orange (to test normalisation email)
UPDATE district_warnings
SET warning_color = 'orange', severity = 'Alert'
WHERE state_id = 10 AND name = 'PURI';

-- Check scraper trigger log
SELECT trigger_type, status, http_code, notes, triggered_at
FROM scraper_trigger_log ORDER BY triggered_at DESC LIMIT 10;

-- Check pg_cron jobs
SELECT jobid, jobname, schedule FROM cron.job;
```

---

## 🚨 Email Alerts

### Alert email (escalation)

Sent when any district moves **from a lower rank to orange or red** compared to the previous scan.

**Contents:**
- IMD-style header (red for WARNING, orange for ALERT) with valid time range
- Scan summary bar (scanned at · active count · escalation count · next refresh)
- 3-column district cards for every currently active district, sorted red → orange, new before existing. Each card shows: time range, severity (with ↑ NEW badge if newly escalated), rain, wind, lightning, circle, divisions
- Kalabaisakhi timing summary box with start/end times per active district
- IMD-branded footer

**Attachments:**
- `district_warning_10.png` — full district overview map
- `district_hover_<name>_10.png` — one per escalated district (balloon visible)
- `IMD_TPCODL_Report_<HHMM>IST.pdf` — structured PDF report containing:
  - Page 1: Summary (IMD header + count strip + 3 tables: status, circles/divisions, weather details)
  - Page 2: Full district overview map screenshot
  - Page 3+: One hover screenshot per escalated district

**Subject format:**
```
IMD [WARNING] WARNING -- Khordha -- 2 Circles, 7 Divisions Affected
IMD [ALERT] ALERT -- Puri, Nayagarh -- 3 Circles, 7 Divisions Affected
```

### All-clear email (normalisation)

Sent when any district drops **from orange/red to yellow or green** compared to the previous scan.

**Contents:**
- Green header with check-circle icon and "IMD NOWCAST — ALL CLEAR (TPCODL)"
- Explanation paragraph
- Downgrade cards per district showing Was → Now severity badge

**Attachments:**
- `IMD_TPCODL_AllClear_<HHMM>IST.pdf` — same summary page structure with green header; overview map only (no hover screenshots)

**Subject format:**
```
IMD [RESOLVED] -- Puri (Alert->No Warning)
IMD [DOWNGRADED] -- Nayagarh (Alert->Watch)
```

> Both emails can fire in the **same run** independently if different districts escalate and normalise simultaneously.

---

## 🔐 GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|---|---|
| `GMAIL_FROM` | Your Gmail address, e.g. `you@gmail.com` |
| `GMAIL_APP_PASSWORD` | 16-character Gmail App Password (not your login password) |
| `ALERT_EMAIL_TO` | Recipient(s), comma-separated, e.g. `a@x.com,b@y.com` |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | **Service role key** (not anon/publishable key) — required for upsert |

> ⚠️ Both repos must use the **service_role key**, not the anon key. The anon key does not have permission to upsert all rows.

### How to create a Gmail App Password

1. Enable **2-Step Verification** at [myaccount.google.com/security](https://myaccount.google.com/security)
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. App name: `IMD Scraper` → click **Create**
4. Copy the 16-character password and save it as `GMAIL_APP_PASSWORD`

---

## 📦 Dependencies

```
playwright>=1.53.0
beautifulsoup4==4.12.3
lxml==5.2.2
supabase==2.10.0
reportlab>=4.0.0
```

Install locally:
```bash
pip install -r requirements.txt
python -m playwright install chromium
```

---

## 🚀 Running Locally

```bash
# Clone the repo
git clone https://github.com/sonalmahapatra63-maker/imd-warning-scraper.git
cd imd-warning-scraper

# Install dependencies
pip install -r requirements.txt
python -m playwright install chromium

# Set environment variables
export IMD_STATE_ID=10
export GMAIL_FROM=you@gmail.com
export GMAIL_APP_PASSWORD=your_app_password
export ALERT_EMAIL_TO=recipient@example.com
export SUPABASE_URL=https://odrvhelastdyozjejqss.supabase.co
export SUPABASE_KEY=your_service_role_key

# Run
python scripts/scrape_imd.py
```

Output files will be saved to the `data/` folder.

---

## 📝 Key Design Decisions

| Rule | Reason |
|---|---|
| Email sent **before** Supabase upsert | Escalation check uses the previous run's data — if upsert ran first, there would be nothing to escalate |
| Always upsert **all 8 rows** every run | Prevents stale state causing repeat alert emails on subsequent runs |
| PDF built **in-memory only** (io.BytesIO) | Never written to disk or committed to git — zero repo storage cost |
| `state_id` cast to `int()` everywhere | Supabase column is int4 — string comparison fails silently |
| IST system clock for email timestamps | Reported-at shown in IST for readability; scraped_at stored in UTC |
| Hover screenshots only on escalation | Second Playwright browser is expensive — not needed for normalisation |
| `UNIQUE(state_id, name)` constraint | Enables safe upsert without duplicate rows accumulating |

---

## 🐛 Known Limitations & Planned Features

| Item | Status |
|---|---|
| Duplicate email guard (both repos read prev=green before either writes) | Planned — add `last_alerted_at` column, skip if alerted < 10 min ago |
| Midnight boundary display (valid_upto < issued_at time) | Planned — detect and append "(next day)" label |
| Telegram Bot integration (free alternative to WhatsApp group alerts) | Planned — ~15 lines using `requests`, add `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` secrets |
