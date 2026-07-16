#!/usr/bin/env python3
"""
Oedi Field — archive crawler.

Runs on a schedule via GitHub Actions. For each source in data/sources.json:
  1. Finds an RSS/Atom feed (uses the configured one, or tries common paths).
  2. Pulls recent entries (title, link, published date, image).
  3. Skips anything already in data/index.json (dedup by link).
  4. Sends new items to Claude for: a short summary, a domain category
     (reusing existing categories where possible), and style/descriptor
     tags used later for similarity search.
  5. Writes everything to data/index.json, which the static site reads.
  6. Writes a run report to data/last_run.json (used to open a GitHub
     Issue summarizing what's new, and to flag any source that failed).

This script is intentionally dependency-light (stdlib + requests + anthropic)
so it runs fast and cheap on GitHub Actions' free tier.
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from anthropic import Anthropic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(ROOT, "data", "sources.json")
INDEX_PATH = os.path.join(ROOT, "data", "index.json")
RUN_REPORT_PATH = os.path.join(ROOT, "data", "last_run.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OediFieldArchive/1.0; RSS feed reader)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html;q=0.8",
}

MAX_NEW_ITEMS_PER_SOURCE = 12  # cap per run so one prolific source doesn't drown out others
REQUEST_TIMEOUT = 15

COMMON_FEED_PATHS = ["feed/", "feed", "rss/", "rss", "atom.xml", "rss.xml", "feed.xml"]


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def discover_feed(homepage):
    """Try the homepage's <link rel=alternate> tag first, then common paths."""
    try:
        r = requests.get(homepage, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        matches = re.findall(
            r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]+href=["\']([^"\']+)["\']',
            r.text, re.IGNORECASE,
        )
        if matches:
            return urljoin(homepage, matches[0])
    except requests.RequestException:
        pass

    for path in COMMON_FEED_PATHS:
        candidate = urljoin(homepage, path)
        try:
            r = requests.get(candidate, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200 and ("<rss" in r.text[:2000] or "<feed" in r.text[:2000]):
                return candidate
        except requests.RequestException:
            continue
    return None


NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def parse_feed(xml_text, source_homepage):
    """Parse RSS 2.0 or Atom into a flat list of entries. Best-effort, tolerant of quirks."""
    entries = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return entries

    # RSS 2.0
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "")
        image = None
        media_content = item.find("media:content", NS)
        if media_content is not None:
            image = media_content.get("url")
        if not image:
            enclosure = item.find("enclosure")
            if enclosure is not None and "image" in (enclosure.get("type") or ""):
                image = enclosure.get("url")
        if not image:
            img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc)
            if img_match:
                image = img_match.group(1)
        if title and link:
            entries.append({"title": title, "link": link, "published": pub, "image": image})

    # Atom
    for entry in root.findall(".//atom:entry", NS):
        title = (entry.findtext("atom:title", default="", namespaces=NS) or "").strip()
        link_el = entry.find("atom:link", NS)
        link = link_el.get("href") if link_el is not None else None
        pub = (entry.findtext("atom:published", default="", namespaces=NS)
               or entry.findtext("atom:updated", default="", namespaces=NS) or "").strip()
        if title and link:
            entries.append({"title": title, "link": link, "published": pub, "image": None})

    return entries


def fetch_page_image(url):
    """If the feed didn't include an image, grab the article's og:image."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text, re.IGNORECASE)
        if m:
            return m.group(1)
    except requests.RequestException:
        pass
    return None


def fetch_shopify_products(homepage, limit=20):
    """
    Shopify stores expose their public product catalog at /products.json.
    Returns (entries, error_string_or_None). Used for brand sources where
    new product drops are the trend signal.
    """
    url = urljoin(homepage, "/products.json?limit=%d" % limit)
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        return [], f"products_json_failed: {e}"

    entries = []
    for p in data.get("products", []):
        handle = p.get("handle")
        title = (p.get("title") or "").strip()
        if not handle or not title:
            continue
        image = None
        if p.get("images"):
            image = p["images"][0].get("src")
        entries.append({
            "title": title,
            "link": urljoin(homepage, "/products/" + handle),
            "published": p.get("published_at", "") or "",
            "image": image,
        })
    return entries, None


def existing_categories(index):
    return sorted({item["category"] for item in index if item.get("category")})


def analyze_with_claude(client, title, link, image_url, known_categories, pinned_category=None):
    """
    Ask Claude for: 1-line summary, a domain category, tags.
    If the source has a pinned category (set in sources.json), Claude only
    provides summary + tags and the category is forced to the pinned value.
    """
    category_hint = ", ".join(known_categories) if known_categories else "none yet — you're defining the first ones"
    if pinned_category:
        category_clause = f'"category": "{pinned_category}"'
        category_instruction = f'The category is fixed as "{pinned_category}" — return it exactly as given.'
    else:
        category_clause = '"category": "a single domain category, e.g. Architecture, Interiors, Fashion, Art, Hospitality, Product Design, Jewellery, Urbanism — REUSE one of the existing categories above if it reasonably fits, only invent a new one if none fit"'
        category_instruction = ""

    prompt = f"""You are tagging one item for a personal design-trend archive spanning architecture, interior design, fashion, art, jewellery, hospitality and product design, viewed from an Indian design perspective watching global signals.

