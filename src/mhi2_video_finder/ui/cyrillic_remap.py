"""Library walk-through: rewrite Cyrillic artist, song, and filename into Latin."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mhi2_video_finder.cyrillic_latin import transliterate_cyrillic

from .styles import HINT_LABEL_STYLE
from .workers import LibrarySaveWorker


class CyrillicRemapDialog(QDialog):
    """Review each checked file before Cyrillic tags and the filename are rewritten."""

    def __init__(self, host: Any, row_indices: list[int], parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remap Cyrillic")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._host = host
        self._row_indices = list(row_indices)
        self._pos = 0
        self._saved = 0
        self._skipped = 0
        self._closing = False
        self._save_worker: LibrarySaveWorker | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_review())
        self._show_current()

    def _build_review(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Cyrillic letters are rewritten to Latin (zh, ch, sh, sht, yu, ya, y; "
            "Serbian lj, nj, dj, dz). Latin letters stay as they are. "
            "Edit artist, song, or the file name, then accept or skip."
        )
        note.setWordWrap(True)
        note.setStyleSheet(HINT_LABEL_STYLE)
        layout.addWidget(note)

        self.progress_label = QLabel()
        layout.addWidget(self.progress_label)
        self.file_label = QLabel()
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.file_label)
        self.was_label = QLabel()
        self.was_label.setWordWrap(True)
        self.was_label.setStyleSheet(HINT_LABEL_STYLE)
        layout.addWidget(self.was_label)

        layout.addWidget(QLabel("Artist"))
        self.artist_edit = QLineEdit()
        layout.addWidget(self.artist_edit)
        layout.addWidget(QLabel("Song"))
        self.song_edit = QLineEdit()
        layout.addWidget(self.song_edit)
        layout.addWidget(QLabel("Rename file to"))
        self.stem_edit = QLineEdit()
        self.stem_edit.textChanged.connect(self._refresh_preview)
        layout.addWidget(self.stem_edit)
        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(HINT_LABEL_STYLE)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._finish)
        buttons.addWidget(self.stop_btn)
        self.skip_btn = QPushButton("Skip")
        self.skip_btn.clicked.connect(self._skip_current)
        buttons.addWidget(self.skip_btn)
        self.accept_btn = QPushButton("Accept")
        self.accept_btn.setDefault(True)
        self.accept_btn.clicked.connect(self._accept_current)
        buttons.addWidget(self.accept_btn)
        layout.addLayout(buttons)
        return page

    def _show_current(self) -> None:
        if self._closing:
            return
        if self._pos >= len(self._row_indices):
            self._finish()
            return
        row_idx = self._row_indices[self._pos]
        rows = self._host._library_rows
        if row_idx < 0 or row_idx >= len(rows):
            self._pos += 1
            self._show_current()
            return
        row = rows[row_idx]
        self.progress_label.setText(f"{self._pos + 1} of {len(self._row_indices)}")
        self.file_label.setText(row.path.name)
        self.file_label.setToolTip(str(row.path))
        was_artist = row.author or "—"
        was_song = row.song_name or "—"
        self.was_label.setText(f"Was: {was_artist} — {was_song}")
        self.artist_edit.setText(transliterate_cyrillic(row.author))
        self.song_edit.setText(transliterate_cyrillic(row.song_name))
        stem = transliterate_cyrillic(row.path.stem).strip() or row.path.stem
        self.stem_edit.setText(stem)
        self.status_label.setText("Ready")
        self._set_busy(False)
        self._refresh_preview()

    def _refresh_preview(self, *_args: object) -> None:
        if self._pos >= len(self._row_indices):
            return
        row_idx = self._row_indices[self._pos]
        rows = self._host._library_rows
        if row_idx < 0 or row_idx >= len(rows):
            return
        suffix = rows[row_idx].path.suffix
        stem = self.stem_edit.text().strip() or rows[row_idx].path.stem
        self.preview_label.setText(f"Proposed name: {stem}{suffix}")

    def _set_busy(self, busy: bool) -> None:
        self.accept_btn.setEnabled(not busy)
        self.skip_btn.setEnabled(not busy)
        self.artist_edit.setEnabled(not busy)
        self.song_edit.setEnabled(not busy)
        self.stem_edit.setEnabled(not busy)
        self.stop_btn.setEnabled(not busy)

    def _accept_current(self) -> None:
        if self._pos >= len(self._row_indices):
            return
        row_idx = self._row_indices[self._pos]
        row = self._host._library_rows[row_idx]
        if not row.path.is_file():
            self.status_label.setText("That file isn’t there anymore — try scanning the folder again.")
            return
        author = self.artist_edit.text().strip()
        song = self.song_edit.text().strip()
        stem = self.stem_edit.text().strip() or row.path.stem or "video"
        row.author = author
        row.song_name = song
        row.filename_stem = stem
        self._sync_selected_editor(row_idx, author, song, stem)
        self.status_label.setText("Saving…")
        self._set_busy(True)
        self._host._library_set_guess_status(
            row_idx,
            "Saving",
            tooltip="Writing tags to the file (stream copy — no re-encode).",
        )
        worker = LibrarySaveWorker(
            row_idx,
            self._host._settings,
            row.path,
            author,
            song,
            stem,
            parent=self._host,
        )
        worker.finished_ok.connect(self._on_save_ok)
        worker.failed.connect(self._on_save_fail)
        worker.finished.connect(worker.deleteLater)
        self._save_worker = worker
        worker.start()

    def _sync_selected_editor(self, row_idx: int, author: str, song: str, stem: str) -> None:
        table = getattr(self._host, "library_table", None)
        if table is None or table.currentRow() != row_idx:
            return
        self._host._library_block_edit_signals = True
        try:
            self._host.ui_lib_author.setText(author)
            self._host.ui_lib_song.setText(song)
            self._host.ui_lib_stem.setText(stem)
        finally:
            self._host._library_block_edit_signals = False

    def _on_save_ok(self, row_idx: int, new_path_obj: object) -> None:
        if self._closing:
            return
        self._save_worker = None
        new_path = new_path_obj if isinstance(new_path_obj, Path) else Path(str(new_path_obj))
        self._host._library_apply_saved_path_to_ui(row_idx, new_path)
        self._host._library_set_guess_status(row_idx, "Saved")
        self._saved += 1
        self._advance()

    def _on_save_fail(self, row_idx: int, err: str) -> None:
        if self._closing:
            return
        self._save_worker = None
        message = err.strip() or "Could not save the file."
        self.status_label.setText(message)
        self._host._library_set_guess_status(row_idx, "Save failed", tooltip=message)
        self._set_busy(False)

    def _skip_current(self) -> None:
        if self._pos >= len(self._row_indices):
            return
        self._host._library_set_guess_status(self._row_indices[self._pos], "Skipped")
        self._skipped += 1
        self._advance()

    def _advance(self) -> None:
        self._pos += 1
        self._show_current()

    def _report(self) -> None:
        self._host._show_status_message(
            f"Cyrillic remap complete ({self._saved} saved, {self._skipped} skipped).",
            kind="success",
        )
        status = getattr(self._host, "_status", None)
        if status is not None:
            status.setText("")

    def _finish(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._report()
        self.accept()

    def closeEvent(self, event: QCloseEvent | None) -> None:
        if event is None:
            return
        if self._save_worker is not None and self._save_worker.isRunning():
            event.ignore()
            return
        if not self._closing:
            self._closing = True
            self._report()
        event.accept()
