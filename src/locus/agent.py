from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai import Agent, RunContext

from locus.tools.build import run_build as _run_build
from locus.tools.clangd import ClangdClient

if TYPE_CHECKING:
    from locus.klee import KleeRunner
from locus.tools.files import edit_file as _edit_file
from locus.tools.files import find_files as _find_files
from locus.tools.files import grep as _grep
from locus.tools.files import read_file as _read_file
from locus.tools.git import git_diff as _git_diff
from locus.tools.git import git_restore as _git_restore

SYSTEM_PROMPT = """\
You are Locus, a C/C++ code instrumentation agent. Your job is to insert \
early-termination predicates that prune fuzzer executions which can never \
trigger the bug described by a MAGMA_LOG canary.

## Canary

The canary has been found at:
- File: {canary_file}
- Line: {canary_line}
- Condition: {canary_condition}

## Procedure

Follow this refinement loop. Each iteration should focus on ONE phase — do NOT \
try to do everything in a single iteration.

### Phase 1: ANALYZE (first iteration)

Read the canary and surrounding code. Use grep and clangd to trace the data flow. \
Query the call hierarchy upstream from the canary location. \
Identify ALL independent necessary-condition dimensions. \
You MUST identify at least 2 dimensions — think broadly:

- **Value range**: Numeric bounds on variables in the canary condition.
- **Format/type validation**: Magic numbers, format fields, enum values that must \
hold for the canary path to be reachable.
- **Control flow**: Conditions on which branch/path leads to the canary (file mode, \
read vs write, etc.).
- **Size/length**: Buffer sizes, file lengths, data lengths that constrain reachability.
- **State**: Global or struct state that must be set before the canary is reachable.
- **Encoding/endianness**: Byte order or encoding that must match.

Use `grep` to search for how variables in the canary condition are assigned, \
and `find_files` to understand the project structure. \
End this iteration by listing ALL dimensions you have identified. \
Do NOT insert any predicates yet — just report your analysis.

### Phase 2: GENERATE (one iteration per dimension)

For each dimension, synthesize:
   - Helper code (tracking variables, helper functions) if needed — at file/global scope.
   - State update code at key assignment/mutation points.
   - The predicate itself: `if (condition) _exit(1);` guard.

Insert at initial locations near the canary, then validate the build. \
If the build fails, fix errors and retry (max 3 retries). After 3 failures, \
remove that predicate's changes with edit_file and move to the next dimension.

### Phase 3: OPTIMIZE PLACEMENT (one iteration per predicate)

For each predicate independently:
   - Use clangd call hierarchy to find callers moving toward program entry.
   - Determine if the condition can still be evaluated at the new location.
   - Move the predicate, validate the build.
   - If the build fails or the condition can't be expressed, keep previous placement.

### Phase 4: FINALIZE

Only after you have completed phases 1-3 for ALL dimensions should you consider \
stopping. Report what you accomplished and respond with 'LOCUS_DONE'.

## Constraints

- All inserted code must use `__locus_` prefix for identifiers.
- All inserted code must have `// Locus:` comment prefixes.
- Use `_exit(1)` (not `exit()`). Ensure `#include <unistd.h>` is present in any \
file where `_exit` is inserted.
- When using edit_file, always re-read the file after prior edits to get current \
line numbers. Never assume line numbers are stable across multiple edits.
- Do NOT respond with 'LOCUS_DONE' until you have addressed ALL identified dimensions.

## Error Recovery

- If clangd fails, fall back to your own reasoning about the call graph.
- If a build fails 3 times for one predicate, remove it with edit_file and move on.
- Use git_restore only to discard every change in a file: it also removes the \
other predicates in that file.
"""


KLEE_PROMPT_ADDENDUM = """\

## Symbolic Validation

After each successful build, your predicates will be automatically verified using \
KLEE symbolic execution (if enabled). You do not need to trigger this — the \
orchestration layer finds every `_exit(1)` guard you add or change and validates \
it. If a predicate is found to be unsound, you will receive a message with the \
counterexample (concrete variable values for which the predicate exits but the \
canary is still reachable). You get one chance to revise the predicate based on \
this feedback. If the revision also fails, the predicate is reverted.
"""

PREDICATE_REPORT_INSTRUCTION = """\

## Reporting

After each successful build following a predicate insertion or move, you MUST output \
a structured report line in this exact format:

LOCUS_PREDICATE: dimension=<name> file=<path> line=<number> condition=<expr> action=<inserted|moved>

Example:
LOCUS_PREDICATE: dimension=value_range file=src/main.c line=42 condition=row_width>65535 action=inserted

The orchestration layer uses this line to name the predicate and to tell a move \
from a new insertion.
"""


