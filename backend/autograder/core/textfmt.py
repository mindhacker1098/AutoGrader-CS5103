"""Small text and number helpers used for grades, feedback and cache keys.

They fix one exact way to round, print numbers and write JSON, so the same
submission always produces byte-for-byte the same report and cache key.
"""
from __future__ import annotations

import math
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

MASK32 = 0xFFFFFFFF

# whitespace characters trimmed from program output (wider than ASCII spaces)
WS_CHARS = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
WS_RE = re.compile(f"[{WS_CHARS}]+")


def trim_ws(s: str) -> str:
    return re.sub(f"^[{WS_CHARS}]+|[{WS_CHARS}]+$", "", s)


def round_half_up(x: float) -> float:
    """Math.round: nearest integer, ties go towards +infinity."""
    if math.isnan(x) or math.isinf(x):
        return x
    f = math.floor(x)
    return float(f + 1) if x - f >= 0.5 else float(f)


def round1(x: float) -> float:
    return round_half_up(x * 10) / 10


def round3(x: float) -> float:
    return round_half_up(x * 1000) / 1000


def to_fixed(x: float, digits: int = 0) -> str:
    """Number.prototype.toFixed (half up on the exact binary value)."""
    if math.isnan(x):
        return "NaN"
    if abs(x) >= 1e21 or math.isinf(x):
        return number_text(x)
    if x == 0:
        x = 0.0
    q = Decimal(1).scaleb(-digits)
    d = Decimal(x).quantize(q, rounding=ROUND_HALF_UP)
    return f"{d:f}"


def number_text(x: Any) -> str:
    """Print a number the short way: 99 not 99.0, 1e-7, 1e+21 ..."""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        if abs(x) < 2**53:
            return str(x)
        x = float(x)
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    # repr gives the shortest digits that read back to the same number
    _, digits_t, exp = Decimal(repr(abs(x))).as_tuple()
    digits = "".join(map(str, digits_t)).rstrip("0") or "0"
    stripped = len("".join(map(str, digits_t))) - len(digits)
    exp += stripped
    k = len(digits)
    n = exp + k
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        es = f"e+{e}" if e >= 0 else f"e-{-e}"
        out = digits + es if k == 1 else digits[0] + "." + digits[1:] + es
    return sign + out


_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_STR_RE = re.compile('["\\\\\x00-\x1f\ud800-\udfff]')


def _quote(s: str) -> str:
    def rep(m: re.Match) -> str:
        c = m.group(0)
        return _ESC.get(c) or f"\\u{ord(c):04x}"
    return '"' + _STR_RE.sub(rep, s) + '"'


def dump_json(value: Any, indent: int | None = None) -> str:
    """JSON.stringify(value) or JSON.stringify(value, null, indent)."""
    def enc(v: Any, pad: str) -> str:
        if v is None:
            return "null"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, str):
            return _quote(v)
        if isinstance(v, (int, float)):
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return "null"
            return number_text(v)
        inner = pad + " " * indent if indent else ""
        if isinstance(v, dict):
            if not v:
                return "{}"
            items = [_quote(str(k)) + (": " if indent else ":") + enc(x, inner) for k, x in v.items()]
            return _wrap("{", "}", items, inner, pad, indent)
        if isinstance(v, (list, tuple)):
            if not v:
                return "[]"
            items = [enc(x, inner) for x in v]
            return _wrap("[", "]", items, inner, pad, indent)
        if hasattr(v, "item"):
            return enc(v.item(), pad)
        raise TypeError(f"cannot serialise {type(v).__name__}")
    return enc(value, "")


def _wrap(open_: str, close: str, items: list[str], inner: str, pad: str, indent: int | None) -> str:
    if not indent:
        return open_ + ",".join(items) + close
    return open_ + "\n" + inner + (",\n" + inner).join(items) + "\n" + pad + close


def u32(x: float) -> int:
    """ToUint32, i.e. x >>> 0."""
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return 0
        x = math.trunc(x)
    return x & MASK32


def i32(x: float) -> int:
    """ToInt32, i.e. x | 0."""
    v = u32(x)
    return v - 0x100000000 if v & 0x80000000 else v


def imul(a: float, b: float) -> int:
    """Math.imul: signed 32-bit product."""
    return i32((u32(a) * u32(b)) & MASK32)


# sort order for printable ASCII used to sort file names (see golden/textfmt.json)
_ICU_ASCII = " _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$0123456789abcdefghijklmnopqrstuvwxyz"
_PRIMARY = {c: i for i, c in enumerate(_ICU_ASCII)}


def locale_key(s: str) -> tuple:
    """Sort key approximating String.prototype.localeCompare (ICU root locale).

    Primary: base letters ignoring case and accents, punctuation before digits
    before letters. Then accents, then case (lowercase first).
    """
    prim: list[int] = []
    sec: list[int] = []
    ter: list[int] = []
    for ch in s:
        base = unicodedata.normalize("NFD", ch)
        b = base[0]
        accent = sum(ord(m) for m in base[1:])
        low = b.lower()
        if low in _PRIMARY:
            p = _PRIMARY[low]
        elif unicodedata.category(b).startswith("C"):
            continue
        else:
            p = 1000 + ord(low)
        prim.append(p)
        sec.append(accent)
        ter.append(1 if b != low else 0)
    return (tuple(prim), tuple(sec), tuple(ter), s)


def text_sort_key(s: str) -> bytes:
    """Key for the default Array.prototype.sort on strings (UTF-16 code units)."""
    return s.encode("utf-16-be", "surrogatepass")
