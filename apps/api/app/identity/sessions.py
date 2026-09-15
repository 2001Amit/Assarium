"""
Session issue, rotation, and revocation.

The design in one line: short access tokens so that removing somebody takes effect in
minutes without a database read on every request, and long refresh tokens that rotate on
every use so that a stolen one is detectable.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import timedelta

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.config import get_settings
from app.core.errors import AssariumError
from app.identity.credentials import new_token, token_hash
from app.identity.models import Session
from app.models.base import ensure_utc, new_id, utcnow

logger = logging.getLogger("assarium.identity")

#: Short enough that revoking a membership takes effect on its own, long enough that a
#: browsing session is not refreshing constantly.
ACCESS_TOKEN_MINUTES = 10
REFRESH_TOKEN_DAYS = 30

ALGORITHM = "HS256"


class SessionError(AssariumError):
    status_code = 401
    code = "session_invalid"


def _signing_key() -> str:
    """
    The key sessions are signed with, derived rather than used directly.

    `secret_key` also encrypts the local secret store. Signing tokens with the same
    bytes means one primitive's weakness reaches the other, so a separate key is derived
    for this purpose with a fixed label. The derivation also guarantees 32 bytes, which
    is the floor RFC 7518 sets for HMAC-SHA256 - a shorter configured key would
    otherwise silently weaken every token we issue.
    """
    settings = get_settings()
    if not settings.secret_key:
        raise SessionError("The platform has no signing key configured.")
    return hmac.new(
        settings.secret_key.encode(), b"assarium/session-signing/v1", hashlib.sha256
    ).hexdigest()


def issue(
    db: DbSession,
    *,
    user_id: str,
    tenant_id: str | None,
    auth_method: str,
    mfa_satisfied: bool,
    user_agent: str | None = None,
    ip_address: str | None = None,
    family_id: str | None = None,
) -> tuple[str, str, Session]:
    """
    Start a session, or continue an existing family after a rotation.

    Returns (access_token, refresh_token, row). The refresh token is returned exactly
    once - only its hash is kept, so a copy of the database is not a set of live
    sessions.
    """
    refresh = new_token()
    row = Session(
        user_id=user_id,
        tenant_id=tenant_id,
        family_id=family_id or new_id(),
        refresh_hash=token_hash(refresh),
        auth_method=auth_method,
        mfa_satisfied=mfa_satisfied,
        expires_at=utcnow() + timedelta(days=REFRESH_TOKEN_DAYS),
        user_agent=(user_agent or "")[:400] or None,
        ip_address=ip_address,
        last_seen_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return access_token(row), refresh, row


def access_token(row: Session) -> str:
    """
    Mint a short-lived bearer token for one session.

    `tid` here is the organisation this session is scoped to. It is put in by the server
    from a membership that was checked at sign-in - it is never read from a request, and
    switching organisation means minting a new token, not editing this one.
    """
    now = utcnow()
    return jwt.encode(
        {
            "sub": row.user_id,
            "tid": row.tenant_id,
            "sid": row.id,
            "fam": row.family_id,
            "amr": row.auth_method,
            "mfa": row.mfa_satisfied,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=ACCESS_TOKEN_MINUTES)).timestamp()),
            "iss": "assarium",
        },
        _signing_key(),
        algorithm=ALGORITHM,
    )


def decode_access(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            _signing_key(),
            algorithms=[ALGORITHM],
            issuer="assarium",
            options={"require": ["exp", "iat", "sub", "sid"], "verify_exp": True},
            leeway=10,
        )
    except jwt.ExpiredSignatureError as exc:
        raise SessionError("Your session has expired. Sign in again.") from exc
    except jwt.PyJWTError as exc:
        raise SessionError("That session token is not valid.") from exc


def rotate(
    db: DbSession,
    refresh_token: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, str, Session]:
    """
    Exchange a refresh token for a new pair.

    A token that has already been rotated must never work again. If one turns up, the
    only explanations are theft or cloning, and both mean every token descended from
    that sign-in is suspect - so the whole family goes. Rotation without this check
    buys almost nothing; the detection is the part that catches a live attack.
    """
    presented = token_hash(refresh_token)
    row = db.execute(
        select(Session).where(Session.refresh_hash == presented)
    ).scalar_one_or_none()

    if row is None:
        raise SessionError("That session is no longer valid. Sign in again.")

    if row.revoked_at is not None or row.rotated_at is not None:
        _revoke_family(db, row.family_id, reason="refresh_token_reuse")
        logger.warning(
            "Refresh token reuse on family %s (user %s) - family revoked",
            row.family_id, row.user_id,
        )
        raise SessionError(
            "This session was already used to sign in elsewhere and has been ended "
            "everywhere as a precaution. Please sign in again."
        )

    if ensure_utc(row.expires_at) <= utcnow():
        raise SessionError("Your session has expired. Sign in again.")

    row.rotated_at = utcnow()
    db.flush()

    return issue(
        db,
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        auth_method=row.auth_method,
        mfa_satisfied=row.mfa_satisfied,
        user_agent=user_agent or row.user_agent,
        ip_address=ip_address or row.ip_address,
        family_id=row.family_id,
    )


def _revoke_rows(db: DbSession, rows: list[Session], *, reason: str) -> int:
    """
    Mark sessions revoked through the ORM rather than with a bulk UPDATE.

    A bulk UPDATE writes to the database but leaves objects already loaded in this
    session holding their old values, so revoking a session and then checking it inside
    the same request reports it as still live. `synchronize_session` is meant to bridge
    that and did not reliably do so here. Loading and mutating is unambiguous, and the
    row counts are tiny - one token family is a handful of rows, and a person has at most
    a few dozen sessions - so there is nothing to optimise away.
    """
    now = utcnow()
    changed = 0
    for row in rows:
        if row.revoked_at is None:
            row.revoked_at = now
            row.revoked_reason = reason
            changed += 1
    db.flush()
    return changed


def _revoke_family(db: DbSession, family_id: str, *, reason: str) -> int:
    rows = list(
        db.execute(select(Session).where(Session.family_id == family_id)).scalars().all()
    )
    return _revoke_rows(db, rows, reason=reason)


def revoke(db: DbSession, session_id: str, *, reason: str = "signed_out") -> None:
    row = db.get(Session, session_id)
    if row is not None:
        _revoke_family(db, row.family_id, reason=reason)


def revoke_all_for_user(
    db: DbSession, user_id: str, *, reason: str, except_family: str | None = None
) -> int:
    """
    End every session a user has.

    Used when a password changes, an MFA factor is removed, or an account is
    deactivated - each of those means any session opened under the old state should stop
    being trusted.
    """
    query = select(Session).where(Session.user_id == user_id)
    if except_family:
        query = query.where(Session.family_id != except_family)
    return _revoke_rows(db, list(db.execute(query).scalars().all()), reason=reason)


def revoke_for_membership(db: DbSession, user_id: str, tenant_id: str, *, reason: str) -> int:
    """
    End a user's sessions for one organisation only.

    Removing somebody from one workspace must not sign them out of another customer's
    workspace they still legitimately belong to.
    """
    rows = list(
        db.execute(
            select(Session).where(
                Session.user_id == user_id, Session.tenant_id == tenant_id
            )
        )
        .scalars()
        .all()
    )
    return _revoke_rows(db, rows, reason=reason)


def active_sessions(db: DbSession, user_id: str) -> list[Session]:
    """What the user sees in "your active sessions", newest first."""
    rows = db.execute(
        select(Session).where(
            Session.user_id == user_id,
            Session.revoked_at.is_(None),
            Session.rotated_at.is_(None),
        )
    ).scalars().all()
    now = utcnow()
    live = [r for r in rows if ensure_utc(r.expires_at) > now]
    return sorted(live, key=lambda r: ensure_utc(r.created_at), reverse=True)


def load_valid(db: DbSession, session_id: str) -> Session:
    """
    Fetch a session that is still allowed to be used.

    Called on the refresh path and wherever an access token's claims are not enough on
    their own. The access token deliberately does not carry a database read, so this is
    the place that notices a revocation.
    """
    row = db.get(Session, session_id)
    if row is None:
        raise SessionError("That session no longer exists.")
    if row.revoked_at is not None:
        raise SessionError("That session has been ended.")
    if ensure_utc(row.expires_at) <= utcnow():
        raise SessionError("Your session has expired. Sign in again.")
    return row
