from tiny_tasks.cli import format_tasks


def test_format_tasks_numbers_items() -> None:
    assert format_tasks(["alpha", " beta "]) == "1. alpha\n2. beta"
