#!/usr/bin/env python3
"""LLM-based curation of the daily urban-design digest.

Reads a list of RSS candidates passed in via stdin (JSON), fetches the
body excerpt for each (with Wayback fallback for Cloudflare-blocked
sites), then asks Opus 4.8 (via `claude -p`, using the user's Claude
subscription — zero per-call API cost) to:

  1. Pick exactly 3 articles per scripts/canonical_prompt.md
     (1 AU + 2 non-AU; prefer soft city / walkable / landscape / urban
     design themes).
  2. Write a 220-280 word substantive summary for each pick.
  3. Write a 35-65 word whyItMatters analysis line for each pick.
  4. Assign 3-5 canonical topic tags.

Returns the 3 picks as a JSON array on stdout. Designed to be invoked
by daily_rss_update.py after the RSS fetch/filter pass.

Auth: uses `claude -p` which reads credentials from the macOS Keychain
("Claude Code-credentials" item). User must run `claude` then `/login`
once before this script is used.

Falls back to non-zero exit + helpful stderr on any failure, so the
caller can shell to a heuristic pick.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL_PROMPT = ROOT / "scripts" / "canonical_prompt.md"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

CITE_OPEN_RE = re.compile(r"<cite\b[^>]*>", re.IGNORECASE)
CITE_CLOSE_RE = re.compile(r"</cite\s*>", re.IGNORECASE)


def log(msg: str) -> None:
    print(f"[curate_with_opus] {msg}", file=sys.stderr, flush=True)


def strip_citations(text: str) -> str:
    if not text:
        return text
    return CITE_CLOSE_RE.sub("", CITE_OPEN_RE.sub("", text))


# ----------------------------------------------------------------------------
# Body extraction
# ----------------------------------------------------------------------------

# Crude main-content extractor: drop scripts/styles, then find the longest
# run of <p>...</p> tags. Good enough for ArchitectureAU, Planetizen,
# Guardian, Sightline, theurbandeveloper, etc. — these are content-first
# sites where the article body dominates.
SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def html_to_text(html: str, max_chars: int = 4000) -> str:
    html = SCRIPT_RE.sub("", html)
    html = STYLE_RE.sub("", html)
    paragraphs: list[str] = []
    for m in P_RE.finditer(html):
        text = TAG_RE.sub("", m.group(1))
        text = WS_RE.sub(" ", text).strip()
        if len(text) > 40:  # skip nav/footer crumbs
            paragraphs.append(text)
        if sum(len(p) for p in paragraphs) > max_chars:
            break
    out = "\n\n".join(paragraphs)
    return out[:max_chars]


def fetch_body_direct(url: str, timeout: int = 12) -> str | None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="ignore")
    text = html_to_text(html)
    return text or None


def fetch_body_wayback(url: str, timeout: int = 12) -> str | None:
    api = "https://archive.org/wayback/available?url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(api, headers={"User-Agent": "urban-design-daily/1.0"})
    payload = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))
    snap = ((payload.get("archived_snapshots") or {}).get("closest") or {})
    snap_url = snap.get("url")
    if not snap_url or snap.get("status") != "200":
        return None
    if snap_url.startswith("http://"):
        snap_url = "https://" + snap_url[len("http://"):]
    req = urllib.request.Request(snap_url, headers={"User-Agent": UA})
    html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="ignore")
    text = html_to_text(html)
    return text or None


def fetch_body(url: str) -> str | None:
    for fn, name in ((fetch_body_direct, "direct"), (fetch_body_wayback, "wayback")):
        try:
            text = fn(url)
            if text:
                log(f"  body [{name}] {url[:70]}: {len(text)} chars")
                return text
        except Exception as e:
            log(f"  body [{name}] {url[:70]}: {type(e).__name__}: {str(e)[:60]}")
    return None


# ----------------------------------------------------------------------------
# Prompt construction
# ----------------------------------------------------------------------------

def render_prompt(candidates: list[dict], today_str: str, existing_urls: list[str]) -> str:
    canonical = CANONICAL_PROMPT.read_text(encoding="utf-8")

    cand_block_lines: list[str] = []
    for i, c in enumerate(candidates, 1):
        cand_block_lines.append(f"### Candidate {i}")
        cand_block_lines.append(f"- title: {c['title']}")
        cand_block_lines.append(f"- source: {c['source']}")
        cand_block_lines.append(f"- url: {c['link']}")
        if c.get("published_str"):
            cand_block_lines.append(f"- published: {c['published_str']}")
        rss_desc = (c.get("summary") or "").strip()
        if rss_desc:
            cand_block_lines.append(f"- rss_desc: {rss_desc[:600]}")
        body = (c.get("body") or "").strip()
        if body:
            cand_block_lines.append(f"- body_excerpt:\n{body[:3500]}")
        cand_block_lines.append("")
    candidates_block = "\n".join(cand_block_lines)

    dedup_block = "\n".join(f"- {u}" for u in existing_urls[-80:])

    return f"""ultrathink: You are curating today's urban design daily digest. Today is {today_str} (Melbourne local).

Follow this canonical prompt EXACTLY:

================ canonical_prompt.md ================
{canonical}
================ end canonical_prompt.md ================

