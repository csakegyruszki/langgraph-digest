"""Topic-agnostic digest as a LangGraph StateGraph.

Graph: collect(skippable) -> load -> rank(LLM#1) -> fetch -> write(LLM#2) -> repair
       -> [unresolved links AND rewrite_count < 1 ? write : save] -> save -> (email only with --send-email)

All pipeline logic lives in digest/pipeline.py; this module only wires it into a graph.
Output: $DATA_DIR/digests/<day>_<slug>.md (+ .meta.json); the slug comes from the config. Inbox files are never moved.

Langfuse (optional): active only if LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set. Uses the
SDK v3+/v4 `@observe` decorator (nodes = spans, LLM calls = generations with usage_details).
Spans carry metadata only (counts, sizes, token usage), never prompt or response text.

Config comes from the process environment; `--env-file PATH` optionally loads KEY=VALUE lines into the
environment first (existing variables win, values are never printed). Note: --env-file is read at import
time (from sys.argv) so that LANGFUSE_* keys are seen before the tracing decorators are applied.

Usage: python -m digest.graph --config examples/tech-news/digest.json --skip-collect --inbox-file path/to/run-XXXX.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from . import pipeline as D

MAX_REWRITES = 1


# ---------- config / langfuse ----------
def load_env_file(path: str | None) -> None:
    """Load KEY=VALUE lines from `path` into os.environ without overriding existing variables."""
    if not path:
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if sep and k and v and not os.environ.get(k):  # empty values (as in .env.example) are ignored
            os.environ[k] = v


def _env_file_from_argv(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == "--env-file" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--env-file="):
            return a.split("=", 1)[1]
    return None


def load_llm_defaults() -> None:
    for k, v in (("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
                 ("DEEPSEEK_MODEL", "deepseek/deepseek-v4.1-flash"),
                 ("LLM_API_KEY_VAR", "OPENROUTER_API_KEY")):
        if not os.environ.get(k):  # unset or empty
            os.environ[k] = v


load_env_file(_env_file_from_argv(sys.argv))
LANGFUSE_ON = bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))
if LANGFUSE_ON:
    from langfuse import get_client, observe
else:
    get_client = None

    def observe(*a, **k):  # no-op decorator
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]
        return lambda f: f

USAGE_LOG: list[dict] = []   # one entry per real LLM call


@observe(name="llm", as_type="generation", capture_input=False, capture_output=False)
def traced_llm(system: str, user: str, temperature: float = 0.3) -> tuple[str, dict]:
    t0 = time.time()
    txt, usage = _orig_llm(system, user, temperature)
    USAGE_LOG.append({"model": usage.get("_model"), "prompt_tokens": usage.get("prompt_tokens"),
                      "completion_tokens": usage.get("completion_tokens"),
                      "seconds": round(time.time() - t0, 1)})
    if LANGFUSE_ON:
        get_client().update_current_generation(
            model=usage.get("_model"), input={"system_chars": len(system), "user_chars": len(user)},
            usage_details={"input": usage.get("prompt_tokens") or 0, "output": usage.get("completion_tokens") or 0})
    return txt, usage


_orig_llm = D.llm
D.llm = traced_llm  # pick_focus() resolves llm via the pipeline module global -> traced too


# ---------- state ----------
class State(TypedDict, total=False):
    day: str
    cfg: dict             # loaded digest config (digest.load_config)
    skip_collect: bool
    inbox_file: str | None
    send_email: bool
    framing: str
    step4: str
    items: list
    files: list
    listing: str
    picks: list
    focus: list           # [(idx, url, text)]
    user: str
    known: list
    draft: str
    unresolved: list
    fixed: list
    dropped: list
    rewrite_count: int
    write_calls: int
    out_file: str
    meta: dict


# ---------- nodes ----------
@observe(name="collect", capture_input=False, capture_output=False)
def n_collect(s: State) -> State:
    if s.get("skip_collect"):
        print("collect: skipped")
    else:
        D.collect(s["cfg"])
    return {}


@observe(name="load", capture_input=False, capture_output=False)
def n_load(s: State) -> State:
    framing, step4 = D.spec_parts(s["cfg"])
    if s.get("inbox_file"):
        f = Path(s["inbox_file"])
        files = [f]
        items = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    else:
        files, items = D.load_inbox(s["cfg"])
    listing = "\n".join(D.item_line(i, it) for i, it in enumerate(items))
    return {"framing": framing, "step4": step4, "items": items, "files": [str(x) for x in files],
            "listing": listing, "rewrite_count": 0, "write_calls": 0, "unresolved": [], "draft": ""}


@observe(name="rank", capture_input=False, capture_output=False)
def n_rank(s: State) -> State:
    n = s["cfg"]["digest"]["focus_count"]
    if n <= 0:
        return {"picks": []}
    return {"picks": D.pick_focus(s["framing"], s["listing"], n * 2)}


@observe(name="fetch", capture_input=False, capture_output=False)
def n_fetch(s: State) -> State:
    items, focus = s["items"], []
    for idx in s["picks"]:
        if 0 <= idx < len(items) and len(focus) < s["cfg"]["digest"]["focus_count"]:
            got = D.fetch_full(items[idx])
            if got:
                focus.append((idx, got[0], got[1]))
            else:
                print(f"in-focus candidate [{idx}] not fetchable, trying next")
    return {"focus": focus}


@observe(name="write", capture_input=False, capture_output=False)
def n_write(s: State) -> State:
    items, focus, day, cfg = s["items"], s["focus"], s["day"], s["cfg"]
    focus_name = D.focus_section_name(cfg)
    sources = sorted({it.get("source", "?") for it in items})
    focus_block = "\n\n".join(
        f"### IN-FOCUS [{i}] — full text fetched from {u}\n{t}" for i, u, t in focus) or "(none fetchable)"
    system = (s["framing"] + "\n\n" + s["step4"] + "\n\n"
              "OUTPUT RULES (server edition): return ONLY the digest in Markdown. The in-focus pieces "
              f"(section '{focus_name}') are the full texts given below; if fewer than "
              f"{cfg['digest']['focus_count']} are given, present only those, without explaining why. "
              "Never mention how the digest was produced (collector, tagging, model, pipeline, fetching). Links: Markdown [Outlet name](URL) with a human-readable "
              "outlet name as the visible text, never a bare URL or domain. Copy URLs exactly from the "
              "input — never build or invent one. Do not invent facts, names, numbers or dates.")
    user = (f"Generated: {day}. Window: last {D.lookback_days(cfg)} days. Items: {len(items)} from "
            f"{len(sources)} sources ({', '.join(sources)}).\n\n## IN-FOCUS FULL TEXTS\n{focus_block}\n\n"
            f"## ALL COLLECTED ITEMS\n{s['listing']}")
    known = sorted({u.rstrip(".,;") for u in D.URL_RE.findall(user)})
    rewrite = s.get("rewrite_count", 0)
    prompt_user = user
    if s.get("unresolved"):  # rewrite pass
        rewrite += 1
        prompt_user += ("\n\n## CORRECTION REQUIRED\nYour previous draft contained URLs that do not appear in the "
                        "input (they were not usable):\n" + "\n".join(f"- {u}" for u in s["unresolved"]) +
                        "\nRewrite the digest. Use ONLY URLs copied exactly from the input above; if no input "
                        "URL supports a claim, give the outlet name without a link.")
    digest, _ = D.llm(system, prompt_user)
    digest = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", digest.strip())
    return {"user": user, "known": known, "draft": digest, "rewrite_count": rewrite,
            "write_calls": s.get("write_calls", 0) + 1}


@observe(name="repair", capture_input=False, capture_output=False)
def n_repair(s: State) -> State:
    known = set(s["known"])
    digest, fixed, dropped = D.repair_links(s["draft"], known)
    unknown = sorted({u.rstrip(".,;") for u in D.URL_RE.findall(digest)} - known)
    for f in fixed:
        print(f"link repaired: {f}")
    return {"draft": digest, "fixed": fixed, "dropped": dropped, "unresolved": sorted(set(dropped + unknown))}


def route_after_repair(s: State) -> str:
    return "write" if s.get("unresolved") and s.get("rewrite_count", 0) < MAX_REWRITES else "save"


@observe(name="save", capture_input=False, capture_output=False)
def n_save(s: State) -> State:
    out_dir = D.state_dir(s["cfg"], "digests_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = D.slug(s["cfg"])
    p, n = out_dir / f"{s['day']}_{name}.md", 2
    while p.exists():
        p, n = out_dir / f"{s['day']}_{name}-v{n}.md", n + 1
    p.write_text(s["draft"].rstrip() + "\n", encoding="utf-8")
    meta = {"items": len(s["items"]), "focus_fetched": len(s["focus"]), "picks": s["picks"],
            "links_repaired": s["fixed"], "links_removed": s["dropped"], "links_unverified_final": s["unresolved"],
            "rewrite_count": s["rewrite_count"], "write_calls": s["write_calls"], "config": s["cfg"]["_path"],
            "llm_calls": USAGE_LOG, "inbox_files": s["files"], "langfuse": LANGFUSE_ON}
    p.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"digest: {p} | items={len(s['items'])} focus={len(s['focus'])} rewrites={s['rewrite_count']}")
    return {"out_file": str(p), "meta": meta}


@observe(name="email", capture_input=False, capture_output=False)
def n_email(s: State) -> State:
    mid = D.send_mail(f"{s['cfg']['digest']['title']} — {s['day']}", Path(s["out_file"]).read_text(encoding="utf-8"))
    print(f"email sent: resend id={mid}")
    return {}


def route_after_save(s: State) -> str:
    return "email" if s.get("send_email") else END


def _transient(e: Exception) -> bool:
    if isinstance(e, D.LLMHTTPError):
        return e.code in (408, 429) or e.code >= 500
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (408, 429) or e.code >= 500
    return isinstance(e, (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError))


def build_graph():
    g = StateGraph(State)
    for name, fn in [("collect", n_collect), ("load", n_load), ("rank", n_rank), ("fetch", n_fetch),
                     ("write", n_write), ("repair", n_repair), ("save", n_save), ("email", n_email)]:
        # Network-bound nodes retry on transient errors (measured 2026-10-02: a connect timeout to the
        # LLM endpoint killed a whole run when llm() had no retry).
        retry = (RetryPolicy(max_attempts=4, initial_interval=5.0, retry_on=_transient)
                 if name in ("rank", "fetch", "write") else None)
        # Never "email": a timeout after Resend accepted the message would send a duplicate.
        g.add_node(name, fn, retry_policy=retry)
    g.add_edge(START, "collect")
    for a, b in [("collect", "load"), ("load", "rank"), ("rank", "fetch"), ("fetch", "write"), ("write", "repair")]:
        g.add_edge(a, b)
    g.add_conditional_edges("repair", route_after_repair, {"write": "write", "save": "save"})
    g.add_conditional_edges("save", route_after_save, {"email": "email", END: END})
    g.add_edge("email", END)
    return g.compile()


@observe(name="digest-run", capture_input=False, capture_output=False)
def run_graph(graph, init: dict) -> dict:
    """One Langfuse trace per run: every node/LLM span nests under this root span."""
    final = graph.invoke(init)
    if LANGFUSE_ON:
        get_client().update_current_span(metadata={
            "day": final.get("day"), "items": len(final.get("items") or []),
            "rewrite_count": final.get("rewrite_count"), "llm_calls": len(USAGE_LOG)})
    return final


def _today() -> str:
    tz = os.environ.get("DIGEST_TZ", "").strip()
    if tz:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz)).date().isoformat()
    return datetime.now(timezone.utc).date().isoformat()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", help="digest config JSON (default: $DIGEST_CONFIG, else examples/tech-news/digest.json)")
    ap.add_argument("--skip-collect", action="store_true", help="reuse existing inbox files")
    ap.add_argument("--no-email", action="store_true", help="accepted for parity; email is off by default")
    ap.add_argument("--send-email", action="store_true", help="explicit opt-in to send via Resend")
    ap.add_argument("--inbox-file", help="feed a specific jsonl; files are never moved")
    ap.add_argument("--env-file", help="optional KEY=VALUE file loaded into the environment (existing vars win)")
    a = ap.parse_args(argv)
    send = a.send_email and not a.no_email

    load_env_file(a.env_file)
    load_llm_defaults()
    print("langfuse: enabled" if LANGFUSE_ON else "langfuse: disabled (no keys)")
    cfg = D.load_config(a.config)
    print(f"config: {cfg['_path']}")
    graph = build_graph()
    t0 = time.time()
    final = run_graph(graph, {"day": _today(), "cfg": cfg, "skip_collect": a.skip_collect, "inbox_file": a.inbox_file,
                              "send_email": send})
    tok = [(u["prompt_tokens"], u["completion_tokens"]) for u in USAGE_LOG]
    print(f"done in {time.time() - t0:.0f}s | llm_calls={len(USAGE_LOG)} tokens(prompt,completion)={tok} "
          f"rewrite_count={final['rewrite_count']}")
    if LANGFUSE_ON:
        get_client().flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
