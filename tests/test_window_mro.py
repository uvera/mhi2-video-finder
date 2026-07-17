"""MainWindow's mixin/QMainWindow base ordering (no Qt widgets, but MainWindow needs PyQt6 importable).

Regression test for a real bug: WindowGeometryMixin.changeEvent/showEvent call
super().changeEvent(event)/super().showEvent(event) expecting to reach
QMainWindow's C++ implementation. That only works if QMainWindow's ancestor
chain is contiguous at the END of the MRO -- i.e. QMainWindow must be the
LAST base MainWindow lists, not the first. Listing it first (the original
mixin-split ordering) pushes the whole Qt ancestor chain ahead of every
mixin in the MRO, so those super() calls fall through to `object` and raise
AttributeError at runtime -- something no import check, ruff run, or
text-diff verification catches, since the moved method bodies are byte-
identical either way; only the *class declaration* determines whether
super() resolves correctly.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QMainWindow

from mhi2_video_finder.ui.window import MainWindow
from mhi2_video_finder.ui.window_geometry import WindowGeometryMixin


def test_mainwindow_lists_qmainwindow_after_every_local_mixin() -> None:
    mro = MainWindow.__mro__
    qmainwindow_index = mro.index(QMainWindow)
    for base in MainWindow.__bases__:
        if base is QMainWindow:
            continue
        assert mro.index(base) < qmainwindow_index, (
            f"{base.__name__} must precede QMainWindow in MainWindow's MRO so its "
            "super() calls into Qt event handlers (changeEvent, showEvent, ...) resolve"
        )


def test_window_geometry_mixin_super_calls_resolve_to_qwidget() -> None:
    """WindowGeometryMixin.changeEvent/showEvent call super().<method>(event); confirm
    that resolves to a real Qt implementation (QWidget's), not falls through to object."""
    mro = MainWindow.__mro__
    geometry_index = mro.index(WindowGeometryMixin)
    remaining = mro[geometry_index + 1 :]
    assert any(hasattr(cls, "changeEvent") for cls in remaining), (
        "no class after WindowGeometryMixin in the MRO defines changeEvent -- "
        "super().changeEvent(event) would raise AttributeError"
    )
    assert any(hasattr(cls, "showEvent") for cls in remaining), (
        "no class after WindowGeometryMixin in the MRO defines showEvent -- "
        "super().showEvent(event) would raise AttributeError"
    )
