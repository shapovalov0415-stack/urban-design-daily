#!/usr/bin/env python3
"""Generate today's 3 urban-design news articles via Claude + web_search.

- Reads existing data.json to dedupe by URL.
- Calls Anthropic API with the server-side web_search tool to find fresh stories.
- Writes 3 new entries into data.json.
- Saves a Markdown archive into archive/urban_design_news_YYYY-MM-DD.md.

Required env:
  ANTHROPIC_API_KEY  - Anthropic Console API key
  ANTHROPIC_MODEL    - (optional) model id, defaults to claude-sonnet-4-5
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import anthropic  # type: ignore
except ImportError:
    print("ERROR: anthropic package missing. Run: pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    from zoneinfo import ZoneInfo  # py3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data.json"
ARCHIVE_DIR = ROOT / "archive"

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# Hard guarantees for the daily run.
TARGET_ARTICLES = 3
MIN_AUSTRALIA = 1  # at least this many of TARGET must be Australia-domestic
MAX_ATTEMPTS = 4
# Anthropic free/standard tier is 30k input tokens/min. A single web_search
# call in this script burns ~10–20k tokens, so back-to-back retries trip the
# limit. Sleep ~70s between attempts to let the per-minute window roll over.
RETRY_BACKOFF_SEC = 70

# Topics that mark an article as Australia-domestic. The prompt instructs the
# model to use these exact tags when picking Australian content; matching is
# case-insensitive.
AU_TAGS = frozenset(
    {
        "australia",
        "australian",
        "melbourne",
        "sydney",
        "brisbane",
        "perth",
        "adelaide",
        "canberra",
        "hobart",
        "darwin",
        "gold coast",
        "newcastle",
        "wollongong",
        "geelong",
        "victoria",
        "nsw",
        "queensland",
        "wa",
        "sa",
        "tas",
        "nt",
        "act",
    }
)

# Site is read by a Melbourne-based user; date the digest by Melbourne local
# day so the calendar matches what the reader sees on their morning. Handles
# AEST/AEDT automatically via tzdata.
MELBOURNE = ZoneInfo("Australia/Melbourne")
TODAY = _dt.datetime.now(MELBOURNE).date()
TODAY_STR = TODAY.isoformat()

PROMPT_TEMPLATE = """\
You are curating a daily English-language digest of urban-design news for a
practising urban designer based in Melbourne who reads globally.

Today is {today} (Melbourne local date). Find {needed} fresh article(s)
published in the last ~7 days from reputable urban-design / architecture /
planning outlets. Aim for a balanced mix across regions and themes (housing,
public realm, transit/TOD, zoning & policy, climate adaptation, heritage).
Avoid pure starchitecture / building-only stories — favour pieces with
planning, policy, urban form, or city-scale implications.

REGIONAL CONSTRAINT — HARD REQUIREMENT: The full daily set of 3 must include
at least 1 Australia-domestic article (federal/state policy, an Australian
city's planning news, Australian housing/transit, etc. — Melbourne, Sydney,
Brisbane, Adelaide, Perth, Canberra, Hobart, Darwin, or any Australian
state). For Australian articles, include "Australia" plus the relevant
city/state name in the topics array (e.g. ["Australia", "Melbourne",
"Housing"]) so the pipeline can detect them. Of the {needed} article(s) you
pick in THIS batch, **{au_needed}** MUST be Australia-domestic (today's set
so far has {au_have} Australian).

DEDUP — CRITICAL: do NOT pick any article whose URL is in this list. If you
are about to pick one, pick a different story instead. Picking duplicates
breaks the pipeline.
{existing_urls}

Use web_search aggressively. Prefer ArchitectureAU, Planetizen, The Guardian
Cities, CityLab/Bloomberg, Dezeen Urbanism, Next City, Strong Towns, Smart
Cities Dive, ArchDaily Urbanism, etc. Each chosen article must come from a
real, reachable URL you found via web_search.

For each article, write:
- title: the article's headline
- source: outlet name (e.g. "ArchitectureAU", "Planetizen")
- url: canonical URL
- summary: 200-300 words. In-depth, factual, reads like a senior planner
  explaining the story to a peer. Include numbers, locations, named people
  where they appear.
