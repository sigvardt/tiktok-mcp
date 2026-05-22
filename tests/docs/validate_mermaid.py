#!/usr/bin/env python3
"""Validate mermaid fenced code blocks inside a markdown file.

For every ```mermaid block in the input file the validator runs:

  1. A structural sanity check: the block is non-empty, starts with a
     recognised diagram type keyword, has balanced quotes / parens /
     brackets, and (for sequence diagrams) every interaction line carries
     a valid mermaid arrow token (``->>``, ``-->>``, ``->``, ``-->``,
     ``-x``, ``--x``, ``-)``, ``--)``).
  2. If the ``mmdc`` mermaid-cli binary is on ``PATH`` the validator also
     renders each block to a temporary SVG and asserts a zero exit code
     and a non-empty SVG output.

Exit code is 0 when every block passes every check, 1 when any block
fails. The script intentionally degrades gracefully when ``mmdc`` is not
installed so the CI gate can run with structural checks only.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MERMAID_BLOCK_PATTERN = re.compile(
    r"^```mermaid[ \t]*\n(.*?)\n```",
    re.MULTILINE | re.DOTALL,
)

KNOWN_DIAGRAM_TYPES = (
    "sequenceDiagram",
    "flowchart",
    "graph",
    "classDiagram",
    "stateDiagram",
    "stateDiagram-v2",
    "erDiagram",
    "gantt",
    "journey",
    "pie",
    "mindmap",
    "timeline",
)

SEQUENCE_ARROW_PATTERN = re.compile(r"->>|-->>|->|-->|-[xX]|--[xX]|-\)|--\)")

SEQUENCE_KEYWORD_PREFIXES = (
    "participant",
    "actor",
    "Note",
    "note",
    "loop",
    "end",
    "alt",
    "else",
    "opt",
    "par",
    "and",
    "critical",
    "rect",
    "activate",
    "deactivate",
    "title",
    "autonumber",
    "link",
    "links",
    "properties",
    "details",
    "box",
    "create",
    "destroy",
)


def extract_blocks(markdown: str) -> list[str]:
    return [match.group(1) for match in MERMAID_BLOCK_PATTERN.finditer(markdown)]


def _balanced(text: str, opener: str, closer: str) -> bool:
    return text.count(opener) == text.count(closer)


def _is_sequence_keyword_line(line: str) -> bool:
    if line in {"end", "autonumber"}:
        return True
    for prefix in SEQUENCE_KEYWORD_PREFIXES:
        if line == prefix:
            return True
        if line.startswith(prefix + " "):
            return True
    return False


def structural_check(block: str, index: int) -> list[str]:
    errors: list[str] = []
    stripped = block.strip()
    if not stripped:
        errors.append(f"block #{index}: empty")
        return errors

    first_token = stripped.split(None, 1)[0]
    if first_token not in KNOWN_DIAGRAM_TYPES:
        errors.append(f"block #{index}: unknown diagram type '{first_token}'")

    if stripped.count('"') % 2 != 0:
        errors.append(f"block #{index}: unbalanced double quotes")
    if not _balanced(stripped, "(", ")"):
        errors.append(f"block #{index}: unbalanced parentheses")
    if not _balanced(stripped, "[", "]"):
        errors.append(f"block #{index}: unbalanced square brackets")
    if not _balanced(stripped, "{", "}"):
        errors.append(f"block #{index}: unbalanced curly braces")

    if first_token == "sequenceDiagram":
        for lineno, raw in enumerate(stripped.splitlines()[1:], start=2):
            line = raw.strip()
            if not line or line.startswith("%%"):
                continue
            if _is_sequence_keyword_line(line):
                continue
            if ":" not in line:
                continue
            lhs = line.split(":", 1)[0]
            if not SEQUENCE_ARROW_PATTERN.search(lhs):
                errors.append(
                    f"block #{index}: line {lineno} missing valid arrow: {line!r}"
                )
    return errors


def render_with_mmdc(block: str, index: int, tmp_dir: Path) -> list[str]:
    mmdc = shutil.which("mmdc")
    if mmdc is None:
        return []
    in_mmd = tmp_dir / f"diagram-{index}.mmd"
    out_svg = tmp_dir / f"diagram-{index}.svg"
    _ = in_mmd.write_text(block, encoding="utf-8")
    result = subprocess.run(
        [mmdc, "-i", str(in_mmd), "-o", str(out_svg)],
        capture_output=True,
        text=True,
        check=False,
    )
    errors: list[str] = []
    if result.returncode != 0:
        stderr = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
        errors.append(f"block #{index}: mmdc exit {result.returncode}: {stderr}")
    elif not out_svg.exists() or out_svg.stat().st_size == 0:
        errors.append(f"block #{index}: mmdc produced empty SVG")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_mermaid.py <markdown-file>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2

    markdown = path.read_text(encoding="utf-8")
    blocks = extract_blocks(markdown)
    if not blocks:
        print(f"no mermaid blocks found in {path}", file=sys.stderr)
        return 1

    has_mmdc = shutil.which("mmdc") is not None
    if not has_mmdc:
        print(
            "info: mermaid-cli (mmdc) not on PATH; "
            "running structural checks only",
            file=sys.stderr,
        )

    errors: list[str] = []
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp_dir = Path(raw_tmp)
        for index, block in enumerate(blocks, start=1):
            errors.extend(structural_check(block, index))
            if has_mmdc:
                errors.extend(render_with_mmdc(block, index, tmp_dir))

    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1

    suffix = " (rendered with mmdc)" if has_mmdc else " (structural only)"
    print(
        f"ok: {len(blocks)} mermaid block(s) validated{suffix}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
