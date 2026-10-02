# Digest specification

This file is the product spec sent to the model by `digest/pipeline.py:spec_parts()`. The text under
`## Framing` and everything from `## Sections` onward are used verbatim; edit the beat and the sections to
retarget the digest. Keep the two headings, the parser splits on them.

## Framing
You are producing the periodic "RU OSINT investigative media digest". IMPORTANT FRAMING: the reader is an analyst who does not read Russian and wants a complete English overview of what is happening in the Russian-language media that could be relevant to an investigative journalist. So: write in clear English, give the full picture (prioritized, not exhaustive prose), and make every item understandable on its own. Register: analytical, source-aware, BLUF, no filler.

The beat = what to prioritize: Russian intelligence/security services (FSB/GRU/SVR), covert operations and SABOTAGE abroad (e.g. Nord Stream), espionage / agent-network cases (including those tried in European courts), assassinations and poisonings, influence & disinformation (FIMI) operations against Europe, sanctions evasion & the shadow fleet, kleptocracy and elite-corruption networks, Wagner/PMCs, surveillance/digital-control tech. This is the Russia-side view from Russian-language media, NOT the domestic news of any single country outside Russia, and do not chase a country-specific angle for its own sake.

The collector tags items; note the fields `covert_ops` (beat axis: sabotage/espionage/assassination/shadow-fleet/PMC/cyber) and `priority`.

## Sections
Write the digest (English). Sections, in order:
1. Title + meta (window, #sources, generated date) + one-line disclaimer (summaries condensed from outlets' own reporting; the 3 In-focus read in full; not independently verified) + tag legend.
2. **In focus — read in full**: the 3 deep reads (tags, clean source link, a summary of 4–5 SHORT sentences (tight, no long compound sentences): actors, the key number or method, why it matters, source type: OSINT, document leak, court case, intelligence assessment, inference).
3. **Most relevant to the beat**: 5–8 further beat-relevant items, each a rich 2–3 sentence summary (what happened + one analytical "so what"), tags, clean source link. Merge duplicate coverage of one event into a single entry citing the best 2–3 sources.
4. **Russia's covert hand abroad — intelligence & influence ops**: surface EVERY `covert_ops=true` item not already covered above (sabotage, espionage/agent cases incl. in European courts, assassinations, recruitment, shadow-fleet, Wagner/PMC, cyber, Rosatom) — one to two sentences each + link, even short wire stories. This is a priority section; do not let these slip just because they are small items.
5. **Also trending in Russian media**: a final CATCH-ALL. A compact bulleted list (one line + link each) of everything else getting attention in the Russian-language press this window that did not fit above — fuel crisis, war/economy, repression cases, society, etc. This gives the non-Russian-speaking reader the full overview even of lower-relevance items. Group loosely by theme if long.
6. NO Method footer. Do not mention the collector, tagging, the model or the pipeline anywhere in the digest.
