from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class Canary(BaseModel):
    """A MAGMA_LOG canary found in the target codebase."""

    file: Path
    line: int
    condition: str
    raw_line: str


class BuildResult(BaseModel):
    """Result of running the build command."""

    success: bool
    output: str


class ValidationResult(BaseModel):
    """Result of KLEE symbolic validation of a predicate."""

    sound: bool
    counterexample: dict[str, str] | None
    klee_output: str


class PredicateReport(BaseModel):
    """Structured report of a predicate insertion, emitted by the agent."""

    dimension: str
    file: str
    line: int
    condition: str
    action: str
