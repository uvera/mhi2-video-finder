"""Tests for interactive pick parsing helpers."""

from mhi2_video_finder.workflow import infer_subdir_name, parse_multi_pick, slug_dir_name


def test_parse_multi_pick_single() -> None:
    assert parse_multi_pick("1", 5) == [0]
    assert parse_multi_pick("3", 5) == [2]


def test_parse_multi_pick_list_and_range() -> None:
    assert parse_multi_pick("1,3", 5) == [0, 2]
    assert parse_multi_pick("2-4", 5) == [1, 2, 3]
    assert parse_multi_pick("1, 2-3", 5) == [0, 1, 2]


def test_parse_multi_pick_reverse_range() -> None:
    assert parse_multi_pick("4-2", 5) == [1, 2, 3]


def test_parse_multi_pick_all() -> None:
    assert parse_multi_pick("all", 3) == [0, 1, 2]


def test_parse_multi_pick_empty_and_out_of_range() -> None:
    assert parse_multi_pick("", 3) == []
    assert parse_multi_pick("0", 3) == []
    assert parse_multi_pick("9,1", 3) == [0]


def test_slug_dir_name() -> None:
    assert "daft" in slug_dir_name("Daft Punk Around", "x").lower()
    assert slug_dir_name("   ", "fallback") == "fallback"


def test_infer_subdir_name_prefers_artist() -> None:
    a = infer_subdir_name(artist="Daft Punk", channel="Vevo")
    b = infer_subdir_name(artist=None, channel="Some Channel")
    assert "daft" in a.lower()
    assert "some" in b.lower()


def test_infer_subdir_name_fallback() -> None:
    assert infer_subdir_name(artist=None, channel=None, fallback="z") == "z"
