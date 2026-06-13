"""
TenderHaraka — AI summarisation step (OPTIONAL but recommended)
===============================================================
Reads tenders from tenders.db that have no summary yet, sends the raw
notice text to Claude, and stores a plain-English summary plus any
deadline/value the AI can extract (marked ai_extracted=1 so you always
know which fields came from the source vs the AI).

Run AFTER scraper.py:
  export ANTHROPIC_API_KEY=sk-ant-...
  python summarise.py            # summarise everything pending
  python summarise.py --limit 20 # cap per run (cost control)

Cost guide: with claude-haiku, ~100 tenders ≈ a few US cents.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import anthropic

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "tenders.db"
MODEL    = "claude-3-5-haiku-latest"   # cheapest; switch to sonnet for quality

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("haraka.ai")

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

SYSTEM = """You summarise Kenyan public tender notices for small businesses.
Rules:
- Be factual. NEVER invent details that are not in the notice text.
- If a field is not stated in the text, return null for it.
- AGPO = reservations for Youth / Women / PWD enterprises. Only set agpo
  if the notice explicitly mentions a reservation or special group.
- Return ONLY valid JSON, no markdown fences, no commentary."""

PROMPT = """Summarise this Kenyan tender notice. Return JSON exactly like:
{{
  "summary": "2-3 plain-English sentences: what is being procured, by whom, scale if stated.",
  "deadline": "YYYY-MM-DD or null if not stated",
  "value_kes": "e.g. 'KES 5M' or null if not stated",
  "agpo": "Youth|Women|PWD|null",
  "entity": "procuring entity name or null"
}}

NOTICE TEXT:
{raw}
"""


def pending(limit: int) -> list[tuple]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id, title, raw_text, entity, deadline, value_kes, agpo
           FROM tenders WHERE summary = '' AND raw_text != ''
           ORDER BY first_seen DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def summarise_one(raw_text: str) -> dict | None:
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=500,
            system=SYSTEM,
            messages=[{"role": "user", "content": PROMPT.format(raw=raw_text[:4000])}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("Model returned non-JSON; skipping this tender.")
        return None
    except Exception as e:
        log.error("API error: %s", e)
        return None


def run(limit: int) -> None:
    rows = pending(limit)
    log.info("%d tenders pending summarisation", len(rows))
    done = 0
    for (tid, title, raw, entity, deadline, value, agpo) in rows:
        result = summarise_one(raw)
        if not result:
            continue
        conn = sqlite3.connect(DB_PATH)
        # Only fill fields the source did NOT already provide; mark AI-extracted.
        ai_extracted = 0
        new_deadline = deadline
        if not deadline and result.get("deadline"):
            new_deadline = result["deadline"]; ai_extracted = 1
        new_value = value
        if not value and result.get("value_kes"):
            new_value = result["value_kes"]; ai_extracted = 1
        new_agpo = agpo
        if not agpo and result.get("agpo"):
            new_agpo = result["agpo"]; ai_extracted = 1
        new_entity = entity
        if (not entity or entity.endswith("-listed entity")) and result.get("entity"):
            new_entity = result["entity"]

        conn.execute(
            """UPDATE tenders SET summary=?, deadline=?, value_kes=?, agpo=?,
                                  entity=?, ai_extracted=? WHERE id=?""",
            (result.get("summary","")[:600], new_deadline, new_value,
             new_agpo, new_entity, ai_extracted, tid),
        )
        conn.commit()
        conn.close()
        done += 1
        log.info("Summarised: %s", title[:60])
        time.sleep(1.2)   # gentle rate limit
    log.info("Done: %d summarised", done)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=50)
    args = p.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY first.")
    run(args.limit)
