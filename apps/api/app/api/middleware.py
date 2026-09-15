"""
Cross-cutting request handling: security headers, rate limits, and the access log.

All three are middleware rather than per-endpoint decorations, for the same reason the
route guard exists. The audit found an audit-logging module with no callers and a
rate-limiting story that was "the identity service has backoff" - both true, both
unreachable. Anything a developer has to remember to add to each new endpoint will
eventually be missing from one, and it will be the one that matters.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings

logger = logging.getLogger("assarium.access")

# ---------------------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------------------

#: Applied to every response. Cheap, and the first thing an enterprise security review
#: looks for.
SECURITY_HEADERS = {
    # The API returns JSON. Letting a browser sniff a different type is how a JSON
    # endpoint becomes a script-injection vector.
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # The API serves no HTML of its own, so everything can be denied outright. A page
    # served from here has no legitimate reason to load or run anything.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-origin",
    # Nothing here needs a camera, a microphone or a location.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cache-Control": "no-store",
}

#: Only sent when the request arrived over TLS. Sending HSTS over plain HTTP is ignored
#: by browsers, and setting it in local development would pin `localhost` to HTTPS in the
#: developer's browser for a year - a genuinely annoying thing to debug.
HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Stamp the headers, and make sure an unhandled error still gets them.

    A registered `Exception` handler does not help here: Starlette runs it in
    `ServerErrorMiddleware`, which sits *outside* user middleware, so its response never
    passes back through this one. The only way to guarantee the headers on a 500 is to
    catch here - which also means the traceback is turned into a plain message rather
    than being sent to whoever triggered it.
    """

    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - deliberately the last line of defence
            logger.exception("Unhandled error on %s %s", request.method, request.url.path)
            response = JSONResponse(
                status_code=500,
                content={"error": {
                    "code": "internal_error",
                    "message": "Something went wrong on our side. The failure has been "
                               "logged.",
                    "details": {},
                }},
            )
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            response.headers.setdefault("Strict-Transport-Security", HSTS)
        return response


# ---------------------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Limit:
    requests: int
    seconds: int

    def __str__(self) -> str:
        return f"{self.requests} requests per {self.seconds}s"


#: Ordinary reads and writes. Generous - this is a defence against a runaway client or a
#: scraper, not a quota anybody should feel.
DEFAULT_LIMIT = Limit(requests=600, seconds=60)

#: The paths that cost real money or real time: a query against the warehouse, a
#: profiling pass, a pipeline run, an export, an LLM call. One tenant hammering these
#: degrades the platform for everybody, so they get their own smaller budget.
EXPENSIVE_LIMIT = Limit(requests=60, seconds=60)

#: Matched as (methods, path fragment). The method matters: starting a pipeline run costs
#: a warehouse; polling that run's status costs a row lookup, and a client watching a
#: five-minute load will poll it hundreds of times. Charging both at the same rate makes
#: the platform throttle its own progress bar.
EXPENSIVE_ROUTES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"POST"}), "/runs"),
    (frozenset({"POST"}), "/profile"),
    (frozenset({"POST"}), "/relationships"),
    (frozenset({"POST"}), "/semantic/build"),
    (frozenset({"POST"}), "/dashboards/generate"),
    (frozenset({"POST", "GET"}), "/semantic/query"),
    (frozenset({"POST", "GET"}), "/semantic/values"),
    (frozenset({"POST", "GET"}), "/chat"),
    (frozenset({"POST", "GET"}), "/explain"),
    (frozenset({"POST", "GET"}), "/export"),
    (frozenset({"POST", "GET"}), "/preview"),
    (frozenset({"POST", "GET"}), "/sample"),
    (frozenset({"POST"}), "/dashboards/") ,
)


