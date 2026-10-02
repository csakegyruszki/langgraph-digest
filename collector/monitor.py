#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collector/monitor.py - main collector script.

Pipeline:  config -> per-source fetch (RSS/Telegram) -> theme tagging ->
           date filter (lookback) -> URL dedup (seen_urls.json) ->
           new items written to <DATA_DIR>/inbox/run-<id>.jsonl

This script does NOT write the summary; it only collects structured raw material for the
digest pipeline (digest/graph.py). Stdlib only, runs on Windows and Linux.

Usage (from the repository root):
    python -m collector.monitor                 # live run, writes to the inbox
    python -m collector.monitor --dry-run       # writes nothing, prints a summary
    python -m collector.monitor --since 7       # look back 7 days
    python -m collector.monitor --source proekt # a single source only
    python -m collector.monitor --verbose

State (inbox, seen-URL store, log) lives under DATA_DIR (env var, default ./data).
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import fetcher

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("osint-collector")


def data_dir():
    return Path(os.environ.get("DATA_DIR") or ROOT / "data").resolve()


def setup_logging(log_path, verbose):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Cannot read %s: %s - using default.", path, e)
        return default


def url_id(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def tag_item(item, themes, priority_kw):
    """Compute theme tags and priority/relevance from title + lead."""
    text = ((item.get("title") or "") + " \n " + (item.get("summary") or "")).lower()
    matched = []
    for theme, kws in themes.items():
        if any(kw in text for kw in kws):
            matched.append(theme)
    hot = any(kw in text for kw in priority_kw)
    covert = "covert_ops" in matched
    score = len(matched) + (3 if hot else 0) + (3 if covert else 0)
    item["themes"] = matched
    item["priority_hit"] = hot
    item["covert_ops"] = covert
    item["relevance_score"] = score
    item["priority"] = bool(hot or covert or len(matched) >= 2)
    return item


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def run(args):
    cfg = load_json(args.config, None)
    if cfg is None:
        log.error("Missing or invalid config: %s", args.config)
        return 2
    settings = cfg.get("settings", {})
    themes = cfg.get("themes", {})
    priority_kw = [k.lower() for k in cfg.get("priority_keywords", [])]

    base = data_dir()
    inbox = base / settings.get("inbox_dir", "inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    seen_path = base / settings.get("seen_path", "seen_urls.json")
    log_path = base / settings.get("log_path", "monitor.log")
    setup_logging(str(log_path), args.verbose)

    fetcher.TIMEOUT = settings.get("request_timeout", 20)
    lookback = args.since if args.since is not None else settings.get("lookback_days", 4)
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback)
    max_per = settings.get("max_items_per_source", 25)

    theme_filter = set(settings.get("theme_filter_sources", []))

    seen = load_json(seen_path, {})
    if isinstance(seen, list):
        seen = {h: "" for h in seen}

    sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    if args.source:
        sources = [s for s in sources if s["id"] == args.source]
        if not sources:
            log.error("No such (enabled) source: %s", args.source)
            return 2

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_items = []
    per_source_stats = {}
    skipped_old = skipped_seen = 0

    log.info("=== Run: %d sources, lookback=%d days, cutoff=%s ===",
             len(sources), lookback, cutoff.strftime("%Y-%m-%d"))

    def _fetch_one(src):
        try:
            return src, fetcher.fetch_source(src)
        except Exception as e:
            log.warning("[%s] unexpected error: %s", src["id"], e)
            return src, []
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=16) as _ex:
        _fetched = list(_ex.map(_fetch_one, sources))
    for src, items in _fetched:
        sid = src["id"]
        kept = 0
        for it in items[:max_per]:
            url = (it.get("url") or "").strip()
            if not url:
                continue
            pub = parse_iso(it.get("published"))
            if pub is not None and pub < cutoff:
                skipped_old += 1
                continue
            iid = url_id(url)
            if iid in seen:
                skipped_seen += 1
                continue
            tag_item(it, themes, priority_kw)
            if sid in theme_filter and it["relevance_score"] == 0:
                continue
            it["id"] = iid
            it["collected_at"] = now_iso
            new_items.append(it)
            seen[iid] = now_iso
            kept += 1
        per_source_stats[sid] = {"fetched": len(items), "new": kept}
        log.info("[%s] %d fetched, %d new", sid, len(items), kept)

    new_items.sort(key=lambda x: (x.get("priority", False), x.get("published") or ""),
                   reverse=True)

    prio = sum(1 for x in new_items if x.get("priority"))
    print("\n" + "=" * 64)
    print("  OSINT collector - " + now_iso)
    print("  New items: %d  (of which priority: %d)" % (len(new_items), prio))
    print("  Skipped: %d already seen, %d old (>%d days)" % (skipped_seen, skipped_old, lookback))
    print("=" * 64)
    for sid, st in sorted(per_source_stats.items(), key=lambda kv: -kv[1]["new"]):
        if st["new"]:
            print("  %-16s %3d new / %3d fetched" % (sid, st["new"], st["fetched"]))
    print("=" * 64)

    if args.dry_run:
        log.info("DRY-RUN: writing no files. (%d new items)", len(new_items))
        for it in new_items[:6]:
            tags = ",".join(it.get("themes", [])) or "-"
            star = "*" if it.get("priority") else " "
            print("  %s [%s] %s  (%s)" % (star, it["source"], it["title"][:68], tags))
        return 0

    if not new_items:
        log.info("No new items - no inbox file written.")
        return 0

    runid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    out_path = inbox / ("run-%s.jsonl" % runid)
    with out_path.open("w", encoding="utf-8") as f:
        for it in new_items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    seen_path.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    log.info("Written: %s (%d items). seen_urls=%d", out_path, len(new_items), len(seen))
    print("\n  -> inbox/%s  (%d items)\n" % (out_path.name, len(new_items)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="OSINT investigative monitor - collector")
    ap.add_argument("--config", default=str(ROOT / "config" / "sources.json"))
    ap.add_argument("--dry-run", action="store_true", help="write no files, only summarize")
    ap.add_argument("--since", type=int, default=None, help="lookback in days")
    ap.add_argument("--source", default=None, help="only this single source (id)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    try:
        sys.exit(run(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
