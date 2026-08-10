#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scrape Microsoft's Azure OpenAI model retirement schedule page and:
  1) Extract the Azure OpenAI section table from the Model retirement schedule page
  2) Save a clean CSV with a "Type" column (always "Azure OpenAI")
  3) Compare against a local snapshot (JSON) to detect:
        - New rows
        - Changes to any fields (esp. Retirement date)
  4) Generate an RSS feed with entries for differences detected during this run

First run: creates a baseline snapshot; RSS will include a single "Baseline created" item.
Subsequent runs: RSS includes entries for new/changed rows only.

Usage:
  python scrape_ms_retirements.py
  python scrape_ms_retirements.py --outdir ./out     # change output dir
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
from typing import List, Dict, Tuple

import requests
from bs4 import BeautifulSoup

# As of 2026-04 Microsoft moved the per-model tables off the lifecycle policy
# page (model-retirements) onto a dedicated schedule page, organised by provider
# instead of by modality. We scrape the "Azure OpenAI" section only.
MS_URL_BASE = "https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule"
AZURE_OPENAI_LINK = f"{MS_URL_BASE}#azure-openai"
TYPE_LABEL = "Azure OpenAI"

# Old snapshot keys used modality-based Type values. Migrate them on load so
# the first run after the page restructure doesn't flood RSS with "new model"
# items for things subscribers already know about.
LEGACY_TYPES = {"Text", "Audio", "Image and video", "Embedding"}


def fetch_page() -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    r = requests.get(MS_URL_BASE, headers=headers, timeout=30)
    r.raise_for_status()
    # Microsoft Learn returns text/html without an explicit charset, so
    # requests falls back to ISO-8859-1 and mojibakes UTF-8 characters
    # (e.g. em-dash). Force UTF-8 to match the actual page encoding.
    r.encoding = "utf-8"
    return r.text

def normalize_text(s: str) -> str:
    if s is None:
        return ""
    # Remove surrounding backticks and trim whitespace and consecutive spaces
    s = s.strip().strip("`").strip()
    s = re.sub(r"\s+", " ", s)
    return s

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
    # Expected (new): Model Name | Model Version | Lifecycle Status | Deprecation Date (No New Customers) | Retirement Date | Replacement Model
    # Expected (old): Model | Version | Lifecycle Status | Retirement date | Replacement model
    # We'll map by best-effort matching to handle both old and new column structures.
    def header_key(h):
        hl = h.lower()
        # Check for "Model Version" BEFORE "Model Name" to avoid false matches
        # (e.g., "model version 1" starts with "model " so it could match the Model check)
        if "model version" in hl or ("version" in hl and "model" not in hl):
            return "Version"
        # Handle "Model Name" or "Model"
        if "model name" in hl or hl == "model" or hl.startswith("model "):
            return "Model"
        # Handle Lifecycle Status
        if "lifecycle" in hl or "status" in hl:
            return "Lifecycle status"
        # Handle "Deprecation Date (No New Customers)" - NEW COLUMN
        if "deprecation" in hl:
            return "Deprecation date"
        # Handle regular "Retirement Date"
        if "retirement" in hl:
            return "Retirement date"
        # Handle Replacement model
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
        # Normalize MS shorthand back to the historical wording so subscribers
        # don't see a spurious "Generally Available -> GA" diff for every model.
        ls = row.get("Lifecycle status", "")
        if ls == "GA":
            row["Lifecycle status"] = "Generally Available"
        # The schedule page uses an em-dash to mean "no replacement". The
        # legacy snapshot used "" for the same thing; collapse so they match.
        repl = row.get("Replacement model", "")
        if repl in {"—", "-", "–"}:
            row["Replacement model"] = ""
        # Attach our Type field
        row["Type"] = type_label
        rows.append(row)
    return rows


