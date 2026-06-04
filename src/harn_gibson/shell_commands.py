"""Small shell-command helpers for renderer perception."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence

SHELL_COMMAND_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:\.{0,2}/)?[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)+"
)

_SHELL_SEPARATORS = {"&", "&&", "|", "||", ";"}
_SED_SCRIPT_OPTIONS = {"-e", "--expression", "-f", "--file"}
_PERL_EXPRESSION_OPTIONS = {"-e", "-E"}


def shell_command_path_candidates(command: str) -> tuple[str, ...]:
    """Return slash-like file path tokens from a shell command, skipping sed/perl edit programs."""

    tokens = _shell_tokens(command)
    if not tokens:
        return _regex_path_candidates(command)
    candidates: list[str] = []
    for segment in _shell_segments(tokens):
        candidates.extend(_segment_path_candidates(segment))
    return tuple(candidates)


def shell_command_has_in_place_edit(command: str) -> bool:
    """Return whether a shell command contains a sed/perl in-place edit segment."""

    return any(_segment_has_in_place_edit(segment) for segment in _shell_segments(_shell_tokens(command)))


def _shell_tokens(command: str) -> tuple[str, ...]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        return tuple(lexer)
    except ValueError:
        return ()


def _regex_path_candidates(command: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in SHELL_COMMAND_PATH_PATTERN.finditer(command))


def _shell_segments(tokens: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                segments.append(tuple(current))
                current = []
            continue
        current.append(token)
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def _segment_path_candidates(tokens: Sequence[str]) -> tuple[str, ...]:
    command_index = _command_index(tokens)
    if command_index is None:
        return ()
    command_name = _command_name(tokens[command_index])
    command_tokens = tokens[command_index:]
    if command_name in {"sed", "gsed"}:
        return _sed_path_candidates(command_tokens)
    if command_name == "perl":
        return _perl_path_candidates(command_tokens)
    return tuple(token for token in command_tokens if _is_path_token(token))


def _segment_has_in_place_edit(tokens: Sequence[str]) -> bool:
    command_index = _command_index(tokens)
    if command_index is None:
        return False
    command_name = _command_name(tokens[command_index])
    command_tokens = tokens[command_index:]
    if command_name in {"sed", "gsed"}:
        return any(_is_sed_in_place_option(token) for token in command_tokens[1:])
    if command_name == "perl":
        return any(_is_perl_in_place_option(token) for token in command_tokens[1:])
    return False


def _command_index(tokens: Sequence[str]) -> int | None:
    for index, token in enumerate(tokens):
        if _is_env_assignment(token):
            continue
        return index
    return None


def _command_name(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _is_env_assignment(token: str) -> bool:
    if "=" not in token or token.startswith("="):
        return False
    name = token.split("=", 1)[0]
    return bool(name) and all(character == "_" or character.isalnum() for character in name)


def _sed_path_candidates(tokens: Sequence[str]) -> tuple[str, ...]:
    candidates: list[str] = []
    script_seen = False
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            script_seen = True
            continue
        if token == "--":
            continue
        if token in _SED_SCRIPT_OPTIONS:
            skip_next = True
            script_seen = True
            continue
        if token.startswith("--expression=") or token.startswith("--file="):
            script_seen = True
            continue
        if token.startswith("-"):
            continue
        if not script_seen:
            script_seen = True
            continue
        if _is_path_token(token):
            candidates.append(token)
    return tuple(candidates)


def _perl_path_candidates(tokens: Sequence[str]) -> tuple[str, ...]:
    candidates: list[str] = []
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            continue
        if token in _PERL_EXPRESSION_OPTIONS:
            skip_next = True
            continue
        if token.startswith("-e") or token.startswith("-E"):
            continue
        if token.startswith("-"):
            flags = token[1:]
            if not token.startswith("--") and ("e" in flags or "E" in flags):
                skip_next = True
            continue
        if _is_path_token(token):
            candidates.append(token)
    return tuple(candidates)


def _is_sed_in_place_option(token: str) -> bool:
    return token == "-i" or token.startswith("-i") or token.startswith("--in-place")


def _is_perl_in_place_option(token: str) -> bool:
    if token == "-i" or token.startswith("-i"):
        return True
    return token.startswith("-") and not token.startswith("--") and "i" in token[1:]


def _is_path_token(token: str) -> bool:
    if not SHELL_COMMAND_PATH_PATTERN.fullmatch(token):
        return False
    final_part = token.rsplit("/", 1)[-1]
    return "." in final_part or token.startswith(("./", "../"))


__all__ = [
    "SHELL_COMMAND_PATH_PATTERN",
    "shell_command_has_in_place_edit",
    "shell_command_path_candidates",
]
