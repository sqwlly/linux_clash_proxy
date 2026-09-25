from __future__ import annotations

import unicodedata


def display_width(value: object) -> int:
    width = 0
    for char in str(value):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def pad_left(value: object, width: int) -> str:
    text = str(value)
    return " " * max(0, width - display_width(text)) + text


def pad_right(value: object, width: int) -> str:
    text = str(value)
    return text + " " * max(0, width - display_width(text))
