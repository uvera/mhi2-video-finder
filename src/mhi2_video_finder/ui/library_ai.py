"""Library tab mixin: filesystem scan, AI metadata tagging, and bulk file operations."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QTableWidgetItem

from mhi2_video_finder.config import save_settings
from mhi2_video_finder.local_library import LibraryFileRow

from .tab_builders import (
    _LIB_COL_ARTIST,
    _LIB_COL_CHECK,
    _LIB_COL_FILE,
    _LIB_COL_GUESS_STATUS,
    _LIB_COL_SONG,
)
from .workers import (
    GroqInferWorker,
    LibraryBulkClearMetadataWorker,
    LibraryBulkRenameToSongWorker,
    LibraryProbeWorker,
    LibrarySaveWorker,
    LibraryScanWorker,
)

# Table rows inserted per event-loop slice so Find-all-videos stays responsive.
_LIBRARY_TABLE_FILL_BATCH_ROWS = 100
# Concurrent Groq requests while earlier rows may still be saving (ffmpeg).
_MAX_PARALLEL_LIBRARY_GROQ = 3
# Concurrent ffprobe reads while loading Library artist/title columns.
_MAX_PARALLEL_LIBRARY_PREFETCH_PROBES = 4


class LibraryAIMixin:
    """Library tab: filesystem scan, AI metadata tagging, and bulk file operations."""

    def _library_scan(self) -> None:
        raw = self.ui_library_folder.text().strip()
        if not raw:
            self._show_status_message("Choose or paste a folder above first.", kind="info")
            return
        root = Path(raw).expanduser().resolve()
        if not root.is_dir():
            self._show_status_message(
                "That path isn’t a folder — check the path and try again.",
                kind="error",
            )
            return
        if self._library_scan_worker is not None and self._library_scan_worker.isRunning():
            self._show_status_message("Already scanning — please wait.", kind="info")
            return
        self._library_cancel_prefetch_probes()
        if self._library_probe_worker is not None:
            self._library_probe_worker.requestInterruption()
            self._library_probe_worker.wait(3000)
            self._library_probe_worker = None
        self._sync_widgets_to_settings()
        self._settings.library_last_folder = root
        self._library_root = root
        self._status.setText(f"Scanning folder… ({root.name})")
        self.library_scan_btn.setEnabled(False)
        w = LibraryScanWorker(root, parent=self)
        self._library_scan_worker = w
        w.finished_ok.connect(self._library_on_scan_finished)
        w.failed.connect(self._library_on_scan_failed)
        w.finished.connect(self._library_on_scan_worker_finished)
        w.start()

    def _library_on_scan_worker_finished(self) -> None:
        self._library_scan_worker = None

    def _library_on_scan_failed(self, err: str) -> None:
        self._status.setText("")
        self.library_scan_btn.setEnabled(True)
        self._show_status_message(err, kind="error")

    def _library_on_scan_finished(self, paths_as_strs: object) -> None:
        if not isinstance(paths_as_strs, list):
            self.library_scan_btn.setEnabled(True)
            self._show_status_message("Scan returned unexpected data.", kind="error")
            return
        paths = [Path(str(s)) for s in paths_as_strs]
        n = len(paths)
        self._library_rows = [LibraryFileRow(p) for p in paths]
        self._status.setText(f"Building table… ({n} video{'s' if n != 1 else ''})")
        self._library_fill_table_batched(0)

    def _library_fill_table_batched(self, start_idx: int = 0) -> None:
        batch = _LIBRARY_TABLE_FILL_BATCH_ROWS
        self._library_populating_table = True
        n = len(self._library_rows)
        if start_idx == 0:
            self.library_table.setRowCount(n)
            self._library_table_root_res = (
                self._library_root.resolve() if self._library_root is not None else None
            )
        root_resolved = getattr(self, "_library_table_root_res", None)
        end = min(start_idx + batch, n)
        for i in range(start_idx, end):
            row = self._library_rows[i]
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)
            self.library_table.setItem(i, _LIB_COL_CHECK, chk)
            if root_resolved is not None:
                try:
                    rel = str(row.path.relative_to(root_resolved))
                except ValueError:
                    rel = row.path.name
            else:
                rel = str(row.path)
            it0 = QTableWidgetItem(rel)
            it0.setData(Qt.ItemDataRole.UserRole, str(row.path))
            it0.setToolTip(str(row.path))
            self.library_table.setItem(i, _LIB_COL_FILE, it0)
            self.library_table.setItem(i, _LIB_COL_ARTIST, QTableWidgetItem(row.author))
            self.library_table.setItem(i, _LIB_COL_SONG, QTableWidgetItem(row.song_name))
            self.library_table.setItem(i, _LIB_COL_GUESS_STATUS, QTableWidgetItem(row.ai_guess_status))
        if end < n:
            QTimer.singleShot(0, lambda e=end: self._library_fill_table_batched(e))
        else:
            self._library_populating_table = False
            self.library_scan_btn.setEnabled(True)
            lib_root = self._library_root
            if lib_root is not None:
                self._status.setText(f"Library: {n} video(s) in {lib_root.name}")
                self._show_status_message(
                    f"Found {n} video{'s' if n != 1 else ''} in this folder.",
                    kind="success",
                )
                self._library_start_prefetch_probes()
            else:
                self._status.setText("")

    def _library_start_prefetch_probes(self) -> None:
        self._library_cancel_prefetch_probes()
        pending: list[int] = []
        for i, row in enumerate(self._library_rows):
            if row.probe_summary:
                continue
            pending.append(i)
        pending.reverse()
        self._library_prefetch_pending_probe_rows = pending
        self._library_prefetch_probe_total = len(pending)
        self._library_prefetch_probe_done = 0
        self._library_prefetch_probe_failures = 0
        if not pending:
            return
        self._library_try_start_more_prefetch_probes()

    def _library_cancel_prefetch_probes(self) -> None:
        self._library_prefetch_pending_probe_rows = []
        workers = list(self._library_prefetch_probe_workers.values())
        self._library_prefetch_probe_workers.clear()
        for w in workers:
            if w.isRunning():
                w.requestInterruption()
                w.wait(3000)
        self._library_prefetch_probe_total = 0
        self._library_prefetch_probe_done = 0
        self._library_prefetch_probe_failures = 0

    def _library_try_start_more_prefetch_probes(self) -> None:
        while (
            len(self._library_prefetch_probe_workers) < _MAX_PARALLEL_LIBRARY_PREFETCH_PROBES
            and self._library_prefetch_pending_probe_rows
        ):
            row_idx = self._library_prefetch_pending_probe_rows.pop()
            if row_idx < 0 or row_idx >= len(self._library_rows):
                continue
            path = self._library_rows[row_idx].path
            w = LibraryProbeWorker(row_idx, path, root=self._library_root, parent=self)
            self._library_prefetch_probe_workers[row_idx] = w
            w.finished_ok.connect(self._library_on_prefetch_probe_ok)
            w.failed.connect(self._library_on_prefetch_probe_fail)
            w.finished.connect(self._library_on_prefetch_probe_worker_finished)
            w.start()

    def _library_on_prefetch_probe_worker_finished(self) -> None:
        sender_obj = self.sender()
        if sender_obj is None:
            return
        for row_idx, worker in list(self._library_prefetch_probe_workers.items()):
            if worker is sender_obj:
                self._library_prefetch_probe_workers.pop(row_idx, None)
                break
        self._library_try_start_more_prefetch_probes()

    def _library_on_prefetch_probe_ok(
        self, row_idx: int, author: str, title: str, summary: str, rel_display: str
    ) -> None:
        self._library_apply_probe_result(row_idx, author, title, summary, rel_display)
        self._library_prefetch_probe_done += 1
        self._library_finish_prefetch_if_complete()

    def _library_on_prefetch_probe_fail(self, _row_idx: int, _err: str) -> None:
        self._library_prefetch_probe_failures += 1
        self._library_prefetch_probe_done += 1
        self._library_finish_prefetch_if_complete()

    def _library_finish_prefetch_if_complete(self) -> None:
        total = self._library_prefetch_probe_total
        done = self._library_prefetch_probe_done
        if total <= 0 or done < total:
            return
        if self._library_prefetch_probe_failures > 0:
            self._show_status_message(
                f"Loaded metadata for most files ({total - self._library_prefetch_probe_failures}/{total}).",
                kind="info",
            )
        self._library_prefetch_pending_probe_rows = []
        self._library_prefetch_probe_total = 0
        self._library_prefetch_probe_done = 0
        self._library_prefetch_probe_failures = 0

    def _library_checked_row_indices(self) -> list[int]:
        out: list[int] = []
        for i in range(self.library_table.rowCount()):
            chk = self.library_table.item(i, _LIB_COL_CHECK)
            if chk is not None and chk.checkState() == Qt.CheckState.Checked:
                out.append(i)
        return out

    def _library_set_all_checks(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.library_table.rowCount()):
            chk = self.library_table.item(i, _LIB_COL_CHECK)
            if chk is not None:
                chk.setCheckState(state)

    def _library_set_guess_status(self, row_idx: int, status: str, *, tooltip: str = "") -> None:
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        row = self._library_rows[row_idx]
        row.ai_guess_status = status
        st_item = self.library_table.item(row_idx, _LIB_COL_GUESS_STATUS)
        if st_item is None:
            st_item = QTableWidgetItem(status)
            self.library_table.setItem(row_idx, _LIB_COL_GUESS_STATUS, st_item)
        else:
            st_item.setText(status)
        st_item.setToolTip(tooltip)

    def _library_current_row_index(self) -> int:
        r = self.library_table.currentRow()
        if r < 0 or r >= len(self._library_rows):
            return -1
        return r

    def _library_on_selection_changed(self) -> None:
        if self._library_populating_table:
            return
        r = self._library_current_row_index()
        if r < 0:
            return
        row = self._library_rows[r]
        self._library_block_edit_signals = True
        try:
            self.ui_lib_author.setText(row.author)
            self.ui_lib_song.setText(row.song_name)
            self.ui_lib_stem.setText(row.filename_stem)
        finally:
            self._library_block_edit_signals = False
        self._library_start_probe(r)

    def _library_start_probe(self, row_idx: int) -> None:
        if self._library_probe_worker is not None:
            self._library_probe_worker.requestInterruption()
            self._library_probe_worker.wait(3000)
            self._library_probe_worker = None
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        path = self._library_rows[row_idx].path
        self._status.setText(f"Reading {path.name}…")
        w = LibraryProbeWorker(row_idx, path, root=self._library_root, parent=self)
        self._library_probe_worker = w
        w.finished_ok.connect(self._library_on_probe_ok)
        w.failed.connect(self._library_on_probe_fail)
        w.finished.connect(self._library_probe_finished_cleanup)
        w.start()

    def _library_probe_finished_cleanup(self) -> None:
        self._library_probe_worker = None

    def _library_on_probe_ok(
        self, row_idx: int, author: str, title: str, summary: str, rel_display: str
    ) -> None:
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        self._library_apply_probe_result(row_idx, author, title, summary, rel_display)
        row = self._library_rows[row_idx]
        if self.library_table.currentRow() == row_idx and not self._library_block_edit_signals:
            self._library_block_edit_signals = True
            try:
                self.ui_lib_author.setText(row.author)
                self.ui_lib_song.setText(row.song_name)
            finally:
                self._library_block_edit_signals = False
        self._status.setText(f"Ready — {row.path.name}")

    def _library_apply_probe_result(
        self, row_idx: int, author: str, title: str, summary: str, rel_display: str
    ) -> None:
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        row = self._library_rows[row_idx]
        row.probe_summary = summary
        if author and not row.author:
            row.author = author
        if title and not row.song_name:
            row.song_name = title
        it0 = self.library_table.item(row_idx, _LIB_COL_FILE)
        if it0 is not None:
            it0.setText(rel_display)
            it0.setToolTip(str(row.path))
        it_a = self.library_table.item(row_idx, _LIB_COL_ARTIST)
        it_s = self.library_table.item(row_idx, _LIB_COL_SONG)
        if it_a is not None:
            it_a.setText(row.author)
        if it_s is not None:
            it_s.setText(row.song_name)

    def _library_on_probe_fail(self, row_idx: int, err: str) -> None:
        self._status.setText("")
        self._show_status_message(f"Could not read media info: {err}", kind="error")

    def _library_on_edit_changed(self, *_args: object) -> None:
        if self._library_block_edit_signals:
            return
        r = self._library_current_row_index()
        if r < 0:
            return
        row = self._library_rows[r]
        row.author = self.ui_lib_author.text().strip()
        row.song_name = self.ui_lib_song.text().strip()
        row.filename_stem = self.ui_lib_stem.text().strip()
        a_item = self.library_table.item(r, _LIB_COL_ARTIST)
        s_item = self.library_table.item(r, _LIB_COL_SONG)
        if a_item is not None:
            a_item.setText(row.author)
        if s_item is not None:
            s_item.setText(row.song_name)

    def _library_on_bulk_infer_option_toggled(self, _checked: bool) -> None:
        if hasattr(self, "convert_skip_tagged_cb"):
            self.convert_skip_tagged_cb.blockSignals(True)
            self.convert_skip_tagged_cb.setChecked(self.library_skip_tagged_cb.isChecked())
            self.convert_skip_tagged_cb.blockSignals(False)
        if hasattr(self, "convert_mp4_compat_cb"):
            self.convert_mp4_compat_cb.blockSignals(True)
            self.convert_mp4_compat_cb.setChecked(self.library_mp4_compat_cb.isChecked())
            self.convert_mp4_compat_cb.blockSignals(False)
        self._sync_widgets_to_settings()
        try:
            save_settings(self._settings, self._config_path)
        except OSError:
            pass

    def _library_infer_groq(self) -> None:
        r = self._library_current_row_index()
        if r < 0:
            self._show_status_message("Select a video in the list first.", kind="info")
            return
        self._library_start_infer_queue(
            [r],
            skip_if_tagged=False,
            mp4_compat_mode=self.library_mp4_compat_cb.isChecked(),
        )

    def _library_infer_checked_groq(self) -> None:
        checked = self._library_checked_row_indices()
        if not checked:
            self._show_status_message("Check one or more rows first.", kind="info")
            return
        self._library_start_infer_queue(
            checked,
            skip_if_tagged=self.library_skip_tagged_cb.isChecked(),
            mp4_compat_mode=self.library_mp4_compat_cb.isChecked(),
        )

    def _library_infer_batch_in_progress(self) -> bool:
        return self._library_infer_total > 0 and self._library_infer_done < self._library_infer_total

    def _library_start_infer_queue(
        self,
        row_indices: list[int],
        *,
        skip_if_tagged: bool = False,
        mp4_compat_mode: bool = False,
    ) -> None:
        self._sync_widgets_to_settings()
        key = (self._settings.groq_api_key or "").strip()
        if not key:
            self._show_status_message(
                "Add your API key under Settings → AI assistant (or set GROQ_API_KEY).",
                kind="info",
            )
            return
        if self._library_infer_batch_in_progress():
            self._show_status_message("Already asking the AI — one moment.", kind="info")
            return
        valid = [i for i in row_indices if 0 <= i < len(self._library_rows)]
        if not valid:
            self._show_status_message("Nothing to guess right now.", kind="info")
            return
        self._library_infer_skip_if_tagged = skip_if_tagged
        self._library_infer_mp4_compat_mode = mp4_compat_mode
        self._library_infer_pending = valid
        self._library_infer_total = len(valid)
        self._library_infer_done = 0
        self._library_try_start_more_groq_infers()

    def _library_refresh_infer_status_line(self) -> None:
        if self._library_infer_total <= 0:
            return
        self._status.setText(
            f"Library AI… Groq {self._library_infer_groq_slots_busy} active, "
            f"{len(self._library_infer_pending)} queued • "
            f"{self._library_infer_done}/{self._library_infer_total} finished"
        )

    def _library_try_start_more_groq_infers(self) -> None:
        while (
            self._library_infer_groq_slots_busy < _MAX_PARALLEL_LIBRARY_GROQ and self._library_infer_pending
        ):
            row_idx = self._library_infer_pending.pop(0)
            self._library_start_groq_for_row(row_idx)

    def _library_bump_infer_batch_progress(self) -> None:
        """One library row finished the infer pipeline (probe skip, Groq fail, or save done)."""
        if self._library_infer_total <= 0:
            return
        self._library_infer_done += 1
        self._library_refresh_infer_status_line()
        self._library_finish_infer_batch_if_complete()

    def _library_on_groq_probe_cached(self, row_idx: int, summary: str) -> None:
        if 0 <= row_idx < len(self._library_rows):
            self._library_rows[row_idx].probe_summary = summary

    def _library_start_groq_for_row(self, row_idx: int) -> None:
        row = self._library_rows[row_idx]
        folder_hint = ""
        try:
            if self._library_root is not None:
                folder_hint = str(row.path.parent.resolve().relative_to(self._library_root.resolve()))
            else:
                folder_hint = row.path.parent.name
        except ValueError:
            folder_hint = row.path.parent.name
        self._library_set_guess_status(row_idx, "Guessing")
        self._library_infer_groq_slots_busy += 1
        self._library_refresh_infer_status_line()
        w = GroqInferWorker(
            self._settings,
            row_idx=row_idx,
            filename=row.path.name,
            folder_hint=folder_hint,
            path=row.path,
            probe_summary=row.probe_summary or "",
            skip_if_existing_tags=self._library_infer_skip_if_tagged,
            parent=self,
        )
        w.probe_cached.connect(self._library_on_groq_probe_cached)
        w.skipped.connect(self._library_on_parallel_infer_skipped)
        w.finished_ok.connect(partial(self._library_on_parallel_infer_ok, row_idx))
        w.failed.connect(partial(self._library_on_parallel_infer_fail, row_idx))
        w.finished.connect(self._library_on_parallel_groq_thread_finished)
        w.start()

    def _library_on_parallel_groq_thread_finished(self) -> None:
        self._library_infer_groq_slots_busy = max(0, self._library_infer_groq_slots_busy - 1)
        self._library_refresh_infer_status_line()
        self._library_try_start_more_groq_infers()

    def _library_on_parallel_infer_ok(self, row_idx: int, author: str, song: str) -> None:
        r = row_idx
        if r < 0 or r >= len(self._library_rows):
            return
        row = self._library_rows[r]
        if self._library_infer_mp4_compat_mode:
            row.author = ""
            if song and author:
                row.song_name = f"{song} - {author}"
            elif song:
                row.song_name = song
            elif author:
                row.song_name = author
        else:
            if author:
                row.author = author
            if song:
                row.song_name = song
        if self.library_table.currentRow() == r:
            self._library_block_edit_signals = True
            try:
                self.ui_lib_author.setText(row.author)
                self.ui_lib_song.setText(row.song_name)
            finally:
                self._library_block_edit_signals = False
        a_item = self.library_table.item(r, _LIB_COL_ARTIST)
        s_item = self.library_table.item(r, _LIB_COL_SONG)
        if a_item is not None:
            a_item.setText(row.author)
        if s_item is not None:
            s_item.setText(row.song_name)
        payload = self._library_prepare_save_payload(r, show_errors=True)
        if payload is None:
            self._library_set_guess_status(r, "Save failed", tooltip="Could not read row or file is missing.")
            self._library_bump_infer_batch_progress()
            return
        path, art, song_name, stem = payload
        self._library_refresh_infer_status_line()
        self._library_set_guess_status(
            r,
            "Saving",
            tooltip="Writing tags to the file (stream copy — no re-encode).",
        )
        sw = LibrarySaveWorker(
            r,
            self._settings,
            path,
            art,
            song_name,
            stem,
            parent=self,
        )
        sw.finished_ok.connect(self._library_on_infer_save_ok)
        sw.failed.connect(self._library_on_infer_save_fail)
        sw.finished.connect(sw.deleteLater)
        sw.start()

    def _library_on_parallel_infer_fail(self, row_idx: int, err: str) -> None:
        self._library_set_guess_status(row_idx, "Failed", tooltip=err.strip())
        self._show_status_message(err.strip() or "Could not reach the AI.", kind="error")
        self._library_bump_infer_batch_progress()

    def _library_on_parallel_infer_skipped(self, row_idx: int, reason: str) -> None:
        self._library_set_guess_status(row_idx, "Skipped", tooltip=reason.strip())
        self._library_bump_infer_batch_progress()

    def _library_finish_infer_batch_if_complete(self) -> None:
        if self._library_infer_total <= 0:
            return
        if self._library_infer_done < self._library_infer_total:
            return
        self._show_status_message(
            f"AI guessing complete ({self._library_infer_done}/{self._library_infer_total}).",
            kind="success",
        )
        self._status.setText("")
        self._library_infer_total = 0
        self._library_infer_done = 0
        self._library_infer_pending.clear()
        self._library_infer_skip_if_tagged = False
        self._library_infer_mp4_compat_mode = False

    def _library_prepare_save_payload(
        self, r: int, *, show_errors: bool
    ) -> tuple[Path, str, str, str] | None:
        """Gather path and tag fields on the main thread before starting ``LibrarySaveWorker``."""
        self._sync_widgets_to_settings()
        if r < 0 or r >= len(self._library_rows):
            msg = "Invalid row."
            if show_errors:
                self._show_status_message(msg, kind="error")
            return None
        row = self._library_rows[r]
        if self.library_table.currentRow() == r:
            row.author = self.ui_lib_author.text().strip()
            row.song_name = self.ui_lib_song.text().strip()
            row.filename_stem = self.ui_lib_stem.text().strip()
        path = row.path
        if not path.is_file():
            msg = "That file isn’t there anymore — try scanning the folder again."
            if show_errors:
                self._show_status_message(msg, kind="error")
            return None
        return path, row.author, row.song_name, row.filename_stem

    def _library_apply_saved_path_to_ui(self, r: int, new_path: Path) -> None:
        row = self._library_rows[r]
        row.path = new_path
        row.filename_stem = new_path.stem
        it0 = self.library_table.item(r, _LIB_COL_FILE)
        if it0 is not None:
            it0.setData(Qt.ItemDataRole.UserRole, str(new_path))
            if self._library_root is not None:
                try:
                    rel = str(new_path.resolve().relative_to(self._library_root.resolve()))
                except ValueError:
                    rel = new_path.name
            else:
                rel = new_path.name
            it0.setText(rel)
            it0.setToolTip(str(new_path))
        if self.library_table.currentRow() == r:
            self._library_block_edit_signals = True
            try:
                self.ui_lib_stem.setText(row.filename_stem)
            finally:
                self._library_block_edit_signals = False
        it_a = self.library_table.item(r, _LIB_COL_ARTIST)
        it_s = self.library_table.item(r, _LIB_COL_SONG)
        if it_a is not None:
            it_a.setText(row.author)
        if it_s is not None:
            it_s.setText(row.song_name)

    def _library_on_infer_save_ok(self, r: int, new_path_obj: object) -> None:
        new_path = new_path_obj if isinstance(new_path_obj, Path) else Path(new_path_obj)
        self._library_apply_saved_path_to_ui(r, new_path)
        bits = [x for x in (self._library_rows[r].author, self._library_rows[r].song_name) if x]
        if bits:
            self._library_set_guess_status(r, "Saved")
        else:
            self._library_set_guess_status(
                r,
                "Saved",
                tooltip="AI returned empty artist/title; file was updated anyway.",
            )
        self._library_bump_infer_batch_progress()

    def _library_on_infer_save_fail(self, r: int, err: str) -> None:
        self._library_set_guess_status(r, "Save failed", tooltip=err)
        self._show_status_message(err, kind="error")
        self._library_bump_infer_batch_progress()

    def _library_on_apply_save_worker_finished(self) -> None:
        self._library_apply_save_worker = None
        self.library_apply_btn.setEnabled(True)

    def _library_on_apply_save_ok(self, r: int, new_path_obj: object) -> None:
        new_path = new_path_obj if isinstance(new_path_obj, Path) else Path(new_path_obj)
        self._library_apply_saved_path_to_ui(r, new_path)
        self._library_set_guess_status(r, "Saved")
        self._show_status_message(f"All set — saved “{new_path.name}”.", kind="success")
        self._status.setText("")

    def _library_on_apply_save_fail(self, r: int, err: str) -> None:
        self._library_set_guess_status(r, "Save failed", tooltip=err)
        self._show_status_message(err, kind="error")
        self._status.setText("")

    def _library_apply_changes(self) -> None:
        if self._library_apply_save_worker is not None:
            self._show_status_message("Another save is still in progress.", kind="info")
            return
        r = self._library_current_row_index()
        if r < 0:
            self._show_status_message("Select a video in the list first.", kind="info")
            return
        payload = self._library_prepare_save_payload(r, show_errors=True)
        if payload is None:
            return
        path, author, song_name, stem = payload
        self._status.setText("Saving…")
        self._library_set_guess_status(
            r,
            "Saving",
            tooltip="Writing tags to the file (stream copy — no re-encode).",
        )
        self.library_apply_btn.setEnabled(False)
        w = LibrarySaveWorker(
            r,
            self._settings,
            path,
            author,
            song_name,
            stem,
            parent=self,
        )
        self._library_apply_save_worker = w
        w.finished_ok.connect(self._library_on_apply_save_ok)
        w.failed.connect(self._library_on_apply_save_fail)
        w.finished.connect(w.deleteLater)
        w.finished.connect(self._library_on_apply_save_worker_finished)
        w.start()

    def _library_bulk_rename_to_song_titles(self) -> None:
        if self._library_bulk_rename_worker is not None:
            self._show_status_message("Bulk rename is already in progress.", kind="info")
            return
        checked = self._library_checked_row_indices()
        if not checked:
            self._show_status_message("Check one or more rows first.", kind="info")
            return
        targets: list[tuple[int, Path]] = []
        for idx in checked:
            if 0 <= idx < len(self._library_rows):
                targets.append((idx, self._library_rows[idx].path))
                self._library_set_guess_status(
                    idx,
                    "Renaming",
                    tooltip="Reading metadata title and renaming the file.",
                )
        if not targets:
            self._show_status_message("Nothing to rename right now.", kind="info")
            return
        self.library_bulk_rename_song_btn.setEnabled(False)
        self._status.setText(f"Bulk rename… {len(targets)} file(s)")
        w = LibraryBulkRenameToSongWorker(targets, parent=self)
        self._library_bulk_rename_worker = w
        w.row_renamed.connect(self._library_on_bulk_rename_row_renamed)
        w.row_skipped.connect(self._library_on_bulk_rename_row_skipped)
        w.row_failed.connect(self._library_on_bulk_rename_row_failed)
        w.batch_done.connect(self._library_on_bulk_rename_batch_done)
        w.finished.connect(w.deleteLater)
        w.finished.connect(self._library_on_bulk_rename_worker_finished)
        w.start()

    def _library_on_bulk_rename_row_renamed(self, row_idx: int, new_path_obj: object, song: str) -> None:
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        row = self._library_rows[row_idx]
        row.song_name = (song or "").strip()
        new_path = new_path_obj if isinstance(new_path_obj, Path) else Path(str(new_path_obj))
        self._library_apply_saved_path_to_ui(row_idx, new_path)
        if self.library_table.currentRow() == row_idx:
            self._library_block_edit_signals = True
            try:
                self.ui_lib_song.setText(row.song_name)
            finally:
                self._library_block_edit_signals = False
        self._library_set_guess_status(row_idx, "Renamed")

    def _library_on_bulk_rename_row_skipped(self, row_idx: int, reason: str) -> None:
        self._library_set_guess_status(row_idx, "Skipped", tooltip=reason.strip())

    def _library_on_bulk_rename_row_failed(self, row_idx: int, err: str) -> None:
        self._library_set_guess_status(row_idx, "Rename failed", tooltip=err.strip())

    def _library_on_bulk_rename_batch_done(self, renamed: int, skipped: int, failed: int) -> None:
        self._status.setText("")
        total = renamed + skipped + failed
        self._show_status_message(
            f"Bulk rename complete ({renamed} renamed, {skipped} skipped, {failed} failed; {total} total).",
            kind="success" if failed == 0 else "info",
        )

    def _library_on_bulk_rename_worker_finished(self) -> None:
        self._library_bulk_rename_worker = None
        self.library_bulk_rename_song_btn.setEnabled(True)

    def _library_bulk_clear_metadata(self) -> None:
        if self._library_bulk_clear_metadata_worker is not None:
            self._show_status_message("Bulk metadata clear is already in progress.", kind="info")
            return
        checked = self._library_checked_row_indices()
        if not checked:
            self._show_status_message("Check one or more rows first.", kind="info")
            return
        targets: list[tuple[int, Path]] = []
        skipped_empty = 0
        for idx in checked:
            if 0 <= idx < len(self._library_rows):
                row = self._library_rows[idx]
                if not row.author.strip() and not row.song_name.strip():
                    skipped_empty += 1
                    self._library_set_guess_status(
                        idx,
                        "Skipped",
                        tooltip="Artist and Song are empty in the table.",
                    )
                    continue
                targets.append((idx, row.path))
                self._library_set_guess_status(
                    idx,
                    "Clearing metadata",
                    tooltip="Removing embedded metadata from this file.",
                )
        if not targets:
            if skipped_empty > 0:
                self._show_status_message(
                    "Nothing to clear: selected rows have empty Artist and Song.",
                    kind="info",
                )
            else:
                self._show_status_message("Nothing to clear right now.", kind="info")
            return
        self.library_bulk_clear_metadata_btn.setEnabled(False)
        self._status.setText(f"Clearing metadata… {len(targets)} file(s)")
        w = LibraryBulkClearMetadataWorker(targets, self._settings, parent=self)
        self._library_bulk_clear_metadata_worker = w
        w.row_cleared.connect(self._library_on_bulk_clear_metadata_row_cleared)
        w.row_failed.connect(self._library_on_bulk_clear_metadata_row_failed)
        w.batch_done.connect(self._library_on_bulk_clear_metadata_batch_done)
        w.finished.connect(w.deleteLater)
        w.finished.connect(self._library_on_bulk_clear_metadata_worker_finished)
        w.start()

    def _library_on_bulk_clear_metadata_row_cleared(self, row_idx: int) -> None:
        if row_idx < 0 or row_idx >= len(self._library_rows):
            return
        row = self._library_rows[row_idx]
        row.author = ""
        row.song_name = ""
        row.probe_summary = ""
        it_a = self.library_table.item(row_idx, _LIB_COL_ARTIST)
        it_s = self.library_table.item(row_idx, _LIB_COL_SONG)
        if it_a is not None:
            it_a.setText("")
        if it_s is not None:
            it_s.setText("")
        if self.library_table.currentRow() == row_idx:
            self._library_block_edit_signals = True
            try:
                self.ui_lib_author.setText("")
                self.ui_lib_song.setText("")
            finally:
                self._library_block_edit_signals = False
        self._library_set_guess_status(row_idx, "Metadata cleared")

    def _library_on_bulk_clear_metadata_row_failed(self, row_idx: int, err: str) -> None:
        self._library_set_guess_status(row_idx, "Clear failed", tooltip=err.strip())

    def _library_on_bulk_clear_metadata_batch_done(self, cleared: int, failed: int) -> None:
        self._status.setText("")
        total = cleared + failed
        self._show_status_message(
            f"Metadata clear complete ({cleared} cleared, {failed} failed; {total} total).",
            kind="success" if failed == 0 else "info",
        )

    def _library_on_bulk_clear_metadata_worker_finished(self) -> None:
        self._library_bulk_clear_metadata_worker = None
        self.library_bulk_clear_metadata_btn.setEnabled(True)
