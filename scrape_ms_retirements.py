#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scrape Microsoft's Azure OpenAI "model deprecations and retirements" page and:
  1) Extract ONLY "Current models" tables (skip Fine-tuned and Default)
  2) Combine across all tabs/types (Text generation, Audio, Image and video, Embedding)
  3) Save a clean CSV with a "Type" column
  4) Compare against a local snapshot (JSON) to detect:
        - New rows
        - Changes to any fields (esp. Retirement date)
  5) Generate an RSS feed with entries for differences detected during this run

First run: creates a baseline snapshot; RSS will include a single "Baseline created" item.
Subsequent runs: RSS includes entries for new/changed rows only.

Usage:
  python scrape_ms_retirements.py
  python scrape_ms_retirements.py --only text        # (optional) focus on text only
  python scrape_ms_retirements.py --outdir ./out     # change output dir
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from typing import List, Dict, Tuple, Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

MS_URL_BASE = "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-retirements"
# We parse a single page that already contains the Current models section broken down by subsections.
# The query param (?tabs=...) is used for deep links; we still rely on the combined page so parsing is simpler.
TAB_MAP = {
    "Text": "text",
    "Audio": "audio",
    "Image and video": "image",
    "Embedding": "embedding",
}

# Headings we consider inside "Current models"
VALID_SECTION_TITLES = list(TAB_MAP.keys())

def canonical_type_from_title(title: str) -> Optional[str]:
    """
    Map section heading text to one of: "Text", "Audio", "Image and video", "Embedding".
    Handles variants like "Text generation", "Text models", "Image & video", etc.
    """
    if not title:
        return None
    s = title.strip().lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    # Checks are intentionally broad
    if "embedding" in s:
        return "Embedding"
    if "audio" in s or "speech" in s:
        return "Audio"
    if "image" in s or "video" in s or "vision" in s:
        return "Image and video"
    if "text" in s or "text generation" in s or "chat" in s:
        return "Text"
    return None


