#!/usr/bin/env python3
"""Cross-reference markdown documentation against the source tree.

For every inline code span in the input markdown the script extracts:

  * filesystem paths that begin with ``src/`` (e.g. ``src/foo/bar.py``)
    and asserts the path exists on disk;
  * dotted symbol references of the form ``ClassName.method_name`` and
    asserts the method exists on the named class in the source tree.

Class and method extraction uses ``ast.parse`` over every ``*.py`` file
under the supplied source root. The script exits 0 when every reference
resolves and 1 when at least one reference is missing.

Usage:
    cross_ref.py <markdown-file> <src-root>
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

INLINE_CODE_PATTERN = re.compile(r"`([^`\n]+)`")
SRC_PATH_PATTERN = re.compile(r"^(src/[A-Za-z0-9_./-]+)$")
DOTTED_SYMBOL_PATTERN = re.compile(r"^([A-Z][A-Za-z0-9_]+)\.([A-Za-z_][A-Za-z0-9_]+)$")
FENCED_BLOCK_PATTERN = re.compile(
    r"^```[^\n]*\n.*?\n```\s*$",
    re.MULTILINE | re.DOTALL,
)


def strip_fenced_blocks(markdown: str) -> str:
    return FENCED_BLOCK_PATTERN.sub("", markdown)


def load_class_methods(src_root: Path) -> dict[str, set[str]]:
    methods_by_class: dict[str, set[str]] = {}
    for py_file in src_root.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names: set[str] = set()
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.add(child.name)
                methods_by_class.setdefault(node.name, set()).update(names)
    return methods_by_class


def extract_code_spans(markdown: str) -> list[str]:
    return [match.group(1) for match in INLINE_CODE_PATTERN.finditer(markdown)]


def find_repo_root(start: Path) -> Path:
    candidate = start.resolve()
    while candidate != candidate.parent:
        if (candidate / "src").exists():
            return candidate
        candidate = candidate.parent
    return start.resolve()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: cross_ref.py <markdown-file> <src-root>", file=sys.stderr)
        return 2

    markdown_path = Path(argv[1])
    src_root = Path(argv[2])
    if not markdown_path.exists():
        print(f"markdown file not found: {markdown_path}", file=sys.stderr)
        return 2
    if not src_root.exists():
        print(f"source root not found: {src_root}", file=sys.stderr)
        return 2

    repo_root = find_repo_root(markdown_path.parent)
    markdown = markdown_path.read_text(encoding="utf-8")
    prose_only = strip_fenced_blocks(markdown)
    spans = extract_code_spans(prose_only)
    class_methods = load_class_methods(src_root)

    src_paths: set[str] = set()
    dotted_refs: set[tuple[str, str]] = set()

    for span in spans:
        path_match = SRC_PATH_PATTERN.match(span)
        if path_match is not None:
            src_paths.add(path_match.group(1))
            continue
        dotted_match = DOTTED_SYMBOL_PATTERN.match(span)
        if dotted_match is not None:
            dotted_refs.add((dotted_match.group(1), dotted_match.group(2)))

    errors: list[str] = []

    for relative_path in sorted(src_paths):
        candidate_under_repo = repo_root / relative_path
        candidate_relative = Path(relative_path)
        if not candidate_under_repo.exists() and not candidate_relative.exists():
            errors.append(f"missing src path: {relative_path}")

    for class_name, method_name in sorted(dotted_refs):
        methods = class_methods.get(class_name)
        if methods is None:
            errors.append(f"missing class for dotted reference: {class_name}.{method_name}")
            continue
        if method_name not in methods:
            errors.append(f"class {class_name} has no method {method_name}")

    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1

    print(
        f"ok: {len(src_paths)} src path(s), {len(dotted_refs)} dotted symbol(s) validated",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
