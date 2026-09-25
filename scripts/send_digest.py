#!/usr/bin/env python3
"""
Oedi Field — daily WhatsApp digest via CallMeBot.

Runs after the crawl each day. Reads data/digest_schedule.json to find
today's category (by weekday in India time), picks the most recent items
in that category — one per source, up to items_per_digest — and sends
them as a single WhatsApp message through CallMeBot's free API.

Required repository secrets:
  CALLMEBOT_PHONE   your WhatsApp number with country code, digits only
                    (e.g. 919876543210 for +91 98765 43210)
  CALLMEBOT_APIKEY  the key CallMeBot sends you after one-time opt-in

If either secret is missing, the script exits quietly without failing the
workflow, so the crawl keeps running even before WhatsApp is set up.
"""

import json
import os
import sys
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(ROOT, "data", "index.json")
SCHEDULE_PATH = os.path.join(ROOT, "data", "digest_schedule.json")

# CallMeBot rejects very long messages; keep a safe margin.
MAX_MESSAGE_CHARS = 1500
SUMMARY_CHARS = 110


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def pick_items(index, category, count, distinct_sources):
    """Newest-first items in this category, at most one per source."""
    matches = [i for i in index if (i.get("category") or "").lower() == category.lower()]
    matches.sort(key=lambda i: i.get("indexed_at", ""), reverse=True)

    picked, seen_sources = [], set()
    for item in matches:
        if len(picked) >= count:
            break
        src = item.get("source", "")
        if distinct_sources and src in seen_sources:
            continue
        seen_sources.add(src)
        picked.append(item)
    return picked


def trim(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def build_message(day_name, category, items):
    header = f"*Oedi Field* · {day_name.capitalize()} · {category}"
    if not items:
        return f"{header}\n\nNo new {category} items in the archive yet. Tomorrow's category will run as usual."

    blocks = [header, ""]
    for n, item in enumerate(items, 1):
        blocks.append(f"{n}. *{trim(item.get('title'), 90)}*")
        if item.get("summary"):
            blocks.append(trim(item["summary"], SUMMARY_CHARS))
        blocks.append(f"— {item.get('source', '')}")
        blocks.append(item.get("link", ""))
        blocks.append("")

    message = "\n".join(blocks).strip()
    if len(message) > MAX_MESSAGE_CHARS:
        # Drop summaries rather than items if the message runs long.
        blocks = [header, ""]
        for n, item in enumerate(items, 1):
            blocks.append(f"{n}. *{trim(item.get('title'), 90)}* — {item.get('source', '')}")
            blocks.append(item.get("link", ""))
            blocks.append("")
        message = "\n".join(blocks).strip()
    return message


def main():
    phone = os.environ.get("CALLMEBOT_PHONE", "").strip()
    apikey = os.environ.get("CALLMEBOT_APIKEY", "").strip()
    if not phone or not apikey:
        print("CallMeBot secrets not set — skipping WhatsApp digest.")
        return

    config = load_json(SCHEDULE_PATH, {})
    schedule = config.get("schedule", {})
    count = int(config.get("items_per_digest", 5))
    distinct = bool(config.get("require_distinct_sources", True))

    day_name = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%A").lower()
    category = schedule.get(day_name)
    if not category:
        print(f"No category scheduled for {day_name} — skipping.")
        return

    index = load_json(INDEX_PATH, [])
    items = pick_items(index, category, count, distinct)
    message = build_message(day_name, category, items)

    url = (
        "https://api.callmebot.com/whatsapp.php"
        f"?phone={quote(phone)}&text={quote(message)}&apikey={quote(apikey)}"
    )
    try:
        r = requests.get(url, timeout=30)
        print(f"CallMeBot response {r.status_code}: {r.text[:200]}")
        if r.status_code != 200:
            sys.exit(1)
    except requests.RequestException as e:
        print(f"CallMeBot request failed: {e}")
        sys.exit(1)

    print(f"Sent {len(items)} {category} item(s) for {day_name}.")


if __name__ == "__main__":
    main()
