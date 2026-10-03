"""Offline tests: graph topology, routing bounds, env-file handling, spec parsing, link repair.

No network, no LLM calls, no API keys needed.
"""
import os

from digest import graph as G
from digest import pipeline as P


def test_graph_compiles_with_expected_topology():
    g = G.build_graph().get_graph()
    nodes = set(g.nodes) - {"__start__", "__end__"}
    assert nodes == {"collect", "load", "rank", "fetch", "write", "repair", "save", "email"}
    edges = {(e.source, e.target) for e in g.edges}
    for e in [("collect", "load"), ("load", "rank"), ("rank", "fetch"), ("fetch", "write"),
              ("write", "repair"), ("repair", "write"), ("repair", "save"), ("email", "__end__")]:
        assert e in edges, e


def test_route_after_repair_bounded():
    assert G.MAX_REWRITES == 1
    assert G.route_after_repair({"unresolved": ["x"], "rewrite_count": 0}) == "write"
    assert G.route_after_repair({"unresolved": ["x"], "rewrite_count": 1}) == "save"
    assert G.route_after_repair({"unresolved": [], "rewrite_count": 0}) == "save"


def test_email_requires_explicit_flag():
    assert G.route_after_save({"send_email": False}) == G.END
    assert G.route_after_save({}) == G.END
    assert G.route_after_save({"send_email": True}) == "email"


def test_retry_policy_only_on_network_nodes():
    nodes = G.build_graph().nodes
    for name in ("rank", "fetch", "write"):
        assert nodes[name].retry_policy, name
    # email is deliberately NOT retried: a timeout after the provider accepted the send would duplicate it
    for name in ("collect", "load", "repair", "save", "email"):
        assert not nodes[name].retry_policy, name


def test_transient_classifier():
    import urllib.error
    assert G._transient(TimeoutError())
    assert G._transient(urllib.error.URLError("x"))
    assert not G._transient(ValueError("x"))


TECH = P.ROOT / "examples" / "tech-news" / "digest.json"
RU = P.ROOT / "examples" / "ru-media-watch" / "digest.json"


def test_spec_parts_split():
    framing, sections = P.spec_parts(P.load_config(TECH))
    assert framing and not framing.startswith("##")
    assert sections.startswith("## Sections")
    assert "{{" not in framing + sections  # every placeholder was filled


def test_prompt_contains_every_configured_section_header():
    cfg = P.load_config(TECH)
    framing, sections = P.spec_parts(cfg)
    prompt = framing + "\n" + sections
    names = [s["name"] for s in cfg["digest"]["sections"]]
    assert len(names) == 3
    for i, name in enumerate(names, 2):
        assert f"{i}. **{name}**" in prompt, name
    assert cfg["digest"]["title"] in prompt and cfg["digest"]["audience"] in prompt


def test_both_examples_load_and_default_is_tech():
    assert P.DEFAULT_CONFIG == TECH
    tech, ru = P.load_config(TECH), P.load_config(RU)
    assert P.slug(tech) == "tech-news" and P.slug(ru) == "ru-media-watch"
    assert len(ru["sources"]) == 36 and sum(1 for x in ru["sources"] if x.get("enabled", True)) == 35
    assert ru["digest"]["priority_keywords"] == []
    assert P.focus_section_name(tech) == "In focus"


def test_load_config_rejects_missing_focus_section(tmp_path):
    import json
    import pytest
    cfg = json.loads(TECH.read_text(encoding="utf-8"))
    for s in cfg["digest"]["sections"]:
        s.pop("focus", None)
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(SystemExit):
        P.load_config(f)


def test_tag_item_uses_config_keywords():
    from collector.monitor import tag_item
    cfg = P.load_config(TECH)
    it = tag_item({"title": "New zero-day in a Linux kernel driver", "summary": ""}, cfg["themes"],
                  cfg["digest"]["priority_keywords"])
    assert it["priority"] and "security" in it["themes"] and "open_source" in it["themes"]


def test_env_file_does_not_override_and_skips_empty(tmp_path, monkeypatch):
    f = tmp_path / "x.env"
    f.write_text("# comment\nA_SET=from_file\nA_EMPTY=\nA_KEEP=file\n", encoding="utf-8")
    monkeypatch.setenv("A_KEEP", "process")
    monkeypatch.delenv("A_SET", raising=False)
    monkeypatch.delenv("A_EMPTY", raising=False)
    G.load_env_file(str(f))
    assert os.environ["A_SET"] == "from_file"
    assert os.environ["A_KEEP"] == "process"
    assert "A_EMPTY" not in os.environ
    monkeypatch.delenv("A_SET", raising=False)


def test_data_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert P.data_dir() == tmp_path.resolve()


def test_repair_links_swaps_corrupted_slug_and_unwraps_unknown():
    # Slug corruption pattern measured 2026-09-29 (see repair_links docstring).
    known = {"https://example.org/news/pozar-na-sklade"}
    md = ("[A](https://example.org/news/pozhar-na-sklade) "
          "[B](https://other.example/never-collected) [C](https://example.org/news/pozar-na-sklade)")
    out, fixed, dropped = P.repair_links(md, known)
    assert "[A](https://example.org/news/pozar-na-sklade)" in out
    assert len(fixed) == 1
    assert dropped == ["https://other.example/never-collected"]
    assert "(https://other.example" not in out and "[B]" not in out


# --- retry behaviour (synthetic exceptions, labelled: they stand in for network failures that
# were observed on 2026-10-02 — a read timeout and HTTP errors from the LLM endpoint) ---------

def test_transient_classifier_covers_llm_http_errors():
    from digest import pipeline as P
    from digest.graph import _transient
    assert _transient(P.LLMHTTPError(503, "x")) and _transient(P.LLMHTTPError(429, "x"))
    assert not _transient(P.LLMHTTPError(401, "x"))
    assert _transient(TimeoutError("The read operation timed out"))


def test_retry_policy_reruns_node_on_timeout():
    from typing import TypedDict
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import RetryPolicy
    from digest.graph import _transient

    class S(TypedDict, total=False):
        n: int

    calls = {"n": 0}

    def node(s):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("The read operation timed out")
        return {"n": calls["n"]}

    g = StateGraph(S)
    g.add_node("w", node, retry_policy=RetryPolicy(max_attempts=4, initial_interval=0.01, retry_on=_transient))
    g.add_edge(START, "w")
    g.add_edge("w", END)
    assert g.compile().invoke({}) == {"n": 3}
