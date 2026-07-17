"""Settings load/save/apply logic for MainWindow (spans widgets from every tab)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QMessageBox

from mhi2_video_finder.config import default_config_path, load_settings, save_settings

# Stored in QComboBox userData; must match config ``video_encoder`` values.
_ENCODER_CHOICES: tuple[tuple[str, str], ...] = (
    ("libx264 (CPU — best for picky car USB / MHI2)", "libx264"),
    ("h264_vaapi (Intel / AMD GPU)", "h264_vaapi"),
    ("h264_nvenc (NVIDIA GPU)", "h264_nvenc"),
)


class SettingsMixin:
    @staticmethod
    def _canonical_video_encoder(enc: str) -> str:
        e = (enc or "").strip().lower()
        if e == "vaapi":
            return "h264_vaapi"
        if e == "nvenc":
            return "h264_nvenc"
        if e in ("h264_vaapi", "h264_nvenc", "libx264"):
            return e
        return "libx264"

    def _apply_settings_to_widgets(self) -> None:
        want_remote = self._settings.processing_backend.strip().lower() == "remote"
        self.ui_processing_backend.blockSignals(True)
        self.ui_processing_backend.setCurrentIndex(1 if want_remote else 0)
        self.ui_processing_backend.blockSignals(False)
        self.ui_remote_url.setText((self._settings.remote_base_url or "").strip())
        self.ui_remote_token.setText((self._settings.remote_bearer_token or "").strip())
        self.ui_remote_dl_dir.setText(str(self._settings.remote_download_dir))
        self.ui_remote_auto_download.setChecked(self._settings.remote_auto_download)
        self.ui_remote_auto_download_daemon_imports.setChecked(
            self._settings.remote_auto_download_daemon_imports
        )

        self.limit_spin.setValue(self._settings.search_limit if self._settings.search_limit else 15)
        self.ui_ffmpeg_threads.setValue(max(0, min(32, self._settings.ffmpeg_threads)))
        self.ui_ffmpeg_nice.setValue(max(0, min(19, self._settings.ffmpeg_nice)))
        self.ui_ffmpeg_cpu_limit.setValue(max(0, min(100, self._settings.ffmpeg_cpu_limit_percent)))
        self.ui_max_parallel_dl.setValue(max(1, min(32, self._settings.max_parallel_downloads)))
        self.ui_max_parallel_cv.setValue(max(1, min(32, self._settings.max_parallel_converts)))

        want_enc = self._canonical_video_encoder(self._settings.video_encoder)
        idx = 0
        for i in range(self.ui_video_encoder.count()):
            if self.ui_video_encoder.itemData(i) == want_enc:
                idx = i
                break
        self.ui_video_encoder.blockSignals(True)
        self.ui_video_encoder.setCurrentIndex(idx)
        self.ui_video_encoder.blockSignals(False)
        self.ui_embed_metadata.setChecked(self._settings.embed_metadata)
        self.ui_embed_album_art.setChecked(self._settings.embed_album_art)
        self.ui_vaapi_device.setText((self._settings.vaapi_device or "").strip())
        self.ui_vaapi_cbr.setChecked(self._settings.vaapi_cbr)
        self._update_vaapi_options_visibility()

        lf = self._settings.library_last_folder
        self.ui_library_folder.setText(str(lf) if lf is not None else "")

        self.ui_groq_enabled.setChecked(self._settings.groq_enabled)
        self.ui_groq_api_key.setText((self._settings.groq_api_key or "").strip())
        self.ui_groq_model.setText((self._settings.groq_model or "").strip() or "llama-3.1-8b-instant")
        self.ui_groq_base_url.setText((self._settings.groq_base_url or "").strip())
        self.ui_groq_temperature.setValue(float(self._settings.groq_temperature))

        if hasattr(self, "library_skip_tagged_cb"):
            self.library_skip_tagged_cb.blockSignals(True)
            self.library_skip_tagged_cb.setChecked(self._settings.library_skip_bulk_infer_if_tagged)
            self.library_skip_tagged_cb.blockSignals(False)
        if hasattr(self, "library_mp4_compat_cb"):
            self.library_mp4_compat_cb.blockSignals(True)
            self.library_mp4_compat_cb.setChecked(self._settings.library_bulk_infer_mp4_compat_mode)
            self.library_mp4_compat_cb.blockSignals(False)
        if hasattr(self, "convert_skip_tagged_cb"):
            self.convert_skip_tagged_cb.blockSignals(True)
            self.convert_skip_tagged_cb.setChecked(self._settings.library_skip_bulk_infer_if_tagged)
            self.convert_skip_tagged_cb.blockSignals(False)
        if hasattr(self, "convert_mp4_compat_cb"):
            self.convert_mp4_compat_cb.blockSignals(True)
            self.convert_mp4_compat_cb.setChecked(self._settings.library_bulk_infer_mp4_compat_mode)
            self.convert_mp4_compat_cb.blockSignals(False)

    def _sync_widgets_to_settings(self) -> None:
        pb = self.ui_processing_backend.currentData()
        self._settings.processing_backend = pb if pb in ("local", "remote") else "local"
        self._settings.remote_base_url = self.ui_remote_url.text().strip()
        self._settings.remote_bearer_token = self.ui_remote_token.text().strip()
        rd = self.ui_remote_dl_dir.text().strip()
        if rd:
            self._settings.remote_download_dir = Path(rd).expanduser().resolve()
        self._settings.remote_auto_download = self.ui_remote_auto_download.isChecked()
        self._settings.remote_auto_download_daemon_imports = (
            self.ui_remote_auto_download_daemon_imports.isChecked()
        )

        self._settings.ffmpeg_threads = self.ui_ffmpeg_threads.value()
        self._settings.ffmpeg_nice = self.ui_ffmpeg_nice.value()
        self._settings.ffmpeg_cpu_limit_percent = self.ui_ffmpeg_cpu_limit.value()
        self._settings.max_parallel_downloads = self.ui_max_parallel_dl.value()
        self._settings.max_parallel_converts = self.ui_max_parallel_cv.value()
        data = self.ui_video_encoder.currentData()
        self._settings.video_encoder = data if isinstance(data, str) else "libx264"
        self._settings.embed_metadata = self.ui_embed_metadata.isChecked()
        self._settings.embed_album_art = self.ui_embed_album_art.isChecked()
        dev = self.ui_vaapi_device.text().strip()
        self._settings.vaapi_device = dev if dev else "/dev/dri/renderD128"
        self._settings.vaapi_cbr = self.ui_vaapi_cbr.isChecked()

        lib = self.ui_library_folder.text().strip()
        self._settings.library_last_folder = Path(lib).expanduser().resolve() if lib else None

        self._settings.groq_enabled = self.ui_groq_enabled.isChecked()
        ga = self.ui_groq_api_key.text().strip()
        self._settings.groq_api_key = ga if ga else None
        self._settings.groq_model = self.ui_groq_model.text().strip() or "llama-3.1-8b-instant"
        self._settings.groq_base_url = (
            self.ui_groq_base_url.text().strip() or "https://api.groq.com/openai/v1"
        )
        self._settings.groq_temperature = float(self.ui_groq_temperature.value())

        if hasattr(self, "library_skip_tagged_cb"):
            self._settings.library_skip_bulk_infer_if_tagged = self.library_skip_tagged_cb.isChecked()
        if hasattr(self, "library_mp4_compat_cb"):
            self._settings.library_bulk_infer_mp4_compat_mode = self.library_mp4_compat_cb.isChecked()
        if hasattr(self, "convert_skip_tagged_cb"):
            self._settings.library_skip_bulk_infer_if_tagged = self.convert_skip_tagged_cb.isChecked()
        if hasattr(self, "convert_mp4_compat_cb"):
            self._settings.library_bulk_infer_mp4_compat_mode = self.convert_mp4_compat_cb.isChecked()

    def _update_vaapi_options_visibility(self) -> None:
        data = self.ui_video_encoder.currentData()
        enc = data if isinstance(data, str) else "libx264"
        self._vaapi_frame.setVisible(enc in ("h264_vaapi", "vaapi"))

    def _on_video_encoder_changed(self, _index: int) -> None:
        self._update_vaapi_options_visibility()

    def _settings_config_target(self) -> Path:
        t = self.config_edit.text().strip()
        return Path(t) if t else default_config_path()

    def _apply_session_settings(self) -> None:
        self._sync_widgets_to_settings()
        want_remote = self._settings.processing_backend.strip().lower() == "remote"
        if want_remote != self._initial_remote:
            QMessageBox.warning(
                self,
                "Restart required",
                "Switching between local and remote processing only takes effect after you "
                "restart mhi2-video-finder-ui.",
            )
        self._dl.set_settings(self._settings)
        self._cv.set_settings(self._settings)
        self._dl.set_max_workers(self._settings.max_parallel_downloads)
        self._cv.set_max_workers(self._settings.max_parallel_converts)
        QMessageBox.information(
            self,
            "Settings",
            "Applied for this session. New encodes use the encoder, embed options, FFmpeg limits, "
            "and queue width from this tab.",
        )

    def _save_settings_to_file(self) -> None:
        self._sync_widgets_to_settings()
        path = self._settings_config_target()
        try:
            save_settings(self._settings, path)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        if not self.config_edit.text().strip():
            self.config_edit.setText(str(path))
        self._config_path = path
        QMessageBox.information(self, "Settings", f"Saved to:\n{path}")

    def _reload_settings(self) -> None:
        p = Path(self.config_edit.text().strip()) if self.config_edit.text().strip() else None
        self._config_path = p
        self._settings = load_settings(p)
        self._apply_settings_to_widgets()
        self._dl.set_settings(self._settings)
        self._cv.set_settings(self._settings)
        QMessageBox.information(
            self,
            "Settings",
            "Config reloaded. Encode options and paths apply to new work; restart the app if something "
            "still looks stale.",
        )
