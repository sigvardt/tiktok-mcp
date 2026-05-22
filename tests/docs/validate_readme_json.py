"""Validate every ```json fenced block in the repo-root README.md."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_FENCE_PATTERN = re.compile(
    r"^[ \t]*```json[ \t]*\n(?P<body>.*?)^[ \t]*```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def _find_readme() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "README.md"
        if candidate.is_file() and (parent / "pyproject.toml").is_file():
            return candidate
    raise FileNotFoundError("Could not locate repo-root README.md from tests/docs/")


def main() -> int:
    readme = _find_readme()
    text = readme.read_text(encoding="utf-8")
    blocks = list(_FENCE_PATTERN.finditer(text))

    if not blocks:
        sys.stderr.write(f"validate_readme_json: no ```json fences found in {readme}\n")
        return 1

    failures: list[str] = []
    for index, match in enumerate(blocks, start=1):
        body = match.group("body")
        line_no = text.count("\n", 0, match.start()) + 1
        try:
            json.loads(body)
        except json.JSONDecodeError as exc:
            failures.append(
                f"block #{index} (README.md line {line_no}): "
                f"{exc.msg} at body line {exc.lineno}, col {exc.colno}"
            )

    if failures:
        sys.stderr.write(
            f"validate_readme_json: {len(failures)} invalid JSON block(s) in {readme}\n"
        )
        for failure in failures:
            sys.stderr.write(f"  - {failure}\n")
        return 1

    sys.stdout.write(
        f"validate_readme_json: validated {len(blocks)} JSON block(s) in {readme}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
