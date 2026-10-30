"""Setup page for the Library assisted-guess dialog."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from mhi2_video_finder.config import Settings
from mhi2_video_finder.groq_infer import DEFAULT_SYSTEM_PROMPT
from mhi2_video_finder.local_library import LibraryFileRow
from mhi2_video_finder.ui.assisted_guess import AssistedGuessDialog


@pytest.fixture
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_assisted_guess_setup_defaults(qapp: QApplication) -> None:
    host = SimpleNamespace(_settings=Settings())
    dlg = AssistedGuessDialog(host, [])
    assert dlg.template_edit.text() == "{song} - {artist}"
    assert dlg.prompt_edit.toPlainText() == DEFAULT_SYSTEM_PROMPT
    assert 'Example: "Song name - Artist.mp4"' in dlg.example_label.text()
    dlg.template_edit.setText("   ")
    assert not dlg.start_btn.isEnabled()
    dlg.template_edit.setText("{song}")
    assert dlg.start_btn.isEnabled()
    dlg.prompt_edit.setPlainText("custom")
    dlg.reset_prompt_btn.click()
    assert dlg.prompt_edit.toPlainText() == DEFAULT_SYSTEM_PROMPT
    dlg.close()


def test_assisted_guess_prefills_saved_prompt_and_template(qapp: QApplication) -> None:
    host = SimpleNamespace(
        _settings=Settings(
            library_assisted_prompt="Use only the chorus.\n",
            library_assisted_rename_template="{artist}_{song}",
        )
    )
    dlg = AssistedGuessDialog(host, [])
    assert dlg.prompt_edit.toPlainText() == "Use only the chorus.\n"
    assert dlg.template_edit.text() == "{artist}_{song}"
    dlg.close()


def test_legacy_saved_prompt_opens_as_current_default(qapp: QApplication) -> None:
    host = SimpleNamespace(
        _settings=Settings(
            library_assisted_prompt=(
                "Do not invent, correct, translate, or expand words beyond the filename tokens.\n"
                "Perfer taking cylirics if song has both fonetics in name."
            )
        )
    )
    dlg = AssistedGuessDialog(host, [])
    assert dlg.prompt_edit.toPlainText() == DEFAULT_SYSTEM_PROMPT
    dlg.close()


def test_guess_failure_leaves_fields_empty(qapp: QApplication) -> None:
    row = LibraryFileRow(
        path=Path("IVANA - ALO_ DEVOYKITE.mp4"),
        author="PlanetaOfficial",
        song_name="IVANA - ALO, DEVOYKITE / Ивана - Ало, девойките, 2011",
    )
    host = SimpleNamespace(
        _settings=Settings(),
        _library_rows=[row],
        _library_set_guess_status=lambda *_args, **_kwargs: None,
    )
    dlg = AssistedGuessDialog(host, [0])
    dlg._on_guess_fail("HTTP 404: model not found", dlg._guess_gen)
    assert dlg.artist_edit.text() == ""
    assert dlg.song_edit.text() == ""
    assert "404" in dlg.status_label.text()
    dlg.close()


def test_assisted_guess_commit_setup_saves_blank_when_prompt_is_default(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[Settings] = []
    monkeypatch.setattr(
        "mhi2_video_finder.ui.assisted_guess.save_settings",
        lambda settings, path=None: saved.append(settings),
    )
    host = SimpleNamespace(_settings=Settings(library_assisted_prompt="old"))
    dlg = AssistedGuessDialog(host, [])
    dlg.prompt_edit.setPlainText(DEFAULT_SYSTEM_PROMPT)
    dlg.template_edit.setText("  {artist} - {song}  ")
    assert dlg._commit_setup() is True
    assert host._settings.library_assisted_prompt == ""
    assert host._settings.library_assisted_rename_template == "{artist} - {song}"
    assert dlg.system_prompt is None
    assert saved == [host._settings]
    dlg.close()
