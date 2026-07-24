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
            "Content-Type":
