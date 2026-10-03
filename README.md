# langgraph-digest

A topic-agnostic **news/feed digest pipeline** built as an explicit **LangGraph** state machine with optional **Langfuse** tracing. You describe the digest in one JSON config (title, output language, audience, sections, priority keywords, sources); the pipeline collects public RSS/Atom feeds and public Telegram web previews, has an LLM pick the most significant stories, fetches their full text, writes the digest in Markdown, repairs hallucinated links, and saves the result. An HTML e-mail via Resend is sent only if you ask for it.

The digest is a reading aid: summaries are condensed from the sources' own reporting and are not independently verified. This project is not affiliated with any of the outlets, providers or tools it can be pointed at.

Two ready-made configs ship under [`examples/`](examples):

| Example | What it shows |
|---|---|
| [`examples/tech-news`](examples/tech-news) | **Default.** 4 well-known public tech feeds, 3 sections, a few priority keywords. The smallest complete config. |
| [`examples/ru-media-watch`](examples/ru-media-watch) | 35 enabled public RSS/Telegram sources, 4 sections, theme dictionaries, an empty keyword list. The larger, real-world config the cost numbers below were measured on. Source notes in [`sources.md`](examples/ru-media-watch/sources.md). |

The default is the tech example: it works with no edits and is the safest thing to run first. To make your own digest, copy an example folder and edit `digest.json`.

## Architecture

```mermaid
flowchart TD
    S([START]) --> collect["collect<br/>(skippable: --skip-collect)"]
    collect --> load["load<br/>inbox jsonl + prompt from config"]
    load --> rank["rank<br/>LLM #1: pick in-focus candidates"]
    rank --> fetch["fetch<br/>full text of up to focus_count articles"]
    fetch --> write["write<br/>LLM #2: digest in Markdown"]
    write --> repair["repair<br/>deterministic link repair"]
    repair -->|"unresolved links AND rewrite_count < 1"| write
    repair -->|otherwise| save["save<br/>digest + .meta.json"]
    save -->|"--send-email"| email["email<br/>Resend"]
    save -->|default| E([END])
    email --> E
```

| Node | What it does | Retry |
|---|---|---|
| `collect` | runs `python -m collector.monitor --config <config> --since <lookback_days>`: fetch RSS/Telegram, theme-tag, dedup by URL, write `inbox/run-*.jsonl` | no |
| `load` | reads the inbox (or `--inbox-file`) and builds the prompt from `prompts/digest_template.md` + the config | no |
| `rank` | LLM call #1, JSON list of candidate item numbers for the in-focus section (skipped when `focus_count` is 0) | 4 attempts, transient errors only |
| `fetch` | trafilatura full-text extraction for the top `focus_count` fetchable candidates (Telegram links skipped) | 4 attempts |
| `write` | LLM call #2, the digest; the section spec is the config's `sections` rendered into the template | 4 attempts |
| `repair` | every Markdown link URL in the draft that was not in the input is swapped for the closest collected URL (difflib, cutoff 0.9) or unwrapped to plain text | no |
| `save` | writes `<DATA_DIR>/digests/<day>_<slug>.md` and a `.meta.json` sidecar (counts, token usage, repaired/removed links, config path) | no |
| `email` | Resend, only with `--send-email` | no retry (a retry after an accepted send would duplicate the e-mail) |

## Configuration

One JSON file holds everything topic-specific. Select it with `--config PATH` or the `DIGEST_CONFIG` variable (default: `examples/tech-news/digest.json`).

Top-level blocks: `digest` (what to write), `settings` (collector behaviour), `themes` (keyword dictionaries for tagging), `sources` (feeds).

### `digest` block

| Key | Required | Meaning |
|---|---|---|
| `title` | yes | Digest name; used in the prompt and the e-mail subject |
| `slug` | yes | Output filename stem: `<day>_<slug>.md` (lowercased, non-alphanumerics become `-`) |
| `language` | no (English) | Language the digest is written in |
| `audience` | no | One line: who reads it and what they want. Drives tone and level of explanation |
| `beat` | no | What to prioritize: the topic scope in a few sentences. Used by both LLM calls |
| `focus_count` | no (3) | Number of "in focus" items whose full text is fetched and read in full; `0` disables rank and fetch |
| `priority_keywords` | no (empty) | Lowercase substrings; an item whose title or lead contains one is flagged `PRIORITY` |
| `sections` | yes | Ordered list of `{name, instruction}`; exactly one has `"focus": true` when `focus_count > 0` |

### `settings` block (all optional)

