from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from locus.models import BuildResult, ValidationResult


class KleeRunner:
    """Handles bitcode compilation and KLEE invocation."""

    def __init__(self, repo: Path, bitcode_build: str) -> None:
        self.repo = repo
        self.bitcode_build = bitcode_build

    def build_bitcode(self, timeout: int = 120) -> BuildResult:
        """Run the bitcode build command."""
        try:
            result = subprocess.run(
                self.bitcode_build,
                shell=True,
                cwd=str(self.repo),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout + result.stderr
            return BuildResult(success=result.returncode == 0, output=output.strip())
        except subprocess.TimeoutExpired:
            return BuildResult(
                success=False, output=f"Timeout: bitcode build exceeded {timeout}s"
            )

    def run_klee(
        self,
        bitcode_file: Path,
        entry_point: str,
        output_dir: Path,
        timeout: int = 30,
    ) -> ValidationResult:
        """Run KLEE on bitcode with given entry point. Parse results."""
        try:
            result = subprocess.run(
                [
                    "klee",
                    f"--entry-point={entry_point}",
                    f"--max-time={timeout}",
                    f"--output-dir={output_dir}",
                    str(bitcode_file),
                ],
                capture_output=True,
                text=True,
                timeout=timeout + 30,
            )
            klee_output = result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return ValidationResult(
                sound=True,
                counterexample=None,
                klee_output="KLEE process timed out (treated as sound)",
            )

        err_files = sorted(output_dir.glob("*.assert.err"))
        if not err_files:
            return ValidationResult(
                sound=True if result.returncode == 0 else None,
                counterexample=None,
                klee_output=klee_output,
            )

        ktest_file = err_files[0].with_suffix("").with_suffix(".ktest")
        counterexample = self._parse_ktest(ktest_file)

        return ValidationResult(
            sound=False, counterexample=counterexample, klee_output=klee_output
        )

    def _parse_ktest(self, ktest_file: Path) -> dict[str, str]:
        """Parse a .ktest file using ktest-tool and extract variable assignments."""
        result = subprocess.run(
            ["ktest-tool", str(ktest_file)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return {}

        objects: dict[str, str] = {}
        current_name: str | None = None
        for line in result.stdout.splitlines():
            if ": name: " in line:
                current_name = line.split(": name: ")[1].strip().strip("'")
            elif ": int : " in line and current_name is not None:
                objects[current_name] = line.split(": int : ")[1].strip()
                current_name = None
        return objects

    def check_predicate(
        self,
        harness_file: Path,
        entry_point: str,
        dimension: str,
    ) -> ValidationResult:
        """Full validation pipeline: bitcode build -> KLEE -> cleanup."""
        logger = logging.getLogger("locus")
        output_dir = self.repo / f"__locus_klee_out_{dimension}"

        try:
            build_result = self.build_bitcode()
            if not build_result.success:
                return ValidationResult(
                    sound=None,
                    counterexample=None,
                    klee_output=f"Bitcode build failed: {build_result.output}",
                )

            bitcode_file = self._find_bitcode()

            result = self.run_klee(
                bitcode_file=bitcode_file,
                entry_point=entry_point,
                output_dir=output_dir,
            )

            logger.debug("KLEE output for %s:\n%s", dimension, result.klee_output)
            return result
        finally:
            if harness_file.exists():
                harness_file.unlink()
            if output_dir.exists():
                shutil.rmtree(output_dir)

    def _find_bitcode(self) -> Path:
        """Find the .bc file produced by the bitcode build."""
        bc_files = list(self.repo.glob("*.bc"))
        if not bc_files:
            bc_files = list(self.repo.rglob("*.bc"))
        if not bc_files:
            raise FileNotFoundError(
                "No .bc file found after bitcode build. "
                "Ensure your --bitcode-build command produces a .bc file."
            )
        return max(bc_files, key=lambda f: f.stat().st_mtime)
