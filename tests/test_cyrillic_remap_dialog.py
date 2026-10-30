"""Review dialog for Cyrillic-to-Latin library remapping."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from mhi2_video_finder.config import Settings
from mhi2_video_finder.local_library import LibraryFileRow
from mhi2_video_finder.ui.cyrillic_remap import CyrillicRemapDialog


@pytest.fixture
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_cyrillic_remap_prefills_latin(qapp: QApplication) -> None:
    row = LibraryFileRow(path=Path("Юлия - моя.mp4"), author="Юлия", song_name="моя")
    host = SimpleNamespace(
        _settings=Settings(),
        _library_rows=[row],
        _library_set_guess_status=lambda *_args, **_kwargs: None,
        _show_status_message=lambda *_args, **_kwargs: None,
        _status=SimpleNamespace(setText=lambda _text: None),
    )
    dlg = CyrillicRemapDialog(host, [0])
    assert dlg.artist_edit.text() == "Yuliya"
    assert dlg.song_edit.text() == "moya"
    assert dlg.stem_edit.text() == "Yuliya - moya"
    assert dlg.preview_label.text() == "Proposed name: Yuliya - moya.mp4"
    dlg.close()
