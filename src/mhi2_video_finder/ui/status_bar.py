"""Transient status-bar message widget and job-queued notification for MainWindow."""

from __future__ import annotations

from typing import Literal

from PyQt6.QtCore import QSize, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox, QStyle, QSystemTrayIcon


class StatusBarMixin:
    def _hide_status_message(self) -> None:
        self._message_bar.setVisible(False)
        self._message_text.clear()
        self._message_icon.clear()

    def _show_status_message(
        self,
        message: str,
        *,
        kind: Literal["success", "error", "info"] = "success",
        duration_ms: int = 10000,
    ) -> None:
        """Transient bottom bar with icon (check / error / info); hides after ``duration_ms``."""
        style = self.style()
        if kind == "success":
            icon = style.standardIcon(QStyle.StandardPixmap.SP_DialogYesButton)
        elif kind == "error":
            icon = style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxCritical)
        else:
            icon = style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
        pm = icon.pixmap(QSize(20, 20))
        self._message_icon.setPixmap(pm)
        self._message_text.setPlainText(message)
        self._message_text.verticalScrollBar().setValue(0)
        self._message_bar.setVisible(True)
        self._status_message_timer.start(duration_ms)

    def _notify_queued(self, count: int) -> None:
        msg = f"Queued {count} video(s) for download."
        self._status.setText(msg)
        QApplication.beep()
        if self._tray is not None:
            self._tray.showMessage(
                "mhi2-video-finder",
                msg,
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )
        else:
            box = QMessageBox(
                QMessageBox.Icon.Information,
                "mhi2-video-finder",
                msg,
                QMessageBox.StandardButton.Ok,
                self,
            )
            box.setModal(False)
            box.show()
            QTimer.singleShot(3500, box.close)
