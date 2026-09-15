from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from app.core.config import get_settings
from app.core.errors import ConfigurationError
from app.engine.base import Engine

logger = logging.getLogger("assarium.engine")

#: How many tenants' engines stay warm. Each holds a live connection, so this is a
#: resource budget rather than a correctness setting - an evicted tenant simply
#: reconnects on its next request.
MAX_CACHED_ENGINES = 32

_engines: OrderedDict[str, Engine] = OrderedDict()
_lock = threading.Lock()


def get_engine(tenant_id: str | None = None) -> Engine:
    """
    The compute engine for one tenant.

    Tenant-keyed, because the engine decides which catalog a query runs against. Calling
    this without a tenant outside a test is how every customer ends up sharing one
    warehouse, which is what the audit found.

    Cached with an explicit LRU rather than `functools.lru_cache` for one reason:
    `lru_cache` drops an evicted value on the floor. These values hold live database
    connections, so the thirty-third tenant would leak the first tenant's session instead
    of closing it.
    """
    key = tenant_id or "__default__"

    with _lock:
        engine = _engines.get(key)
        if engine is not None:
            _engines.move_to_end(key)
            return engine

    engine = _build(tenant_id)

    with _lock:
        # Another thread may have built one while this one was connecting. Keep theirs
        # and close ours, so there is only ever one engine per tenant in the map.
        existing = _engines.get(key)
        if existing is not None:
            _close(engine)
            _engines.move_to_end(key)
            return existing

        _engines[key] = engine
        while len(_engines) > MAX_CACHED_ENGINES:
            evicted_key, evicted = _engines.popitem(last=False)
            logger.info("Closing cached engine for %s (cache full)", evicted_key)
            _close(evicted)

    return engine


def _build(tenant_id: str | None) -> Engine:
    engine = get_settings().engine.lower()
    if engine == "duckdb":
        from app.engine.duckdb_engine import DuckDBEngine

        return DuckDBEngine(tenant_id=tenant_id)
    if engine == "databricks":
        from app.engine.databricks_engine import DatabricksEngine

        return DatabricksEngine(tenant_id=tenant_id)
    raise ConfigurationError(
        f"Unknown engine '{engine}'. Set ASSARIUM_ENGINE to 'duckdb' or 'databricks'."
    )


def _close(engine: Engine) -> None:
    """Closing must never take the caller down - the engine is being discarded anyway."""
    try:
        engine.close()
    except Exception:  # noqa: BLE001
        logger.warning("Engine for eviction did not close cleanly", exc_info=True)


def close_all() -> None:
    """Release every cached engine. Called at shutdown and between tests."""
    with _lock:
        while _engines:
            _, engine = _engines.popitem()
            _close(engine)
