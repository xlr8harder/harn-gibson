from __future__ import annotations

from harn_gibson.shell_commands import shell_command_has_in_place_edit, shell_command_path_candidates


def test_shell_command_path_candidates_skip_sed_and_perl_program_fragments() -> None:
    command = (
        "sed -i 's/return 2/return 0/' src/repo_map/cli.py && "
        "perl -pi -e 's/foo/bar/' tests/test_cli.py"
    )

    assert shell_command_path_candidates(command) == ("src/repo_map/cli.py", "tests/test_cli.py")
    assert shell_command_has_in_place_edit(command) is True


def test_shell_command_path_candidates_handle_script_options_and_env_assignments() -> None:
    sed_with_expression = "LC_ALL=C gsed --in-place=.bak -e 's/a/b/' -- src/app.py docs/guide.md"
    sed_with_inline_expression = "sed -i --expression=s/a/b/ docs/guide src/app.py"
    perl_with_inline_expression = "perl -i.bak -e's/a/b/' src/app.py"
    perl_with_short_expression = "perl -pe 's/a/b/' docs/guide src/app.py"

    assert shell_command_path_candidates(sed_with_expression) == ("src/app.py", "docs/guide.md")
    assert shell_command_path_candidates(sed_with_inline_expression) == ("src/app.py",)
    assert shell_command_path_candidates(perl_with_inline_expression) == ("src/app.py",)
    assert shell_command_path_candidates(perl_with_short_expression) == ("src/app.py",)
    assert shell_command_has_in_place_edit(sed_with_expression) is True
    assert shell_command_has_in_place_edit(perl_with_inline_expression) is True
    assert shell_command_has_in_place_edit("perl -pi -e 's/a/b/' src/app.py") is True


def test_shell_command_path_candidates_keep_generic_paths_and_fallback_on_bad_quotes() -> None:
    generic = "uv run pytest tests/test_rendering.py docs/renderer-agent.md"
    bad_quotes = "cat 'unterminated src/fallback.py"

    assert shell_command_path_candidates(generic) == ("tests/test_rendering.py", "docs/renderer-agent.md")
    assert shell_command_path_candidates(bad_quotes) == ("src/fallback.py",)
    assert shell_command_has_in_place_edit("python scripts/tool.py") is False
    assert shell_command_path_candidates("VAR=1") == ()
    assert shell_command_has_in_place_edit("VAR=1") is False


def test_shell_command_path_candidates_ignore_scripts_without_file_args_and_read_only_sed() -> None:
    assert shell_command_path_candidates("sed 's/a/b/'") == ()
    assert shell_command_has_in_place_edit("sed 's/a/b/' src/app.py") is False
    assert shell_command_path_candidates("sed 's/a/b/' src/app.py") == ("src/app.py",)
    assert shell_command_path_candidates("perl -ne 'print' -- docs/guide tests/test_app.py") == (
        "tests/test_app.py",
    )
    assert shell_command_path_candidates("echo s/return 2/return 0/") == ()
    assert shell_command_path_candidates("&& pytest tests/test_app.py &&") == ("tests/test_app.py",)