@dataclass
class AgentDeps:
    """Dependencies injected into the agent's tool functions."""

    repo: str
    build_command: str
    clangd: ClangdClient
    klee: KleeRunner | None = None


def create_agent(
    model: str,
    canary_file: str,
    canary_line: int,
    canary_condition: str,
    klee_enabled: bool = False,
) -> Agent[AgentDeps, str]:
    """Create and configure the Locus pydantic-ai agent."""

    system = SYSTEM_PROMPT.format(
        canary_file=canary_file,
        canary_line=canary_line,
        canary_condition=canary_condition,
    )
    if klee_enabled:
        system += KLEE_PROMPT_ADDENDUM
    system += PREDICATE_REPORT_INSTRUCTION

    agent = Agent(
        model,
        system_prompt=system,
        deps_type=AgentDeps,
        output_type=str,
    )

    @agent.tool
    async def read_file(
        ctx: RunContext[AgentDeps],
        file: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Read a source file, optionally a line range. Lines are 1-indexed."""
        return _read_file(file, start_line, end_line)

    @agent.tool
    async def edit_file(
        ctx: RunContext[AgentDeps],
        file: str,
        start_line: int,
        content: str,
        end_line: int | None = None,
    ) -> str:
        """Insert or replace code in a source file.

        If end_line is None: insert content before start_line.
        If end_line is provided: replace lines start_line..end_line (inclusive).
        Lines are 1-indexed. Always re-read the file after edits to get fresh line numbers.
        """
        return _edit_file(file, start_line, content, end_line)

    @agent.tool
    async def grep(
        ctx: RunContext[AgentDeps],
        pattern: str,
        glob: str | None = None,
    ) -> str:
        """Search C/C++ files in the repo for lines matching a regex pattern.

        Returns file:line: content for each match. Use to find variable assignments,
        function calls, macro definitions, etc. across the codebase.
        Optionally filter to files matching a glob (e.g. '*.c', 'src/**/*.h').
        """
        return _grep(ctx.deps.repo, pattern, glob)

    @agent.tool
    async def find_files(
        ctx: RunContext[AgentDeps],
        glob: str = "*",
    ) -> str:
        """Find C/C++ files in the repo matching a glob pattern.

        Returns relative paths. Use to discover project structure and locate files.
        Examples: '*.c', 'src/**/*.h', '*paf*'.
        """
        return _find_files(ctx.deps.repo, glob)

    @agent.tool
    async def build(ctx: RunContext[AgentDeps]) -> str:
        """Run the build command. Returns success/failure with compiler output."""
        result = _run_build(ctx.deps.build_command, cwd=ctx.deps.repo)
        status = "SUCCESS" if result.success else "FAILED"
        return f"Build {status}:\n{result.output}"

    @agent.tool
    async def git_diff(ctx: RunContext[AgentDeps]) -> str:
        """Show all uncommitted changes in the repo."""
        return _git_diff(ctx.deps.repo)

    @agent.tool
    async def git_restore(ctx: RunContext[AgentDeps], file: str) -> str:
        """Restore a file to its last committed state, reverting all changes.

        This also removes every other predicate in the file. To drop one
        predicate, delete its lines with edit_file instead.
        """
        return _git_restore(ctx.deps.repo, file)

    @agent.tool
    async def clangd_call_hierarchy(
        ctx: RunContext[AgentDeps],
        file: str,
        line: int,
        character: int = 0,
    ) -> str:
        """Get incoming callers of the function at the given position.

        Use this to trace the call chain from the canary back toward program entry.
        """
        return await ctx.deps.clangd.call_hierarchy(file, line, character)

    @agent.tool
    async def clangd_go_to_definition(
        ctx: RunContext[AgentDeps],
        file: str,
        line: int,
        character: int = 0,
    ) -> str:
        """Get the definition location of the symbol at the given position."""
        return await ctx.deps.clangd.go_to_definition(file, line, character)

    @agent.tool
    async def clangd_find_references(
        ctx: RunContext[AgentDeps],
        file: str,
        line: int,
        character: int = 0,
    ) -> str:
        """Find all references to the symbol at the given position."""
        return await ctx.deps.clangd.find_references(file, line, character)

    @agent.tool
    async def clangd_document_symbols(ctx: RunContext[AgentDeps], file: str) -> str:
        """List all symbols defined in a file."""
        return await ctx.deps.clangd.document_symbols(file)

    return agent
