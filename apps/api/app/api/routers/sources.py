from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.connectors.registry import list_specs

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.get("")
def get_sources() -> list[dict[str, Any]]:
    """Every source the platform can connect to, with its credential specification.

    The connection dialog is rendered entirely from this response, so a new driver
    reaches the UI without a frontend change.
    """
    return list_specs()
