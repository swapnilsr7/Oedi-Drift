#!/usr/bin/env python3
"""
Oedi Field — archive crawler.

Runs on a schedule via GitHub Actions. For each source in data/sources.json:
  1. Finds an RSS/Atom feed (configured or auto-discovered), and/or reads a
     Shopify store's public product catalog.
  2. Pulls recent entries (title, link, published date, up to 4 images).
  3. Dedups robustly: link URLs are normalized (tracking params stripped)
     and same-title-same-source repeats are skipped. A cleanup pass also
     removes duplicates already present in the index from earlier runs.
  4. Sends new items to Claude for summary, category (pinned per-source or
     AI-decided), and style tags. If an image URL is blocked, retries
     text-only rather than failing.
  5. Repairs previously broken items (Uncategorized + no summary) a batch
     per run, so temporary API failures heal automatically.
  6. Writes data/index.json and data/last_run.json, including
     analysis-error and duplicate-removal counts for the Issue report.

Per-source options in sources.json:
  "category": "Jewellery"        -> pin every item's category
  "shopify": true                -> also index product drops
  "exclude_keywords": ["horoscope"] -> skip entries whose title contains any
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

MAX_NEW_ITEMS_PER_SOURCE = 12
REPAIR_PER_RUN = 40
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


def normalize_link(url):
    """
    Canonical form for dedup: drop query strings (utm_* tracking params make
    the same article look like a new URL every run), fragments, trailing
    slashes, www, and case differences in the host.
    """
    try:
        p = urlparse(url)
        host = p.netloc.lower().replace("www.", "")
        path = p.path.rstrip("/")
        return host + path
    except Exception:
        return url


def title_key(source, title):
    return (source, re.sub(r"\s+", " ", (title or "").strip().lower()))


def dedupe_index(index):
    """
    One-time-per-run cleanup of duplicates already in the archive.
    Keeps the first occurrence, but upgrades to a later duplicate if the
    kept one is broken (Uncategorized) and the duplicate was analyzed fine.
    """
    by_link = {}
    for item in index:
        k = normalize_link(item.get("link", ""))
        if k not in by_link:
            by_link[k] = item
        else:
            kept = by_link[k]
            if kept.get("category") == "Uncategorized" and item.get("category") not in (None, "", "Uncategorized"):
                by_link[k] = item

    by_title = {}
    for item in by_link.values():
        k = title_key(item.get("source", ""), item.get("title", ""))
        if k not in by_title:
            by_title[k] = item
        else:
            kept = by_title[k]
            if kept.get("category") == "Uncategorized" and item.get("category") not in (None, "", "Uncategorized"):
                by_title[k] = item

    return list(by_title.values())


def discover_feed(homepage):
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
    entries = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return entries

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

    for entry in root.findall(".//atom:entry", NS):
        title = (entry.findtext("atom:title", default="", namespaces=NS) or "").strip()
        link_el = entry.find("atom:link", NS)
        link = link_el.get("href") if link_el is not None else None
        pub = (entry.findtext("atom:published", default="", namespaces=NS)
               or entry.findtext("atom:updated", default="", namespaces=NS) or "").strip()
        if title and link:
            entries.append({"title": title, "link": link, "published": pub, "image": None})

    return entries


def extract_images_from_page(url, max_images=4):
    images = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        html = r.text
    except requests.RequestException:
        return images

    candidates = re.findall(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE
    )
    candidates += re.findall(r'<img[^>]+data-src=["\']([^"\']+)["\']', html)
    candidates += re.findall(r'<img[^>]+data-lazy-src=["\']([^"\']+)["\']', html)
    candidates += re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html)

    skip_words = ("logo", "icon", "avatar", "sprite", "badge", "placeholder", "pixel", "advert")
    seen = set()
    for src in candidates:
        if len(images) >= max_images:
            break
        src = src.strip()
        if src.startswith("data:"):
            continue
        src = urljoin(url, src)
        low = src.lower()
        if not low.startswith("http"):
            continue
        if low.endswith(".svg") or low.endswith(".gif"):
            continue
        if any(w in low for w in skip_words):
            continue
        if src in seen:
            continue
        seen.add(src)
        images.append(src)
    return images


def fetch_shopify_products(homepage, limit=20):
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
        images = [img.get("src") for img in (p.get("images") or []) if img.get("src")][:4]
        entries.append({
            "title": title,
            "link": urljoin(homepage, "/products/" + handle),
            "published": p.get("published_at", "") or "",
            "image": images[0] if images else None,
            "images": images,
        })
    return entries, None


def existing_categories(index):
    return sorted({item["category"] for item in index if item.get("category") and item["category"] != "Uncategorized"})


def analyze_with_claude(client, title, link, image_url, known_categories, pinned_category=None):
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
            content = [
                {"type": "image", "source": {"type": "url", "url": image_url}},
                {"type": "text", "text": prompt},
            ]
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                messages=[{"role": "user", "content": content}],
            )
        except Exception:
            if not image_url:
                raise
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
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
    pins = {s["name"]: s.get("category") for s in sources}
    index = load_json(INDEX_PATH, [])

    # Cleanup pass: remove duplicates accumulated by earlier runs.
    before = len(index)
    index = dedupe_index(index)
    removed_duplicates = before - len(index)

    known_links = {normalize_link(item["link"]) for item in index}
    known_titles = {title_key(item.get("source", ""), item.get("title", "")) for item in index}

    run_report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "new_items": 0,
        "removed_duplicates": removed_duplicates,
        "analysis_errors": 0,
        "analysis_error_sample": None,
        "repaired": 0,
        "sources": [],
    }

    for src in sources:
        name = src["name"]
        homepage = src["homepage"]
        feed_url = src.get("rss")
        exclude = [k.lower() for k in src.get("exclude_keywords", [])]
        report_entry = {"name": name, "status": "ok", "new_items": 0}

        entries = []

        if not feed_url:
            feed_url = discover_feed(homepage)
        if feed_url:
            try:
                r = requests.get(feed_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                entries.extend(parse_feed(r.text, homepage))
            except requests.RequestException as e:
                report_entry["feed_status"] = f"fetch_failed: {e}"

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
            nl = normalize_link(entry["link"])
            tk = title_key(name, entry["title"])
            if nl in known_links or tk in known_titles:
                continue
            if exclude and any(k in entry["title"].lower() for k in exclude):
                continue

            images = entry.get("images") or []
            if not images:
                images = extract_images_from_page(entry["link"], max_images=4)
                if entry.get("image") and entry["image"] not in images:
                    images.insert(0, entry["image"])
                images = images[:4]
            image = images[0] if images else None

            analysis = analyze_with_claude(
                client, entry["title"], entry["link"], image,
                existing_categories(index), pinned_category=src.get("category")
            )
            if analysis.get("_error"):
                run_report["analysis_errors"] += 1
                if not run_report["analysis_error_sample"]:
                    run_report["analysis_error_sample"] = analysis["_error"][:300]

            index.append({
                "title": entry["title"],
                "link": entry["link"],
                "image": image,
                "images": images,
                "published": entry.get("published", ""),
                "source": name,
                "domain": domain_of(entry["link"]),
                "summary": analysis.get("summary", ""),
                "category": analysis.get("category", "Uncategorized"),
                "tags": analysis.get("tags", []),
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            })
            known_links.add(nl)
            known_titles.add(tk)
            new_count += 1
            time.sleep(0.5)

        report_entry["new_items"] = new_count
        run_report["new_items"] += new_count
        run_report["sources"].append(report_entry)

    # Self-repair: re-analyze previously broken items, bounded per run.
    for item in index:
        if run_report["repaired"] >= REPAIR_PER_RUN:
            break
        if item.get("category") == "Uncategorized" and not item.get("summary"):
            analysis = analyze_with_claude(
                client, item["title"], item["link"], item.get("image"),
                existing_categories(index), pinned_category=pins.get(item.get("source"))
            )
            if analysis.get("_error"):
                run_report["analysis_errors"] += 1
                if not run_report["analysis_error_sample"]:
                    run_report["analysis_error_sample"] = analysis["_error"][:300]
                break
            item["summary"] = analysis.get("summary", "")
            item["category"] = analysis.get("category", "Uncategorized")
            item["tags"] = analysis.get("tags", [])
            run_report["repaired"] += 1
            time.sleep(0.5)

    save_json(INDEX_PATH, index)
    save_json(RUN_REPORT_PATH, run_report)
    print(json.dumps(run_report, indent=2))


if __name__ == "__main__":
    main()
