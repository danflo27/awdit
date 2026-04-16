from __future__ import annotations

import argparse
import builtins
import re
import sys
from dataclasses import dataclass
from typing import TextIO


@dataclass
class _LayoutState:
    seen_content: bool = False
    blank_line_open: bool = False


_STREAM_STATES: dict[int, tuple[TextIO, _LayoutState]] = {}


def _resolve_stream(stream: TextIO | None) -> TextIO:
    return sys.stdout if stream is None else stream


def _layout_state(stream: TextIO | None) -> tuple[TextIO, _LayoutState]:
    resolved = _resolve_stream(stream)
    key = id(resolved)
    current = _STREAM_STATES.get(key)
    if current is None or current[0] is not resolved:
        state = _LayoutState()
        _STREAM_STATES[key] = (resolved, state)
        return resolved, state
    return resolved, current[1]


def print_line(line: str = "", *, stream: TextIO | None = None, flush: bool = False) -> None:
    resolved, state = _layout_state(stream)
    if not line:
        if state.seen_content and not state.blank_line_open:
            builtins.print("", file=resolved, flush=flush)
            state.blank_line_open = True
        return
    builtins.print(line, file=resolved, flush=flush)
    state.seen_content = True
    state.blank_line_open = False


def print_lines(lines: list[str] | tuple[str, ...], *, stream: TextIO | None = None, flush: bool = False) -> None:
    for line in lines:
        print_line(line, stream=stream, flush=flush)


def write_fragment(text: str, *, stream: TextIO | None = None, flush: bool = False) -> None:
    if not text:
        return
    resolved, state = _layout_state(stream)
    resolved.write(text)
    if flush:
        resolved.flush()
    if any(not chunk.isspace() for chunk in text.splitlines() or [text]):
        state.seen_content = True
    if text.endswith("\n\n"):
        state.blank_line_open = True
    elif text.endswith("\n"):
        state.blank_line_open = False
    else:
        state.blank_line_open = False


def print_section(title: str, *, stream: TextIO | None = None, flush: bool = False) -> None:
    print_line("", stream=stream, flush=flush)
    print_line(title, stream=stream, flush=flush)


def prompt_input(prompt: str, *, separated: bool = False) -> str:
    if separated:
        print_line("")
    return builtins.input(prompt)


class ModerateSpacingHelpFormatter(argparse.HelpFormatter):
    _SECTION_HEADERS = frozenset(
        {
            "positional arguments:",
            "options:",
            "optional arguments:",
            "commands:",
            "subcommands:",
        }
    )

    def format_help(self) -> str:
        formatted = super().format_help().strip("\n")
        if not formatted:
            return "\n"
        lines = formatted.splitlines()
        normalized: list[str] = []
        for line in lines:
            if line in self._SECTION_HEADERS and normalized and normalized[-1] != "":
                normalized.append("")
            normalized.append(line)
        text = "\n".join(normalized)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.rstrip() + "\n"


class ModerateSpacingArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("formatter_class", ModerateSpacingHelpFormatter)
        super().__init__(*args, **kwargs)