def fetch_page() -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    r = requests.get(MS_URL_BASE, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text

def normalize_text(s: str) -> str:
    if s is None:
        return ""
    # Remove surrounding backticks and trim whitespace and consecutive spaces
    s = s.strip().strip("`").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def find_current_models_section(soup: BeautifulSoup):
    # Find the H2 with text like "Current models"
    for h2 in soup.find_all(["h2"]):
        if h2.get_text(strip=True).lower().startswith("current models"):
            return h2
    return None

def next_tag(tag):
    t = tag
    while t is not None:
        t = t.find_next()
        if getattr(t, "name", None):
            return t
    return None

def parse_table(table, type_label: str) -> List[Dict[str, str]]:
    # Convert HTML table to list of dicts. Headers could vary slightly; normalize by header text.
    rows = []
    if not table:
        return rows
    # Find header
    thead = table.find("thead")
    headers = []
    if thead:
        for th in thead.find_all(["th", "td"]):
            headers.append(normalize_text(th.get_text(" ", strip=True)))
    else:
        # Try first row as header
        tr0 = table.find("tr")
        if tr0:
            headers = [normalize_text(th.get_text(" ", strip=True)) for th in tr0.find_all(["th", "td"])]
    # Normalize expected headers
    # Expected: Model | Version | Lifecycle Status | Retirement date | Replacement model
    # But sometimes a combined column title is used (e.g., "Model", "Version", "Lifecycle", etc.)
    # We'll map by best-effort matching.
    def header_key(h):
        hl = h.lower()
        if "model" == hl or hl.startswith("model "):
            return "Model"
        if "version" in hl:
            return "Version"
        if "lifecycle" in hl or "status" in hl:
            return "Lifecycle status"
        if "retirement" in hl:
            return "Retirement date"
        if "replacement" in hl:
            return "Replacement model"
        return h  # fallback

    keys = [header_key(h) for h in headers]

    # Iterate data rows
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        cols = tr.find_all(["td", "th"])
        if not cols or len(cols) < 2:
            continue
        values = [normalize_text(c.get_text(" ", strip=True)) for c in cols]
        # If first row was header and there is no thead, skip it by checking equality with headers
        if not thead and values == headers:
            continue
        row = {keys[i] if i < len(keys) else f"Col{i+1}": values[i] for i in range(len(values))}
        # Clean backticks in each field
        for k in list(row.keys()):
            row[k] = normalize_text(row[k])
        # Attach our Type field
        row["Type"] = type_label
        rows.append(row)
    return rows


def parse_current_models(html: str, only: Optional[str] = None) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    h2 = find_current_models_section(soup)
    if not h2:
        raise RuntimeError("Could not locate 'Current models' section on the page.")

    # Collect all nodes until the next H2 – this bounds our search to the Current models area
    bounded_nodes = []
    node = h2
    while True:
        node = node.find_next()
        if not node:
            break
        if getattr(node, "name", None) == "h2":
            break
        bounded_nodes.append(node)

    # Scan for tables by aria-label to be resilient to extra wrappers/divs
    wanted_by_aria = {
        "text generation": "Text",
        "audio": "Audio",
        "image and video": "Image and video",
        "image & video": "Image and video",
        "embedding": "Embedding",
    }

    rows: List[Dict[str, str]] = []
    for n in bounded_nodes:
        if getattr(n, "name", None) == "table":
            aria = (n.get("aria-label") or "").strip().lower()
            # normalize punctuation
            aria = aria.replace("&", "and")
            aria = re.sub(r"\s+", " ", aria)
            if aria in wanted_by_aria:
                type_label = wanted_by_aria[aria]
                if only and not type_label.lower().startswith(only.lower()):
                    continue
                rows.extend(parse_table(n, type_label=type_label))

    # Fallback: if nothing found via aria-label, try the heading-based walk (rare)
    if not rows:
        # Walk from H3 headings as before (handles unexpected DOMs)
        node = h2
        while node:
            node = node.find_next(["h2", "h3", "table"])
            if not node:
                break
            if node.name == "h2" and node is not h2:
                break
            if node.name == "h3":
                raw_title = node.get_text(" ", strip=True)
                type_label = canonical_type_from_title(raw_title)
                if not type_label:
                    continue
                if only and not type_label.lower().startswith(only.lower()):
                    continue
                tbl = node.find_next("table")
                if tbl:
                    rows.extend(parse_table(tbl, type_label=type_label))
    return rows


def key_for_row(row: Dict[str, str]) -> Tuple[str, str, str]:
    # Key by (Type, Model, Version)
    return (
        row.get("Type", ""),
        row.get("Model", ""),
        row.get("Version", ""),
    )

def load_snapshot(path: str) -> Dict[str, Dict[str, str]]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    # Ensure keys are tuples joined by '||' for portability
    return {tuple(k.split("||")): v for k, v in raw.items()}

def save_snapshot(snapshot: Dict[Tuple[str, str, str], Dict[str, str]], path: str) -> None:
    # Store keys as "Type||Model||Version" to be JSON-serializable
    serial = {"||".join(k): v for k, v in snapshot.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(serial, f, indent=2, ensure_ascii=False)

def compare_snapshots(old: Dict[Tuple[str, str, str], Dict[str, str]], new_rows: List[Dict[str, str]]):
    changes = []
    new_snapshot = {}

    for row in new_rows:
        k = key_for_row(row)
        new_snapshot[k] = row
        if k not in old:
            changes.append({
                "type": "new",
                "key": k,
                "old": None,
                "new": row,
                "message": f"New model listed in Current models: [{k[0]}] {k[1]} {k[2]}",
            })
        else:
            # Detect any field changes
            old_row = old[k]
            diffs = {}
            for field in ["Lifecycle status", "Retirement date", "Replacement model"]:
                if old_row.get(field, "") != row.get(field, ""):
                    diffs[field] = (old_row.get(field, ""), row.get(field, ""))
            if diffs:
                changes.append({
                    "type": "update",
                    "key": k,
                    "old": old_row,
                    "new": row,
                    "diffs": diffs,
                    "message": f"Updated fields for [{k[0]}] {k[1]} {k[2]}: " +
                               ", ".join(f"{f}: '{a}' → '{b}'" for f, (a, b) in diffs.items())
                })
    return changes, new_snapshot

def write_csv(rows: List[Dict[str, str]], out_csv: str) -> None:
    # Ensure consistent column order
    fields = ["Type", "Model", "Version", "Lifecycle status", "Retirement date", "Replacement model"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})

def make_tabs_link(type_label: str) -> str:
    tab = TAB_MAP.get(type_label, TAB_MAP.get(type_label.replace(" and video", ""), "text"))
    return f"{MS_URL_BASE}?{urlencode({'tabs': tab})}"

def read_existing_rss_items(rss_path: str) -> List[str]:
    """Extract existing <item> elements from RSS file to preserve them when no changes occur."""
    if not os.path.exists(rss_path):
        return []
    
    try:
        with open(rss_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Parse existing items using BeautifulSoup
        soup = BeautifulSoup(content, "xml")
        items = soup.find_all("item")
        
        # Convert back to XML strings
        existing_items = []
        for item in items:
            # Preserve the original formatting
            existing_items.append(str(item))
        
        return existing_items
    except Exception:
        # If parsing fails, return empty list to start fresh
        return []

def write_rss(changes, out_rss: str) -> None:
    # Simple RSS 2.0
    now = dt.datetime.now(dt.timezone.utc)
    pubdate = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    channel_title = "Azure OpenAI Current Models – Changes"
    channel_link = MS_URL_BASE
    channel_desc = "RSS feed of changes (new rows or field updates) detected in the 'Current models' tables."

    items_xml = []
    
    if not changes:
        # No changes this run - preserve existing items, don't add new ones
        existing_items = read_existing_rss_items(out_rss)
        items_xml.extend(existing_items)
    else:
        # Add new change items to the feed
        for ch in changes:
            type_label, model, version = ch["key"]
            link = make_tabs_link(type_label)
            title = ch["message"]
            desc_lines = []
            if ch["type"] == "new":
                desc_lines.append("New row detected in Current models table.")
            elif ch["type"] == "update":
                for field, (a, b) in ch.get("diffs", {}).items():
                    desc_lines.append(f"{field}: '{a}' → '{b}'")
            elif ch["type"] == "baseline":
                desc_lines.append("Initial baseline snapshot created.")
            description = "\n".join(desc_lines)
            guid = f"{type_label}|{model}|{version}|{hash(model+version+str(now.timestamp()))}"
            items_xml.append(f"""    <item>
      <title>{escape_xml(title)}</title>
      <link>{link}</link>
      <guid isPermaLink="false">{escape_xml(guid)}</guid>
      <pubDate>{pubdate}</pubDate>
      <description>{escape_xml(description)}</description>
    </item>""")
        
        # Also include existing items (new items first, then existing)
        existing_items = read_existing_rss_items(out_rss)
        items_xml.extend(existing_items)

    rss_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{escape_xml(channel_title)}</title>
    <link>{channel_link}</link>
    <description>{escape_xml(channel_desc)}</description>
    <language>en-us</language>
    <pubDate>{pubdate}</pubDate>
{''.join(items_xml)}
  </channel>
</rss>"""
    with open(out_rss, "w", encoding="utf-8") as f:
        f.write(rss_xml)

def escape_xml(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["text", "audio", "image", "embedding"], help="Scrape only a single type")
    ap.add_argument("--outdir", default="output", help="Output directory (default: ./output)")
    ap.add_argument("--datadir", default="data", help="Data directory for snapshots (default: ./data)")
    args = ap.parse_args()

    # Resolve directories relative to script location
    root = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(root, args.outdir)
    data_dir = os.path.join(root, args.datadir)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    snapshot_path = os.path.join(data_dir, "snapshot.json")
    out_csv = os.path.join(out_dir, "current_models.csv")
    out_rss = os.path.join(out_dir, "rss.xml")

    html = fetch_page()
    rows = parse_current_models(html, only=args.only)

    # Sort with Text first to "focus on Text models first", then by Model/Version
    type_order = {"Text": 0, "Audio": 1, "Image and video": 2, "Embedding": 3}
    rows.sort(key=lambda r: (type_order.get(r.get("Type"), 99), r.get("Model",""), r.get("Version","")))

    # Write CSV
    write_csv(rows, out_csv)

    # Compare
    old_snapshot = load_snapshot(snapshot_path)
    changes, new_snapshot = compare_snapshots(old_snapshot, rows)

    # Save snapshot and RSS
    save_snapshot(new_snapshot, snapshot_path)

    # If no previous snapshot existed, include a baseline entry
    if not old_snapshot:
        changes = [{"type": "baseline", "key": ("-", "-", "-"), "message": "Baseline created; snapshot initialized."}]

    write_rss(changes, out_rss)

    print(f"Wrote CSV: {out_csv}")
    print(f"Wrote RSS: {out_rss}")
    print(f"Snapshot: {snapshot_path}")
    if changes and changes[0].get("type") == "baseline":
        print("Baseline created: no change entries yet.")
    else:
        print(f"Detected {len(changes)} change(s).")

if __name__ == "__main__":
    main()