def parse_azure_openai_schedule(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    h3 = soup.find("h3", id="azure-openai")
    if not h3:
        raise RuntimeError(
            "Could not locate '#azure-openai' section on the schedule page. "
            "The page structure may have changed again."
        )
    table = h3.find_next("table")
    if not table:
        raise RuntimeError("Found 'Azure OpenAI' heading but no table follows it.")
    return parse_table(table, type_label=TYPE_LABEL)


def key_for_row(row: Dict[str, str]) -> Tuple[str, str, str]:
    # Key by (Type, Model, Version)
    return (
        row.get("Type", ""),
        row.get("Model", ""),
        row.get("Version", ""),
    )

# Fields we track for change detection. Kept in one place so dedup and compare
# stay in sync.
DIFF_FIELDS = ["Lifecycle status", "Retirement date", "Replacement model"]

def merge_duplicate_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Collapse rows that share (Type, Model, Version) into one row.

    As of ~2026-08 Microsoft's schedule page lists some model versions on more
    than one row (one per deployment type) with DIFFERENT retirement dates, e.g.
    gpt-realtime-mini 2025-12-15 appears as both 2027-06-15 and 2026-12-15.

    Our snapshot is keyed on (Type, Model, Version), so two rows with the same
    key but conflicting values make the comparison non-convergent: one row is
    seen as a change while the other rewrites the snapshot back to the old value
    on the same run (``new_snapshot[k] = row`` keeps whichever row is processed
    last). The result is the identical diff being re-emitted on every run
    forever. Merging distinct values per field (sorted, ' / '-joined) collapses
    each key to a single deterministic row, so the snapshot can stabilise while
    still preserving every deployment-type date.
    """
    order: List[Tuple[str, str, str]] = []
    grouped: Dict[Tuple[str, str, str], List[Dict[str, str]]] = {}
    for row in rows:
        k = key_for_row(row)
        if k not in grouped:
            grouped[k] = []
            order.append(k)
        grouped[k].append(row)

    merged: List[Dict[str, str]] = []
    collapsed = 0
    for k in order:
        group = grouped[k]
        if len(group) == 1:
            merged.append(group[0])
            continue
        collapsed += len(group) - 1
        base = dict(group[0])
        for field in DIFF_FIELDS:
            seen: List[str] = []
            for r in group:
                v = r.get(field, "")
                if v and v not in seen:
                    seen.append(v)
            base[field] = " / ".join(sorted(seen))
        merged.append(base)
    if collapsed:
        print(f"Merged {collapsed} duplicate row(s) sharing (Type, Model, Version).")
    return merged

def load_snapshot(path: str) -> Dict[Tuple[str, str, str], Dict[str, str]]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    snapshot: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    for k, v in raw.items():
        parts = k.split("||")
        if len(parts) != 3:
            continue
        type_label, model, version = parts
        # Migrate legacy modality-based Type values to the new provider-based
        # scheme so the first run after the MS page restructure doesn't flag
        # every existing model as "new".
        if type_label in LEGACY_TYPES:
            type_label = TYPE_LABEL
            v = {**v, "Type": TYPE_LABEL}
        snapshot[(type_label, model, version)] = v
    return snapshot

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
            # "Deprecation date" was dropped from the schedule page; ignore it
            # so existing snapshot values don't show as "blanked out".
            for field in DIFF_FIELDS:
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
    fields = ["Type", "Model", "Version", "Lifecycle status", "Deprecation date", "Retirement date", "Replacement model"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})

_ITEM_RE = re.compile(r"<item>.*?</item>", re.DOTALL)

def read_existing_rss_items(rss_path: str) -> List[str]:
    """Extract existing <item> elements from RSS file to preserve them across runs.

    Uses a regex rather than an XML parser because:
      - We control the writer's format, so <item> blocks never nest and never
        contain a literal "</item>" in CDATA or attributes.
      - The previous BeautifulSoup(..., "xml") implementation silently failed
        when lxml wasn't installed, wiping the feed on every no-change run.
    """
    if not os.path.exists(rss_path):
        return []
    with open(rss_path, "r", encoding="utf-8") as f:
        content = f.read()
    return _ITEM_RE.findall(content)

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
            link = AZURE_OPENAI_LINK
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
            # Deterministic GUID derived from the CHANGE CONTENT, not the wall
            # clock. The previous form embedded now.timestamp() (and a
            # per-process-salted hash()), so an identical change re-detected on a
            # later run got a brand-new GUID and RSS consumers (e.g. Slack)
            # re-posted it as new. Keying on the actual field transitions means
            # the same change always yields the same GUID and is de-duplicated
            # downstream.
            if ch["type"] == "update":
                change_sig = ";".join(
                    f"{f}:{a}=>{b}" for f, (a, b) in sorted(ch.get("diffs", {}).items())
                )
            else:
                change_sig = ch["type"]
            guid = f"{type_label}|{model}|{version}|{change_sig}"
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
    rows = parse_azure_openai_schedule(html)

    rows.sort(key=lambda r: (r.get("Model", ""), r.get("Version", "")))

    # Collapse duplicate (Type, Model, Version) rows that Microsoft now emits
    # per deployment type; without this the snapshot never converges and the
    # same diff is re-published on every run.
    rows = merge_duplicate_rows(rows)

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
