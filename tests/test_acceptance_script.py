from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "acceptance.sh"


def test_acceptance_script_has_valid_bash_syntax() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], cwd=ROOT, text=True, check=True, stdout=subprocess.PIPE)


def test_acceptance_script_dry_run_lists_release_gates() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"],
        cwd=ROOT,
        text=True,
        check=True,
        capture_output=True,
    )
    output = result.stdout

    assert "+ uv run ruff check ." in output
    assert "+ uv run pytest" in output
    assert "+ uv run harn-gibson dogfood --harn-bin true" in output
    assert "+ uv run harn-gibson replay-dir examples/replays" in output
    assert "+ uv run harn-gibson replay-dir examples/dogfood-replays" in output
    assert "+ git diff --check" in output
    assert "+ bash -c 'secret pattern scan'" in output
    assert "+ bash -c 'runtime config scan'" in output
    assert result.stderr == ""


def test_acceptance_script_rejects_unknown_arguments() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--unknown"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == "unknown argument: --unknown"
