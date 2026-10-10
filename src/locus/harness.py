from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent, RunContext

from locus.tools.files import read_file as _read_file

HARNESS_SYSTEM_PROMPT = """\
You are a KLEE harness generator for the Locus predicate verification system.

Your task: Write a KLEE verification harness to check whether a predicate is sound.

## Context

- Predicate location: {predicate_file}:{predicate_line}
- Predicate condition: {predicate_condition}
- Canary location: {canary_file}:{canary_line}
- Canary condition: {canary_condition}

## Instructions

1. Read the code between the predicate and the canary to understand the data flow.
2. Write a C harness function named `__locus_klee_harness_{dimension}` that:
   - Includes `"klee/klee.h"`
   - Declares symbolic variables via `klee_make_symbolic()` for inputs relevant \
to the path between the predicate and the canary
   - Uses `klee_assume(predicate_condition)` to constrain inputs to cases where \
the predicate would kill the execution (the guard is `if (predicate_condition) _exit(1);`)
   - Uses `klee_assert(!(canary_condition))` — if KLEE can violate this assertion, \
a counterexample exists and the predicate is unsound
3. Call write_harness with the complete C source code.

## Pattern

```c
#include "klee/klee.h"

void __locus_klee_harness_{dimension}(void) {{
    // Declare symbolic variables
    int var;
    klee_make_symbolic(&var, sizeof(var), "var");

    // Constrain: predicate would kill (predicate condition holds)
    klee_assume(predicate_condition);

    // Assert: canary should be unreachable
    klee_assert(!(canary_condition));
}}
```
"""


@dataclass
class HarnessDeps:
    """Dependencies for the harness sub-agent."""

    repo: str
    dimension: str


def write_harness(repo: str, dimension: str, content: str) -> str:
    """Write a KLEE harness C file to the repo root."""
    path = Path(repo) / f"__locus_harness_{dimension}.c"
    path.write_text(content)
    return f"OK: wrote harness to {path}"


def create_harness_agent(
    model: str,
    predicate_file: str,
    predicate_line: int,
    predicate_condition: str,
    canary_file: str,
    canary_line: int,
    canary_condition: str,
    dimension: str,
) -> Agent[HarnessDeps, str]:
    """Create a harness-generation sub-agent."""
    system = HARNESS_SYSTEM_PROMPT.format(
        predicate_file=predicate_file,
        predicate_line=predicate_line,
        predicate_condition=predicate_condition,
        canary_file=canary_file,
        canary_line=canary_line,
        canary_condition=canary_condition,
        dimension=dimension,
    )

    agent = Agent(
        model,
        system_prompt=system,
        deps_type=HarnessDeps,
        output_type=str,
    )

    @agent.tool
    async def read_file(
        ctx: RunContext[HarnessDeps],
        file: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Read a source file, optionally a line range."""
        return _read_file(file, start_line, end_line)

    @agent.tool
    async def harness_write(
        ctx: RunContext[HarnessDeps],
        content: str,
    ) -> str:
        """Write the KLEE harness C file."""
        return write_harness(ctx.deps.repo, ctx.deps.dimension, content)

    return agent


async def generate_harness(
    model: str,
    repo: str,
    predicate_file: str,
    predicate_line: int,
    predicate_condition: str,
    canary_file: str,
    canary_line: int,
    canary_condition: str,
    dimension: str,
) -> Path:
    """Fork a sub-agent to generate a KLEE harness. Returns path to harness file."""
    agent = create_harness_agent(
        model=model,
        predicate_file=predicate_file,
        predicate_line=predicate_line,
        predicate_condition=predicate_condition,
        canary_file=canary_file,
        canary_line=canary_line,
        canary_condition=canary_condition,
        dimension=dimension,
    )

    deps = HarnessDeps(repo=repo, dimension=dimension)
    await agent.run(
        "Read the code around the predicate and canary, then write the KLEE harness.",
        deps=deps,
    )

    return Path(repo) / f"__locus_harness_{dimension}.c"