DEDUP LIST — do NOT pick any URL already in this list (recent ~80 entries):
{dedup_block}

================ CANDIDATES ({len(candidates)} total) ================
{candidates_block}
================ END CANDIDATES ================

Your task:
1. Pick EXACTLY 3 of the candidates per the canonical prompt's composition rules
   (1 AU + 2 non-AU; fall back to 3 AU only if non-AU is fully exhausted).
2. Prefer candidates that touch the reader's interest cluster
   (soft city / walkable / landscape / urban design / public realm /
    streetscape / placemaking / parks / 15-minute / human-scale).
3. For each pick, write:
   - summary: 220-280 words, senior-planner tone, factual, no <cite>, no markdown,
     facts must come from the body_excerpt or rss_desc (do NOT invent figures
     or named people not present in the source material).
   - whyItMatters: 35-65 words, 1-2 sentences. Must not begin with "This is
     important", "It's important", "Policymakers should". Lead with what the
     article demonstrates; relate to Australian planning precedent where
     relevant.
   - topics: 3-5 tags, first tag is country, AU articles include
     "Australia" + city/state, use canonical names
     ("Transit-Oriented Development", not "TOD").

Self-validation (run before responding):
- exactly 3 picks
- summary word count 220-280 each
- whyItMatters word count 35-65 each
- topics 3-5 each
- no "<cite" substring in any text
- none of the picks' URLs appear in the dedup list

Return STRICT JSON only, no surrounding prose, no markdown fence:
{{"picks": [
  {{
    "url": "...",
    "title": "...",
    "source": "...",
    "summary": "...",
    "whyItMatters": "...",
    "topics": ["...","...","..."]
  }},
  {{...}},
  {{...}}
]}}
"""


# ----------------------------------------------------------------------------
# Opus invocation
# ----------------------------------------------------------------------------

def invoke_opus(prompt: str, timeout: int = 600) -> str:
    """Run `claude -p --model opus` with the prompt on stdin. Returns the
    model's response text."""
    cmd = [
        "claude", "-p",
        "--model", "opus",
        "--no-session-persistence",
        "--output-format", "text",
    ]
    log(f"  invoking: {' '.join(cmd)} (prompt {len(prompt)} chars)")
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p exit {proc.returncode}: stderr={proc.stderr.strip()[:300]}"
        )
    return proc.stdout.strip()


def extract_json(text: str) -> dict:
    """Find the first {...} block that parses as JSON."""
    # Try fenced block first
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Fall back to outermost braces
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"No JSON object found in response:\n{text[:600]}")


# ----------------------------------------------------------------------------
# Public entrypoint
# ----------------------------------------------------------------------------

def curate(candidates: list[dict], today_str: str, existing_urls: list[str]) -> list[dict]:
    """Curate using Opus 4.8 + ultrathink. Returns 3 article dicts ready to
    drop into data.json (without id/date/thumbnail — caller assigns)."""
    # Fetch bodies for top N (cap to keep prompt size reasonable).
    BODY_FETCH_LIMIT = 10
    enriched: list[dict] = []
    for c in candidates[: max(BODY_FETCH_LIMIT, 6)]:
        body = fetch_body(c["link"])
        enriched.append({**c, "body": body or ""})
        if c.get("published"):
            try:
                enriched[-1]["published_str"] = c["published"].isoformat()
            except Exception:
                pass

    # Send a slightly larger candidate pool to the model — even candidates
    # without body still have title + rss_desc and can be selection-worthy.
    pool = enriched + [
        {**c, "body": ""} for c in candidates[BODY_FETCH_LIMIT:16]
    ]

    prompt = render_prompt(pool, today_str, existing_urls)
    log(f"prompt {len(prompt)} chars, {len(pool)} candidates")

    response = invoke_opus(prompt)
    log(f"opus response {len(response)} chars")

    parsed = extract_json(response)
    picks = parsed.get("picks") or []
    if len(picks) != 3:
        log(f"WARNING: opus returned {len(picks)} picks (expected 3)")

    # Clean + normalise
    out: list[dict] = []
    for p in picks[:3]:
        out.append({
            "title": strip_citations((p.get("title") or "").strip()),
            "source": (p.get("source") or "").strip(),
            "url": (p.get("url") or "").strip(),
            "summary": strip_citations((p.get("summary") or "").strip()),
            "whyItMatters": strip_citations((p.get("whyItMatters") or "").strip()),
            "topics": [t for t in (p.get("topics") or []) if isinstance(t, str)],
        })
    return out


def main() -> int:
    """Stdin: JSON {candidates: [...], today: "YYYY-MM-DD", existing_urls: [...]}
    Stdout: JSON {picks: [...]} (3 entries)."""
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        log(f"ERROR: bad stdin JSON: {e}")
        return 2
    cands = payload.get("candidates") or []
    today = payload.get("today") or ""
    existing = payload.get("existing_urls") or []
    if not cands or not today:
        log("ERROR: candidates or today missing")
        return 2
    picks = curate(cands, today, existing)
    json.dump({"picks": picks}, sys.stdout, indent=2, ensure_ascii=False)
    return 0 if len(picks) >= 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
