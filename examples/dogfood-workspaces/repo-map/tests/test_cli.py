from __future__ import annotations

from pathlib import Path

import pytest
from repo_map.cli import format_summary, summarize_lines


def test_summarize_lines_tracks_priority_and_owner() -> None:
    assert summarize_lines(
        """
        # ignored heading
        !ops::reroute packets
        docs::publish topology note
        review screenshot thresholds
        """
    ) == [
        "03 [high] ops: reroute packets",
        "04 [normal] docs: publish topology note",
        "05 [normal] unassigned: review screenshot thresholds",
    ]


def test_format_summary_reads_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "tasks.txt"
    fixture.write_text("agent::capture events\n!tests::fix failing case\n", encoding="utf-8")

    assert format_summary(fixture) == (
        "01 [normal] agent: capture events\n"
        "02 [high] tests: fix failing case\n"
    )


def test_format_summary_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        format_summary(tmp_path / "missing.txt")