| Key | Default | Meaning |
|---|---|---|
| `lookback_days` | 4 | Items older than this are dropped |
| `max_items_per_source` | 25 | Per-source cap per run |
| `request_timeout` | 20 | Seconds per HTTP request |
| `accept_language` | `en-US,en;q=0.9` | `Accept-Language` header for fetches (set it for non-English sources) |
| `theme_filter_sources` | `[]` | Source ids that keep only items matching a theme or keyword (noise filter for high-volume desks) |
| `inbox_dir`, `archive_dir`, `digests_dir`, `seen_path`, `log_path` | see examples | State paths, relative to `DATA_DIR` |

### `themes`

An object `{theme_name: [lowercase substrings]}`. An item gets a tag for every theme with a substring in its title or lead; two or more tags, or a priority keyword, set the `PRIORITY` flag. Section instructions can refer to tags in backticks (for example "every item tagged `security`"), and the model sees them in the item listing.

### How to add a source

Add one object to `sources`:

```json
{"id": "lwn", "name": "LWN.net", "lang": "en", "method": "rss",
 "urls": ["https://lwn.net/headlines/rss"], "enabled": true, "tier": "thematic"}
```

`method` is `rss` (RSS 2.0 or Atom; `urls` may list several; an optional `"telegram": "<channel>"` is a fallback when the feed fails) or `telegram` (public web preview `t.me/s/<channel>`, field `"telegram"`). Check a new source with `python -m collector.monitor --dry-run --source <id>`.

### How to write sections

Each section is a name plus one instruction that says what goes in it, how many items, how long each, and what each entry must contain (tags, a clean link, an analytical "so what"). The template renders them as a numbered list after a fixed first item (title, window, disclaimer, tag legend):

- Put the section that gets full-text reads first and mark it `"focus": true`.
- Give counts and lengths ("5-8 items, 2-3 sentences each"); the model follows concrete limits better than adjectives.
- A catch-all section ("also trending: one line + link each") keeps lower-priority items visible without bloating the main sections.
- To make a section depend on tagging, reference a theme name from `themes`.

The generic prompt lives in [`prompts/digest_template.md`](prompts/digest_template.md); topic wording does not belong there.

## Why LangGraph

- **Retry policy per node.** A single connect timeout to the LLM endpoint killed a whole run before the pipeline was a graph. The LLM and fetch nodes now carry a `RetryPolicy(max_attempts=4, initial_interval=5.0)` (LLM read timeout 300 s) that retries only transient errors (URLError, timeouts, HTTP 408/429/5xx); everything else fails fast. The e-mail node is not retried (a timeout after an accepted send would duplicate the e-mail). Provider error bodies go to the local log only; exceptions carry the status code. Measured end-to-end on 2026-10-02 on a slow connection: the rank call timed out three times and succeeded on the fourth attempt; the run completed in 943 s (normal: 41-123 s).
- **Bounded rewrite loop.** The model sometimes invents or corrupts URLs. `repair` fixes what it can deterministically; if links still cannot be resolved, one (and only one, `MAX_REWRITES = 1`) rewrite pass goes back to `write` with the offending URLs listed. The bound lives in a pure routing function that is unit-tested.
- **Explicit state.** Everything that crosses a step (`cfg`, `picks`, `focus`, `draft`, `unresolved`, `rewrite_count`, `write_calls`) is a typed `State` key. The same state feeds the `.meta.json` sidecar and the Langfuse root-span metadata, so a run can be audited without rereading prompts.
- **Opt-in side effects.** E-mail is a separate node behind a conditional edge that is taken only with `--send-email`.

## Langfuse tracing design

Tracing is active only when `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set; otherwise `observe` is a no-op and nothing is imported from Langfuse.

- One **root span per run** (`digest-run`); every node is a child span; every LLM call is a `generation` with `usage_details` (input/output tokens) and model name.
- **Metadata-only spans.** All decorators use `capture_input=False, capture_output=False`. Spans carry counts and sizes (`system_chars`, `user_chars`, item count, rewrite count, token usage), never prompt or response text.
- **Why.** The first version captured full inputs and outputs. A run's prompts are roughly 150k tokens of article lists, and the export of those spans timed out. Metadata-only spans export reliably and still answer the operational questions (latency, tokens, retries, rewrites). If you need the text, it is in the saved digest and the inbox file.
- Uses the Langfuse Python SDK `@observe` decorator, not the LangChain callback handler: the LLM calls here are plain `urllib`, not LangChain.

## Setup

Python 3.13 was used; 3.11+ should work.

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt     # requirements-dev.txt adds pytest
cp .env.example .env            # fill in the names you need; never commit it
```

