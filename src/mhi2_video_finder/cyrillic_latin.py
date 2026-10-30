"""Rewrite Serbian, Bulgarian, and Russian Cyrillic into ASCII Latin."""

from __future__ import annotations

# One table for all three alphabets. Letters that need more than one Latin
# character use the usual spellings with y: zh, ch, sh, yu, ya, and y.
# Serbian uses lj, nj, and dj. щ is sht and ъ is a, which matches Bulgarian;
# edit a Russian word in the review step if that reading is wrong.
_TO_LATIN: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "ј": "j",
    "к": "k",
    "л": "l",
    "љ": "lj",
    "м": "m",
    "н": "n",
    "њ": "nj",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "ђ": "dj",
    "ћ": "c",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "џ": "dz",
    "ш": "sh",
    "щ": "sht",
    "ъ": "a",
    "ы": "y",
    "ь": "y",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def _apply_case(latin: str, *, upper: bool, next_upper: bool) -> str:
    if not upper:
        return latin
    if next_upper:
        return latin.upper()
    return latin[:1].upper() + latin[1:]


def _next_cyrillic_is_upper(text: str, start: int) -> bool:
    for ch in text[start:]:
        if ch.lower() in _TO_LATIN:
            return ch.isupper()
    return False


def transliterate_cyrillic(text: str) -> str:
    """Replace Cyrillic letters. Latin letters, digits, and punctuation stay."""
    out: list[str] = []
    for index, ch in enumerate(text or ""):
        latin = _TO_LATIN.get(ch.lower())
        if latin is None:
            out.append(ch)
            continue
        out.append(
            _apply_case(
                latin,
                upper=ch.isupper(),
                next_upper=_next_cyrillic_is_upper(text, index + 1),
            )
        )
    return "".join(out)
