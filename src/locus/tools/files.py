from __future__ import annotations

import fnmatch
import re
from pathlib import Path

_C_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
_MAX_GREP_MATCHES = 100
_MAX_FIND_RESULTS = 200


def read_file(
    file: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a source file, optionally a specific line range.

    Lines are 1-indexed. Returns numbered lines for the agent.
    """
    path = Path(file)
    if not path.exists():
        return f"Error: file not found: {file}"
    if path.is_dir():
        return f"Error: {file} is a directory, not a file"

    lines = path.read_text().splitlines(keepends=True)

    if start_line is not None and end_line is not None:
        selected = lines[start_line - 1 : end_line]
    elif start_line is not None:
        selected = lines[start_line - 1 :]
    else:
        selected = lines

    offset = start_line or 1
    numbered = [f"{offset + i:4d} | {line}" for i, line in enumerate(selected)]
    return "".join(numbered)


def edit_file(
    file: str,
    start_line: int,
    content: str,
    end_line: int | None = None,
) -> str:
    """Insert or replace code in a source file.

    If end_line is None: insert content before start_line.
    If end_line is provided: replace lines start_line..end_line (inclusive) with content.
    Lines are 1-indexed.
    """
    path = Path(file)
    if not path.exists():
        return f"Error: file not found: {file}"

    lines = path.read_text().splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    content_lines = content.splitlines(keepends=True)
    if content_lines and not content_lines[-1].endswith("\n"):
        content_lines[-1] += "\n"

    if end_line is None:
        idx = start_line - 1
        lines[idx:idx] = content_lines
    else:
        lines[start_line - 1 : end_line] = content_lines

    path.write_text("".join(lines))
    return "OK"


def grep(
    repo: str,
    pattern: str,
    glob: str | None = None,
    max_results: int = _MAX_GREP_MATCHES,
) -> str:
    """Search C/C++ files in the repo for lines matching a regex pattern.

    Returns matching lines as file:line_number: content.
    Optionally filter to files matching a glob pattern (e.g. '*.c', 'src/**/*.h').
    """
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"

    repo_path = Path(repo)
    if not repo_path.is_dir():
        return f"Error: repo not found: {repo}"

    matches: list[str] = []
    for source_file in sorted(repo_path.rglob("*")):
        if source_file.suffix not in _C_EXTENSIONS:
            continue
        if not source_file.is_file():
            continue
        if glob is not None and not fnmatch.fnmatch(
            str(source_file.relative_to(repo_path)), glob
        ):
            continue
        try:
            lines = source_file.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line_num, line_text in enumerate(lines, start=1):
            if regex.search(line_text):
                rel = source_file.relative_to(repo_path)
                matches.append(f"{rel}:{line_num}: {line_text.rstrip()}")
                if len(matches) >= max_results:
                    matches.append(f"... (truncated at {max_results} matches)")
                    return "\n".join(matches)

    if not matches:
        return "No matches found."
    return "\n".join(matches)


def find_files(
    repo: str,
    glob: str = "*",
    max_results: int = _MAX_FIND_RESULTS,
) -> str:
    """Find files in the repo matching a glob pattern.

    Returns relative paths, one per line. Defaults to listing all files.
    Only includes C/C++ source files unless glob explicitly targets other extensions.
    """
    repo_path = Path(repo)
    if not repo_path.is_dir():
        return f"Error: repo not found: {repo}"

    has_ext = any(ext in glob for ext in _C_EXTENSIONS)

    results: list[str] = []
    for path in sorted(repo_path.rglob(glob)):
        if not path.is_file():
            continue
        if not has_ext and path.suffix not in _C_EXTENSIONS:
            continue
        results.append(str(path.relative_to(repo_path)))
        if len(results) >= max_results:
            results.append(f"... (truncated at {max_results} results)")
            break

    if not results:
        return "No files found."
    return "\n".join(results)
