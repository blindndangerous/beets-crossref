"""Open an issue when a newer stable CPython can join the test matrix.

The newer Python is ready when the project and its test extra resolve on it
from wheels only, for Linux x86-64. Needs uv and gh on PATH, and GH_TOKEN.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

WORKFLOW = Path(".github/workflows/test.yml")


def run(*args):
    return subprocess.run(args, capture_output=True, text=True)


def matrix_newest():
    match = re.search(r"python-version:\s*\[([^\]]*)\]", WORKFLOW.read_text())
    versions = re.findall(r"(\d+)\.(\d+)", match.group(1))
    return max((int(major), int(minor)) for major, minor in versions)


def stable_newest():
    out = run("uv", "python", "list", "--only-downloads", "--output-format", "json")
    out.check_returncode()
    return max(
        (p["version_parts"]["major"], p["version_parts"]["minor"])
        for p in json.loads(out.stdout)
        if p["implementation"] == "cpython"
        and p["variant"] == "default"
        and re.fullmatch(r"\d+\.\d+\.\d+", p["version"])
    )


def main():
    in_matrix, stable = matrix_newest(), stable_newest()
    current, newest = ".".join(map(str, in_matrix)), ".".join(map(str, stable))
    if stable <= in_matrix:
        print(f"Newest stable Python is {newest}; the matrix already reaches {current}.")
        return 0

    check = run(
        *["uv", "pip", "compile", "pyproject.toml", "--extra", "test", "--no-build", "--quiet"],
        *["--no-header", "--python-version", newest, "--python-platform", "x86_64-manylinux_2_28"],
    )
    if check.returncode != 0:
        print(f"Python {newest} is not ready yet:")
        print(check.stderr)
        return 0

    title = f"Python {newest} is ready"
    found = run(
        *["gh", "issue", "list", "--state", "all", "--json", "title", "--jq", ".[].title"],
        *["--search", f'in:title "{title}"'],
    )
    found.check_returncode()
    if title in found.stdout.splitlines():
        print(f"Python {newest} is ready; the issue already exists.")
        return 0

    body = (
        f"beets-crossref and its test dependencies now install from wheels on "
        f"Python {newest} (Linux x86-64), so it can join the test matrix.\n\n"
        f'Add "{newest}" to `python-version` in `.github/workflows/test.yml`, '
        f"next to {current}.\n\n"
        f"Resolved with `uv pip compile pyproject.toml --extra test "
        f"--python-version {newest} --no-build`:\n\n```\n{check.stdout}```\n"
    )
    created = run("gh", "issue", "create", "--title", title, "--body", body)
    created.check_returncode()
    print(f"Opened {created.stdout.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
