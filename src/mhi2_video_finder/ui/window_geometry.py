"""Wayland/X11 window work-area geometry sizing and positioning for MainWindow."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QTimer
from PyQt6.QtGui import QGuiApplication, QShowEvent

# Shrink max window height below availableGeometry so the bottom strip stays visible (Wayland /
# fractional scaling often clips the last ~10–20 logical px if we use the full work area).
_WORK_AREA_BOTTOM_INSET_PX = 48


class WindowGeometryMixin:
    def _apply_initial_window_geometry(self) -> None:
        """Place and size the window inside the work area (respects top/bottom panels)."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(960, 640)
            return
        ag = screen.availableGeometry()
        w, h = 960, 640
        # Leave room for title bar / frame before first paint (frameGeometry not reliable yet).
        frame_allow = 48
        w = min(w, max(400, ag.width() - 24))
        h = min(h, max(300, ag.height() - frame_allow - 24))
        x = ag.x() + max(0, (ag.width() - w) // 2)
        y = ag.y() + max(0, (ag.height() - h) // 2)
        self.setGeometry(x, y, w, h)

    def _frame_decoration_extra(self) -> tuple[int, int]:
        """Extra width/height of the window frame beyond the client area (for availableGeometry math)."""
        wh = self.windowHandle()
        if wh is not None:
            fm = wh.frameMargins()
            ew = fm.left() + fm.right()
            eh = fm.top() + fm.bottom()
            if ew > 0 or eh > 0:
                return ew, eh
        fg = self.frameGeometry()
        ew = max(0, fg.width() - self.width())
        eh = max(0, fg.height() - self.height())
        if ew == 0 and eh == 0:
            eh = max(eh, 36)
        return ew, eh

    def _apply_window_maximum_to_work_area(self) -> None:
        """Cap window client size so the full frame fits in the screen work area (non-fullscreen)."""
        if self.isFullScreen() or self.isMaximized():
            self.setMaximumSize(16777215, 16777215)
            return
        screen = self.screen()
        if screen is None:
            return
        ag = screen.availableGeometry()
        ew, eh = self._frame_decoration_extra()
        inset = _WORK_AREA_BOTTOM_INSET_PX
        try:
            dpr = float(screen.devicePixelRatio())
            inset = max(inset, int(16 * max(1.0, dpr)))
        except (TypeError, ValueError):
            pass
        mw = max(self.minimumWidth(), ag.width() - ew)
        mh = max(self.minimumHeight(), ag.height() - eh - inset)
        self.setMaximumSize(mw, mh)
        # Some platforms keep a taller window until an explicit resize.
        nw = max(self.minimumWidth(), min(self.width(), mw))
        nh = max(self.minimumHeight(), min(self.height(), mh))
        if nw != self.width() or nh != self.height():
            self.resize(nw, nh)

    def _fit_window_to_work_area(self) -> None:
        """Keep the window frame inside ``availableGeometry`` (panel-safe when not fullscreen)."""
        if self.isFullScreen() or self.isMaximized():
            return
        screen = self.screen()
        if screen is None:
            return
        ag = screen.availableGeometry()
        inset = _WORK_AREA_BOTTOM_INSET_PX
        try:
            dpr = float(screen.devicePixelRatio())
            inset = max(inset, int(16 * max(1.0, dpr)))
        except (TypeError, ValueError):
            pass
        target_bottom = ag.bottom() - inset
        fg = self.frameGeometry()
        if fg.height() > ag.height() - inset:
            nh = max(self.minimumHeight(), self.height() - (fg.height() - (ag.height() - inset)))
            self.resize(self.width(), nh)
            fg = self.frameGeometry()
        if fg.width() > ag.width():
            nw = max(self.minimumWidth(), self.width() - (fg.width() - ag.width()))
            self.resize(nw, self.height())
            fg = self.frameGeometry()
        x, y = fg.x(), fg.y()
        if fg.right() > ag.right():
            x = ag.right() - fg.width()
        if fg.left() < ag.left():
            x = ag.left()
        if fg.bottom() > target_bottom:
            y = fg.y() + target_bottom - fg.bottom()
        if fg.top() < ag.top():
            y = ag.top()
        if x != fg.x() or y != fg.y():
            self.move(x, y)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._work_area_screen_hooked:
            wh = self.windowHandle()
            if wh is not None:
                wh.screenChanged.connect(self._on_window_screen_changed)
                self._work_area_screen_hooked = True
            else:
                QTimer.singleShot(0, self._try_hook_screen_changed_for_work_area)
        QTimer.singleShot(0, self._apply_window_maximum_to_work_area)
        QTimer.singleShot(0, self._fit_window_to_work_area)
        QTimer.singleShot(50, self._apply_window_maximum_to_work_area)
        QTimer.singleShot(150, self._apply_window_maximum_to_work_area)

    def _try_hook_screen_changed_for_work_area(self) -> None:
        if self._work_area_screen_hooked:
            return
        wh = self.windowHandle()
        if wh is not None:
            wh.screenChanged.connect(self._on_window_screen_changed)
            self._work_area_screen_hooked = True

    def _on_window_screen_changed(self, _screen: object) -> None:
        QTimer.singleShot(0, self._apply_window_maximum_to_work_area)
        QTimer.singleShot(0, self._fit_window_to_work_area)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            QTimer.singleShot(0, self._apply_window_maximum_to_work_area)
            QTimer.singleShot(0, self._fit_window_to_work_area)
