"""Local processing uses :class:`mhi2_video_finder.ui.workers.DownloadService` and ``ConvertService``."""

# Intentionally minimal: MainWindow constructs local workers directly when
# ``processing_backend`` is ``local``. See ``RemoteJobController`` for the remote adapter.
