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
import signal
import subprocess
import sys
import time
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

def _run_claude(cmd: list[str], stdin_text: str, timeout: int):
    """Run a `claude` CLI command with a HARD timeout that actually fires.

    `subprocess.run(timeout=...)` only kills the *direct* child. `claude` is a
    node app that spawns grandchild helpers which inherit the stdout/stderr
    pipes; when the timeout fires, run()'s internal reap calls communicate()
    again with no timeout and blocks FOREVER waiting for those pipes to close.
    That is exactly what hung the 2026-07-01 morning run for 3.5 hours.

    Fix: put the child in its own session (process-group leader) via
    start_new_session=True, and on timeout kill the WHOLE group with
    os.killpg(SIGKILL) so the grandchildren die and we never deadlock.

    Returns (returncode, stdout, stderr). Raises subprocess.TimeoutExpired
    after the group has been killed, so callers treat it as a failed attempt."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=10)  # reap; group is dead so this is quick
        except Exception:
            pass
        raise


def _claude_preflight() -> bool:
    """5-token sanity probe. Cheap. If it fails, the main call almost
    certainly will too and we don't want to burn rate-window seconds on it."""
    try:
        returncode, _out, err = _run_claude(
            ["claude", "-p", "--model", "opus", "--no-session-persistence", "Say ok"],
            "",
            timeout=30,
        )
        if returncode != 0:
            log(f"  preflight FAIL exit={returncode} stderr={(err or '').strip()[:200]!r}")
            return False
        log(f"  preflight ok")
        return True
    except subprocess.TimeoutExpired:
        log(f"  preflight TIMEOUT after 30s (process group killed)")
        return False
    except Exception as e:
        log(f"  preflight EXC {type(e).__name__}: {str(e)[:120]}")
        return False


def invoke_opus(prompt: str, timeout: int = 600) -> str:
    """Run `claude -p --model opus` with the prompt on stdin. Returns the
    model's response text.

    Day 1 (2026-06-23) failed with exit 1 + empty stderr. Reproducing the same
    56k prompt the next day succeeded with the same exit 0 — so the failure
    was almost certainly transient (subscription rate window at 06:12 AEST peak
    hour, or a network blip). Two defensive changes:
      1. ALWAYS log full stderr (truncated to 500 chars) on every failure so
         we never again debug an empty-stderr exit code.
      2. ONE retry with 30s sleep on non-zero exit. Throttle windows clear in
         under a minute 95% of the time."""
    cmd = [
        "claude", "-p",
        "--model", "opus",
        "--no-session-persistence",
        "--output-format", "text",
    ]
    log(f"  invoking: {' '.join(cmd)} (prompt {len(prompt)} chars)")

    last_err: str = ""
    for attempt in (1, 2):
        try:
            returncode, out, err = _run_claude(cmd, prompt, timeout)
        except subprocess.TimeoutExpired:
            # Hard timeout fired and the process group was killed. Treat as a
            # failed attempt so we retry once then fall back to heuristic —
            # NEVER hang the morning run (cf. 2026-07-01 3.5h hang).
            last_err = f"TIMEOUT after {timeout}s (process group killed)"
            log(f"  attempt {attempt} FAIL: {last_err}")
            if attempt == 1:
                log(f"  sleeping 30s then retrying once…")
                time.sleep(30)
            continue
        # Always log usage / stderr so the diagnosis is in the log next time.
        stderr_tail = (err or "").strip()
        if stderr_tail:
            log(f"  attempt {attempt} stderr: {stderr_tail[:500]!r}")
        if returncode == 0:
            return out.strip()
        last_err = (
            f"exit={returncode} stderr={stderr_tail[:300]!r} "
            f"stdout_head={(out or '').strip()[:200]!r}"
        )
        log(f"  attempt {attempt} FAIL: {last_err}")
        if attempt == 1:
            log(f"  sleeping 30s then retrying once…")
            time.sleep(30)
    raise RuntimeError(f"claude -p failed after 2 attempts: {last_err}")


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
    # Preflight: cheap 5-token call. If it fails, body fetch is wasted work.
    if not _claude_preflight():
        raise RuntimeError("claude -p preflight failed; LLM curation aborted")
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
