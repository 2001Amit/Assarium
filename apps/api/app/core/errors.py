from __future__ import annotations


class AssariumError(Exception):
    """Base class for errors the API turns into a structured response."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ConfigurationError(AssariumError):
    status_code = 500
    code = "configuration_error"


class NotFoundError(AssariumError):
    status_code = 404
    code = "not_found"


class ValidationError(AssariumError):
    status_code = 422
    code = "validation_error"


class ConflictError(AssariumError):
    """The request is well-formed but conflicts with something that already exists."""

    status_code = 409
    code = "conflict"


class DriverNotInstalledError(AssariumError):
    """A connector's optional dependency is absent from this install."""

    status_code = 503
    code = "driver_not_installed"

    def __init__(self, source: str, package: str, extra: str):
        super().__init__(
            f"The {source} connector needs the '{package}' package, which is not installed.",
            details={
                "package": package,
                # A command the user can paste and run, rather than one that assumes
                # a working Poetry on the host.
                "install": f"./.venv/bin/pip install {package}",
                "extra": extra,
                "source": source,
            },
        )


class ConnectionFailedError(AssariumError):
    """The remote system rejected or could not complete the connection."""

    status_code = 400
    code = "connection_failed"


class QueryError(AssariumError):
    status_code = 400
    code = "query_error"
