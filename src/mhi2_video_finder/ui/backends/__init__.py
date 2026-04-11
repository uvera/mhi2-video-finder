"""UI processing backends (local workers vs remote daemon)."""

from mhi2_video_finder.ui.backends.remote import RemoteJobController

__all__ = ["RemoteJobController"]
