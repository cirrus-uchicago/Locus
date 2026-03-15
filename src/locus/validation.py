from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from locus.harness import generate_harness
from locus.klee import KleeRunner
from locus.models import PredicateReport, ValidationResult

logger = logging.getLogger("locus")


def format_validation_message(result: ValidationResult, dimension: str) -> str:
    """Format a KLEE validation result as a message for the main agent."""
    if result.sound:
        return f"Predicate [{dimension}] passed symbolic validation."
    ce = result.counterexample or {}
    ce_str = ", ".join(f"{k}={v}" for k, v in ce.items()) if ce else "unknown"
    return (
        f"Predicate [{dimension}] UNSOUND. Counterexample: {ce_str}. "
        f"Canary reachable with these values while predicate would _exit. "
        f"Please revise."
    )


async def validate_predicate_with_klee(
    klee_runner: KleeRunner,
    model: str,
    repo: str,
    predicate_file: str,
    predicate_line: int,
    predicate_condition: str,
    canary_file: str,
    canary_line: int,
    canary_condition: str,
    dimension: str,
) -> str:
    """Run KLEE validation for a single predicate. Returns a status message."""
    harness_path = await generate_harness(
        model=model,
        repo=repo,
        predicate_file=predicate_file,
        predicate_line=predicate_line,
        predicate_condition=predicate_condition,
        canary_file=canary_file,
        canary_line=canary_line,
        canary_condition=canary_condition,
        dimension=dimension,
    )

    entry_point = f"__locus_klee_harness_{dimension}"
    result = klee_runner.check_predicate(
        harness_file=harness_path,
        entry_point=entry_point,
        dimension=dimension,
    )

    logger.debug("KLEE validation for %s: %s", dimension, result.klee_output)
    return format_validation_message(result, dimension)


def parse_predicate_reports(text: str) -> list[PredicateReport]:
    """Extract LOCUS_PREDICATE structured reports from agent output."""
    reports = []
    pattern = re.compile(
        r"LOCUS_PREDICATE:\s+"
        r"dimension=(\S+)\s+"
        r"file=(\S+)\s+"
        r"line=(\d+)\s+"
        r"condition=(\S+)\s+"
        r"action=(\S+)"
    )
    for match in pattern.finditer(text):
        reports.append(
            PredicateReport(
                dimension=match.group(1),
                file=match.group(2),
                line=int(match.group(3)),
                condition=match.group(4),
                action=match.group(5),
            )
        )
    return reports


def detect_predicates_from_diff(repo: str) -> list[PredicateReport]:
    """Detect inserted predicates by parsing git diff for // Locus: patterns.

    Fallback for when the agent doesn't emit LOCUS_PREDICATE report lines.
    Scans the unified diff for added lines containing '// Locus:' comments
    followed by 'if (...) _exit(1);' guards.
    """
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []

    reports: list[PredicateReport] = []
    current_file: str | None = None
    current_line = 0
    pending_comment: str | None = None
    pending_line = 0
    pending_code = ""
    dim_counter = 0

    def _try_match_predicate() -> bool:
        """Try to extract a predicate from accumulated pending_code."""
        nonlocal pending_comment, pending_code, dim_counter
        if pending_comment is None or current_file is None:
            return False
        joined = " ".join(pending_code.split())
        exit_match = re.search(
            r"if\s*\((.+)\)\s*(?:\{?\s*)?_exit\s*\(\s*1\s*\)", joined
        )
        if exit_match:
            condition = exit_match.group(1).strip()
            dim_counter += 1
            dimension = _dimension_from_comment(pending_comment, dim_counter)
            reports.append(
                PredicateReport(
                    dimension=dimension,
                    file=current_file,
                    line=pending_line,
                    condition=condition,
                    action="inserted",
                )
            )
            pending_comment = None
            pending_code = ""
            return True
        return False

    for diff_line in result.stdout.splitlines():
        if diff_line.startswith("+++ b/"):
            current_file = str(Path(repo) / diff_line[6:])
            pending_comment = None
            pending_code = ""
            continue

        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", diff_line)
        if hunk_match:
            current_line = int(hunk_match.group(1)) - 1
            pending_comment = None
            pending_code = ""
            continue

        if diff_line.startswith("+"):
            current_line += 1
            added = diff_line[1:]

            locus_match = re.search(r"//\s*Locus:\s*(.+)", added)
            if locus_match:
                pending_comment = locus_match.group(1).strip()
                pending_line = current_line
                pending_code = ""
                continue

            if pending_comment is not None:
                pending_code += " " + added
                if _try_match_predicate():
                    continue
                if pending_code.count("\n") > 5 or len(pending_code) > 500:
                    pending_comment = None
                    pending_code = ""
                continue

        elif diff_line.startswith("-"):
            pass
        else:
            current_line += 1
            if diff_line.strip():
                pending_comment = None
                pending_code = ""

    return reports


def _dimension_from_comment(comment: str, index: int) -> str:
    """Derive a dimension name from a // Locus: comment string."""
    comment_lower = comment.lower()
    for keyword, dim in [
        ("channel", "channels"),
        ("sample_rate", "sample_rate"),
        ("samplerate", "sample_rate"),
        ("sample rate", "sample_rate"),
        ("format", "format"),
        ("value range", "value_range"),
        ("range", "value_range"),
        ("type", "type_check"),
        ("bounds", "bounds_check"),
        ("state", "state_check"),
    ]:
        if keyword in comment_lower:
            return dim
    return f"dim_{index}"
