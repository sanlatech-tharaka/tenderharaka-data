"""
TenderHaraka Scraper
====================
Collects Kenyan public tender notices from multiple official sources,
de-duplicates them, optionally AI-summarises them, and writes:

  1. tenders.db      — SQLite database (full history, dedup, alert state)
  2. tenders.json    — lightweight feed your static website fetches

Sources (in order of reliability):
  A. PPRA WordPress RSS  — ppra.go.ke (easiest, structured XML)
  B. tenders.go.ke PPIP  — official portal (tries JSON API, falls back to HTML)
  C. MyGov notices page  — mygov.go.ke weekly tender listings

Design principles:
  - Polite: identifies itself, rate-limits requests, obeys timeouts.
    All data scraped is PUBLIC procurement information published by the
    Government of Kenya for the express purpose of reaching suppliers.
  - Resilient: every source is wrapped so one failing portal never
    kills the run. Site layouts change; selectors are isolated per
    source so fixes are one-function jobs.
  - Honest: only fields actually found are stored. Nothing invented.
    Deadline/value left empty if not present (AI step may fill them
    from the notice text, marked as ai_extracted).

Usage:
  pip install -r requirements.txt
  python scraper.py                 # scrape all sources -> db + json
  python scraper.py --source ppra   # scrape one source only
  python scraper.py --json-only     # rebuild tenders.json from db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

# ─────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent
DB_PATH    = BASE_DIR / "tenders.db"
JSON_PATH  = BASE_DIR / "tenders.json"

USER_AGENT = "TenderHarakaBot/1.0 (Kenya tender aggregator; contact: hello@tenderharaka.co.ke)"
HEADERS    = {"User-Agent": USER_AGENT, "Accept-Language": "en-KE,en;q=0.9"}

REQUEST_DELAY_SECONDS = 2.0     # politeness delay between requests to the same host
REQUEST_TIMEOUT       = 25
MAX_JSON_TENDERS      = 60      # how many recent tenders the website feed carries

SOURCES = {
    "ppra":  "https://ppra.go.ke/category/tenders/feed/",
    "ppip":  "https://tenders.go.ke",
    "mygov": "https://www.mygov.go.ke/all-jobs-tenders",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(BASE_DIR / "scraper.log")],
)
log = logging.getLogger("haraka")


# ─────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    id           TEXT PRIMARY KEY,        -- md5 of title+url
    title        TEXT NOT NULL,
    entity       TEXT DEFAULT '',
    category     TEXT DEFAULT '',
    county       TEXT DEFAULT '',
    deadline     TEXT DEFAULT '',         -- ISO date if known, else ''
    value_kes    TEXT DEFAULT '',
    agpo         TEXT DEFAULT '',         -- '', 'Youth', 'Women', 'PWD'
    url          TEXT NOT NULL,
    source       TEXT NOT NULL,
    raw_text     TEXT DEFAULT '',
    summary      TEXT DEFAULT '',         -- filled by AI step (optional)
    ai_extracted INTEGER DEFAULT 0,       -- 1 if deadline/value came from AI
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    alerted      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tenders_first_seen ON tenders(first_seen);
CREATE INDEX IF NOT EXISTS idx_tenders_deadline   ON tenders(deadline);
"""

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def tender_id(title: str, url: str) -> str:
    return hashlib.md5(f"{title.strip().lower()}|{url.strip()}".encode()).hexdigest()


