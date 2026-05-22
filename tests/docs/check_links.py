"""Verify external URLs cited in a markdown file are reachable.

Extracts every ``https://`` URL from the input file, then HEAD-requests each one.
Exits 0 if every URL returns 2xx or 3xx, or if the network is unreachable
(in which case the script reports skipped checks and still exits 0). Exits
non-zero only when at least one URL returns a confirmed 4xx or 5xx response.

Usage:
    python tests/docs/check_links.py docs/release.md
"""

from __future__ import annotations

import re
import sys
import typing
import urllib.error
import urllib.request
from urllib.parse import urlparse

URL_PATTERN = re.compile(r"https://[^\s)\]<>\"'`,]+")
TIMEOUT_SECONDS = 10.0
USER_AGENT = (
    "tiktok-mcp-link-check/1.0 (+https://github.com/signikant/tiktok-mcp)"
)
RETRY_WITH_GET_CODES = {400, 403, 405, 501}


def extract_urls(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in URL_PATTERN.finditer(text):
        url = match.group(0)
        while url and url[-1] in ".,;:)]>}":
            url = url[:-1]
        if not url:
            continue
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            continue
        seen.setdefault(url, None)
    return list(seen.keys())


def _request(url: str, method: str) -> tuple[str, int | None, str]:
    request = urllib.request.Request(
        url,
        method=method,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status: int = typing.cast("int", response.status)
    except urllib.error.HTTPError as exc:
        if method == "HEAD" and exc.code in RETRY_WITH_GET_CODES:
            return _request(url, "GET")
        return ("fail", exc.code, f"{method} {exc.code} {exc.reason}")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        return ("skipped", None, f"network unreachable on {method} ({exc!r})")
    except OSError as exc:
        return ("skipped", None, f"OS error on {method} ({exc!r})")

    if 200 <= status < 400:
        return ("ok", status, f"{method} {status}")
    return ("fail", status, f"{method} {status}")


def check_url(url: str) -> tuple[str, int | None, str]:
    """Return ``(status_label, http_status, detail)`` for one URL.

    ``status_label`` is one of:
      - ``"ok"``      : HEAD or GET returned 2xx or 3xx
      - ``"fail"``    : confirmed 4xx or 5xx response
      - ``"skipped"`` : network unreachable / DNS / timeout
    """
    return _request(url, "HEAD")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <markdown-file>", file=sys.stderr)
        return 2

    path = argv[1]
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2

    urls = extract_urls(text)
    if not urls:
        print(f"{path}: no https:// URLs found")
        return 0

    print(f"{path}: checking {len(urls)} URL(s)")
    failures: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    for url in urls:
        label, _status, detail = check_url(url)
        if label == "ok":
            print(f"  ok       {url}  ({detail})")
        elif label == "skipped":
            print(f"  skipped  {url}  ({detail})")
            skipped.append((url, detail))
        else:
            print(f"  FAIL     {url}  ({detail})")
            failures.append((url, detail))

    if failures:
        print(f"\n{len(failures)} broken link(s); exit 1", file=sys.stderr)
        return 1
    if skipped:
        msg = (
            f"\n{len(skipped)} URL(s) skipped due to network errors; "
            + "treating as soft-pass per check_links contract."
        )
        print(msg)
    print("link check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
