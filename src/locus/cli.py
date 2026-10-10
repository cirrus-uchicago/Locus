from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import typer
from pydantic_ai.usage import UsageLimits

from locus.agent import AgentDeps, create_agent
from locus.canary import discover_canary
from locus.tools.clangd import ClangdClient

app = typer.Typer(help="Locus: Fuzzing predicate insertion agent")
logger = logging.getLogger("locus")


def validate_preconditions(
    repo: str, build_command: str, bitcode_build: str | None = None
) -> None:
    """Check that the repo is a clean git working tree with compile_commands.json."""
    repo_path = Path(repo)

    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error: {repo} is not a git repository.", file=sys.stderr)
        raise SystemExit(1)

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        print(
            "Error: working tree is not clean. Commit or stash changes first.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if not (repo_path / "compile_commands.json").exists():
        print(
            "Error: compile_commands.json not found.\n"
            "Generate it with one of:\n"
            "  bear -- make\n"
            "  cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON .",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if bitcode_build is not None:
        import shutil as _shutil

        if _shutil.which("klee") is None:
            print(
                "Error: klee not found on PATH. Install KLEE to use --bitcode-build.",
                file=sys.stderr,
            )
            raise SystemExit(1)


async def _run(
    repo: str,
    build_command: str,
    bitcode_build: str | None,
    model: str,
    max_iterations: int,
    min_iterations: int = 1,
) -> None:
    """Main async entry point for the agent loop."""
    repo_path = Path(repo).resolve()

    canary = discover_canary(repo_path)
    print(f"Found canary at {canary.file}:{canary.line}")
    print(f"Condition: {canary.condition}")

    clangd = ClangdClient(str(repo_path))
    try:
        await clangd.start()
    except FileNotFoundError:
        print(
            "Error: clangd not found. Install clangd and ensure it's on PATH.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    from locus.klee import KleeRunner

    klee_runner = (
        KleeRunner(repo=Path(repo), bitcode_build=bitcode_build)
        if bitcode_build
        else None
    )

    deps = AgentDeps(
        repo=str(repo_path),
        build_command=build_command,
        clangd=clangd,
        klee=klee_runner,
    )

    agent = create_agent(
        model=model,
        canary_file=str(canary.file),
        canary_line=canary.line,
        canary_condition=canary.condition,
        klee_enabled=klee_runner is not None,
    )

    try:
        from locus.validation import PredicateTracker, validate_predicate_with_klee

        prompt = (
            f"Begin the refinement loop. The canary is at {canary.file}:{canary.line} "
            f"with condition: {canary.condition}\n\n"
            f"Start by reading the canary file and surrounding code, then query clangd "
            f"for the call hierarchy. Identify dimensions and insert predicates. "
            f"You have a maximum of {max_iterations} iterations to optimize placement.\n\n"
            f"When you are done (or cannot improve further), respond with EXACTLY the "
            f"text 'LOCUS_DONE' as your final message."
        )
        message_history = None
        result = None
        tracker = PredicateTracker(str(repo_path))
        for iteration in range(1, max_iterations + 1):
            print(f"\n--- Iteration {iteration}/{max_iterations} ---")
            if iteration == 1:
                iter_prompt = prompt
            elif result is not None and "LOCUS_DONE" in result.output:
                iter_prompt = (
                    "You stopped too early. You MUST continue refining. "
                    "Have you identified and inserted predicates for ALL dimensions? "
                    "Have you optimized their placement toward program entry? "
                    "Focus on the next unaddressed dimension or move an existing "
                    "predicate closer to entry. "
                    "Only respond with 'LOCUS_DONE' when ALL dimensions are addressed."
                )
            else:
                iter_prompt = (
                    "Continue to the next phase of the refinement loop. "
                    "Respond with 'LOCUS_DONE' only if ALL dimensions are addressed "
                    "and all predicates are optimally placed."
                )

            result = await agent.run(
                iter_prompt,
                deps=deps,
                message_history=message_history,
                usage_limits=UsageLimits(request_limit=200),
            )
            message_history = result.all_messages()

            if klee_runner:
                for report in tracker.new_predicates(result.new_messages()):
                    print(f"  Validating predicate [{report.dimension}]...")
                    validation_msg = await validate_predicate_with_klee(
                        klee_runner=klee_runner,
                        model=model,
                        repo=str(repo_path),
                        predicate_file=report.file,
                        predicate_line=report.line,
                        predicate_condition=report.condition,
                        canary_file=str(canary.file),
                        canary_line=canary.line,
                        canary_condition=canary.condition,
                        dimension=report.dimension,
                    )
                    print(f"  {validation_msg}")

                    if "UNSOUND" in validation_msg:
                        if report.action == "moved":
                            revert_result = await agent.run(
                                f"{validation_msg}\n"
                                f"Revert this move and keep the previous placement.",
                                deps=deps,
                                message_history=message_history,
                            )
                            message_history = revert_result.all_messages()
                        else:
                            retry_result = await agent.run(
                                f"{validation_msg}\n"
                                f"You have 1 chance to revise this predicate.",
                                deps=deps,
                                message_history=message_history,
                            )
                            message_history = retry_result.all_messages()
                            revisions = tracker.new_predicates(
                                retry_result.new_messages()
                            )
                            for r2 in revisions:
                                retry_msg = await validate_predicate_with_klee(
                                    klee_runner=klee_runner,
                                    model=model,
                                    repo=str(repo_path),
                                    predicate_file=r2.file,
                                    predicate_line=r2.line,
                                    predicate_condition=r2.condition,
                                    canary_file=str(canary.file),
                                    canary_line=canary.line,
                                    canary_condition=canary.condition,
                                    dimension=r2.dimension,
                                )
                                print(f"  Retry: {retry_msg}")
                                if "UNSOUND" in retry_msg:
                                    revert_result = await agent.run(
                                        f"{retry_msg}\nRevision also failed. "
                                        f"Remove predicate [{report.dimension}] "
                                        f"with edit_file and skip this dimension. "
                                        f"Keep the other predicates in the file.",
                                        deps=deps,
                                        message_history=message_history,
                                    )
                                    message_history = revert_result.all_messages()
                                    break
                            if not revisions:
                                revert_result = await agent.run(
                                    f"Revision did not produce a new predicate. "
                                    f"Remove [{report.dimension}] with edit_file. "
                                    f"Keep the other predicates in the file.",
                                    deps=deps,
                                    message_history=message_history,
                                )
                                message_history = revert_result.all_messages()

            if "LOCUS_DONE" in result.output:
                if iteration >= min_iterations:
                    print(f"\nAgent stopped after {iteration} iteration(s).")
                    break
                else:
                    print(
                        f"  Agent wants to stop, but min_iterations={min_iterations} "
                        f"not reached (iteration {iteration}). Continuing..."
                    )
        else:
            print(f"\nMax iterations ({max_iterations}) reached.")

        print("\n--- Locus finished ---")
        if result is not None:
            print(result.output.replace("LOCUS_DONE", "").strip())
    finally:
        await clangd.stop()


@app.command()
def run(
    repo: str = typer.Option(..., help="Path to the target C/C++ codebase"),
    binary_build: str = typer.Option(
        ..., "--binary-build", help="Build command for binary compilation"
    ),
    bitcode_build: str | None = typer.Option(
        None,
        "--bitcode-build",
        help="Build command for LLVM bitcode (enables KLEE validation)",
    ),
    model: str = typer.Option(
        "anthropic:claude-sonnet-4-20250514",
        help="pydantic-ai model string",
    ),
    max_iterations: int = typer.Option(10, help="Maximum refinement iterations"),
    min_iterations: int = typer.Option(
        1,
        "--min-iterations",
        help="Minimum iterations before agent can stop (forces deeper refinement)",
    ),
    verbose: bool = typer.Option(False, help="Enable detailed logging to stderr"),
) -> None:
    """Run Locus to instrument a codebase with fuzzing predicates."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    else:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    if min_iterations > max_iterations:
        print(
            f"Error: --min-iterations ({min_iterations}) cannot exceed "
            f"--max-iterations ({max_iterations}).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    repo_path = str(Path(repo).resolve())
    validate_preconditions(repo_path, binary_build, bitcode_build)
    asyncio.run(
        _run(
            repo_path,
            binary_build,
            bitcode_build,
            model,
            max_iterations,
            min_iterations,
        )
    )
