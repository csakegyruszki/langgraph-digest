# Digest prompt template

This file is the generic prompt sent to the model; `digest/pipeline.py:spec_parts()` fills the `{{...}}`
placeholders from the `digest` block of the config (`title`, `audience`, `language`, `beat`,
`focus_count`, `sections`). The text under `## Framing` and everything from `## Sections` onward is
used; keep the two headings, the parser splits on them. Topic-specific wording belongs in the config, not here.

## Framing
You are producing the periodic "{{title}}". IMPORTANT FRAMING: the reader is {{audience}}. Write in {{language}}, give the full picture (prioritized, not exhaustive prose), and make every item understandable on its own. Register: analytical, source-aware, BLUF, no filler.

The beat = what to prioritize: {{beat}}

The collector tags items with `themes` and a `PRIORITY` flag (the item matched a configured priority keyword or several themes).

## Sections
Write the digest ({{language}}). Sections, in order:
1. Title + meta (window, number of sources, generated date) + one-line disclaimer (summaries condensed from the outlets' own reporting; the {{focus_count}} in-focus pieces are read in full; not independently verified) + tag legend.
{{sections}}
Do NOT add a Method footer. Do not mention the collector, tagging, the model or the pipeline anywhere in the digest.