- whyItMatters: 1-2 sentences on the implication for urban designers.
- topics: 3-5 short tags (e.g. ["Melbourne", "Housing", "Policy"]).

Return EXACTLY {needed} article object(s) — no more, no fewer — as a JSON
object with no prose before or after, wrapped in a fenced ```json``` code
block, of the form:

```json
{{
  "articles": [
    {{
      "title": "...",
      "source": "...",
      "url": "https://...",
      "summary": "...",
      "whyItMatters": "...",
      "topics": ["...", "..."]
    }}
  ]
}}
```
"""


def load_data() -> dict:
    if DATA_PATH.exists():
        with DATA_PATH.open() as f:
            return json.load(f)
    return {"articles": []}


def existing_urls(data: dict) -> list[str]:
    return [a.get("url", "") for a in data.get("articles", []) if a.get("url")]


def is_australia(article: dict) -> bool:
    """True if any topic tag matches an Australian region/city marker."""
    topics = {(t or "").lower() for t in (article.get("topics") or [])}
    return bool(topics & AU_TAGS)


def today_articles(data: dict) -> list[dict]:
    return [
        a for a in data.get("articles", []) if a.get("date", "").startswith(TODAY_STR)
    ]


def call_claude(existing: list[str], needed: int, au_needed: int, au_have: int) -> dict:
    client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env
    prompt = PROMPT_TEMPLATE.format(
        today=TODAY_STR,
        needed=needed,
        au_needed=au_needed,
        au_have=au_have,
        existing_urls=json.dumps(existing[-100:], indent=2, ensure_ascii=False),
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 8,
            }
        ],
        messages=[{"role": "user", "content": prompt}],
    )
    # Concatenate all text blocks (the final assistant turn after web_search loops).
    text_parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    text = "\n".join(text_parts).strip()
    if not text:
        raise RuntimeError(f"Empty model response. Stop reason: {resp.stop_reason!r}")
    return parse_json(text)


def parse_json(text: str) -> dict:
    # Try fenced ```json ... ```
    m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        return json.loads(m.group(1))
    # Try fenced ``` ... ```
    m = re.search(r"```\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        return json.loads(m.group(1))
    # Last resort: first {...} block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"Could not extract JSON from model response:\n{text[:600]}")


def next_id_seq(data: dict, date_str: str) -> int:
    pattern = re.compile(rf"^{re.escape(date_str)}-(\d{{3}})$")
    nums = [
        int(m.group(1))
        for a in data.get("articles", [])
        if (m := pattern.match(a.get("id", "")))
    ]
    return (max(nums) + 1) if nums else 1


def append_articles(data: dict, new_articles: list[dict]) -> list[dict]:
    seq = next_id_seq(data, TODAY_STR)
    existing = set(existing_urls(data))
    appended: list[dict] = []
    for a in new_articles:
        url = (a.get("url") or "").strip()
        if not url or url in existing:
            continue
        existing.add(url)
        entry = {
            "id": f"{TODAY_STR}-{seq:03d}",
            "date": TODAY_STR,
            "title": (a.get("title") or "").strip(),
            "source": (a.get("source") or "").strip(),
            "url": url,
            # Placeholder thumbnail — enrich_thumbnails.py replaces with og:image.
            "thumbnail": "https://images.unsplash.com/photo-1514565131-fce0801e5785?w=800",
            "summary": (a.get("summary") or "").strip(),
            "whyItMatters": (a.get("whyItMatters") or "").strip(),
            "topics": list(a.get("topics") or []),
        }
        data.setdefault("articles", []).append(entry)
        appended.append(entry)
        seq += 1
    return appended


def write_archive(appended: list[dict]) -> Path:
    ARCHIVE_DIR.mkdir(exist_ok=True)
    path = ARCHIVE_DIR / f"urban_design_news_{TODAY_STR}.md"
    parts = [f"# Urban Design News — {TODAY_STR}\n"]
    for a in appended:
        parts.append(f"## {a['title']}\n")
        parts.append(f"**Source:** {a['source']}  ")
        parts.append(f"**URL:** {a['url']}  ")
        parts.append(f"**Topics:** {', '.join(a['topics'])}\n")
        parts.append(a["summary"] + "\n")
        parts.append(f"**Why it matters:** {a['whyItMatters']}\n")
        parts.append("---\n")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def main() -> int:
    data = load_data()

    today_arts = today_articles(data)
    today_count = len(today_arts)
    au_count = sum(1 for a in today_arts if is_australia(a))
    if today_count >= TARGET_ARTICLES:
        print(
            f"[skip] data.json already has {today_count}/{TARGET_ARTICLES} entries "
            f"for {TODAY_STR} (AU={au_count}/{MIN_AUSTRALIA}) — nothing to do."
        )
        return 0

    print(
        f"[generate] {TODAY_STR} via {MODEL}, "
        f"target={TARGET_ARTICLES}, already_have={today_count} (AU={au_count})"
    )

    appended_all: list[dict] = []
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Recompute AU/today counts each attempt — append_articles mutates `data`.
        today_arts = today_articles(data)
        today_count = len(today_arts)
        au_count = sum(1 for a in today_arts if is_australia(a))
        needed = TARGET_ARTICLES - today_count
        if needed <= 0:
            break
        au_needed = max(0, MIN_AUSTRALIA - au_count)
        au_needed_in_call = min(au_needed, needed)
        if attempt > 1:
            print(f"  sleeping {RETRY_BACKOFF_SEC}s for rate-limit window…")
            time.sleep(RETRY_BACKOFF_SEC)
        print(
            f"[attempt {attempt}/{MAX_ATTEMPTS}] requesting {needed} more "
            f"(AU need {au_needed_in_call} of them; have {au_count} AU so far)"
        )
        try:
            payload = call_claude(
                existing_urls(data), needed, au_needed_in_call, au_count
            )
        except Exception as e:
            last_error = e
            print(f"  attempt {attempt} failed: {e}", file=sys.stderr)
            continue
        candidates = payload.get("articles") or []
        appended_this = append_articles(data, candidates)
        appended_all.extend(appended_this)
        au_added = sum(1 for a in appended_this if is_australia(a))
        print(
            f"  attempt {attempt}: model returned {len(candidates)}, "
            f"appended {len(appended_this)} unique (AU added {au_added})"
        )

    if not appended_all:
        msg = "no new (deduped) articles to append after retries"
        if last_error:
            msg += f"; last error: {last_error}"
        print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    # Persist whatever we got — partial day is better than nothing.
    with DATA_PATH.open("w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    archive_path = write_archive(appended_all)
    final_arts = today_articles(data)
    final_today = len(final_arts)
    final_au = sum(1 for a in final_arts if is_australia(a))
    status = (
        "ok"
        if final_today >= TARGET_ARTICLES and final_au >= MIN_AUSTRALIA
        else "partial"
    )
    print(
        f"[{status}] appended {len(appended_all)} articles → {DATA_PATH.name} "
        f"(today total {final_today}/{TARGET_ARTICLES}, AU {final_au}/{MIN_AUSTRALIA})"
    )
    print(f"[ok] wrote {archive_path.relative_to(ROOT)}")
    for a in appended_all:
        print(f"  - {a['id']} | {a['title'][:80]}")

    # Always exit 0 if we have ANY new articles — the downstream workflow
    # commits them so the live site shows partial-day content rather than
    # nothing. A separate "Verify daily target" step at the end of the
    # workflow turns the run red when final_today < TARGET_ARTICLES or
    # final_au < MIN_AUSTRALIA, so the gap is still surfaced.
    if final_today < TARGET_ARTICLES:
        print(
            f"WARNING: only have {final_today}/{TARGET_ARTICLES} articles for "
            f"{TODAY_STR} after {MAX_ATTEMPTS} attempts. "
            f"Partial day committed; rerun manually to backfill.",
            file=sys.stderr,
        )
    if final_au < MIN_AUSTRALIA:
        print(
            f"WARNING: today has {final_au}/{MIN_AUSTRALIA} Australian "
            f"article(s). Manual rerun may help if a non-AU slot can be "
            f"replaced; otherwise edit data.json by hand.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
