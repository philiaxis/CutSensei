"""Exception types shared across the application."""


class CutSenseiError(Exception):
    """Base class for errors that should be shown to the user."""


class FFmpegNotFoundError(CutSenseiError):
    pass


class FFmpegError(CutSenseiError):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


class Cancelled(CutSenseiError):
    """Raised when a long running job is cancelled by the user."""