Variables (names only in `.env.example`):

| Variable | Purpose |
|---|---|
| `LLM_API_KEY_VAR` | name of the variable that holds the LLM key (default `OPENROUTER_API_KEY`) |
| `OPENROUTER_API_KEY` | the key itself (or whichever variable `LLM_API_KEY_VAR` names) |
| `LLM_BASE_URL` | OpenAI-compatible base URL (default `https://openrouter.ai/api/v1`) |
| `DEEPSEEK_MODEL` | model id (default `deepseek/deepseek-v4.1-flash`) |
| `RESEND_API_KEY`, `DIGEST_TO`, `RESEND_FROM` | e-mail, used only with `--send-email` |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | tracing, optional |
| `DIGEST_CONFIG` | path to the digest config JSON (default `examples/tech-news/digest.json`) |
| `DATA_DIR` | working data: `inbox/`, `digests/`, `seen_urls.json`, `monitor.log` (default `./data`) |
| `DIGEST_TZ` | optional time zone for the digest date (default UTC) |

The process environment always wins. Instead of exporting variables you can pass `--env-file PATH` (KEY=VALUE lines, empty values ignored). The `--env-file` is read before the tracing decorators are applied, so it can carry the `LANGFUSE_*` keys.

## Run

```bash
# full run with the default (tech-news) config: collect, rank, fetch, write, repair, save (no e-mail)
python -m digest.graph

# another config
python -m digest.graph --config examples/ru-media-watch/digest.json

# re-run on an existing inbox file, no network collection
python -m digest.graph --skip-collect --inbox-file data/inbox/run-YYYYMMDD-HHMM.jsonl

# send the e-mail as well (explicit opt-in)
python -m digest.graph --send-email --env-file .env

# collector only
python -m collector.monitor --dry-run
python -m collector.monitor --since 7 --source hackernews

# tests (offline)
python -m pytest
```

Scheduling: [`deploy/systemd/`](deploy/systemd) has a oneshot service and a timer with a placeholder schedule (daily 07:00 UTC), user and paths.

## Cost per run

Measured on 2026-10-02 on the **ru-media-watch** example (35 sources) with `deepseek/deepseek-v4.1-flash` via OpenRouter, 404 collected items: about **155k prompt tokens and 13k completion tokens** per run (two LLM calls when the rewrite loop does not fire), **41 to 123 s** wall time. One data point, one model, one day, one config; token counts scale with the number of items collected, so the 4-source tech example will be far smaller. The exact figures per run are in the `.meta.json` sidecar (`llm_calls`).

## Limitations

- **The rewrite loop rarely fires.** Deterministic link repair resolves nearly every bad URL first, so the second `write` pass is a safety net that has seen little use. Its bound is tested; its quality benefit is not well measured.
- **Transliteration and name errors are not caught mechanically.** With non-Latin sources, names of people and organisations are transliterated by the model; inconsistent or wrong spellings pass through. Link repair checks URLs, not prose.
- **Claims are not fact-checked.** Item summaries come from the sources' own reporting (some are single-source breaking channels); the model can still misstate details of the full-text pieces. Treat the digest as a lead list, verify before quoting.
- **The inbox is not archived by the graph.** `save` never moves inbox files, so on a schedule you need to rotate `DATA_DIR/inbox/` yourself or each run will include earlier files.
- **Telegram previews and some RSS feeds are fragile** (Cloudflare 403, changed endpoints); the Telegram fallback covers part of this. Source status of the ru-media-watch example is dated 2026-06-23; the tech-news feeds were checked on 2026-10-03.
- **Only the example configs have been exercised end to end.** The LLM-dependent steps (`rank`, `write`) were run on the ru-media-watch example; the tech-news example is covered by offline tests and a collector dry run only.
- **Keyword tagging is substring matching** on lowercased text: cheap and transparent, but noisy for short keywords. Tune `themes` and `priority_keywords` per topic.

## Layout

```
digest/        pipeline.py (steps as plain functions, config loading), graph.py (StateGraph, tracing, CLI)
collector/     monitor.py (tagging, dedup, inbox), fetcher.py (RSS/Atom, Telegram preview)
prompts/       digest_template.md (generic prompt, filled from the config)
examples/      tech-news/ (default), ru-media-watch/ (digest.json per example)
deploy/        systemd unit and timer
tests/         offline tests
```

## License

Apache-2.0, see [`LICENSE`](LICENSE).
