"""Application-specific exceptions."""


class NewsWithWillyError(Exception):
    """Base class for expected application failures."""


class ConfigurationError(NewsWithWillyError):
    """Raised when runtime configuration is invalid."""


class ExternalServiceError(NewsWithWillyError):
    """Raised when an external news, AI, or notification service fails."""
