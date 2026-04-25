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
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


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
        try:
            img = try_microlink(url)
            if img:
                method = "microlink"
        except Exception:
            pass
        if not img:
            try:
                img = try_direct(url)
                if img:
                    method = "direct"
            except Exception as e:
                print(f"  (skip {a.get('id', '?')}: {str(e)[:60]})")
                continue
        if img and img.startswith("http"):
            a["thumbnail"] = img
            changed += 1
            print(f"  [{method}] {a.get('id', '?')} -> {img[:70]}")
        else:
            print(f"  (skip {a.get('id', '?')}: no image found)")

    if changed:
        with DATA_PATH.open("w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[ok] enriched {changed} thumbnails")
    else:
        print("[ok] no thumbnails to enrich")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
