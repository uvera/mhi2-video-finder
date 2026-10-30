"""Cyrillic to Latin remapping for library filenames and tags."""

from __future__ import annotations

from mhi2_video_finder.cyrillic_latin import transliterate_cyrillic


def test_serbian_digraphs_and_case() -> None:
    assert transliterate_cyrillic("Љубав") == "Ljubav"
    assert transliterate_cyrillic("Његош") == "Njegosh"
    assert transliterate_cyrillic("ЂОРЂЕ") == "DJORDJE"
    assert transliterate_cyrillic("џез") == "dzez"
    assert transliterate_cyrillic("ћ") == "c"


def test_bulgarian_and_russian_y_spellings() -> None:
    assert transliterate_cyrillic("Юлия") == "Yuliya"
    assert transliterate_cyrillic("моя") == "moya"
    assert transliterate_cyrillic("Щъркел") == "Shtarkel"
    assert transliterate_cyrillic("ёлка") == "yolka"
    assert transliterate_cyrillic("мы") == "my"
    assert transliterate_cyrillic("день") == "deny"


def test_latin_digits_and_punctuation_stay() -> None:
    assert transliterate_cyrillic("Ivana - Alo, 2011") == "Ivana - Alo, 2011"
    assert transliterate_cyrillic("Ало, мой") == "Alo, moy"
