#!/usr/bin/env python3
"""For any article whose thumbnail is still an Unsplash placeholder, fetch the
real og:image from the article URL.

Tries microlink.io first (handles JS-heavy sites), then falls back to a direct
HTML fetch + regex on og:image / twitter:image meta tags.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


def try_microlink(url: str) -> str | None:
    api = "https://api.microlink.io/?url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(api, headers={"User-Agent": "urban-design-daily/1.0"})
    body = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
    payload = json.loads(body)
    return ((payload.get("data") or {}).get("image") or {}).get("url")


OG_PATTERNS = [
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
]


def _find_og(html: str) -> str | None:
    for pat in OG_PATTERNS:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def try_direct(url: str) -> str | None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")
    return _find_og(html)


def try_wayback(url: str) -> str | None:
    """For Cloudflare-blocked sites (Planetizen, Bloomberg, IIASA, …) the
    original page returns 403 but the Internet Archive often has a recent
    snapshot. Look up the closest snapshot, scrape og:image out of its HTML,
    and return the Wayback proxy URL — that URL serves the original image
    indefinitely without going through the blocked origin."""
    api = "https://archive.org/wayback/available?url=" + urllib.parse.quote(
        url, safe=""
    )
    req = urllib.request.Request(api, headers={"User-Agent": "urban-design-daily/1.0"})
    payload = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    snap = ((payload.get("archived_snapshots") or {}).get("closest") or {})
    snap_url = snap.get("url")
    if not snap_url or snap.get("status") != "200":
        return None
    # snap_url is http://… — upgrade to https for our (https) site to avoid mixed content.
    if snap_url.startswith("http://"):
        snap_url = "https://" + snap_url[len("http://"):]
    req = urllib.request.Request(snap_url, headers={"User-Agent": UA})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")
    return _find_og(html)


def main() -> int:
    if not DATA_PATH.exists():
        print(f"[skip] {DATA_PATH} missing", file=sys.stderr)
        return 0
    with DATA_PATH.open() as f:
        data = json.load(f)

    changed = 0
    for a in data.get("articles", []):
        thumb = a.get("thumbnail", "") or ""
        url = a.get("url", "") or ""
        if not url or "images.unsplash.com" not in thumb:
            continue
        img: str | None = None
        method = ""
        last_err: str = ""
        for fn, name in (
            (try_microlink, "microlink"),
            (try_direct, "direct"),
            (try_wayback, "wayback"),
        ):
            try:
                img = fn(url)
                if img:
                    method = name
                    break
            except Exception as e:
                last_err = f"{name}: {str(e)[:50]}"
        if img and img.startswith("http"):
            a["thumbnail"] = img
            changed += 1
            print(f"  [{method}] {a.get('id', '?')} -> {img[:70]}")
        else:
            tail = f" ({last_err})" if last_err else ""
            print(f"  (skip {a.get('id', '?')}: no image found{tail})")

    if changed:
        with DATA_PATH.open("w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[ok] enriched {changed} thumbnails")
    else:
        print("[ok] no thumbnails to enrich")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
