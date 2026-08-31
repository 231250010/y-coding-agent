"""Small reviewed entry point for the test-review example Skill."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    targets = sys.argv[1:] or ["tests"]
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *targets],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
