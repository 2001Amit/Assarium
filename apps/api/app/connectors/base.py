from __future__ import annotations

import importlib
import importlib.util
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, ClassVar

from app.connectors.types import (
    BrowseNode,
    ConnectionTestResult,
    CredentialSpec,
    DatasetSchema,
    SampleResult,
)
from app.core.errors import DriverNotInstalledError

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow as pa


class Connector(ABC):
    """
    One source system. Drivers receive already-decrypted credentials and are responsible
    for their own connection lifecycle.

    `config` holds non-secret settings (host, database, warehouse...). `secrets` holds the
    values the spec marked secret. They are kept apart so config can be logged and secrets
    can be stored encrypted and never round-tripped to the browser.
    """

    spec: ClassVar[CredentialSpec]
    # Optional dependency guard: (import_name, pip_package, poetry_extra)
    requires: ClassVar[tuple[str, str, str] | None] = None
    # True when describe() returns a counted total rather than an optimiser estimate.
    exact_row_counts: ClassVar[bool] = False

    def __init__(self, config: dict[str, Any], secrets: dict[str, Any]):
        self.config = config
        self.secrets = secrets
        self.auth_method: str = config.get("auth_method") or (
            self.spec.auth_methods[0].id if self.spec.auth_methods else "default"
        )

    # -- dependency handling ----------------------------------------------------------

    @classmethod
    def ensure_driver(cls) -> Any:
        if not cls.requires:
            return None
        module, package, extra = cls.requires
        try:
            return importlib.import_module(module)
        except ImportError as exc:
            raise DriverNotInstalledError(cls.spec.name, package, extra) from exc

    @classmethod
    def is_available(cls) -> bool:
        if not cls.requires:
            return True
        try:
            # find_spec raises rather than returning None when a parent package is absent.
            return importlib.util.find_spec(cls.requires[0]) is not None
        except (ImportError, AttributeError, ValueError):
            return False

    # -- lifecycle --------------------------------------------------------------------

    def test(self) -> ConnectionTestResult:
        started = time.perf_counter()
        try:
            details = self.probe()
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the user
            return ConnectionTestResult(ok=False, message=_clean_error(exc))
        elapsed = int((time.perf_counter() - started) * 1000)
        return ConnectionTestResult(
            ok=True,
            message="Connection established.",
            latency_ms=elapsed,
            server_version=details.pop("server_version", None),
            details=details,
        )

    @abstractmethod
    def probe(self) -> dict[str, Any]:
        """Do the cheapest call that proves the credentials work. Raise on failure."""

    @abstractmethod
    def browse(self, path: list[str]) -> list[BrowseNode]:
        """List children of `path`. An empty path lists the source's top level."""

    @abstractmethod
    def describe(self, path: list[str]) -> DatasetSchema:
        """Return the column schema of the dataset at `path`."""

    @abstractmethod
    def sample(self, path: list[str], limit: int = 100) -> SampleResult:
        """Return up to `limit` rows for profiling and preview."""

    def read_batches(
        self, path: list[str], batch_size: int = 50_000
    ) -> Iterator[pa.RecordBatch]:
        """
        Stream the whole dataset as Arrow batches, for ingestion into a layer.

        Sources that can only be previewed leave this unimplemented; the pipeline then
        reports that the source cannot be ingested rather than silently loading a sample.
        """
        raise NotImplementedError(
            f"{self.spec.name} does not support full reads yet, only preview and profiling."
        )

    def close(self) -> None:  # noqa: B027 - optional hook, not every source pools
        """Release any pooled resource. Safe to call more than once."""

    def __enter__(self) -> Connector:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _clean_error(exc: Exception) -> str:
    """Give the user the driver's own message without a stack trace or credential echo."""
    message = str(exc).strip() or exc.__class__.__name__
    return message.splitlines()[0][:400]
