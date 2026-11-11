"""Tiện ích xử lý tiếng Việt: bỏ dấu, tách từ, định dạng số kiểu Việt Nam."""
from __future__ import annotations

import math
import re
import unicodedata

_D_MAP = str.maketrans({"đ": "d", "Đ": "D"})


def strip_accents(s: str) -> str:
    s = s.translate(_D_MAP)
    s = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


def norm(s: str) -> str:
    """Chuẩn hóa để so khớp: thường, bỏ dấu, gom khoảng trắng."""
    s = strip_accents((s or "").lower())
    s = re.sub(r"[^a-z0-9%./\- ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", norm(s)) if t]


def bigrams(toks: list[str]) -> list[str]:
    return [f"{a}_{b}" for a, b in zip(toks, toks[1:])]


def contains_phrase(text_norm: str, phrase: str) -> bool:
    """So khớp cụm từ đã chuẩn hóa theo ranh giới từ."""
    p = norm(phrase)
    if not p:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", text_norm) is not None


def find_phrase(text_norm: str, phrase: str):
    p = norm(phrase)
    if not p:
        return None
    return re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", text_norm)


# ----------------------------------------------------------------------------- định dạng số
def fmt_int(x: float) -> str:
    return f"{int(round(x)):,}".replace(",", ".")


def fmt_dec(x: float, nd: int = 1) -> str:
    s = f"{x:,.{nd}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_money(x) -> str:
    """Định dạng tiền VND rút gọn: 1,23 tỷ ₫ / 456,7 triệu ₫ / 12.500 ₫."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    x = float(x)
    a = abs(x)
    if a >= 1e9:
        return f"{fmt_dec(x / 1e9, 2)} tỷ ₫"
    if a >= 1e6:
        return f"{fmt_dec(x / 1e6, 1)} triệu ₫"
    return f"{fmt_int(x)} ₫"


def fmt_number(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    x = float(x)
    if abs(x - round(x)) < 1e-9:
        return fmt_int(x)
    return fmt_dec(x, 2)


def fmt_pct(x, nd: int = 1, signed: bool = False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    s = fmt_dec(float(x), nd) + "%"
    if signed and x > 0:
        s = "+" + s
    return s


def fmt_value(x, fmt: str | None) -> str:
    if fmt == "currency":
        return fmt_money(x)
    if fmt == "percent":
        return fmt_pct(x)
    if fmt == "ratio":
        return fmt_dec(float(x), 2) if x is not None else "—"
    if isinstance(x, (int, float)):
        return fmt_number(x)
    return str(x)


def human_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"