def _is_expensive(method: str, path: str) -> bool:
    return any(method in methods and marker in path for methods, marker in EXPENSIVE_ROUTES)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    A sliding-window limit per caller.

    Held in process, which is an honest limitation worth naming: with several replicas
    each one enforces its own share, so the effective limit is the configured one times
    the replica count. That is fine for what this defends against - a stuck retry loop or
    an accidental scrape - and it is not fine as a billing quota. A shared counter
    (Redis) is the upgrade when limits become contractual rather than protective.
    """

    def __init__(self, app):
        super().__init__(app)
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _key(self, request: Request) -> str:
        """
        Who to charge this request to.

        Not the resolved tenant, however much that would be nicer. The tenant is resolved
        by a dependency, which runs *inside* the route - by which point the limiter has
        already decided. Reading `request.state` here would silently always miss and
        every caller would share the IP bucket.

        So: the presented credential, reduced to a hash. Two callers with different
        tokens get different budgets even behind one egress IP, which is the property
        that actually matters for a shared office or a corporate NAT. A forged token
        earns its own bucket but cannot spend anybody else's, and an unauthenticated
        caller is limited by address.
        """
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
            if token:
                return "caller:" + hashlib.sha256(token.encode()).hexdigest()[:16]

        forwarded = request.headers.get("x-forwarded-for", "")
        client = forwarded.split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )
        return f"ip:{client}"

    async def dispatch(self, request: Request, call_next):
        limit = (
            EXPENSIVE_LIMIT
            if _is_expensive(request.method, request.url.path)
            else DEFAULT_LIMIT
        )
        key = f"{self._key(request)}|{limit.seconds}|{limit.requests}"
        now = time.monotonic()

        window = self._hits[key]
        cutoff = now - limit.seconds
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= limit.requests:
            retry_after = max(1, int(window[0] + limit.seconds - now))
            logger.warning("Rate limit hit by %s on %s", key, request.url.path)
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={"error": {
                    "code": "rate_limited",
                    "message": (
                        f"Too many requests. This endpoint allows {limit}. "
                        f"Try again in {retry_after} seconds."
                    ),
                    "details": {"retry_after_seconds": retry_after},
                }},
            )

        window.append(now)
        # Unbounded growth would be a slow leak keyed by whoever calls us.
        if len(self._hits) > 10_000:
            self._prune(now)
        return await call_next(request)

    def _prune(self, now: float) -> None:
        stale = [
            key for key, window in self._hits.items()
            if not window or now - window[-1] > max(DEFAULT_LIMIT.seconds, EXPENSIVE_LIMIT.seconds)
        ]
        for key in stale:
            self._hits.pop(key, None)


# ---------------------------------------------------------------------------------------
# Access log
# ---------------------------------------------------------------------------------------

#: Methods that change something. Reads are not recorded here: at this volume the log
#: would be mostly noise, and the questions an audit actually asks - who changed this,
#: who deleted that, who exported it - are all about writes.
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Reads that are recorded anyway, because taking data out of the platform is exactly the
#: event a compliance review wants to see.
AUDITED_READS = ("/export", "/sample", "/preview")


class AuditMiddleware(BaseHTTPMiddleware):
    """
    Record every mutating request, automatically.

    Written as middleware rather than as a call inside each handler because the audit
    module already had zero callers once. An audit trail that is empty by construction is
    worse than none: somebody reading "no unauthorised access recorded" takes it as
    evidence rather than as an absence of instrumentation.
    """

    async def dispatch(self, request: Request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        duration_ms = int((time.monotonic() - started) * 1000)

        interesting = request.method in MUTATING or any(
            marker in request.url.path for marker in AUDITED_READS
        )
        if not interesting or request.url.path == "/api/health":
            return response

        context = getattr(request.state, "audit_context", None)
        tenant_id = getattr(context, "tenant_id", None) or "unresolved"
        actor = getattr(context, "subject", None) or "anonymous"

        # Logged whatever the outcome: a run of 404s and 403s from one actor is the
        # shape of somebody probing for other tenants' ids, and that is precisely the
        # pattern worth being able to find afterwards.
        logger.info(
            "%s %s -> %s  tenant=%s actor=%s %dms",
            request.method, request.url.path, response.status_code,
            tenant_id, actor, duration_ms,
        )

        if response.status_code < 400 and context is not None:
            _persist(request, context, response.status_code, duration_ms)
        return response


def _persist(request: Request, context, status: int, duration_ms: int) -> None:
    """
    Write the durable audit row.

    Best-effort on purpose: a failure to record an audit row must not fail the user's
    request, which has already succeeded. It is logged loudly instead, so a broken audit
    pipeline is visible rather than silent.
    """
    from app.audit.service import log_audit_event
    from app.db.session import session_scope

    try:
        with session_scope() as db:
            log_audit_event(
                db,
                tenant_id=context.tenant_id,
                actor_id=context.subject,
                action=f"{request.method} {request.scope.get('route_path') or request.url.path}",
                target_type="http",
                target_id=request.url.path,
                details={"status": status, "duration_ms": duration_ms},
            )
    except Exception:  # noqa: BLE001
        logger.error("Failed to write an audit entry", exc_info=True)


def install(app) -> None:
    """
    Attach the middleware, outermost first.

    Order matters. Rate limiting runs before anything expensive so a flood is rejected
    cheaply; the audit log wraps the handler so it sees the real status code; security
    headers are outermost so they are present on error responses too, including the
    rate-limit rejection.
    """
    settings = get_settings()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    logger.info(
        "Request middleware installed (rate limit %s, expensive %s, environment %s)",
        DEFAULT_LIMIT, EXPENSIVE_LIMIT, settings.environment,
    )
