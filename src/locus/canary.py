from __future__ import annotations

import re
import sys
from pathlib import Path

from locus.models import Canary


def discover_canary(repo: Path) -> Canary:
    """Search for a single MAGMA_LOG statement in the repo.

    Aborts if zero or more than one is found.
    """
    matches: list[Canary] = []

    for source_file in _iter_c_files(repo):
        for line_num, line_text in enumerate(
            source_file.read_text().splitlines(), start=1
        ):
            if "MAGMA_LOG" not in line_text:
                continue
            condition = _extract_condition(line_text)
            if condition is not None:
                matches.append(
                    Canary(
                        file=source_file,
                        line=line_num,
                        condition=condition,
                        raw_line=line_text.strip(),
                    )
                )

    if len(matches) == 0:
        print("Error: No MAGMA_LOG statement found in the codebase.", file=sys.stderr)
        raise SystemExit(1)

    if len(matches) > 1:
        print(
            f"Error: Found {len(matches)} MAGMA_LOG statements, expected exactly 1.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return matches[0]


def _extract_condition(line: str) -> str | None:
    """Extract the condition (second argument) from a MAGMA_LOG call.

    Handles nested parentheses correctly.
    """
    match = re.search(r"MAGMA_LOG\s*\(", line)
    if match is None:
        return None

    start = match.end()

    if start >= len(line) or line[start] != '"':
        return None

    i = start + 1
    while i < len(line) and line[i] != '"':
        if line[i] == "\\":
            i += 1
        i += 1
    i += 1

    while i < len(line) and line[i] in " \t":
        i += 1
    if i >= len(line) or line[i] != ",":
        return None
    i += 1
    while i < len(line) and line[i] in " \t":
        i += 1

    cond_start = i
    depth = 1
    while i < len(line) and depth > 0:
        if line[i] == "(":
            depth += 1
        elif line[i] == ")":
            depth -= 1
        i += 1

    if depth != 0:
        return None

    condition = line[cond_start : i - 1].strip()
    condition = condition.rstrip(";").strip()
    return condition


def _iter_c_files(repo: Path) -> list[Path]:
    """Return all C/C++ source files in the repo, sorted for determinism."""
    extensions = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
    files = [f for f in repo.rglob("*") if f.suffix in extensions]
    return sorted(files)
