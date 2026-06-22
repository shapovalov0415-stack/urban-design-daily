# Canonical Prompt — Urban Design Daily Curation

This is the source of truth for what gets posted on the urban design
daily site. Anyone — human or LLM — generating content for this site
follows this document. When `daily_rss_update.py`'s filters, the
classifier, or any future AI handoff disagree with this document, the
document wins; update the code to match.

## Reader profile

The site is read by a practising urban designer based in Melbourne who
follows the discipline globally. Their interests cluster in:

- **Soft city** (Jan Gehl, David Sim) — human-scale density, mixed-use
  mid-rise, street-level social activity
- **Walkable city** (Jeff Speck) — pedestrian-first urbanism, 15-minute
  neighbourhoods
- **Landscape (urbanism)** — green infrastructure, parks, urban ecology,
  street trees, stormwater as public realm
- **Urban design** — public space, placemaking, streetscape, lanes,
  plazas, shared streets

They are **less interested** in: pure housing-finance, election
mechanics, single-building reviews, starchitecture, amusement-property
news, stock-market coverage.

## Daily output: exactly 3 articles

### Geographic composition (HARD)

- **EXACTLY 1 Australia-domestic article** — federal/state policy, an
  Australian city's planning news, an Australian academic study, etc.
- **EXACTLY 2 articles from non-Australian sources** — US / Canada /
  UK / Europe / Asia / Latin America. Aim to vary regions across the
  week but no strict daily constraint.

Fallback: if non-AU candidates are exhausted for the day (rare), allow
up to 3 AU rather than ship a partial day.

### Topic alignment (PREFERRED)

At least 2 of the 3 articles **should** hit the reader's interest area
(soft city / walkable / landscape / urban design). The third may be a
hard-policy or supply piece if it sets context. `score_entry()` in
`daily_rss_update.py` enforces this via the `SOFT_CITY` regex bonus.

### Hard exclusions (drop the candidate)

- Voting, ballot, electoral process, redistricting, voter ID,
  primary-election mechanics
- Single-building reviews ("X tower opens", "Y house wins award") with
  no planning or urban-form implication
- Lifestyle (dating, recipes, gardening, weekend reading, book of the
  week, trivia/quiz)
- Sport, celebrity, royal family, coronation
- Stock market, corporate earnings, bond yields
- Pure starchitecture without planning implications

These are enforced by `NOISE_TITLE` and `SINGLE_BUILDING_TITLE` in
`daily_rss_update.py`. When a new noise class slips through, add it to
both this section AND the regex.

## Per-article schema

```json
{
  "id":           "YYYY-MM-DD-NNN",
  "date":         "YYYY-MM-DD",          // Melbourne local
  "title":        "<article headline>",
  "source":       "<outlet name>",       // e.g. "ArchitectureAU"
  "url":          "https://...",         // real, fresh article URL
  "thumbnail":    "<placeholder; enrich_thumbnails.py replaces>",
  "summary":      "<220-280 words>",
  "whyItMatters": "<35-65 words>",
  "topics":       ["<3-5 tags>"]
}
```

### summary — 220-280 words

- Senior planner explaining the story to a peer
- Lead sentence: state the WHAT (decision, data, mechanism)
- Include named officials, cities, figures, dates **only if they appear
  in the source material**; never invent
- Past tense for events, present tense for ongoing programs
- No first-person ("I", "we"), no rhetorical questions, no
  `<cite>` tags, no markdown formatting
- If the source material has less than 80 words of substance, end with:
  "Reported by {source}. See linked article for full detail."
- Must not repeat the headline verbatim

### whyItMatters — 35-65 words

- Exactly 1-2 sentences
- **DO NOT begin with** "This is important", "It's important",
  "Policymakers should", "Important for"
- Lead with what the article DEMONSTRATES or PROVES
- For non-AU articles: when relevant, identify the precedent value for
  Australian planning
- For AU articles: identify the federal / state-level signal
- No platitudes, no rhetorical questions, no "interesting"

### topics — 3-5 tags

- First tag = country (`Australia` / `United States` / `Germany` /
  `Japan` / etc.)
- For AU articles: include `Australia` + the relevant city or state
  (`Melbourne`, `NSW`, etc.)
- Use canonical names: `Transit-Oriented Development` not `TOD`,
  `Public Realm` not `public-realm`
- Never use bare `Urban Design` alone — combine with a specific theme

## Self-validation (run before saving)

- `summary` word count 220–280 → if outside, rewrite
- `whyItMatters` word count 35–65 → if outside, rewrite
- `topics` length 3–5
- No `<cite` substring in any text field
- `summary` does not repeat `title` verbatim
- `whyItMatters` does not start with any of the banned openers above
- Geographic split: count today's articles in `data.json` (including
  the new ones) — must be 1 AU + 2 non-AU (or fallback 3 AU)
- Topic alignment: at least 2 of today's 3 articles hit the SOFT_CITY
  pattern (soft city / walkable / landscape / urban design)

## Where this prompt is enforced today

| Layer | File | Role |
|---|---|---|
| Candidate discovery | `daily_rss_update.py` `FEEDS` | RSS source list |
| Per-feed cap | `MAX_CANDIDATES_PER_FEED = 7` | Diversity / load |
| Noise filter | `NOISE_TITLE`, `SINGLE_BUILDING_TITLE` | Hard exclusions |
| Urbanism gate | `is_urban()`, `URBAN_STRONG`, `URBAN_WEAK` | Binary admit/reject |
| Reader preference scoring | `SOFT_CITY` (in `score_entry`) | +12/+5 boost |
| AU bonus | `AU_REGEX` (in `score_entry`) | +6/+3 boost |
| Composition | `select_articles()` step 1-2-3 | 1 AU + 2 non-AU split |
| Summary text | (currently) RSS description directly | Often < 220 words ⚠️ |
| WhyItMatters text | (currently) boilerplate | ⚠️ canonical analysis missing |

The two ⚠️ rows are the unsolved part: RSS descriptions can't be
guaranteed to hit 220–280 words on their own. Options to close that gap:
(a) article body extraction via `requests` + BeautifulSoup, (b) Anthropic
API call (Haiku ≈ $0.70/mo, Sonnet ≈ $1.80/mo), or (c) Opus 4.7 via
`claude -p` subscription ($0, requires Keychain ACL grant). Pick one
and the script can produce on-prompt summaries automatically.

## Change log

- 2026-06-22: Initial draft. Codified reader preference (soft city /
  walkable / landscape / urban design), tightened geographic split to
  exactly 1 AU + 2 non-AU, added SOFT_CITY scoring boost.
