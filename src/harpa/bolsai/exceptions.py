"""Errors raised by the Bolsai API client."""


class BolsaiError(Exception):
    """Base class for Bolsai client errors."""


class BolsaiAuthError(BolsaiError):
    """Invalid or missing API credentials (HTTP 401)."""


class BolsaiRateLimitError(BolsaiError):
    """Daily rate limit exceeded (HTTP 429)."""


class BolsaiHTTPError(BolsaiError):
    """Unexpected HTTP status from the Bolsai API."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
