#!/usr/bin/env python3
"""Re-inline data.json into the <script id="initial-data"> block of index.html.

The site loads articles from this inline block on first paint, so it must stay
in sync with data.json after every pipeline run.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data.json"
HTML_PATH = ROOT / "index.html"

INLINE_RE = re.compile(
    r'(<script id="initial-data" type="application/json">)[\s\S]*?(</script>)',
    re.IGNORECASE,
)


def main() -> int:
    if not DATA_PATH.exists() or not HTML_PATH.exists():
        print("[skip] data.json or index.html missing", file=sys.stderr)
        return 0
    with DATA_PATH.open() as f:
        data_str = json.dumps(json.load(f), indent=2, ensure_ascii=False)

    html = HTML_PATH.read_text(encoding="utf-8")
    if not INLINE_RE.search(html):
        print(
            'ERROR: <script id="initial-data" type="application/json"> block not found in index.html',
            file=sys.stderr,
        )
        return 1

    new_html, n = INLINE_RE.subn(
        lambda m: f"{m.group(1)}\n{data_str.strip()}\n{m.group(2)}",
        html,
        count=1,
    )
    if n != 1:
        print("ERROR: failed to replace initial-data block", file=sys.stderr)
        return 1

    if new_html != html:
        HTML_PATH.write_text(new_html, encoding="utf-8")
        print(f"[ok] re-inlined {len(data_str)} chars of JSON into {HTML_PATH.name}")
    else:
        print("[ok] index.html already in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