def upsert_tender(t: dict) -> bool:
    """Insert if new; update last_seen if already known. Returns True if NEW."""
    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    try:
        existing = conn.execute(
            "SELECT id FROM tenders WHERE id=?", (t["id"],)
        ).fetchone()
        if existing:
            conn.execute("UPDATE tenders SET last_seen=? WHERE id=?", (now, t["id"]))
            conn.commit()
            return False
        conn.execute(
            """INSERT INTO tenders
               (id, title, entity, category, county, deadline, value_kes, agpo,
                url, source, raw_text, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t["id"], t["title"], t.get("entity",""), t.get("category",""),
                t.get("county",""), t.get("deadline",""), t.get("value_kes",""),
                t.get("agpo",""), t["url"], t["source"], t.get("raw_text",""),
                now, now,
            ),
        )
        conn.commit()
        log.info("NEW [%s] %s", t["source"], t["title"][:70])
        return True
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

_session = requests.Session()
_session.headers.update(HEADERS)
_last_request_at: dict[str, float] = {}

def polite_get(url: str, **kwargs) -> requests.Response | None:
    """GET with per-host politeness delay and unified error handling."""
    host = re.sub(r"^https?://", "", url).split("/")[0]
    elapsed = time.time() - _last_request_at.get(host, 0)
    if elapsed < REQUEST_DELAY_SECONDS:
        time.sleep(REQUEST_DELAY_SECONDS - elapsed)
    try:
        resp = _session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
        _last_request_at[host] = time.time()
        resp.raise_for_status()
        return resp
    except Exception as e:
        log.warning("GET failed %s — %s", url, e)
        return None


AGPO_PATTERNS = [
    (re.compile(r"\byouth\b", re.I), "Youth"),
    (re.compile(r"\bwomen\b", re.I), "Women"),
    (re.compile(r"persons?\s+with\s+disabilit|PWD", re.I), "PWD"),
]

def detect_agpo(text: str) -> str:
    """Tag AGPO type only when the notice text actually mentions a reservation."""
    if not re.search(r"reserv|AGPO|special group", text, re.I):
        return ""
    for pattern, label in AGPO_PATTERNS:
        if pattern.search(text):
            return label
    return ""


DEADLINE_PATTERNS = [
    # "closing date: 15th June 2026", "deadline 15/06/2026", "on or before 15 June, 2026"
    re.compile(r"(?:closing|deadline|close|on or before|submission)[:\s]*"
               r"(?:date[:\s]*)?"
               r"(\d{1,2}(?:st|nd|rd|th)?[\s/.-]+\w+[\s/.-]+\d{4})", re.I),
    re.compile(r"(\d{1,2}/\d{1,2}/\d{4})"),
]

def extract_deadline(text: str) -> str:
    """Best-effort deadline extraction from notice text. Returns '' if unsure."""
    for pattern in DEADLINE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(1)
        # Normalise: strip ordinal suffixes, unify separators to single spaces
        cleaned = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", raw, flags=re.I)
        cleaned = re.sub(r"[\s/.,-]+", " ", cleaned).strip()
        for fmt in ("%d %m %Y", "%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(cleaned, fmt).date().isoformat()
            except ValueError:
                continue
        return ""   # found text but couldn't parse confidently -> leave empty
    return ""


KNOWN_COUNTIES = [
    "Nairobi","Mombasa","Kisumu","Nakuru","Kiambu","Machakos","Kajiado","Uasin Gishu",
    "Meru","Nyeri","Kakamega","Kilifi","Bungoma","Turkana","Garissa","Embu","Kericho",
    "Kisii","Homa Bay","Migori","Siaya","Busia","Vihiga","Nandi","Bomet","Narok",
    "Laikipia","Nyandarua","Murang'a","Kirinyaga","Tharaka Nithi","Kitui","Makueni",
    "Taita Taveta","Kwale","Tana River","Lamu","Wajir","Mandera","Marsabit","Isiolo",
    "Samburu","West Pokot","Baringo","Elgeyo Marakwet","Trans Nzoia","Nyamira",
]

def detect_county(text: str) -> str:
    for county in KNOWN_COUNTIES:
        if re.search(rf"\b{re.escape(county)}\b", text, re.I):
            return county
    return ""


CATEGORY_KEYWORDS = {
    "ICT":                ["ict", "computer", "software", "network", "laptop", "server", "digital", "system"],
    "Construction/Works": ["construction", "building", "works", "road", "renovation", "civil", "drilling", "borehole"],
    "Supply of Goods":    ["supply", "delivery", "provision of goods", "furniture", "equipment", "stationery", "uniform"],
    "Consultancy":        ["consultancy", "consulting", "advisory", "study", "strategic plan", "feasibility"],
    "Healthcare":         ["medical", "pharmaceutical", "hospital", "health", "drugs", "laboratory"],
    "Security":           ["security", "guarding", "cctv", "surveillance"],
    "Catering":           ["catering", "food", "meals", "canteen"],
    "Cleaning":           ["cleaning", "sanitation", "fumigation", "garbage", "waste"],
    "Insurance":          ["insurance", "underwriting", "medical cover"],
    "Transport":          ["transport", "vehicle", "motor", "fleet", "hire of"],
}

def detect_category(text: str) -> str:
    lc = text.lower()
    best, best_hits = "", 0
    for cat, kws in CATEGORY_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw in lc)
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


def enrich(t: dict) -> dict:
    """Fill category / county / agpo / deadline from the raw text, rule-based."""
    blob = " ".join([t.get("title",""), t.get("raw_text",""), t.get("entity","")])
    t.setdefault("category", "") or t.update(category=detect_category(blob))
    t.setdefault("county", "")   or t.update(county=detect_county(blob))
    t.setdefault("agpo", "")     or t.update(agpo=detect_agpo(blob))
    if not t.get("deadline"):
        t["deadline"] = extract_deadline(blob)
    return t


# ─────────────────────────────────────────────────────────────────
# Source A — PPRA (WordPress RSS)
# ─────────────────────────────────────────────────────────────────

def scrape_ppra() -> list[dict]:
    log.info("Source A: PPRA RSS …")
    tenders = []

    if HAS_FEEDPARSER:
        feed = feedparser.parse(SOURCES["ppra"], request_headers=HEADERS)
        entries = feed.entries
    else:
        # Manual XML fallback if feedparser missing
        resp = polite_get(SOURCES["ppra"])
        if not resp:
            return []
        soup = BeautifulSoup(resp.content, "xml")
        entries = []
        for item in soup.find_all("item"):
            entries.append(type("E", (), {
                "title": item.title.text if item.title else "",
                "link": item.link.text if item.link else "",
                "summary": item.description.text if item.description else "",
            })())

    for e in entries:
        title = getattr(e, "title", "").strip()
        link  = getattr(e, "link", "").strip()
        if not title or not link:
            continue
        raw = BeautifulSoup(getattr(e, "summary", ""), "html.parser").get_text(" ", strip=True)
        tenders.append(enrich({
            "id":       tender_id(title, link),
            "title":    title,
            "entity":   "PPRA-listed entity",
            "url":      link,
            "source":   "ppra.go.ke",
            "raw_text": raw[:3000],
        }))
    log.info("PPRA: %d notices", len(tenders))
    return tenders


# ─────────────────────────────────────────────────────────────────
# Source B — tenders.go.ke (PPIP)
# ─────────────────────────────────────────────────────────────────
# The PPIP portal is a JS app backed by an API. Endpoints have changed
# over time; we try the known API paths first, then fall back to HTML.
# If everything fails, the run continues with other sources.

PPIP_API_CANDIDATES = [
    "/api/active-tenders?perpage=50&page=1",
    "/api/tenders?status=open&per_page=50",
    "/tenders?format=json",
]

def scrape_ppip() -> list[dict]:
    log.info("Source B: tenders.go.ke …")
    base = SOURCES["ppip"]

    # 1) Try JSON API endpoints
    for path in PPIP_API_CANDIDATES:
        resp = polite_get(base + path, headers={**HEADERS, "Accept": "application/json"})
        if not resp:
            continue
        try:
            data = resp.json()
        except ValueError:
            continue
        items = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(items, list) or not items:
            continue

        tenders = []
        for it in items:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or it.get("description") or it.get("tender_description") or "").strip()
            if not title:
                continue
            ref   = str(it.get("tender_no") or it.get("reference") or it.get("id") or "")
            url   = f"{base}/tenders/{it.get('id','')}" if it.get("id") else base
            tenders.append(enrich({
                "id":        tender_id(title, url or ref),
                "title":     title,
                "entity":    str(it.get("procuring_entity") or it.get("entity") or it.get("pe_name") or ""),
                "deadline":  str(it.get("closing_date") or it.get("close_at") or "")[:10],
                "value_kes": str(it.get("tender_value") or ""),
                "url":       url,
                "source":    "tenders.go.ke",
                "raw_text":  json.dumps(it, default=str)[:3000],
            }))
        log.info("PPIP API (%s): %d tenders", path, len(tenders))
        return tenders

    # 2) HTML fallback
    log.info("PPIP API unavailable, trying HTML …")
    resp = polite_get(base + "/tenders")
    if not resp:
        log.warning("PPIP unreachable this run — skipping (other sources continue).")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    tenders = []
    for row in soup.select("table tbody tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 2:
            continue
        a = row.find("a", href=True)
        url = a["href"] if a else base
        if url.startswith("/"):
            url = base + url
        title = max(cells, key=len)  # longest cell is almost always the description
        tenders.append(enrich({
            "id":       tender_id(title, url),
            "title":    title,
            "entity":   cells[0] if cells[0] != title else "",
            "url":      url,
            "source":   "tenders.go.ke",
            "raw_text": " | ".join(cells)[:3000],
        }))
    log.info("PPIP HTML: %d tenders", len(tenders))
    return tenders


# ─────────────────────────────────────────────────────────────────
# Source C — MyGov notices
# ─────────────────────────────────────────────────────────────────

def scrape_mygov() -> list[dict]:
    log.info("Source C: MyGov …")
    resp = polite_get(SOURCES["mygov"])
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select(".views-row") or soup.select("article") or soup.select(".node")
    tenders = []
    for item in items[:40]:
        title_el = item.select_one("h2, h3, .field--name-title, a")
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        if len(title) < 15:                       # skip nav fragments
            continue
        a = item.find("a", href=True)
        url = a["href"] if a else SOURCES["mygov"]
        if url.startswith("/"):
            url = "https://www.mygov.go.ke" + url
        tenders.append(enrich({
            "id":       tender_id(title, url),
            "title":    title,
            "entity":   "MyGov-listed entity",
            "url":      url,
            "source":   "mygov.go.ke",
            "raw_text": item.get_text(" ", strip=True)[:3000],
        }))
    log.info("MyGov: %d notices", len(tenders))
    return tenders


# ─────────────────────────────────────────────────────────────────
# JSON feed for the website
# ─────────────────────────────────────────────────────────────────

def export_json() -> int:
    """Write tenders.json — the feed the static website fetches.
    Only includes fields the board needs. Honest by construction:
    empty fields stay empty rather than being guessed."""
    conn = db()
    rows = conn.execute(
        """SELECT title, entity, category, county, deadline, agpo, url, source, summary, first_seen
           FROM tenders
           WHERE deadline = '' OR deadline >= date('now')
           ORDER BY first_seen DESC
           LIMIT ?""",
        (MAX_JSON_TENDERS,),
    ).fetchall()
    conn.close()

    feed = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_note": "Public procurement notices aggregated from official Government of Kenya portals.",
        "tenders": [
            {
                "title":    r[0],
                "entity":   r[1],
                "category": r[2],
                "county":   r[3],
                "deadline": r[4],          # '' means: deadline not stated in source
                "agpo":     r[5],          # '' means: no reservation detected
                "url":      r[6],
                "source":   r[7],
                "summary":  r[8],
                "first_seen": r[9],
            }
            for r in rows
        ],
    }
    JSON_PATH.write_text(json.dumps(feed, indent=2, ensure_ascii=False))
    log.info("Exported %d tenders -> %s", len(feed["tenders"]), JSON_PATH.name)
    return len(feed["tenders"])


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

SCRAPERS = {"ppra": scrape_ppra, "ppip": scrape_ppip, "mygov": scrape_mygov}

def run(only_source: str | None = None, json_only: bool = False) -> None:
    if json_only:
        export_json()
        return

    new_count = 0
    for name, fn in SCRAPERS.items():
        if only_source and name != only_source:
            continue
        try:
            for t in fn():
                if upsert_tender(t):
                    new_count += 1
        except Exception as e:
            log.error("Source %s crashed: %s — continuing with others", name, e)

    total = db().execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    log.info("Run complete: %d new tenders (db total: %d)", new_count, total)
    export_json()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="TenderHaraka scraper")
    p.add_argument("--source", choices=SCRAPERS.keys(), help="scrape one source only")
    p.add_argument("--json-only", action="store_true", help="rebuild tenders.json from db")
    args = p.parse_args()
    run(only_source=args.source, json_only=args.json_only)