Item title: {title}
Item URL: {link}

Existing categories already in use in the archive: {category_hint}
{category_instruction}

Respond ONLY with JSON, no preamble, no markdown fences, in exactly this shape:
{{
  "summary": "one or two plain sentences on what this item shows and why it might matter as a design signal",
  {category_clause},
  "tags": ["3 to 6 short lowercase style/descriptor tags, e.g. minimal, jaali, terracotta, art-deco, biophilic — these are used later for similarity search so be specific and visual"]
}}"""

    try:
        content = [{"type": "text", "text": prompt}]
        if image_url:
            # Let Claude look at the image itself when available for richer tags.
            content = [
                {"type": "image", "source": {"type": "url", "url": image_url}},
                {"type": "text", "text": prompt},
            ]
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": content}],
        )
        text = resp.content[0].text.strip()
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(text)
        if pinned_category:
            parsed["category"] = pinned_category
        return parsed
    except Exception as e:
        return {"summary": "", "category": pinned_category or "Uncategorized", "tags": [], "_error": str(e)}


def domain_of(url):
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set — aborting.", file=sys.stderr)
        sys.exit(1)

    client = Anthropic(api_key=api_key)
    sources = load_json(SOURCES_PATH, {"sources": []})["sources"]
    index = load_json(INDEX_PATH, [])
    known_links = {item["link"] for item in index}

    run_report = {"run_at": datetime.now(timezone.utc).isoformat(), "new_items": 0, "sources": []}

    for src in sources:
        name = src["name"]
        homepage = src["homepage"]
        feed_url = src.get("rss")
        report_entry = {"name": name, "status": "ok", "new_items": 0}

        entries = []

        # Editorial channel: configured or auto-discovered RSS/Atom feed.
        if not feed_url:
            feed_url = discover_feed(homepage)
        if feed_url:
            try:
                r = requests.get(feed_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                entries.extend(parse_feed(r.text, homepage))
            except requests.RequestException as e:
                report_entry["feed_status"] = f"fetch_failed: {e}"

        # Product channel: Shopify stores' public catalog (new drops as signal).
        if src.get("shopify"):
            products, shop_err = fetch_shopify_products(homepage)
            entries.extend(products)
            if shop_err:
                report_entry["shopify_status"] = shop_err

        if not entries:
            report_entry["status"] = "nothing_fetched"
            run_report["sources"].append(report_entry)
            continue

        new_count = 0
        for entry in entries:
            if new_count >= MAX_NEW_ITEMS_PER_SOURCE:
                break
            if entry["link"] in known_links:
                continue

            image = entry.get("image") or fetch_page_image(entry["link"])
            analysis = analyze_with_claude(
                client, entry["title"], entry["link"], image,
                existing_categories(index), pinned_category=src.get("category")
            )

            index.append({
                "title": entry["title"],
                "link": entry["link"],
                "image": image,
                "published": entry.get("published", ""),
                "source": name,
                "domain": domain_of(entry["link"]),
                "summary": analysis.get("summary", ""),
                "category": analysis.get("category", "Uncategorized"),
                "tags": analysis.get("tags", []),
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            })
            known_links.add(entry["link"])
            new_count += 1
            time.sleep(0.5)  # be polite to Claude + source rate limits

        report_entry["new_items"] = new_count
        run_report["new_items"] += new_count
        run_report["sources"].append(report_entry)

    save_json(INDEX_PATH, index)
    save_json(RUN_REPORT_PATH, run_report)
    print(json.dumps(run_report, indent=2))


if __name__ == "__main__":
    main()
