#!/usr/bin/env python3
"""Daily RSS-based curation for the urban-design-daily site.

End-to-end single-shot:
  1. Fetch a curated list of RSS feeds (urbanism + Australia general).
  2. Filter to fresh (last N days) urban-design / planning stories.
  3. Dedup against every URL already in data.json.
  4. Pick TARGET_ARTICLES with at least MIN_AUSTRALIA Australian.
  5. Append entries, write archive markdown.
  6. Run enrich_thumbnails.py and inline_data.py.
  7. git add / commit / push.

Designed to be invoked by macOS launchd (or any system cron) — no LLM,
no Anthropic API key, no GitHub Actions. Subscription cost: $0.

Exit codes:
  0 — success (3 articles for today already present, or appended).
  1 — partial day (only 1-2 articles found / pushed).
  2 — hard failure (no feed reachable, no candidates, git failure).

Logging goes to stdout/stderr. launchd captures both to its log file.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data.json"
ARCHIVE_DIR = ROOT / "archive"
ENRICH_SCRIPT = ROOT / "scripts" / "enrich_thumbnails.py"
INLINE_SCRIPT = ROOT / "scripts" / "inline_data.py"

MELBOURNE = ZoneInfo("Australia/Melbourne")
TODAY = _dt.datetime.now(MELBOURNE).date()
TODAY_STR = TODAY.isoformat()
NOW = _dt.datetime.now(_dt.timezone.utc)

PLACEHOLDER = "https://images.unsplash.com/photo-1514565131-fce0801e5785?w=800"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

TARGET_ARTICLES = 3
MIN_AUSTRALIA = 1
# Accept items published in the last N days. RSS feeds vary in how long they
# keep items, so be lenient.
FRESHNESS_DAYS = 14
FEED_TIMEOUT = 15

# Hand-curated set of high-signal RSS feeds. Generic news feeds (SMH, Guardian
# Australia, ArchDaily) tested poorly — too much politics/sport/single-building
# content, not enough true urbanism in titles to make automated selection
# reliable. Stick to feeds whose entire editorial focus is cities and planning.
FEEDS = [
    ("https://theconversation.com/au/cities/articles.atom", "The Conversation AU", "trusted"),
    ("https://www.theguardian.com/cities/rss", "Guardian Cities", "trusted"),
    ("https://www.sightline.org/feed/", "Sightline Institute", "trusted"),
]

AU_TAG_CITIES = [
    "Melbourne", "Sydney", "Brisbane", "Perth", "Adelaide",
    "Canberra", "Hobart", "Darwin", "Gold Coast", "Newcastle",
    "Wollongong", "Geelong",
]
AU_TAG_STATES = ["Victoria", "NSW", "Queensland", "Western Australia",
                 "South Australia", "Tasmania", "ACT", "Northern Territory"]
_AU_TOKENS = (
    ["australia", "australian"]
    + [c.lower() for c in AU_TAG_CITIES]
    + [s.lower() for s in AU_TAG_STATES]
    + ["wa", "sa", "tas", "nt", "qld"]
)
AU_REGEX = re.compile(r"\b(" + "|".join(re.escape(t) for t in _AU_TOKENS) + r")\b", re.I)

# Compound phrases that strongly signal urban-planning relevance. If any of
# these appear in the title, the article qualifies on its own.
URBAN_STRONG = re.compile(
    r"\b(urban planning|urban design|urban form|urban renewal|"
    r"transit-oriented|tod\b|affordable housing|public housing|social housing|"
    r"missing middle|build-to-rent|build to rent|btr\b|"
    r"zoning reform|rezoning|planning reform|planning scheme|"
    r"housing supply|housing crisis|housing target|housing policy|"
    r"density bonus|upzoning|downzoning|public realm|streetscape|"
    r"walkability|cycle network|bike network|bike lane|cycle lane|"
    r"masterplan|master plan|land use|land-use|pattern book|"
    r"smart city|sustainable city|15-minute city|fifteen-minute city|"
    r"complete streets|adaptive reuse|climate adapt|"
    r"transit corridor|rapid transit|light rail|metro line|"
    r"affordable home|infrastructure plan|infrastructure pipeline|"
    r"high-density|mid-rise|low-rise|brownfield|greenfield)\b",
    re.IGNORECASE,
)

# Weaker urban tokens — at least 2 must appear in title for an entry to
# qualify via this path. Keeps single buzzword matches from slipping through.
URBAN_WEAK = re.compile(
    r"\b(urban|planning|housing|zoning|transit|density|"
    r"pedestrian|metro|rail|precinct|neighbourhood|neighborhood|"
    r"affordable|tenant|renter|developer|development|cycl)\b",
    re.IGNORECASE,
)

# Common single-building / object-design article titles, especially in
# ArchDaily and Dezeen. These almost never have urban-system relevance.
SINGLE_BUILDING_TITLE = re.compile(
    r"\b(house|villa|cabin|loft|chapel|pavilion|residence|cottage|"
    r"mansion|tower(?!s)|hotel|spa|gallery|museum|sculpture|installation|"
    r"furniture|chair|table|lamp|kitchen|bathroom|bedroom)\b",
    re.IGNORECASE,
)

# Politics / sport / lifestyle bait that AU general-news feeds surface but
# we never want, even when "housing" or "planning" appears in passing.
NOISE_TITLE = re.compile(
    r"\b(newsroom edition|opinion piece|sport|cricket|footy|afl|nrl|"
    r"recipe|fashion|celebrity|royal family|coronation|murder|"
    r"police charged|election day|by-election|leader's debate|"
    r"surrendering to|backlash|war room|culture wars)\b",
    re.IGNORECASE,
)

# Topic-tag inference keywords → topic label
TOPIC_MAP = [
    ("housing", "Housing"),
    ("affordable", "Affordable Housing"),
    ("rent", "Housing"),
    ("zoning", "Zoning Reform"),
    ("rezoning", "Zoning Reform"),
    ("planning reform", "Planning Reform"),
    ("transit", "Transit-Oriented Development"),
    ("light rail", "Transit"),
    ("metro", "Transit"),
    ("bike", "Active Transport"),
    ("cycl", "Active Transport"),
    ("pedestrian", "Public Realm"),
    ("public realm", "Public Realm"),
    ("streetscape", "Public Realm"),
    ("climate", "Climate Adaptation"),
    ("heritage", "Heritage"),
    ("infrastructure", "Infrastructure"),
    ("data center", "Land Use"),
    ("data centre", "Land Use"),
    ("masterplan", "Urban Design"),
    ("master plan", "Urban Design"),
    ("urban design", "Urban Design"),
    ("density", "Urban Form"),
]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{NOW.isoformat(timespec='seconds')}] {msg}", flush=True)


def strip_html(text: str) -> str:
    """Quick-and-dirty HTML → plain text. RSS descriptions often have <p>, <a>,
    image tags etc. We just want a readable summary."""
    if not text:
        return ""
    # Drop CDATA wrappers.
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.DOTALL)
    # Drop scripts and styles wholesale (rare in RSS but cheap).
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    # Replace block-level closures with paragraph breaks before stripping.
    text = re.sub(r"</p>\s*<p[^>]*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    # Drop all remaining tags.
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_feed_date(s: str) -> _dt.datetime | None:
    """RSS uses RFC822, Atom uses ISO 8601. Try both."""
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d
    except (TypeError, ValueError):
        pass
    try:
        # Atom-style ISO 8601 e.g. "2026-06-15T10:00:00Z"
        s2 = s.rstrip("Z") + "+00:00" if s.endswith("Z") else s
        d = _dt.datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d
    except ValueError:
        return None


def fetch_feed(url: str) -> bytes | None:
    """Fetch RSS/Atom body. Returns None on any error."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=FEED_TIMEOUT) as r:
            return r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout,
            ConnectionResetError, TimeoutError) as e:
        log(f"  feed-error {url}: {type(e).__name__} {e}")
        return None


