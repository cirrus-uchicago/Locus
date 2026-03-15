from __future__ import annotations

import subprocess

from locus.models import BuildResult


def run_build(command: str, cwd: str, timeout: int = 120) -> BuildResult:
    """Execute the build command and return success/failure with output."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        return BuildResult(success=result.returncode == 0, output=output.strip())
    except subprocess.TimeoutExpired:
        return BuildResult(success=False, output=f"Timeout: build exceeded {timeout}s")
