"""Library walk-through: custom prompt, then accept or skip each guessed file."""

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
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from mhi2_video_finder.config import save_settings
from mhi2_video_finder.groq_infer import DEFAULT_SYSTEM_PROMPT, LEGACY_SYSTEM_PROMPT_MARKER
from mhi2_video_finder.local_library import LibraryFileRow
from mhi2_video_finder.rename_template import render_filename_stem

from .styles import HINT_LABEL_STYLE
from .workers import GroqInferWorker, LibrarySaveWorker

_DEFAULT_TEMPLATE = "{song} - {artist}"


def _folder_hint(host: Any, path: Path) -> str:
    root = getattr(host, "_library_root", None)
    try:
        if root is not None:
            return str(path.parent.resolve().relative_to(Path(root).resolve()))
        return path.parent.name
    except ValueError:
        return path.parent.name


class AssistedGuessDialog(QDialog):
    """Set a prompt and filename template, then review each checked library row."""

    def __init__(self, host: Any, row_indices: list[int], parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Assisted guess")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._host = host
        self._row_indices = list(row_indices)
        self._pos = 0
        self._saved = 0
        self._skipped = 0
        self._started = False
        self._closing = False
        self._template = _DEFAULT_TEMPLATE
        self.system_prompt: str | None = None
        self._guess_gen = 0
        self._infer_worker: GroqInferWorker | None = None
        self._save_worker: LibrarySaveWorker | None = None

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_setup_page())
        self._stack.addWidget(self._build_review_page())
        layout = QVBoxLayout(self)
        layout.addWidget(self._stack)
        self._load_saved_setup()
        self._on_template_changed()

    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Prompt"))
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setMinimumHeight(160)
        layout.addWidget(self.prompt_edit)
        self.reset_prompt_btn = QPushButton("Reset prompt")
        self.reset_prompt_btn.clicked.connect(self._reset_prompt)
        layout.addWidget(self.reset_prompt_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        layout.addWidget(QLabel("Filename template"))
        self.template_edit = QLineEdit()
        self.template_edit.setPlaceholderText(_DEFAULT_TEMPLATE)
        self.template_edit.textChanged.connect(self._on_template_changed)
        layout.addWidget(self.template_edit)
        self.example_label = QLabel()
        self.example_label.setWordWrap(True)
        self.example_label.setStyleSheet(HINT_LABEL_STYLE)
        layout.addWidget(self.example_label)

        note = QLabel(
            "The extension is kept automatically. Only {song} and {artist} are placeholders. "
            "The model must reply with JSON keys author and song_name "
            "(the default prompt already says this). "
            "Words that are not already in the filename are dropped. The file name, folder, and media "
            "summary are sent automatically and are not part of the prompt."
        )
        note.setWordWrap(True)
        note.setStyleSheet(HINT_LABEL_STYLE)
        layout.addWidget(note)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        self.start_btn = QPushButton("Start")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._on_start)
        buttons.addWidget(self.start_btn)
        layout.addLayout(buttons)
        return page

    def _build_review_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.progress_label = QLabel()
        layout.addWidget(self.progress_label)
        self.file_label = QLabel()
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.file_label)

        layout.addWidget(QLabel("Artist"))
        self.artist_edit = QLineEdit()
        self.artist_edit.textChanged.connect(self._refresh_preview)
        layout.addWidget(self.artist_edit)
        layout.addWidget(QLabel("Song"))
        self.song_edit = QLineEdit()
        self.song_edit.textChanged.connect(self._refresh_preview)
        layout.addWidget(self.song_edit)
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
        self.accept_btn.clicked.connect(self._accept_current)
        buttons.addWidget(self.accept_btn)
        layout.addLayout(buttons)
        return page

    def _load_saved_setup(self) -> None:
        settings = self._host._settings
        saved_prompt = settings.library_assisted_prompt or ""
        if not saved_prompt.strip() or LEGACY_SYSTEM_PROMPT_MARKER in saved_prompt:
            self.prompt_edit.setPlainText(DEFAULT_SYSTEM_PROMPT)
        else:
            self.prompt_edit.setPlainText(saved_prompt)
        saved_template = (settings.library_assisted_rename_template or "").strip()
        self.template_edit.setText(saved_template or _DEFAULT_TEMPLATE)

    def _reset_prompt(self) -> None:
        self.prompt_edit.setPlainText(DEFAULT_SYSTEM_PROMPT)

    def _on_template_changed(self, _text: str = "") -> None:
        template = self.template_edit.text().strip()
        self.start_btn.setEnabled(bool(template))
        self._update_example(template or _DEFAULT_TEMPLATE)

    def _update_example(self, template: str) -> None:
        stem = render_filename_stem(
            template,
            song="Song name",
            artist="Artist",
            fallback="example",
        )
        self.example_label.setText(f'Example: "{stem}.mp4"')

    def _commit_setup(self) -> bool:
        template = self.template_edit.text().strip()
        if not template:
            return False
        raw_prompt = self.prompt_edit.toPlainText()
        settings = self._host._settings
        if not raw_prompt.strip() or raw_prompt.strip() == DEFAULT_SYSTEM_PROMPT.strip():
            settings.library_assisted_prompt = ""
            self.system_prompt = None
        else:
            settings.library_assisted_prompt = raw_prompt
            self.system_prompt = raw_prompt
        settings.library_assisted_rename_template = template
        self._template = template
        save_settings(settings)
        return True

    def _on_start(self) -> None:
        if not self._commit_setup():
            return
        self._started = True
        self.start_btn.setDefault(False)
        self.accept_btn.setDefault(True)
        self._stack.setCurrentIndex(1)
        self._show_current()

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
        self.preview_label.setText(f"Proposed name: {row.path.name}")
        self._begin_guess(row_idx, row)

    def _begin_guess(self, row_idx: int, row: LibraryFileRow) -> None:
        self._guess_gen += 1
        gen = self._guess_gen
        self.artist_edit.clear()
        self.song_edit.clear()
        self.status_label.setText("Guessing…")
        self._set_review_busy(True)
        self._host._library_set_guess_status(row_idx, "Guessing")
        worker = GroqInferWorker(
            self._host._settings,
            row_idx=row_idx,
            filename=row.path.name,
            folder_hint=_folder_hint(self._host, row.path),
            path=row.path,
            probe_summary=row.probe_summary or "",
            skip_if_existing_tags=False,
            system_prompt=self.system_prompt,
            parent=self._host,
        )
        worker.probe_cached.connect(self._on_probe_cached)
        worker.finished_ok.connect(lambda author, song, g=gen: self._on_guess_ok(author, song, g))
        worker.failed.connect(lambda err, g=gen: self._on_guess_fail(err, g))
        worker.finished.connect(worker.deleteLater)
        self._infer_worker = worker
        worker.start()

    def _on_probe_cached(self, row_idx: int, summary: str) -> None:
        rows = self._host._library_rows
        if 0 <= row_idx < len(rows):
            rows[row_idx].probe_summary = summary

    def _on_guess_ok(self, author: str, song: str, gen: int) -> None:
        if self._closing or gen != self._guess_gen:
            return
        self._infer_worker = None
        self.artist_edit.setText(author)
        self.song_edit.setText(song)
        self.status_label.setText("Ready")
        self._host._library_set_guess_status(self._row_indices[self._pos], "Ready")
        self._set_review_busy(False)
        self._refresh_preview()

    def _on_guess_fail(self, err: str, gen: int) -> None:
        if self._closing or gen != self._guess_gen:
            return
        self._infer_worker = None
        self.artist_edit.clear()
        self.song_edit.clear()
        message = err.strip() or "Could not reach the AI."
        self.status_label.setText(message)
        self._host._library_set_guess_status(
            self._row_indices[self._pos],
            "Failed",
            tooltip=message,
        )
        self._set_review_busy(False)
        self._refresh_preview()

    def _refresh_preview(self, *_args: object) -> None:
        if self._stack.currentIndex() != 1 or self._pos >= len(self._row_indices):
            return
        row_idx = self._row_indices[self._pos]
        rows = self._host._library_rows
        if row_idx < 0 or row_idx >= len(rows):
            return
        row = rows[row_idx]
        stem = render_filename_stem(
            self._template,
            song=self.song_edit.text(),
            artist=self.artist_edit.text(),
            fallback=row.path.stem or "video",
        )
        self.preview_label.setText(f"Proposed name: {stem}{row.path.suffix}")

    def _set_review_busy(self, busy: bool, *, saving: bool = False) -> None:
        self.accept_btn.setEnabled(not busy)
        self.skip_btn.setEnabled(not busy)
        self.artist_edit.setEnabled(not busy)
        self.song_edit.setEnabled(not busy)
        self.stop_btn.setEnabled(not saving)

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
        stem = render_filename_stem(
            self._template,
            song=song,
            artist=author,
            fallback=row.path.stem or "video",
        )
        row.author = author
        row.song_name = song
        row.filename_stem = stem
        self._sync_selected_editor(row_idx, author, song, stem)
        self.status_label.setText("Saving…")
        self._set_review_busy(True, saving=True)
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
        self._set_review_busy(False)

    def _skip_current(self) -> None:
        if self._pos >= len(self._row_indices):
            return
        self._host._library_set_guess_status(self._row_indices[self._pos], "Skipped")
        self._skipped += 1
        self._advance()

    def _advance(self) -> None:
        self._pos += 1
        self._show_current()

    def _stop_infer_worker(self) -> None:
        self._guess_gen += 1
        worker = self._infer_worker
        self._infer_worker = None
        if worker is None:
            return
        for name in ("finished_ok", "failed", "probe_cached"):
            signal = getattr(worker, name, None)
            if signal is None:
                continue
            try:
                signal.disconnect()
            except TypeError:
                pass
        if worker.isRunning():
            worker.requestInterruption()
            worker.wait(3000)

    def _report(self) -> None:
        self._host._show_status_message(
            f"Assisted guessing complete ({self._saved} saved, {self._skipped} skipped).",
            kind="success",
        )
        status = getattr(self._host, "_status", None)
        if status is not None:
            status.setText("")

    def _finish(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_infer_worker()
        if self._started:
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
            self._stop_infer_worker()
            if self._started:
                self._report()
        event.accept()