def parse_entries(body: bytes, source: str, trust: str = "filter") -> list[dict]:
    """Extract a normalised list of entries from RSS 2.0 or Atom XML."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        log(f"  parse-error {source}: {e}")
        return []
    entries: list[dict] = []
    # RSS 2.0: rss/channel/item
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
        if title and link:
            entries.append({
                "title": strip_html(title),
                "link": link,
                "summary": strip_html(desc),
                "published": parse_feed_date(pub),
                "source": source,
                "trust": trust,
            })
    # Atom: feed/entry
    for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        link_el = entry.find("{http://www.w3.org/2005/Atom}link")
        link = link_el.get("href") if link_el is not None else ""
        summary = (
            entry.findtext("{http://www.w3.org/2005/Atom}summary")
            or entry.findtext("{http://www.w3.org/2005/Atom}content")
            or ""
        )
        pub = (
            entry.findtext("{http://www.w3.org/2005/Atom}published")
            or entry.findtext("{http://www.w3.org/2005/Atom}updated")
            or ""
        )
        if title and link:
            entries.append({
                "title": strip_html(title),
                "link": link.strip(),
                "summary": strip_html(summary),
                "published": parse_feed_date(pub),
                "source": source,
                "trust": trust,
            })
    return entries


def is_australia(text: str) -> bool:
    return bool(AU_REGEX.search(text or ""))


def is_urban(title: str, trust: str = "filter") -> bool:
    """An entry qualifies as urbanism content. We work off the title only —
    RSS descriptions routinely namedrop "infrastructure" or "housing" in
    passing for politics/sport pieces, which over-recalls.

    `trust='trusted'` (Strong Towns / Sightline / Guardian Cities): accept
        anything that isn't explicitly NOISE or a single-building review.
    `trust='filter'` (general news, ArchDaily): require an URBAN_STRONG
        compound phrase or at least 2 URBAN_WEAK token hits in the title.
    """
    title = title or ""
    if SINGLE_BUILDING_TITLE.search(title):
        return False
    if NOISE_TITLE.search(title):
        return False
    if trust == "trusted":
        return True
    if URBAN_STRONG.search(title):
        return True
    weak_hits = len(URBAN_WEAK.findall(title))
    return weak_hits >= 2


def infer_topics(text: str, is_au: bool) -> list[str]:
    """Best-effort tag inference from title+summary."""
    found: list[str] = []
    lower = (text or "").lower()
    for needle, label in TOPIC_MAP:
        if needle in lower and label not in found:
            found.append(label)
    if is_au:
        # Prepend Australia + first matched city/state for the verify step.
        au_tag = "Australia"
        if au_tag not in found:
            found.insert(0, au_tag)
        for city in AU_TAG_CITIES + AU_TAG_STATES:
            if city.lower() in lower and city not in found:
                found.insert(1, city)
                break
    if not found:
        found.append("Urban Design")
    return found[:5]


def next_id_seq(data: dict) -> int:
    pat = re.compile(rf"^{re.escape(TODAY_STR)}-(\d{{3}})$")
    nums = [int(m.group(1)) for a in data.get("articles", [])
            if (m := pat.match(a.get("id", "")))]
    return (max(nums) + 1) if nums else 1


def truncate_summary(text: str, max_chars: int = 1200) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return cut + "…"


def score_entry(e: dict) -> float:
    """Higher is better. Combines urbanism strength, AU bonus, recency, and
    trust. Used to rank candidates after the binary urbanism filter."""
    title = e.get("title", "") or ""
    summary = (e.get("summary", "") or "")[:600]  # cap so long summaries don't dominate
    score = 0.0
    if URBAN_STRONG.search(title):
        score += 8
    if URBAN_STRONG.search(summary):
        score += 3
    score += 2.0 * len(URBAN_WEAK.findall(title))
    score += 0.5 * len(URBAN_WEAK.findall(summary))
    if AU_REGEX.search(title):
        score += 6
    elif AU_REGEX.search(summary):
        score += 3
    if e.get("trust") == "trusted":
        score += 2
    pub = e.get("published")
    if pub is not None:
        days_old = max(0, (NOW - pub).total_seconds() / 86400)
        score += max(0.0, 4.0 - days_old * 0.3)
    return score


def select_articles(candidates: list[dict], data: dict) -> list[dict]:
    """Score every candidate, then pick the top scorers with the AU
    constraint: ensure at least MIN_AUSTRALIA Australian item in the
    selection if any exists in the pool."""
    existing = {a.get("url", "").lower() for a in data.get("articles", []) if a.get("url")}
    seen_in_run: set[str] = set()
    cutoff = NOW - _dt.timedelta(days=FRESHNESS_DAYS)

    def relevant(e: dict) -> bool:
        url = (e.get("link") or "").strip()
        if not url or url.lower() in existing or url.lower() in seen_in_run:
            return False
        if not is_urban(e.get("title") or "", e.get("trust", "filter")):
            return False
        pub = e.get("published")
        if pub is not None and pub < cutoff:
            return False
        return True

    pool: list[tuple[float, dict]] = []
    for e in candidates:
        if not relevant(e):
            continue
        seen_in_run.add(e["link"].lower())
        pool.append((score_entry(e), e))

    pool.sort(key=lambda t: t[0], reverse=True)

    picked: list[dict] = []
    picked_urls: set[str] = set()

    def is_au_entry(e: dict) -> bool:
        body = (e.get("title") or "") + " " + (e.get("summary") or "")
        return is_australia(body)

    # First, take the top-scoring AU entry if one exists in the pool.
    for _, e in pool:
        if is_au_entry(e):
            picked.append(e)
            picked_urls.add(e["link"].lower())
            break

    # Fill the rest by score, skipping already-picked URLs.
    for _, e in pool:
        if len(picked) >= TARGET_ARTICLES:
            break
        if e["link"].lower() in picked_urls:
            continue
        picked.append(e)
        picked_urls.add(e["link"].lower())

    return picked


def to_article(entry: dict, idx: int) -> dict:
    body = (entry.get("title") or "") + " " + (entry.get("summary") or "")
    au = is_australia(body)
    topics = infer_topics(body, au)
    summary = truncate_summary(entry.get("summary") or "")
    if not summary:
        summary = f"Recent coverage from {entry['source']}. See the linked article for the full story."
    why = (
        f"Surfaced by the daily RSS pipeline from {entry['source']}. "
        "Read the original for full context — automated summaries trade depth "
        "for reliability."
    )
    return {
        "id": f"{TODAY_STR}-{idx:03d}",
        "date": TODAY_STR,
        "title": (entry.get("title") or "").strip(),
        "source": entry["source"],
        "url": entry["link"],
        "thumbnail": PLACEHOLDER,
        "summary": summary,
        "whyItMatters": why,
        "topics": topics,
    }


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


def run_cmd(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    log(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=ROOT, check=check, text=True, capture_output=True)


def run_git_retry(cmd: list[str], attempts: int = 4, backoff_sec: int = 4) -> None:
    """Run a git subcommand with retries. The project lives under
    ~/Desktop/, which is iCloud-synced; iCloud touching files mid-commit
    occasionally trips git's index lock with 'Resource deadlock avoided'.
    A few retries with backoff is usually enough for iCloud to release."""
    import time
    for i in range(1, attempts + 1):
        try:
            run_cmd(cmd)
            return
        except subprocess.CalledProcessError as e:
            err = (e.stderr or "").strip()
            transient = "deadlock" in err.lower() or "could not lock" in err.lower() or "index.lock" in err.lower()
            if i == attempts or not transient:
                raise
            log(f"  git {cmd[1] if len(cmd) > 1 else ''} attempt {i}/{attempts} failed ({err[:80]}) — retrying in {backoff_sec}s")
            time.sleep(backoff_sec)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    log(f"daily_rss_update for {TODAY_STR} (Melbourne)")

    # 0. git pull so we don't race with manual pushes.
    try:
        run_cmd(["git", "pull", "--rebase", "origin", "main"])
    except subprocess.CalledProcessError as e:
        log(f"git pull failed: {e.stderr.strip()[:200]} — continuing anyway")

    data = json.loads(DATA_PATH.read_text())

    today_existing = [a for a in data["articles"] if a.get("date") == TODAY_STR]
    today_au = sum(1 for a in today_existing
                   if any(t.lower() in [tag.lower() for tag in AU_TAG_CITIES + AU_TAG_STATES + ["Australia", "Australian"]]
                          for t in a.get("topics", [])))
    log(f"existing today: {len(today_existing)}/{TARGET_ARTICLES}, AU {today_au}")
    if len(today_existing) >= TARGET_ARTICLES and today_au >= MIN_AUSTRALIA:
        log("[skip] already complete")
        return 0

    # 1. Fetch all feeds.
    all_entries: list[dict] = []
    for url, source, trust in FEEDS:
        body = fetch_feed(url)
        if body is None:
            continue
        entries = parse_entries(body, source, trust)
        log(f"  {source} [{trust}]: {len(entries)} entries")
        all_entries.extend(entries)
    if not all_entries:
        log("ERROR: no feed entries fetched")
        return 2

    # 2. Select.
    picks = select_articles(all_entries, data)
    if not picks:
        log("ERROR: no relevant fresh candidates")
        return 2

    # 3. Append.
    seq = next_id_seq(data)
    appended: list[dict] = []
    for entry in picks:
        article = to_article(entry, seq)
        data["articles"].append(article)
        appended.append(article)
        seq += 1
        log(f"  + {article['id']}  AU={is_australia(article['title']+article['summary'])}  "
            f"{article['title'][:70]}")

    DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    write_archive(appended)

    # 4. Enrich + inline.
    try:
        run_cmd([sys.executable, str(ENRICH_SCRIPT)])
    except subprocess.CalledProcessError as e:
        log(f"enrich step failed: {e.stderr.strip()[:200]}")
    try:
        run_cmd([sys.executable, str(INLINE_SCRIPT)])
    except subprocess.CalledProcessError as e:
        log(f"inline step failed: {e.stderr.strip()[:200]}")
        return 2

    # 5. Commit + push (with deadlock-tolerant retries; see run_git_retry).
    try:
        run_git_retry(["git", "add", "data.json", "index.html", "archive/"])
        status = run_cmd(["git", "status", "--porcelain"], check=False)
        if not status.stdout.strip():
            log("nothing staged — nothing to commit")
            return 0
        run_git_retry(["git", "commit", "-m", f"Add {TODAY_STR} articles (RSS)"])
        run_git_retry(["git", "push", "origin", "main"])
    except subprocess.CalledProcessError as e:
        log(f"git step failed after retries: {e.stderr.strip()[:200]}")
        return 2

    # 6. Verify final state for the workflow's success criteria.
    final = json.loads(DATA_PATH.read_text())
    today_final = [a for a in final["articles"] if a.get("date") == TODAY_STR]
    au_final = sum(
        1 for a in today_final
        if is_australia(a.get("title", "") + " " + a.get("summary", ""))
        or any(t.lower() in ("australia", "australian") for t in a.get("topics", []))
    )
    log(f"final: {len(today_final)}/{TARGET_ARTICLES}, AU {au_final}/{MIN_AUSTRALIA}")
    if len(today_final) < TARGET_ARTICLES or au_final < MIN_AUSTRALIA:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
