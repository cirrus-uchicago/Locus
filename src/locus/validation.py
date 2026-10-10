from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart

from locus.harness import generate_harness
from locus.klee import KleeRunner
from locus.models import PredicateReport, ValidationResult

logger = logging.getLogger("locus")

_EXIT = re.compile(r"\b_exit\s*\(\s*1\s*\)")
_IF = re.compile(r"\bif\s*\(")
_GUARD_BODY = re.compile(r"\s*\{?\s*_exit\s*\(\s*1\s*\)")
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
_LOCUS_COMMENT = re.compile(r"//\s*Locus:\s*(.+)")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def format_validation_message(
    result: ValidationResult, dimension: str, location: str
) -> str:
    """Format a KLEE validation result as a message for the main agent."""
    if result.sound is None:
        return f"Predicate [{dimension}] at {location} not validated: harness build or KLEE run failed."
    if result.sound:
        return f"Predicate [{dimension}] at {location} passed symbolic validation."
    ce = result.counterexample or {}
    ce_str = ", ".join(f"{k}={v}" for k, v in ce.items()) if ce else "unknown"
    return (
        f"Predicate [{dimension}] at {location} UNSOUND. Counterexample: {ce_str}. "
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

    logger.log(
        logging.WARNING if result.sound is None else logging.DEBUG,
        "KLEE validation for %s: %s",
        dimension,
        result.klee_output,
    )
    return format_validation_message(
        result, dimension, f"{predicate_file}:{predicate_line}"
    )


def parse_predicate_reports(messages: list[ModelMessage]) -> list[PredicateReport]:
    """Extract LOCUS_PREDICATE structured reports from the agent's text output."""
    text = "\n".join(
        part.content
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, TextPart)
    )
    reports = []
    pattern = re.compile(
        r"LOCUS_PREDICATE:\s+"
        r"dimension=(\S+)\s+"
        r"file=(\S+)\s+"
        r"line=(\d+)\s+"
        r"condition=(.+?)\s+"
        r"action=(\w+)"
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


class _Guard(NamedTuple):
    file: str
    line: int
    condition: str
    comment: str | None


class PredicateTracker:
    """The diff decides which predicates exist, because the agent often omits its
    LOCUS_PREDICATE reports. A matching report only names the predicate and
    marks it as moved.
    """

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.base = "HEAD"
        self.count = 0

    def new_predicates(self, messages: list[ModelMessage]) -> list[PredicateReport]:
        snapshot = self._git("stash", "create") or "HEAD"
        diff = self._git(
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            self.base,
        )
        self.base = snapshot

        reports = {
            _predicate_key(Path(self.repo, r.file), r.condition): r
            for r in parse_predicate_reports(messages)
        }
        predicates = []
        for file, line, condition, comment in _parse_guards(diff, self.repo):
            self.count += 1
            report = reports.get(_predicate_key(Path(file), condition))
            predicates.append(
                PredicateReport(
                    dimension=report.dimension
                    if report
                    else _dimension_from_comment(comment or "", self.count),
                    file=file,
                    line=line,
                    condition=condition,
                    action=report.action if report else "inserted",
                )
            )
        return predicates

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()


def _predicate_key(file: Path, condition: str) -> tuple[Path, str]:
    return file.resolve(), re.sub(r"\s+", "", condition).strip("`'\"")


def _parse_guards(diff: str, repo: str) -> Iterator[_Guard]:
    file: str | None = None
    line = 0
    window: list[tuple[int, str]] = []
    for diff_line in diff.splitlines():
        if diff_line.startswith("diff --git"):
            file = None
        elif file is None:
            if diff_line.startswith("+++ b/"):
                file = str(Path(repo) / diff_line[6:])
        elif hunk := _HUNK.match(diff_line):
            line = int(hunk.group(1)) - 1
            window = []
        elif diff_line[:1] in ("+", " "):
            line += 1
            window.append((line, diff_line[1:]))
            if not _EXIT.search(_COMMENT.sub("", diff_line)):
                continue
            if diff_line[0] == "+":
                guard = _parse_guard(file, window)
                if guard is None:
                    logger.warning(
                        "No `if (...) _exit(1)` guard found for %s:%d; "
                        "it is not validated.",
                        file,
                        line,
                    )
                else:
                    yield guard
            window = []


def _parse_guard(file: str, window: list[tuple[int, str]]) -> _Guard | None:
    text = "\n".join(content for _, content in window)
    code = _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)
    for match in reversed(list(_IF.finditer(code))):
        end = _closing_paren(code, match.end())
        if end is None or not _GUARD_BODY.match(code, end + 1):
            continue
        line = window[code.count("\n", 0, match.start())][0]
        condition = " ".join(code[match.end() : end].split())
        comments = _LOCUS_COMMENT.findall(text)
        return _Guard(file, line, condition, comments[-1].strip() if comments else None)
    return None


def _closing_paren(code: str, start: int) -> int | None:
    depth = 1
    for i in range(start, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


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
