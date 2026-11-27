"""Small stdlib-only validation shared by the adapter and its owned worker."""
from __future__ import annotations

import os
import stat
from pathlib import Path

WIDTH, HEIGHT = 1280, 800
MAX_MESSAGE = 8 * 1024 * 1024
MAX_REQUEST = 16 * 1024
MAX_TEXT = 256
TYPE_CHUNK = 32
DEPENDENCIES = {"vncdotool": "1.4.2", "Twisted": "26.10.0", "Pillow": "13.0.0"}
MODIFIERS = {"ctrl", "alt", "shift", "super"}
ALIASES = {
    "control": "ctrl", "ctrl": "ctrl", "alt": "alt", "option": "alt",
    "shift": "shift", "super": "super", "cmd": "super", "command": "super",
    "win": "super", "windows": "super", "meta": "super",
    "return": "return", "enter": "return", "escape": "esc", "esc": "esc",
    "tab": "tab", "backspace": "bsp", "bsp": "bsp", "delete": "delete",
    "del": "delete", "insert": "ins", "ins": "ins", "home": "home", "end": "end",
    "pageup": "pgup", "pgup": "pgup", "pagedown": "pgdn", "pgdn": "pgdn",
    "left": "left", "right": "right", "up": "up", "down": "down",
    "space": "space", "plus": 43, "minus": 45,
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}
CANONICAL_KEYS = frozenset(v for v in ALIASES.values() if isinstance(v, str))
BUTTONS = {"left": 1, "middle": 2, "right": 4}
WHEELS = {"up": 8, "down": 16, "left": 32, "right": 64}
# Qualified server keymap is US. A mismatched keysym level makes WayVNC
# temporarily replace all held modifiers, including Ctrl/Alt/Super.
US_SHIFTED = dict(zip("`1234567890-=[]\\;',./", '~!@#$%^&*()_+{}|:"<>?'))


def private_endpoint(raw: str):
    from tools.bot_desktop.external import private_endpoint as canonical_private_endpoint
    return canonical_private_endpoint(raw)


def point(x, y):
    if type(x) is not int or type(y) is not int or not (0 <= x < WIDTH and 0 <= y < HEIGHT):
        raise ValueError("coordinates must be integers inside the captured 1280x800 output")
    return x, y


def modifiers(values):
    if values is None:
        return []
    if not isinstance(values, (list, tuple)) or len(values) > 4:
        raise ValueError("at most four modifiers are supported")
    result = []
    for value in values:
        key = ALIASES.get(value.lower()) if isinstance(value, str) else None
        if key not in MODIFIERS or key in result:
            raise ValueError("unsupported or duplicate modifier")
        result.append(key)
    return result


def combo(value):
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ValueError("expected a short Hermes '+' key combination")
    parts = value.split("+")
    if len(parts) > 5 or any(not p for p in parts):
        raise ValueError("invalid combo; use the named key 'plus' for literal '+'")
    result = modifiers(parts[:-1])
    last = parts[-1]
    key = ALIASES.get(last.lower())
    if key is None and len(last) == 1 and 32 <= ord(last) <= 126:
        key = ord(last)
    if key is None or key in result:
        raise ValueError("unsupported or duplicate key; Fn and Unicode are not supported")
    if result and type(key) is int:
        char = chr(key)
        if char.isalpha():
            char = char.upper() if "shift" in result else char.lower()
        elif "shift" in result:
            char = US_SHIFTED.get(char, char)
        if char in US_SHIFTED.values() and "shift" not in result:
            result.append("shift")
        key = ord(char)
    if "shift" in result and key == "tab":
        raise ValueError("Shift+Tab requires an unsupported ISO_Left_Tab keysym; nothing was sent")
    return [*result, key]


def text_symbols(value):
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        raise ValueError(f"text must contain 1..{MAX_TEXT} characters")
    result = []
    for char in value:
        if char == "\n":
            result.append("return")
        elif char == "\t":
            result.append("tab")
        elif 32 <= ord(char) <= 126:
            result.append(ord(char))
        else:
            raise ValueError("text scope is printable ASCII plus newline and tab; nothing was sent")
    return result
