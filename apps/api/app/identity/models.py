"""
Who a person is, which organisations they belong to, and how they proved it.

The shape here is the one B2B platforms converge on, and the separation is the point:

    User          a person, globally unique by verified email
    Identity      one way that person proves who they are - they may have several
    Tenant        the customer organisation (defined in app.tenancy.models)
    TenantMember  that person's membership of one organisation, with a role
    Session       a refresh-token family, revocable, scoped to one organisation

A user is not a tenant and an email domain is not a tenant. An organisation is. A
consultant working for three customers is one user with three memberships, and moving
between them is an explicit act that mints a new session - never a request header.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedBase

#: How a user proved who they are. Stored on the session so that policy can be applied
#: after the fact - an organisation that later enforces SSO can reject sessions that were
#: established with a password.
IDENTITY_KINDS = ("password", "google", "microsoft", "sso", "break_glass")

#: Second factors, in the order the product should offer them. WebAuthn is origin-bound
#: and therefore phishing-resistant; TOTP is not, and is offered only as a fallback while
#: passkey support is still uneven. SMS is deliberately absent - offering it means an
#: attacker simply chooses it.
MFA_KINDS = ("webauthn", "totp", "recovery")


class User(TimestampedBase):
    """
    A person. One row per human, no matter how many organisations they work with.

    `email` is the identity key across sign-in methods: somebody invited by email who
    later arrives through their employer's SSO must land on *this* row rather than a
    second account holding half their history.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)

    email: Mapped[str] = mapped_column(String(320), index=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # "active" | "deactivated". Deactivated users keep their rows: audit history and
    # authorship stay attributed to a real person rather than becoming orphaned.
    status: Mapped[str] = mapped_column(String(20), default="active")

    # Consecutive failed sign-ins, and how long the next attempt must wait. Backoff
    # rather than a hard lock: a lock that any stranger can trigger by typing a known
    # email address is a denial-of-service tool, not a defence.
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    identities: Mapped[list[Identity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    mfa_factors: Mapped[list[MfaFactor]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class Identity(TimestampedBase):
    """
    One credential belonging to a user.

    Separate from `User` because a person legitimately has several: a password from the
    trial, Google from when they were in a hurry, and their employer's SSO once the
    organisation upgrades. All three are the same person, and revoking one must not
    revoke the others.
    """

    __tablename__ = "identities"
    __table_args__ = (
        # One identity per (kind, provider subject). Two people cannot share a Google
        # account, and one person cannot hold two password rows.
        UniqueConstraint("kind", "provider_subject", name="uq_identity_provider"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))

    #: For "password" this is the user id. For federated kinds it is the provider's own
    #: stable subject claim - never the email, which changes.
    provider_subject: Mapped[str] = mapped_column(String(255), index=True)

    #: Only ever a derived hash, never a password. Null for federated identities.
    secret_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: For "sso", the organisation whose IdP issued it. An SSO identity is meaningful
    #: only inside the organisation that configured it.
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="identities")


class MfaFactor(TimestampedBase):
    """
    A second factor enrolled by a user.

    Recovery codes are stored here too, one row per code, so that using one can consume
    exactly that code. Keeping them as a single blob would mean either re-writing the
    whole set on each use or being unable to tell which were spent.
    """

    __tablename__ = "mfa_factors"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)

    #: TOTP shared secret, WebAuthn credential material, or a recovery code hash.
    #: Never a value that can be replayed as-is for the recovery kind.
    secret: Mapped[str] = mapped_column(String(512))

    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Guards against a replayed TOTP code inside the same time step.
    last_counter: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped[User] = relationship(back_populates="mfa_factors")

    @property
    def is_active(self) -> bool:
        """A factor counts once it is confirmed and, for recovery codes, unspent."""
        return self.confirmed_at is not None and self.used_at is None


class Session(TimestampedBase):
    """
    A refresh-token family for one user in one organisation.

    Only a hash of the refresh token is stored. Every refresh rotates the token and
    invalidates its predecessor; presenting a rotated token again means it was stolen or
    cloned, so the whole family is revoked at once. Rotation without that detection
    buys very little - it is the detection that catches an active attack.
    """

    __tablename__ = "sessions"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )

    #: Every token descended from one sign-in shares this. Revocation works on the
    #: family, so a stolen token cannot be exchanged for a fresh one.
    family_id: Mapped[str] = mapped_column(String(32), index=True)

    refresh_hash: Mapped[str] = mapped_column(String(128), index=True)

    #: How the user signed in. Kept so a later SSO-enforcement change can invalidate
    #: sessions that were established by password.
    auth_method: Mapped[str] = mapped_column(String(20), default="password")
    mfa_satisfied: Mapped[bool] = mapped_column(Boolean, default=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)

    #: Shown in the "your sessions" list so a person can recognise and end their own.
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Invitation(TimestampedBase):
    """
    An offer to join one organisation, addressed to an email.

    Single-use and time-limited. Re-inviting the same address supersedes the previous
    token rather than leaving two valid ways in.
    """

    __tablename__ = "invitations"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(20), default="viewer")

    token_hash: Mapped[str] = mapped_column(String(128), index=True)
    invited_by: Mapped[str | None] = mapped_column(String(32), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class VerifiedDomain(TimestampedBase):
    """
    An email domain an organisation has proved it controls.

    Proof is a DNS TXT record, not a message to a mailbox: control of one address is not
    control of the domain. Until the record is seen, a matching signup gets
    request-to-join rather than silent admission - the gap between those two is the
    invite-impersonation attack that has hit other platforms.
    """

    __tablename__ = "verified_domains"
    __table_args__ = (UniqueConstraint("domain", name="uq_verified_domain"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    domain: Mapped[str] = mapped_column(String(255), index=True)

    verification_token: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Whether a matching signup joins immediately or has to be approved. Only
    #: consulted once the domain is actually verified.
    auto_join: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None


class JoinRequest(TimestampedBase):
    """Someone with a matching but unverified domain asking to be let in."""

    __tablename__ = "join_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_join_request"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AuthPolicy(TimestampedBase):
    """
    One organisation's rules about how its people may sign in.

    Modelled on Snowflake's authentication policies: the organisation decides, not the
    platform, because the platform cannot know which customers are ready.
    """

    __tablename__ = "auth_policies"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_auth_policy_tenant"),)

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )

    require_mfa: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Existing members get until this moment to enrol before they are made to.
    mfa_grace_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Once true, password and social sign-in are refused for this organisation's
    #: members. Without that refusal, SSO is decorative: the customer believes their
    #: conditional access applies while a member quietly keeps using a password.
    enforce_sso: Mapped[bool] = mapped_column(Boolean, default=False)

    #: One owner may keep password + MFA access when SSO is enforced. An expired IdP
    #: certificate otherwise locks out every person who could fix it, admins included.
    #: Its use is logged and announced to the other owners.
    break_glass_user_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    allowed_domains: Mapped[list[Any]] = mapped_column(JSON, default=list)
    session_max_hours: Mapped[int] = mapped_column(Integer, default=720)
