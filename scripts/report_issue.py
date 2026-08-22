#!/usr/bin/env python3
"""
Turns data/last_run.json into a GitHub Issue summarizing the crawl:
new items, repaired items, duplicates removed, broken sources, and
AI-analysis failures (e.g. exhausted API credit) with the actual error.
"""
import json
import os

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_REPORT_PATH = os.path.join(ROOT, "data", "last_run.json")


def main():
    with open(RUN_REPORT_PATH, "r", encoding="utf-8") as f:
        report = json.load(f)

    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]

    broken = [s for s in report["sources"] if s["status"] != "ok" or s.get("feed_status") or s.get("shopify_status")]
    analysis_errors = report.get("analysis_errors", 0)
    repaired = report.get("repaired", 0)
    removed_dupes = report.get("removed_duplicates", 0)

    lines = [f"**New items indexed:** {report['new_items']}"]
    if repaired:
        lines.append(f"**Previously broken items repaired:** {repaired}")
    if removed_dupes:
        lines.append(f"**Duplicate items removed from the archive:** {removed_dupes}")
    lines.append("")

    if analysis_errors:
        lines.append(f"## ⚠️ AI analysis failed for {analysis_errors} item(s)")
        lines.append("These items were saved without summaries/categories (shown as Uncategorized). "
                     "The self-repair pass will fix them on future runs once the cause is resolved.")
        if report.get("analysis_error_sample"):
            lines.append(f"First error: `{report['analysis_error_sample']}`")
        lines.append("**Most common cause: API credit exhausted — check console.anthropic.com → Billing.**")
        lines.append("")

    if report["new_items"] > 0:
        lines.append("**By source:**")
        for s in report["sources"]:
            if s.get("new_items"):
                lines.append(f"- {s['name']}: {s['new_items']} new")
        lines.append("")

    if broken:
        lines.append("**Sources needing attention:**")
        for s in broken:
            detail = s["status"] if s["status"] != "ok" else (s.get("feed_status") or s.get("shopify_status"))
            lines.append(f"- {s['name']}: `{detail}`")
        lines.append("")
        lines.append("Fix these by editing `data/sources.json` (e.g. paste a correct RSS URL) or removing the source.")

    body = "\n".join(lines)
    title = f"Archive update — {report['new_items']} new item(s)"
    if analysis_errors:
        title += f" — ⚠️ {analysis_errors} analysis failures"
    elif broken:
        title += " — action needed"

    if report["new_items"] == 0 and not broken and not analysis_errors and not repaired and not removed_dupes:
        print("Nothing new and nothing broken — skipping issue.")
        return

    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": title, "body": body, "labels": ["archive-run"]},
        timeout=15,
    )
    resp.raise_for_status()
    print("Issue opened:", resp.json().get("html_url"))


if __name__ == "__main__":
    main()
