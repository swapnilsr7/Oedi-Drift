#!/usr/bin/env python3
"""
Turns data/last_run.json into a GitHub Issue so you get a notification
(GitHub emails/notifies you on new issues by default) summarizing what
the crawler found and flagging any source that needs attention.
"""
import json
import os

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_REPORT_PATH = os.path.join(ROOT, "data", "last_run.json")


def main():
    with open(RUN_REPORT_PATH, "r", encoding="utf-8") as f:
        report = json.load(f)

    repo = os.environ["GITHUB_REPOSITORY"]  # e.g. "yourname/field-archive", set automatically by Actions
    token = os.environ["GITHUB_TOKEN"]

    broken = [s for s in report["sources"] if s["status"] != "ok"]
    lines = [f"**New items indexed:** {report['new_items']}", ""]

    if report["new_items"] > 0:
        lines.append("**By source:**")
        for s in report["sources"]:
            if s.get("new_items"):
                lines.append(f"- {s['name']}: {s['new_items']} new")
        lines.append("")

    if broken:
        lines.append("**Sources needing attention:**")
        for s in broken:
            lines.append(f"- {s['name']}: `{s['status']}`")
        lines.append("")
        lines.append("Fix these by editing `data/sources.json` (e.g. paste a correct RSS URL) or removing the source.")

    body = "\n".join(lines)
    title = f"Archive update — {report['new_items']} new item(s)" + (" — action needed" if broken else "")

    # Skip opening an issue on a totally silent, no-news, no-problems run to avoid noise.
    if report["new_items"] == 0 and not broken:
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
