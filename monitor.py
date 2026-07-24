#!/usr/bin/env python3
"""
Christian Concern News Monitor
--------------------------------
A free, no-install-required news monitoring pipeline.

What it does each time it runs:
  1. Fetches every RSS feed listed in feeds.yaml
  2. Optionally fetches The Guardian's free API (needs a free API key)
  3. Matches new articles against the issue keywords in issues.yaml
  4. Writes a Markdown digest (latest_digest.md)
  5. Optionally posts to Slack and/or sends an email, if configured

It remembers which articles it has already seen (seen_articles.json)
so re-running it only surfaces genuinely new items.

Nothing here requires payment. Optional AI-based classification
(instead of keyword matching) can be added later using the Claude API
-- see classify_with_claude() below, which is off by default.
"""

import os
import re
import json
import hashlib
import smtplib
import urllib.request
import urllib.error
import urllib.parse
from email.mime.text import MIMEText
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import yaml

STATE_FILE = "seen_articles.json"
USER_AGENT = "Mozilla/5.0 (compatible; ChristianConcernMonitor/1.0)"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_url(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_rss(xml_bytes):
    """Parse RSS 2.0 or Atom feed bytes into a list of {title, link, summary, published}."""
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items

    # RSS 2.0 style: <item>
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        desc = re.sub("<[^<]+?>", "", desc).strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title and link:
            items.append({"title": title, "link": link, "summary": desc, "published": pub})

    # Atom style: <entry>
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//a:entry", ns):
        title = (entry.findtext("a:title", namespaces=ns) or "").strip()
        link_el = entry.find("a:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        summary = (
            entry.findtext("a:summary", namespaces=ns)
            or entry.findtext("a:content", namespaces=ns)
            or ""
        ).strip()
        summary = re.sub("<[^<]+?>", "", summary).strip()
        pub = (entry.findtext("a:updated", namespaces=ns) or "").strip()
        if title and link:
            items.append({"title": title, "link": link, "summary": summary, "published": pub})

    return items


def fetch_feed(feed):
    try:
        raw = fetch_url(feed["url"])
    except Exception as e:  # noqa: BLE001 - want to keep going even if one feed fails
        print(f"  ! failed to fetch {feed['name']}: {e}")
        return []
    items = parse_rss(raw)
    for it in items:
        it["source"] = feed["name"]
    return items


def fetch_guardian(api_key, page_size=50):
    """Pull recent articles from the free Guardian Open Platform API."""
    if not api_key:
        return []
    params = {"api-key": api_key, "page-size": str(page_size), "show-fields": "trailText"}
    qs = urllib.parse.urlencode(params)
    url = f"https://content.guardianapis.com/search?{qs}"
    try:
        raw = fetch_url(url)
    except Exception as e:  # noqa: BLE001
        print(f"  ! Guardian API failed: {e}")
        return []
    data = json.loads(raw)
    items = []
    for r in data.get("response", {}).get("results", []):
        items.append(
            {
                "title": r.get("webTitle", ""),
                "link": r.get("webUrl", ""),
                "summary": r.get("fields", {}).get("trailText", ""),
                "published": r.get("webPublicationDate", ""),
                "source": "The Guardian",
            }
        )
    return items


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def keyword_classify(item, issues):
    """Free classification: does the title/summary contain any issue keyword?"""
    text = f"{item['title']} {item['summary']}".lower()
    matched = []
    for issue_name, keywords in issues.items():
        for kw in keywords:
            if kw.lower() in text:
                matched.append(issue_name)
                break
    return matched


def classify_with_claude(item, issue_names, api_key):
    """
    Optional upgrade: use the Claude API for smarter, non-keyword classification.
    Only runs if ANTHROPIC_API_KEY is set. Costs a small fraction of a penny per article.
    """
    prompt = (
        "You are triaging a news headline for a Christian advocacy organisation.\n"
        f"Issues we track: {', '.join(issue_names)}.\n"
        f"Headline: {item['title']}\n"
        f"Summary: {item['summary']}\n\n"
        "Reply with ONLY a JSON array of the issue names (from the list above) this "
        "article is relevant to. If none apply, reply with an empty array []."
    )
    body = json.dumps(
        {
            "model": "claude-haiku-4-5",
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        text = data["content"][0]["text"]
        return json.loads(text)
    except Exception as e:  # noqa: BLE001
        print(f"  ! Claude classification failed, falling back to keywords: {e}")
        return None


# ---------------------------------------------------------------------------
# State (so we don't re-alert on the same article every run)
# ---------------------------------------------------------------------------

def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f)


def article_id(item):
    return hashlib.sha256(item["link"].encode()).hexdigest()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_digest(matched_by_issue):
    lines = [
        f"# Christian Concern Monitoring Digest — "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    if not any(matched_by_issue.values()):
        lines.append("No new relevant articles found this run.")
        return "\n".join(lines)

    for issue, articles in matched_by_issue.items():
        if not articles:
            continue
        lines.append(f"## {issue} ({len(articles)})")
        for a in articles:
            lines.append(f"- **[{a['title']}]({a['link']})** — {a['source']}")
            if a["summary"]:
                snippet = a["summary"][:200]
                lines.append(f"  {snippet}")
        lines.append("")
    return "\n".join(lines)


LOG_FILE = "digest_log.md"
MAX_LOG_ENTRIES = 150  # keeps the file from growing forever; ~150 hourly runs with hits


def append_to_log(digest_text, log_path=LOG_FILE, max_entries=MAX_LOG_ENTRIES):
    """
    Prepend this run's digest to a running log, newest entry first, so that
    checking the file at any time shows everything found recently -- not
    just whatever happened in the single most recent run.
    """
    entry = digest_text.strip()
    existing = ""
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            existing = f.read()

    separator = "\n\n---\n\n"
    combined = entry + (separator + existing if existing.strip() else "")

    # Trim to the most recent N entries so the file/repo doesn't grow forever.
    parts = [p for p in combined.split(separator) if p.strip()]
    trimmed = parts[:max_entries]

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(separator.join(trimmed) + "\n")


def send_slack(webhook_url, text):
    if not webhook_url:
        return
    data = json.dumps({"text": text[:3000]}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"  ! Slack post failed: {e}")


def send_email(subject, body, cfg):
    if not cfg.get("enabled"):
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["to_addr"]
    try:
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.login(cfg["smtp_user"], cfg["smtp_pass"])
            server.sendmail(cfg["from_addr"], [cfg["to_addr"]], msg.as_string())
    except Exception as e:  # noqa: BLE001
        print(f"  ! Email send failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    with open("feeds.yaml") as f:
        feeds = yaml.safe_load(f)["feeds"]
    with open("issues.yaml") as f:
        issues = yaml.safe_load(f)["issues"]

    seen = load_seen()

    all_items = []
    print(f"Fetching {len(feeds)} RSS feeds...")
    for feed in feeds:
        items = fetch_feed(feed)
        print(f"  {feed['name']}: {len(items)} items")
        all_items.extend(items)

    guardian_key = os.environ.get("GUARDIAN_API_KEY")
    if guardian_key:
        print("Fetching Guardian API...")
        g_items = fetch_guardian(guardian_key)
        print(f"  Guardian: {g_items and len(g_items) or 0} items")
        all_items.extend(g_items)

    new_items = [it for it in all_items if article_id(it) not in seen]
    print(f"{len(new_items)} new items out of {len(all_items)} fetched")

    claude_key = os.environ.get("ANTHROPIC_API_KEY")  # optional upgrade
    issue_names = list(issues.keys())

    matched_by_issue = {name: [] for name in issue_names}
    for item in new_items:
        matches = None
        if claude_key:
            matches = classify_with_claude(item, issue_names, claude_key)
        if matches is None:  # not using Claude, or the call failed -> free fallback
            matches = keyword_classify(item, issues)
        for m in matches:
            if m in matched_by_issue:
                matched_by_issue[m].append(item)
        seen.add(article_id(item))

    digest = build_digest(matched_by_issue)
    print("\n" + digest)

    # latest_digest.md always shows this run's result (useful for Slack/email/debugging).
    with open("latest_digest.md", "w", encoding="utf-8") as f:
        f.write(digest)

    # digest_log.md is the one to bookmark if you're just checking the file yourself:
    # it accumulates every run that found something, newest first, so you never lose
    # a finding just because you didn't check in the exact hour it appeared.
    if any(matched_by_issue.values()):
        append_to_log(digest)

    if any(matched_by_issue.values()):
        send_slack(os.environ.get("SLACK_WEBHOOK_URL", ""), digest)

        email_cfg = {
            "enabled": os.environ.get("EMAIL_ENABLED", "false").lower() == "true",
            "smtp_host": os.environ.get("SMTP_HOST", ""),
            "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
            "smtp_user": os.environ.get("SMTP_USER", ""),
            "smtp_pass": os.environ.get("SMTP_PASS", ""),
            "from_addr": os.environ.get("EMAIL_FROM", ""),
            "to_addr": os.environ.get("EMAIL_TO", ""),
        }
        send_email("Christian Concern Monitoring Digest", digest, email_cfg)

    save_seen(seen)


if __name__ == "__main__":
    main()
