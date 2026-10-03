#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collector/fetcher.py - source adapters for the digest collector.

Source adapters, all using only the Python stdlib (no pip dependency):
  - parse_rss / fetch_rss   : RSS 2.0 + Atom feeds
  - fetch_telegram          : t.me/s/<channel> public web preview (fallback for outlets without a feed)

Every adapter returns a uniform item dict:
  {source_id, source, lang, method, title, url, published, published_raw, summary, source_url}
Theme tagging and dedup happen in monitor.py.
"""

import urllib.request
import urllib.error
import gzip
import io
import re
import html
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

log = logging.getLogger("digest-collector.fetcher")

# Browser-like headers: reduce trivial 403/bot blocks.
# (Does not help against a Cloudflare JS challenge -> the Telegram fallback catches those.)
DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}

TIMEOUT = 20


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url, timeout=TIMEOUT, referer=None):
    """GET a URL and return bytes. Handles gzip/deflate compression."""
    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            try:
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            except OSError:
                pass
        elif "deflate" in enc:
            import zlib
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\x0b\f\r]+")
_MULTINL_RE = re.compile(r"\n{3,}")


def clean_html(s):
    """HTML -> plain text: <br>/<p> -> newline, tags stripped, entities resolved."""
    if not s:
        return ""
    s = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
    s = re.sub(r"(?i)</\s*(p|div|li)\s*>", "\n", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s)
    s = _MULTINL_RE.sub("\n\n", s)
    return s.strip()


def to_iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_datetime(s):
    """Best-effort date parse: RFC822 (RSS pubDate) and ISO8601 (Atom)."""
    if not s:
        return None
    s = s.strip()
    # RFC822 / RFC2822 (e.g. 'Tue, 07 Apr 2026 12:49:21 GMT')
    try:
        return to_iso(parsedate_to_datetime(s))
    except (TypeError, ValueError, IndexError):
        pass
    # ISO8601 (e.g. '2026-06-22T10:00:00+00:00' or '...Z')
    try:
        s2 = s.replace("Z", "+00:00")
        return to_iso(datetime.fromisoformat(s2))
    except ValueError:
        pass
    # date only
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return to_iso(datetime.strptime(s[:10], fmt))
        except ValueError:
            continue
    return None


def _localname(tag):
    """Tag name without namespace ('{http://...}entry' -> 'entry')."""
    return tag.rsplit("}", 1)[-1].lower()


def _find_text(elem, names):
    """Text of the first matching child element (namespace-independent, list of names)."""
    names = {n.lower() for n in names}
    for child in elem.iter():
        if _localname(child.tag) in names:
            if child.text and child.text.strip():
                return child.text.strip()
            # Atom <content>/<summary> may contain HTML as child elements
            inner = "".join(ET.tostring(c, encoding="unicode") for c in child)
            if inner.strip():
                return inner.strip()
    return None


def _find_link(elem):
    """Extract the link: RSS <link>text</link> or Atom <link href=...>."""
    # Atom: prefer rel="alternate" / no rel + type html
    atom_alt = None
    atom_any = None
    rss_link = None
    for child in elem:
        ln = _localname(child.tag)
        if ln == "link":
            href = child.get("href")
            if href:
                rel = (child.get("rel") or "alternate").lower()
                if rel == "alternate" and atom_alt is None:
                    atom_alt = href
                if atom_any is None:
                    atom_any = href
            elif child.text and child.text.strip():
                rss_link = child.text.strip()
    return rss_link or atom_alt or atom_any


# --------------------------------------------------------------------------- #
# RSS / Atom
# --------------------------------------------------------------------------- #
def parse_rss_bytes(data, source):
    """RSS 2.0 or Atom bytes -> item list."""
    items = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        # Some feeds have a BOM or junk before the root -> try cutting at the first '<'
        try:
            txt = data.decode("utf-8", "replace")
            cut = txt.find("<")
            root = ET.fromstring(txt[cut:]) if cut > 0 else None
            if root is None:
                raise e
        except Exception:
            log.warning("[%s] RSS parse error: %s", source.get("id"), e)
            return items

    rootname = _localname(root.tag)
    if rootname == "feed":               # Atom
        entries = [c for c in root if _localname(c.tag) == "entry"]
        date_names = ("published", "updated", "issued")
        body_names = ("summary", "content")
    else:                                 # RSS (rss > channel > item)
        entries = [c for c in root.iter() if _localname(c.tag) == "item"]
        date_names = ("pubdate", "date", "published")
        body_names = ("description", "encoded", "summary")

    for e in entries:
        title = _find_text(e, ("title",)) or ""
        link = _find_link(e) or ""
        raw_date = _find_text(e, date_names)
        body = _find_text(e, body_names) or ""
        title = clean_html(title)
        summary = clean_html(body)
        if not (title or link):
            continue
        items.append({
            "source_id": source["id"],
            "source": source["name"],
            "lang": source.get("lang", "en"),
            "method": "rss",
            "title": title,
            "url": link.strip(),
            "published": parse_datetime(raw_date),
            "published_raw": raw_date,
            "summary": summary,
            "source_url": link.strip(),
        })
    return items


def fetch_rss(source):
    """Try each URL in source['urls'] and return the first non-empty result."""
    last_err = None
    for url in source.get("urls", []):
        try:
            data = http_get(url, referer=url)
            items = parse_rss_bytes(data, source)
            if items:
                log.info("[%s] RSS OK (%d items) <- %s", source["id"], len(items), url)
                return items
            log.info("[%s] RSS empty response <- %s", source["id"], url)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as ex:
            last_err = ex
            log.warning("[%s] RSS error (%s) <- %s", source["id"], ex, url)
    if last_err:
        log.warning("[%s] all RSS URLs failed", source["id"])
    return []


# --------------------------------------------------------------------------- #
# Telegram /s/ web preview
# --------------------------------------------------------------------------- #
_TG_MSG_RE = re.compile(r'data-post="([^"]+)"')
_TG_TEXT_RE = re.compile(
    r'js-message_text"[^>]*>(.*?)</div>\s*(?:<div class="tgme_widget_message_(?:footer|reply|forwarded|sticker)|<div class="tgme_widget_message_bubble">)',
    re.S)
_TG_TEXT_FALLBACK_RE = re.compile(r'js-message_text"[^>]*>(.*?)</div>', re.S)
_TG_TIME_RE = re.compile(r'<time[^>]+datetime="([^"]+)"')
_TG_HREF_RE = re.compile(r'href="(https?://[^"]+)"')


def fetch_telegram(source):
    """Parse the public t.me/s/<channel> preview into an item list."""
    channel = source.get("telegram")
    if not channel:
        return []
    url = "https://t.me/s/%s" % channel
    try:
        data = http_get(url, referer="https://t.me/")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as ex:
        log.warning("[%s] Telegram error (%s) <- %s", source["id"], ex, url)
        return []

    htmltext = data.decode("utf-8", "replace")
    # In the preview every message is one "tgme_widget_message_wrap" block.
    chunks = htmltext.split('js-widget_message_wrap')
    items = []
    for chunk in chunks[1:]:
        m_post = _TG_MSG_RE.search(chunk)
        if not m_post:
            continue
        post = m_post.group(1)                       # 'channel/12345'
        m_text = _TG_TEXT_RE.search(chunk) or _TG_TEXT_FALLBACK_RE.search(chunk)
        if not m_text:
            continue                                  # media-only post, skipped
        text = clean_html(m_text.group(1))
        if not text or len(text) < 20:
            continue
        m_time = _TG_TIME_RE.search(chunk)
        published = parse_datetime(m_time.group(1)) if m_time else None
        # title = first meaningful line (~140 chars)
        first_line = text.split("\n", 1)[0].strip()
        title = first_line[:140] + ("…" if len(first_line) > 140 else "")
        # external article link (if any): first non-Telegram http link in the text block
        source_url = ""
        for href in _TG_HREF_RE.findall(m_text.group(1)):
            if "t.me" not in href and "telegram.org" not in href and "telesco.pe" not in href:
                source_url = href
                break
        permalink = "https://t.me/%s" % post
        items.append({
            "source_id": source["id"],
            "source": source["name"],
            "lang": source.get("lang", "en"),
            "method": "telegram",
            "title": title,
            "url": permalink,
            "published": published,
            "published_raw": m_time.group(1) if m_time else None,
            "summary": text[:1200],
            "source_url": source_url or permalink,
        })
    log.info("[%s] Telegram OK (%d items) <- @%s", source["id"], len(items), channel)
    return items


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #
def fetch_source(source):
    """Collect one source by its method, with RSS -> Telegram fallback."""
    method = source.get("method", "rss")
    items = []
    if method == "rss":
        items = fetch_rss(source)
        if not items and source.get("telegram"):
            log.info("[%s] RSS empty -> Telegram fallback @%s",
                     source["id"], source["telegram"])
            items = fetch_telegram(source)
    elif method == "telegram":
        items = fetch_telegram(source)
    else:
        log.warning("[%s] unknown method: %s", source["id"], method)
    return items
