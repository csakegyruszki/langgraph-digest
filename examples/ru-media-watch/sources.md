# Source inventory

**35 active sources** (config: `digest.json` in this folder). Tested live and checked against public information on **2026-06-23**. All outlets are real, editorially independent (Vot Tak excepted, see below) and active. Most carry a "foreign agent" or "undesirable" designation and are blocked inside Russia; for this category that is expected, not a warning sign. Only public RSS feeds and public Telegram web previews are used; no logins, no tokens.

## Collection method per source

| # | Source | Tier | Method | Endpoint | Fallback | Notes |
|---|--------|------|--------|----------|----------|-------|
| 1 | Meduza | core | RSS | `meduza.io/rss/en/all` | tg `meduzalive` | undesirable; Riga |
| 2 | iStories | core | **TG** | tg `istories_media` | none | RSS unreliable (/rss 403), so Telegram |
| 3 | Proekt | core | RSS | `proekt.media/feed` | tg `proektproekt` | first outlet designated "undesirable" |
| 4 | The Insider | core | RSS | `theins.ru/feed` | none | undesirable |
| 5 | Mediazona | core | RSS | `zona.media/rss` | tg `mediazzzona` | foreign agent |
| 6 | Novaya Gazeta Europe | core | RSS | `novayagazeta.eu/feed/rss` | tg `novaya_europe` | undesirable |
| 7 | Agentstvo | core | TG | tg `agentstvonews` | none | Proekt alumni team |
| 8 | Verstka | core | RSS | `verstka.media/feed` | none | foreign agent |
| 9 | Holod | core | RSS | `holod.media/feed/` | tg `holodmedia` | foreign agent |
| 10 | Dossier Center | core | TG | tg `dossiercenter` | none | no RSS |
| 11 | Bellingcat | core | RSS | `bellingcat.com/feed/` | none | OSINT, NL/UK |
| 12 | OCCRP | core | RSS | `occrp.org/en/feed` | none | consortium, US |
| 13 | The Moscow Times | desk | RSS | `themoscowtimes.com/rss/news` | none | foreign agent; Amsterdam |
| 14 | The Bell | desk | RSS | `thebell.io/feed` | none | economy; foreign agent |
| 15 | Vot Tak (Belsat) | desk | RSS | `vot-tak.tv/rss` | tg `vottaktv` | **Belsat/TVP (Polish public service), not an independent Russian outlet** |
| 16 | SOTAvision | desk | RSS | `sotavision.world/feed` | none | Sota.Vision, activist/livestream, tip source |
| 17 | ASTRA | desk | RSS | `astra.press/feed` | tg `astrapress` | fast breaking, lead generator |
| 18 | Mozhem Obyasnit | desk | TG | tg `mozhemobyasnit` | none | site pointmedia.io |
| 19 | Systema (RFE/RL) | desk | TG | tg `systemasystema` | none | RFE/RL investigations; blocked 2026-02 |
| 20 | Cherta | desk | RSS | `cherta.media/feed` | none | foreign agent 2025-11 |
| 21 | Republic | desk | **TG** | tg `republicmag` | none | republic.ru is archive only; no live RSS |
| 22 | Vlast (ex-Faridaily) | desk | RSS | `vlast.is/feed` | none | Faridaily renamed in 2026 |
| 23 | TV Rain (Dozhd) | desk | **TG** | tg `tvrain` | none | exile Russian-language TV, Amsterdam; no public RSS |
| 24 | Spektr | desk | RSS | `spektr.press/feed/` | none | exile (Latvia) news/analysis |
| 25 | Echo FM | desk | RSS | `echofm.online/feed` | tg `echofm_online` | successor of the banned Echo of Moscow, Berlin |
| 26 | Zerkalo | desk | **TG** | tg `zerkalo_io` | none | exile (Lithuania), strong Belarus focus |
| 27 | Kedr.media (environment) | thematic | RSS | `kedr.media/feed` | tg `kedr_media` | relaunched, active |
| 28 | People of Baikal | thematic | RSS | `baikal-journal.ru/feed` | none | foreign agent 2026-01 |
| 29 | T-invariant (science) | thematic | RSS | `t-invariant.org/feed` | none | science/academia |
| 30 | Conflict Intelligence Team | thematic | TG | tg `CITeam` | none | undesirable; military OSINT |
| 31 | Current Time (RFE/RL) | thematic | TG | tg `currenttime` | none | RFE/RL; reorganized 2026-05 |
| 32 | 7x7 Horizontal Russia | thematic | RSS | `semnasem.org/rss/default.xml` | tg `horizontal_russia` | regional; active |
| 33 | Sirena | thematic | TG | tg `news_sirena` | none | fast regional/breaking (theme-filtered) |
| 34 | Govorit NeMoskva | thematic | RSS | `nemoskva.net/feed` | tg `Govorit_NeMoskva` | provincial reporting; FA 2024-11 |
| 35 | Bumaga (Paper) | thematic | RSS | `paperpaper.io/feed` | tg `paperpaper_ru` | St Petersburg; CF-403 so Telegram fallback |

(Plus `meduza_ru`, the full Meduza RU feed, disabled by default because it overlaps with the EN feed.)

## Corrections found during verification (2026-06-23)
- **iStories**: Telegram (RSS endpoints return no items to feed readers).
- **Systema** handle: `@systema_project` is wrong, use **`@systemasystema`** (RFE/RL canonical).
- **Republic**: `republic.ru` is archive only; live content has no RSS, so Telegram `@republicmag`.
- **Faridaily to Vlast** (`vlast.is/feed`).
- **ASTRA**: a full site exists, so RSS (`astra.press/feed`).
- **Mediazona** Telegram fallback: `@mediazzzona` (three z).
- **CIT** canonical handle: `@CITeam`. **Holod**: `/feed/` (trailing slash). **7x7**: `semnasem.org/rss/default.xml` plus `@horizontal_russia`.

## Profile notes
- **Vot Tak**: funded by Polish public broadcasting (Belsat/TVP); independent of the Kremlin, but not an independent Russian outlet.
- **SOTAvision**: Sota.Vision (foreign agent), activist/livestream, lower verification threshold; the separate "SOTA" is the undesirable one.
- **ASTRA / Sirena**: fast, single-source breaking news; lead generators, verify before quoting. Both are theme-filtered in the config.

## Technical traps
- **Cloudflare 403** for plain urllib: Proekt, Bumaga (`paperpaper.io`); the Telegram fallback catches them.
- **Test with urllib**: a few feeds come back empty through generic web-fetch tools but work with urllib.
- **Telegram `/s/` preview**: the message block comes after `tgme_widget_message_wrap` (not within the first ~2.5 KB).
