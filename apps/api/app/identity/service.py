"""
Account operations: signing up, signing in, joining an organisation, leaving one.

Most of this file is edge cases. That is the nature of identity - the happy path is
twenty lines and the incidents all live in the other branches.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.core.errors import AssariumError, ConflictError, NotFoundError, ValidationError
from app.identity import sessions as session_store
from app.identity.credentials import (
    check_password_strength,
    hash_password,
    needs_rehash,
    new_token,
    normalise_recovery,
    recovery_hash,
    token_hash,
    verify_password,
    verify_totp,
)
from app.identity.domains import domain_of, is_public_domain, normalise_email
from app.identity.models import (
    AuthPolicy,
    Identity,
    Invitation,
    JoinRequest,
    MfaFactor,
    User,
    VerifiedDomain,
)
from app.models.base import ensure_utc, utcnow
from app.tenancy.models import ROLES, Tenant, TenantMember, validate_slug

logger = logging.getLogger("assarium.identity")

INVITATION_DAYS = 7

#: Consecutive failures to the wait imposed before the next attempt. Backoff, never a
#: permanent lock: an account anyone can disable by typing a known email address into a
#: login form is a denial-of-service tool aimed at our own customers.
BACKOFF_SCHEDULE = (
    (3, timedelta(seconds=30)),
    (5, timedelta(minutes=2)),
    (8, timedelta(minutes=15)),
    (12, timedelta(minutes=30)),
)

#: Every failed sign-in says exactly this, whether the address is unknown, the password
#: is wrong, or the account is deactivated. Anything more specific turns the endpoint
#: into a way to find out who our customers are.
GENERIC_SIGNIN_FAILURE = "That email address and password do not match."

#: Verified once at import so a miss on the sign-in path still costs a real hash.
_DUMMY_HASH = hash_password("assarium-timing-equaliser-not-a-real-password")


class AuthenticationFailed(AssariumError):
    status_code = 401
    code = "authentication_failed"


class MfaRequired(AssariumError):
    """Credentials were right; a second factor is still outstanding."""

    status_code = 401
    code = "mfa_required"


# ---------------------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------------------


def register(
    db: DbSession, email: str, password: str, display_name: str | None = None
) -> tuple[User, str]:
    """
    Create an unverified user with a password identity.

    Returns the user and a one-time verification token. The caller emails it; we never
    store it in a form that could be replayed out of the database.
    """
    email = normalise_email(email)
    check_password_strength(password, email=email)

    existing = _user_by_email(db, email)
    if existing is not None:
        # Not an error the caller may distinguish. Registration must look identical for
        # a taken and an untaken address, or it enumerates our customer list. The real
        # response is an email to the existing address saying somebody tried.
        raise ConflictError("account_exists")

    user = User(email=email, display_name=display_name or email.split("@")[0])
    db.add(user)
    db.flush()

    token = new_token()
    db.add(
        Identity(
            user_id=user.id,
            kind="password",
            provider_subject=user.id,
            secret_hash=hash_password(password),
        )
    )
    db.add(
        MfaFactor(
            user_id=user.id,
            kind="recovery",
            label="email-verification",
            secret=token_hash(token),
            confirmed_at=None,
        )
    )
    db.flush()
    return user, token


def verify_email(db: DbSession, user: User, token: str) -> None:
    """Mark an address as proved. The verification factor is consumed either way."""
    factor = db.execute(
        select(MfaFactor).where(
            MfaFactor.user_id == user.id,
            MfaFactor.kind == "recovery",
            MfaFactor.label == "email-verification",
            MfaFactor.used_at.is_(None),
        )
    ).scalar_one_or_none()

    if factor is None or factor.secret != token_hash(token):
        raise ValidationError("That verification link is not valid or has been used.")

    factor.used_at = utcnow()
    user.email_verified_at = utcnow()
    db.flush()


# ---------------------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------------------


def authenticate(db: DbSession, email: str, password: str) -> User:
    """
    Check an email and password.

    Every failure raises the same message with the same shape, and an unknown address
    still costs a full password hash so the response time does not give it away.
    """
    try:
        email = normalise_email(email)
    except ValidationError:
        verify_password(password, _DUMMY_HASH)
        raise AuthenticationFailed(GENERIC_SIGNIN_FAILURE) from None

    user = _user_by_email(db, email)
    if user is None:
        verify_password(password, _DUMMY_HASH)
        raise AuthenticationFailed(GENERIC_SIGNIN_FAILURE)

    if user.locked_until and ensure_utc(user.locked_until) > utcnow():
        wait = ensure_utc(user.locked_until) - utcnow()
        raise AuthenticationFailed(
            f"Too many attempts. Try again in {max(1, int(wait.total_seconds()))} seconds."
        )

    identity = db.execute(
        select(Identity).where(
            Identity.user_id == user.id, Identity.kind == "password"
        )
    ).scalar_one_or_none()

    if identity is None or not verify_password(password, identity.secret_hash):
        _record_failure(db, user)
        raise AuthenticationFailed(GENERIC_SIGNIN_FAILURE)

    if not user.is_active:
        # Checked after the password so that a deactivated account is indistinguishable
        # from a wrong password to somebody who does not already know the password.
        _record_failure(db, user)
        raise AuthenticationFailed(GENERIC_SIGNIN_FAILURE)

    if needs_rehash(identity.secret_hash):
        # Free upgrade: the only moment we hold the plaintext is now.
        identity.secret_hash = hash_password(password)

    identity.last_used_at = utcnow()
    _record_success(db, user)
    return user


def _record_failure(db: DbSession, user: User) -> None:
    user.failed_attempts = (user.failed_attempts or 0) + 1
    delay = None
    for threshold, wait in BACKOFF_SCHEDULE:
        if user.failed_attempts >= threshold:
            delay = wait
    if delay is not None:
        user.locked_until = utcnow() + delay
    db.flush()


def _record_success(db: DbSession, user: User) -> None:
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    db.flush()


def change_password(
    db: DbSession, user: User, new_password: str, *, keep_family: str | None = None
) -> None:
    """
    Set a new password and end every other session.

    A password change is usually a response to suspicion. Leaving the old sessions alive
    would mean the person who prompted it keeps their access.
    """
    check_password_strength(new_password, email=user.email)
    identity = db.execute(
        select(Identity).where(Identity.user_id == user.id, Identity.kind == "password")
    ).scalar_one_or_none()

    if identity is None:
        identity = Identity(user_id=user.id, kind="password", provider_subject=user.id)
        db.add(identity)

    identity.secret_hash = hash_password(new_password)
    db.flush()
    session_store.revoke_all_for_user(
        db, user.id, reason="password_changed", except_family=keep_family
    )


# ---------------------------------------------------------------------------------------
# Second factors
# ---------------------------------------------------------------------------------------


def active_factors(db: DbSession, user_id: str, kind: str | None = None) -> list[MfaFactor]:
    query = select(MfaFactor).where(
        MfaFactor.user_id == user_id,
        MfaFactor.confirmed_at.is_not(None),
        MfaFactor.used_at.is_(None),
    )
    if kind:
        query = query.where(MfaFactor.kind == kind)
    factors = list(db.execute(query).scalars().all())
    # The email-verification factor is bookkeeping, not a sign-in factor.
    return [f for f in factors if f.label != "email-verification"]


def has_mfa(db: DbSession, user_id: str) -> bool:
    return any(f.kind in ("totp", "webauthn") for f in active_factors(db, user_id))


def verify_second_factor(db: DbSession, user: User, code: str) -> MfaFactor:
    """
    Accept a TOTP code or a recovery code.

    TOTP first, then recovery. A matched TOTP counter is stored so the same code cannot
    be presented twice inside its window - otherwise a code relayed by a proxy stays
    usable for the rest of its thirty seconds.
    """
    for factor in active_factors(db, user.id, kind="totp"):
        counter = verify_totp(factor.secret, code, last_counter=factor.last_counter)
        if counter is not None:
            factor.last_counter = counter
            factor.last_used_at = utcnow()
            db.flush()
            return factor

    presented = recovery_hash(normalise_recovery(code))
    for factor in active_factors(db, user.id, kind="recovery"):
        if secrets.compare_digest(factor.secret, presented):
            factor.used_at = utcnow()
            factor.last_used_at = utcnow()
            db.flush()
            logger.info("Recovery code consumed for user %s", user.id)
            return factor

    raise AuthenticationFailed("That code is not valid.")


def mfa_is_required(db: DbSession, tenant: Tenant | None, user: User) -> bool:
    """
    Whether this sign-in must present a second factor.

    The organisation decides, not the platform - and existing members get a grace
    window, because switching enforcement on at 9am and locking out a whole customer at
    9:01 is not an upgrade they will thank us for.
    """
    if tenant is None:
        return has_mfa(db, user.id)

    policy = db.execute(
        select(AuthPolicy).where(AuthPolicy.tenant_id == tenant.id)
    ).scalar_one_or_none()

    if policy is None or not policy.require_mfa:
        # Enrolled voluntarily still means it is used. Otherwise enrolling would make
        # an account no safer than not bothering.
        return has_mfa(db, user.id)

    if policy.mfa_grace_until and ensure_utc(policy.mfa_grace_until) > utcnow():
        return has_mfa(db, user.id)
    return True


def check_sign_in_method(db: DbSession, tenant: Tenant | None, user: User, method: str) -> None:
    """
    Refuse password and social sign-in for an organisation that enforces SSO.

    Without this refusal SSO is decorative: the customer believes their conditional
    access policies apply while a member quietly keeps using a password that bypasses
    all of them.
    """
    if tenant is None or method in ("sso", "break_glass"):
        return

    policy = db.execute(
        select(AuthPolicy).where(AuthPolicy.tenant_id == tenant.id)
    ).scalar_one_or_none()

    if policy is None or not policy.enforce_sso:
        return

    if policy.break_glass_user_id == user.id:
        # Deliberate: an expired IdP certificate otherwise locks out every person who
        # could fix it. Logged here and announced to the other owners by the caller.
        logger.warning(
            "Break-glass sign-in by user %s into tenant %s", user.id, tenant.slug
        )
        return

    raise AuthenticationFailed(
        f"{tenant.name} requires single sign-on. Use your company sign-in instead of "
        "a password."
    )


# ---------------------------------------------------------------------------------------
# Organisations
# ---------------------------------------------------------------------------------------


def create_organisation(
    db: DbSession, user: User, name: str, slug: str, *, environment: str = "dev"
) -> Tenant:
    """Start a new workspace with this user as its owner."""
    slug = validate_slug(slug)
    if db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none():
        raise ConflictError(f"The name '{slug}' is already taken.")

    tenant = Tenant(
        slug=slug,
        name=name.strip() or slug,
        catalog=f"assarium_{environment}_{slug}",
        storage_container=f"tenant-{slug.replace('_', '-')}",
    )
    db.add(tenant)
    db.flush()
    db.add(
        TenantMember(
            tenant_id=tenant.id,
            user_id=user.id,
            subject=user.id,
            email=user.email,
            display_name=user.display_name,
            role="owner",
        )
    )
    db.flush()
    return tenant


def memberships_for(db: DbSession, user: User) -> list[TenantMember]:
    return list(
        db.execute(
            select(TenantMember).where(
                TenantMember.user_id == user.id, TenantMember.active.is_(True)
            )
        )
        .scalars()
        .all()
    )


def signup_destination(db: DbSession, user: User) -> dict:
    """
    Where a freshly-verified user should land.

    Four outcomes, and the difference between the last two is the whole reason domain
    verification exists: a *verified* domain means the organisation proved it owns the
    namespace, so admitting the user is safe. An unverified one only means somebody
    typed a matching address.
    """
    existing = memberships_for(db, user)
    if existing:
        return {"action": "existing", "memberships": existing}

    domain = domain_of(user.email)
    if is_public_domain(domain):
        return {"action": "create_organisation", "reason": "public_email_domain"}

    claim = db.execute(
        select(VerifiedDomain).where(VerifiedDomain.domain == domain)
    ).scalar_one_or_none()

    if claim is None:
        return {"action": "create_organisation", "reason": "no_matching_organisation"}

    tenant = db.get(Tenant, claim.tenant_id)
    if tenant is None or not tenant.is_active:
        return {"action": "create_organisation", "reason": "no_matching_organisation"}

    if claim.is_verified and claim.auto_join:
        member = TenantMember(
            tenant_id=tenant.id,
            user_id=user.id,
            subject=user.id,
            email=user.email,
            display_name=user.display_name,
            role="viewer",
        )
        db.add(member)
        db.flush()
        return {"action": "joined", "tenant": tenant, "membership": member}

    request = db.execute(
        select(JoinRequest).where(
            JoinRequest.tenant_id == tenant.id, JoinRequest.user_id == user.id
        )
    ).scalar_one_or_none()
    if request is None:
        request = JoinRequest(tenant_id=tenant.id, user_id=user.id)
        db.add(request)
        db.flush()
    return {"action": "requested", "tenant": tenant, "request": request}


def remove_member(db: DbSession, tenant: Tenant, user_id: str) -> None:
    """
    Take somebody out of an organisation.

    An organisation with no owner cannot be administered or recovered by anybody, so the
    last one cannot leave. Handing ownership over first is a small inconvenience;
    an unrecoverable workspace is a support escalation with no good ending.
    """
    member = db.execute(
        select(TenantMember).where(
            TenantMember.tenant_id == tenant.id, TenantMember.user_id == user_id
        )
    ).scalar_one_or_none()

    if member is None:
        raise NotFoundError("That person is not a member of this workspace.")

    if member.role == "owner" and _owner_count(db, tenant.id) <= 1:
        raise ValidationError(
            f"{member.email or 'This person'} is the only owner of {tenant.name}. "
            "Make somebody else an owner first, otherwise nobody can administer the "
            "workspace."
        )

    member.active = False
    db.flush()
    session_store.revoke_for_membership(
        db, user_id, tenant.id, reason="membership_removed"
    )


def change_role(db: DbSession, tenant: Tenant, user_id: str, role: str) -> TenantMember:
    if role not in ROLES:
        raise ValidationError(f"'{role}' is not a role. Use one of: {', '.join(ROLES)}.")

    member = db.execute(
        select(TenantMember).where(
            TenantMember.tenant_id == tenant.id, TenantMember.user_id == user_id
        )
    ).scalar_one_or_none()
    if member is None:
        raise NotFoundError("That person is not a member of this workspace.")

    if member.role == "owner" and role != "owner" and _owner_count(db, tenant.id) <= 1:
        raise ValidationError(
            f"{tenant.name} would be left without an owner. Promote somebody else first."
        )

    member.role = role
    db.flush()
    return member


def _owner_count(db: DbSession, tenant_id: str) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(TenantMember)
            .where(
                TenantMember.tenant_id == tenant_id,
                TenantMember.role == "owner",
                TenantMember.active.is_(True),
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------------------


def invite(
    db: DbSession, tenant: Tenant, email: str, role: str, invited_by: str | None = None
) -> tuple[Invitation, str]:
    """
    Invite an address into an organisation. Returns the row and a one-time token.

    Re-inviting supersedes the previous invitation rather than leaving two live tokens -
    an admin who re-sends because the first "did not arrive" should not be widening the
    window.
    """
    email = normalise_email(email)
    if role not in ROLES:
        raise ValidationError(f"'{role}' is not a role. Use one of: {', '.join(ROLES)}.")

    already = db.execute(
        select(TenantMember).where(
            TenantMember.tenant_id == tenant.id,
            TenantMember.email == email,
            TenantMember.active.is_(True),
        )
    ).scalar_one_or_none()
    if already is not None:
        raise ConflictError(f"{email} is already a member of {tenant.name}.")

    for previous in db.execute(
        select(Invitation).where(
            Invitation.tenant_id == tenant.id,
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    ).scalars():
        previous.revoked_at = utcnow()

    token = new_token()
    invitation = Invitation(
        tenant_id=tenant.id,
        email=email,
        role=role,
        token_hash=token_hash(token),
        invited_by=invited_by,
        expires_at=utcnow() + timedelta(days=INVITATION_DAYS),
    )
    db.add(invitation)
    db.flush()
    return invitation, token


def accept_invitation(db: DbSession, token: str, user: User) -> TenantMember:
    """
    Turn an invitation into a membership.

    The address is checked rather than trusted: an invitation is addressed to a person,
    and letting anybody holding the link redeem it into their own account turns a
    forwarded email into an access grant.
    """
    invitation = db.execute(
        select(Invitation).where(Invitation.token_hash == token_hash(token))
    ).scalar_one_or_none()

    if invitation is None:
        raise ValidationError("That invitation link is not valid.")
    if invitation.revoked_at is not None:
        raise ValidationError("That invitation was withdrawn.")
    if invitation.accepted_at is not None:
        raise ValidationError("That invitation has already been used.")
    if ensure_utc(invitation.expires_at) <= utcnow():
        raise ValidationError(
            "That invitation has expired. Ask for a new one - they last "
            f"{INVITATION_DAYS} days."
        )
    if normalise_email(user.email) != invitation.email:
        raise ValidationError(
            f"That invitation was sent to {invitation.email}. Sign in as that address "
            "to accept it."
        )

    tenant = db.get(Tenant, invitation.tenant_id)
    if tenant is None or not tenant.is_active:
        raise ValidationError("That workspace is no longer available.")

    member = db.execute(
        select(TenantMember).where(
            TenantMember.tenant_id == tenant.id, TenantMember.user_id == user.id
        )
    ).scalar_one_or_none()

    if member is None:
        member = TenantMember(
            tenant_id=tenant.id,
            user_id=user.id,
            subject=user.id,
            email=user.email,
            display_name=user.display_name,
            role=invitation.role,
        )
        db.add(member)
    else:
        # Re-inviting somebody who was removed reinstates them at the invited role.
        member.active = True
        member.role = invitation.role

    invitation.accepted_at = utcnow()
    db.flush()
    return member


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------


def _user_by_email(db: DbSession, email: str) -> User | None:
    return db.execute(select(User).where(User.email == email)).scalar_one_or_none()
