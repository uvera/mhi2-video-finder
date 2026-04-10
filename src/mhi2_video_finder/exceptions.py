"""Shared exceptions for long-running operations."""


class OperationCancelled(Exception):
    """Raised when the user cancels ffmpeg or another subprocess-driven step."""
