"""Pipeline building blocks for the OSINT digest: collect, rank, fetch, write, repair links, e-mail.

These are plain functions with no orchestration of their own; `digest/graph.py` wires them into a
LangGraph StateGraph. Nothing here prints or logs secret values.

Environment (see .env.example): LLM_API_KEY_VAR (name of the variable holding the LLM key),
LLM_BASE_URL, DEEPSEEK_MODEL, RESEND_API_KEY, DIGEST_TO, RESEND_FROM, DATA_DIR.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import markdown
import trafilatura

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "prompts" / "digest_spec.md"
URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
SUMMARY_CHARS = 350       # per inbox item in the prompt
FULLTEXT_CHARS = 12000    # per In-focus article
LOOKBACK_DAYS = 4


def data_dir() -> Path:
    """Working data (inbox, digests, seen-URL store, log). DATA_DIR env var, default ./data."""
    return Path(os.environ.get("DATA_DIR") or ROOT / "data").resolve()


def need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"missing environment variable {name}")
    return v


def spec_parts() -> tuple[str, str]:
    """(framing + beat, section spec) from prompts/digest_spec.md."""
    p = SPEC.read_text(encoding="utf-8")
    framing = p[p.index("## Framing"): p.index("## Sections")].replace("## Framing", "", 1).strip()
    sections = p[p.index("## Sections"):].strip()
    return framing, sections


# Per-socket-read timeout. 600 s x retries meant ~30 min before giving up on a stalled connection
# (measured 2026-10-02); 180 s was too short for ~220k-char prompts on a slow provider (3 wasted
# retries, measured the same day), so 300 s + 4 attempts.
LLM_TIMEOUT_S = 300


class LLMHTTPError(Exception):
    def __init__(self, code: int, body: str):
        super().__init__(f"LLM HTTP {code}: {body}")
        self.code = code


def llm(system: str, user: str, temperature: float = 0.3) -> tuple[str, dict]:
    base = (os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    key_var = os.environ.get("LLM_API_KEY_VAR") or "DEEPSEEK_API_KEY"
    body = {"model": os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature}
    req = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + need(key_var), "Content-Type": "application/json"})
    print(f"llm: call model={body['model']} prompt_chars={len(system) + len(user)}", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:  # the key is not in the error body
        # A real exception (not SystemExit), so the graph's RetryPolicy can see 408/429/5xx.
        raise LLMHTTPError(e.code, e.read().decode(errors="replace")[:300]) from None
    usage = out.get("usage", {}) or {}
    usage["_model"] = out.get("model", body["model"])
    return out["choices"][0]["message"]["content"].strip(), usage


def collect() -> None:
    """Run the collector as a module from the repository root; it inherits DATA_DIR from the environment."""
    r = subprocess.run([sys.executable, "-m", "collector.monitor", "--since", str(LOOKBACK_DAYS)],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    print(r.stdout.strip()[-400:])
    if r.returncode != 0:
        raise SystemExit(f"collector exit {r.returncode}: {r.stderr.strip()[-600:]}")


def load_inbox() -> tuple[list[Path], list[dict]]:
    files = sorted((data_dir() / "inbox").glob("run-*.jsonl"))
    items = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                items.append(json.loads(line))
    return files, items


def item_line(i: int, it: dict) -> str:
    s = re.sub(r"\s+", " ", it.get("summary") or "").strip()[:SUMMARY_CHARS]
    flags = ",".join(x for x in (["COVERT_OPS"] if it.get("covert_ops") else [])
                     + (["PRIORITY"] if it.get("priority") else []))
    link = it.get("url", "")
    if it.get("source_url") and it["source_url"] != link:
        link += f" | original: {it['source_url']}"
    return (f"[{i}] {it.get('source')} ({it.get('lang')}, {it.get('published', '')[:10]}) "
            f"themes={','.join(it.get('themes') or [])} {flags}\n    {it.get('title', '')}\n"
            f"    {s}\n    {link}")


def pick_focus(framing: str, listing: str, n: int) -> list[int]:
    system = (framing + "\n\nTASK: from the numbered items, choose the most significant beat-relevant stories "
              "for the 'In focus — read in full' section (favor COVERT_OPS and intelligence/influence/"
              "sanctions-evasion). Return ONLY JSON: {\"picks\": [<item numbers, best first>]} with "
              f"{n} numbers; each pick must be a different story.")
    txt, _ = llm(system, listing, temperature=0.1)
    m = re.search(r"\{.*\}", txt, re.S)
    picks = json.loads(m.group(0))["picks"] if m else []
    return [int(p) for p in picks if str(p).isdigit()]


def fetch_full(it: dict) -> tuple[str, str] | None:
    for u in (it.get("source_url"), it.get("url")):
        if not u or "t.me/" in u:
            continue
        try:
            raw = trafilatura.fetch_url(u)
            text = trafilatura.extract(raw, include_comments=False) if raw else None
        except Exception as e:  # noqa: BLE001 — one bad site must not kill the run
            print(f"fetch failed {u}: {type(e).__name__}")
            continue
        if text and len(text) > 800:
            return u, text[:FULLTEXT_CHARS]
    return None


def _norm(u: str) -> str:
    return u.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()


def repair_links(digest: str, known: set[str]) -> tuple[str, list[str], list[str]]:
    """Swap every model-written URL that is not in the input for the closest collected URL.
    Measured 2026-09-29: the model drops utm params (harmless) but also corrupts slugs
    (planirovalos -> planirov-alos, pozar -> pozhar), which gives dead links. Below the similarity
    threshold the link is unwrapped (text kept) and reported, never left pointing nowhere."""
    by_norm = {_norm(k): k for k in known}
    fixed, dropped = [], []

    def closest(u: str) -> str | None:
        n = _norm(u)
        if n in by_norm:
            return by_norm[n]
        host = n.split("/")[2] if n.count("/") >= 2 else ""
        cands = [k for k in by_norm if k.split("/")[2:3] == [host]]
        best = difflib.get_close_matches(n, cands, n=1, cutoff=0.9)
        return by_norm[best[0]] if best else None

    def md_link(m: re.Match) -> str:
        text, url = m.group(1), m.group(2)
        if url in known:
            return m.group(0)
        good = closest(url)
        if good:
            fixed.append(f"{url} -> {good}")
            return f"[{text}]({good})"
        dropped.append(url)
        return text

    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", md_link, digest)
    return out, fixed, dropped


def email_html(md_text: str) -> str:
    body = markdown.markdown(md_text, extensions=["extra", "sane_lists"])
    return ('<div style="font-family:Georgia,serif;font-size:15px;line-height:1.5;max-width:760px;'
            f'color:#1a1a1a">{body}</div>')


def send_mail(subject: str, md_text: str) -> str:
    body = {"from": os.environ.get("RESEND_FROM") or "OSINT Digest <onboarding@resend.dev>",
            "to": [need("DIGEST_TO")], "subject": subject,
            "html": email_html(md_text), "text": md_text}
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + need("RESEND_API_KEY"), "Content-Type": "application/json",
                 "User-Agent": "osint-digest-langgraph/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read()).get("id", "?")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Resend HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
