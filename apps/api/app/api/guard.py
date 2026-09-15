"""
A startup check that no route is reachable without a tenant.

The audit found 45 endpoints serving data with no authentication, while the tenancy module
itself was fully built and passing 29 tests. Those tests exercised `get_tenant_context()`
directly, so they proved the lock worked - not that it was on the door.

Reviews do not catch this reliably. The failure mode is adding a new endpoint and simply
not thinking about it, and the new endpoint looks exactly like the others. So the check is
mechanical and runs at import: if a route is neither explicitly public nor guarded, the
process refuses to start.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.core.errors import ConfigurationError

logger = logging.getLogger("assarium.guard")

#: Routes that legitimately serve no tenant data. Every entry is a deliberate decision,
#: and adding one should feel like a decision - that is why this is an explicit list
#: rather than a pattern anyone can widen.
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/api/health",      # liveness, returns no tenant data
    "/api/sources",     # the catalogue of connector types we support; identical for all
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
})


def _guard_names(route: APIRoute) -> set[str]:
    """Every dependency callable reachable from this route, at any depth."""
    names: set[str] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dependency = stack.pop()
        call = getattr(dependency, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", ""))
        stack.extend(dependency.dependencies)
    return names


def iter_api_routes(app: FastAPI) -> list[APIRoute]:
    """
    Every APIRoute the application will actually serve.

    `app.routes` is not the whole answer. This FastAPI version includes routers lazily:
    `include_router` leaves a `_IncludedRouter` placeholder and only resolves the real
    routes later, so walking `app.routes` alone finds the handful defined directly on the
    app and none of the hundreds behind a router.

    That is precisely how the first version of this guard reported success while every
    router was unguarded - it inspected an empty set and found nothing wrong. Hence the
    recursion here and the floor check below.

    Router-level `dependencies=[...]` are merged into each route's dependant when the
    decorator runs, so a route reached this way carries its guard with it.
    """
    found: list[APIRoute] = []
    seen: set[int] = set()
    stack: list[Any] = list(app.routes)
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, APIRoute):
            found.append(item)
            continue
        nested = getattr(item, "original_router", None)
        if nested is not None:
            stack.extend(getattr(nested, "routes", []))
            continue
        stack.extend(getattr(item, "routes", []) or [])
    return found


#: A guard that passes because it looked at nothing is not a guard. If the application
#: ever has fewer routes than this, the enumeration above has broken against a new
#: FastAPI internal and must be fixed rather than trusted.
MINIMUM_EXPECTED_ROUTES = 30


def assert_routes_are_guarded(app: FastAPI) -> None:
    """
    Refuse to start if any route can be reached without resolving a tenant.

    Checked by walking the dependency tree rather than the handler's signature: a
    router-level dependency, a nested one, and a directly declared parameter all count,
    because all three genuinely do resolve a tenant.
    """
    routes = iter_api_routes(app)

    if len(routes) < MINIMUM_EXPECTED_ROUTES:
        raise ConfigurationError(
            f"Refusing to start: the route guard found only {len(routes)} route(s), "
            f"fewer than the {MINIMUM_EXPECTED_ROUTES} this application is known to "
            "have. Route enumeration has broken - most likely FastAPI changed how "
            "included routers are stored - so the guard cannot prove anything and must "
            "not be treated as if it had. Fix `iter_api_routes`."
        )

    unguarded: list[str] = []
    for route in routes:
        if route.path in PUBLIC_PATHS:
            continue
        if not ({"get_tenant_context", "get_scope"} & _guard_names(route)):
            methods = ",".join(sorted(route.methods - {"HEAD", "OPTIONS"}))
            unguarded.append(f"{methods} {route.path}  ({route.name})")

    if unguarded:
        raise ConfigurationError(
            "Refusing to start: these routes serve data without resolving a tenant, so "
            "any caller could reach any customer's rows.\n  - "
            + "\n  - ".join(sorted(unguarded))
            + "\n\nAdd `dependencies=[Depends(get_tenant_context)]` to the router, take "
            "`scope: TenantScope = Depends(get_scope)` in the handler, or - if the route "
            "genuinely serves no tenant data - add its path to guard.PUBLIC_PATHS."
        )

    public = sum(1 for r in routes if r.path in PUBLIC_PATHS)
    logger.info(
        "Route guard: %d route(s) checked, %d tenant-scoped, %d public",
        len(routes), len(routes) - public, public,
    )
