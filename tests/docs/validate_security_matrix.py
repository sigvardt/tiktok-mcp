"""Validate the threat-to-defense matrix in docs/security-model.md.

Asserts:
1. The matrix table under '## Threat-to-defense matrix' exists.
2. Every data row has a non-empty 'Defenses' cell.
3. Every data row cites at least one 'Layer N' reference.
4. Every 'Layer N' reference resolves to a '### Layer N:' section heading.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HEADING_PATTERN = re.compile(r"^###\s+Layer\s+(\d+):", re.MULTILINE)
_MATRIX_SECTION_PATTERN = re.compile(
    r"^##\s+Threat-to-defense matrix\s*\n(?P<body>.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_LAYER_REF_PATTERN = re.compile(r"Layer\s+(\d+)")
_TABLE_SEPARATOR_PATTERN = re.compile(r"^\|\s*[-:]+")


def _find_doc() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "docs" / "security-model.md"
        if candidate.is_file() and (parent / "pyproject.toml").is_file():
            return candidate
    raise FileNotFoundError(
        "Could not locate docs/security-model.md from tests/docs/"
    )


def _split_row(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def main() -> int:
    doc = _find_doc()
    text = doc.read_text(encoding="utf-8")

    layer_headings: set[int] = {int(m.group(1)) for m in _HEADING_PATTERN.finditer(text)}
    if not layer_headings:
        _ = sys.stderr.write(
            f"validate_security_matrix: no '### Layer N:' headings found in {doc}\n"
        )
        return 1

    section_match = _MATRIX_SECTION_PATTERN.search(text)
    if section_match is None:
        _ = sys.stderr.write(
            "validate_security_matrix: '## Threat-to-defense matrix' section not found\n"
        )
        return 1

    section_body = section_match.group("body")
    table_lines = [
        line
        for line in section_body.splitlines()
        if line.startswith("|") and not _TABLE_SEPARATOR_PATTERN.match(line)
    ]
    if len(table_lines) < 2:
        _ = sys.stderr.write(
            f"validate_security_matrix: matrix table has too few rows ({len(table_lines)})\n"
        )
        return 1

    header_cells = _split_row(table_lines[0])
    defenses_idx: int | None = None
    for index, cell in enumerate(header_cells):
        if "defense" in cell.lower():
            defenses_idx = index
            break

    if defenses_idx is None:
        _ = sys.stderr.write(
            f"validate_security_matrix: no 'Defenses' column in header: {header_cells}\n"
        )
        return 1

    failures: list[str] = []
    referenced_layers: set[int] = set()
    data_rows = table_lines[1:]

    for row_index, row in enumerate(data_rows, start=1):
        cells = _split_row(row)
        if len(cells) <= defenses_idx:
            failures.append(f"row {row_index}: too few cells ({cells!r})")
            continue

        defenses = cells[defenses_idx]
        if not defenses:
            failures.append(f"row {row_index}: Defenses cell is empty")
            continue

        layer_refs = [int(m.group(1)) for m in _LAYER_REF_PATTERN.finditer(defenses)]
        if not layer_refs:
            failures.append(
                f"row {row_index}: no 'Layer N' reference in Defenses cell {defenses!r}"
            )
            continue

        referenced_layers.update(layer_refs)

    missing = sorted(referenced_layers - layer_headings)
    if missing:
        failures.append(
            f"Layer references without matching '### Layer N:' headings: {missing}"
        )

    if failures:
        _ = sys.stderr.write(
            f"validate_security_matrix: {len(failures)} problem(s) in {doc}\n"
        )
        for failure in failures:
            _ = sys.stderr.write(f"  - {failure}\n")
        return 1

    summary = (
        f"validate_security_matrix: {len(data_rows)} data row(s) validated, "
        + f"layers referenced: {sorted(referenced_layers)}, "
        + f"layer headings present: {sorted(layer_headings)}\n"
    )
    _ = sys.stdout.write(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
