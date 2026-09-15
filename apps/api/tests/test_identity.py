"""
Accounts, sessions and organisation membership.

The tests worth having here are the ones that describe an incident. A password that
verifies is table stakes; a login form that tells a stranger which email addresses belong
to customers, an organisation left with no owner, or a stolen refresh token that keeps
working - those are the failures that cost something.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError, ValidationError
from app.identity import service
from app.identity import sessions as session_store
from app.identity.credentials import (
    hash_password,
    needs_rehash,
    new_recovery_codes,
    new_totp_secret,
    recovery_hash,
    totp_at,
    verify_password,
    verify_totp,
)
from app.identity.domains import (
    domain_of,
    is_public_domain,
    normalise_email,
    validate_claimable,
    verify_dns_txt,
)
from app.identity.models import AuthPolicy, MfaFactor, User, VerifiedDomain
from app.identity.service import AuthenticationFailed
from app.models import entities as _entities  # noqa: F401 - registers FK targets
from app.models.base import Base, utcnow
from app.tenancy.models import Tenant

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/identity.db")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def db(sessions) -> Session:
    session = sessions()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "secret_key", "test-signing-key-not-a-real-secret-value")
    yield


def make_user(db: Session, email: str = "priya@acme.com", verified: bool = True) -> User:
    user, _ = service.register(db, email, PASSWORD, display_name="Priya")
    if verified:
        user.email_verified_at = utcnow()
        db.flush()
    return user


def make_org(db: Session, user: User, slug: str = "acme") -> Tenant:
    return service.create_organisation(db, user, "Acme REIT", slug)


# =======================================================================================
# Passwords
# =======================================================================================


class TestPasswords:
    def test_a_password_verifies_against_its_own_hash(self):
        assert verify_password(PASSWORD, hash_password(PASSWORD))

    def test_a_wrong_password_does_not(self):
        assert not verify_password("something else entirely", hash_password(PASSWORD))

    def test_two_hashes_of_the_same_password_differ(self):
        """Distinct salts, so identical passwords are not visibly identical at rest."""
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    def test_a_malformed_hash_returns_false_rather_than_raising(self):
        """A caller that must catch exceptions to learn a password was wrong will slip."""
        for broken in ("", "not-a-hash", "scrypt$bad$$", None):
            assert verify_password(PASSWORD, broken) is False

    def test_the_same_password_typed_on_two_keyboards_matches(self):
        """NFKC, so a precomposed and a decomposed accent are the same password."""
        stored = hash_password("passé-partout-secure")   # e + combining acute
        assert verify_password("passé-partout-secure", stored)  # precomposed e-acute

    def test_a_short_password_is_refused_with_advice(self):
        with pytest.raises(ValidationError) as caught:
            service.register(None, "a@b.com", "short")
        assert "12 characters" in str(caught.value)

    def test_a_breach_list_password_is_refused(self):
        with pytest.raises(ValidationError) as caught:
            service.register(None, "a@b.com", "password12345")
        assert "breach" in str(caught.value).lower()

    def test_a_password_containing_the_email_is_refused(self):
        with pytest.raises(ValidationError):
            service.register(None, "priya@acme.com", "priya-priya-priya")

    def test_a_hash_at_current_cost_needs_no_rehash(self):
        assert needs_rehash(hash_password(PASSWORD)) is False

    def test_a_weaker_hash_is_flagged_for_upgrade(self):
        assert needs_rehash("scrypt$n=1024,r=8,p=1$c2FsdA$aGFzaA") is True


# =======================================================================================
# Second factors
# =======================================================================================


class TestTotp:
    def test_a_current_code_verifies(self):
        secret = new_totp_secret()
        now = time.time()
        assert verify_totp(secret, totp_at(secret, int(now // 30)), at=now) is not None

    def test_a_code_from_the_previous_step_still_works(self):
        """One step of tolerance, because clocks drift."""
        secret = new_totp_secret()
        now = time.time()
        code = totp_at(secret, int(now // 30) - 1)
        assert verify_totp(secret, code, at=now) is not None

    def test_a_code_from_long_ago_does_not(self):
        secret = new_totp_secret()
        now = time.time()
        assert verify_totp(secret, totp_at(secret, int(now // 30) - 10), at=now) is None

    def test_a_code_cannot_be_replayed_inside_its_own_window(self):
        """
        Without the counter check a code relayed by a proxy stays usable for the rest of
        its thirty seconds, which is exactly long enough to matter.
        """
        secret = new_totp_secret()
        now = time.time()
        counter = int(now // 30)
        code = totp_at(secret, counter)
        assert verify_totp(secret, code, at=now) == counter
        assert verify_totp(secret, code, at=now, last_counter=counter) is None

    def test_a_non_numeric_code_is_rejected_without_work(self):
        assert verify_totp(new_totp_secret(), "abcdef") is None


class TestRecoveryCodes:
    def test_codes_are_unique(self):
        codes = new_recovery_codes(10)
        assert len(set(codes)) == 10

    def test_codes_avoid_confusable_characters(self):
        """Read off a printout, O/0 and I/1 cause support tickets."""
        for code in new_recovery_codes(20):
            assert not set(code) & set("OIL01AEU")

    def test_formatting_does_not_change_the_hash(self):
        code = new_recovery_codes(1)[0]
        assert recovery_hash(code) == recovery_hash(code.replace("-", "").lower())


# =======================================================================================
# Domains
# =======================================================================================


class TestDomains:
    @pytest.mark.parametrize(
        "domain", ["gmail.com", "outlook.com", "yahoo.co.uk", "proton.me", "mailinator.com"]
    )
    def test_a_public_provider_cannot_be_claimed(self, domain):
        """
        Otherwise one person verifies gmail.com and auto-joins every Gmail user on the
        platform into their organisation.
        """
        assert is_public_domain(domain)
        with pytest.raises(ValidationError) as caught:
            validate_claimable(domain)
        assert "public email provider" in str(caught.value)

    def test_a_company_domain_can_be_claimed(self):
        assert validate_claimable("acme-reit.com") == "acme-reit.com"

    def test_a_leading_at_sign_is_tolerated(self):
        assert validate_claimable("@acme.com") == "acme.com"

    def test_nonsense_is_refused(self):
        for bad in ("not a domain", "acme", "", "-acme.com"):
            with pytest.raises(ValidationError):
                validate_claimable(bad)

    def test_email_normalisation_lowercases_and_trims(self):
        assert normalise_email("  Priya@Acme.COM ") == "priya@acme.com"

    def test_gmail_aliases_are_left_alone(self):
        """
        Two addresses are only the same person if their provider says so. Guessing that
        rule wrong merges two people's accounts, which is unrecoverable.
        """
        assert normalise_email("a.b+x@gmail.com") == "a.b+x@gmail.com"

    def test_an_invalid_address_is_refused(self):
        for bad in ("no-at-sign", "@nothing.com", "two@@at.com", ""):
            with pytest.raises(ValidationError):
                normalise_email(bad)

    def test_the_domain_is_the_part_after_the_at(self):
        assert domain_of("Priya@Acme.com") == "acme.com"

    def test_dns_verification_matches_the_published_record(self):
        token = "abc123"
        published = [f'"assarium-verification={token}"']
        assert verify_dns_txt("acme.com", token, resolver=lambda d: published)

    def test_dns_verification_fails_on_a_different_token(self):
        other = ["assarium-verification=some-other-token"]
        assert not verify_dns_txt("acme.com", "abc123", resolver=lambda d: other)

    def test_an_unreachable_resolver_means_not_yet_verified(self):
        def explode(_domain):
            raise OSError("no network")

        assert verify_dns_txt("acme.com", "abc", resolver=explode) is False


# =======================================================================================
# Registration and sign-in
# =======================================================================================


class TestRegistration:
    def test_a_new_user_starts_unverified(self, db):
        user, token = service.register(db, "priya@acme.com", PASSWORD)
        assert user.is_verified is False
        assert token

    def test_verification_marks_the_address_proved(self, db):
        user, token = service.register(db, "priya@acme.com", PASSWORD)
        service.verify_email(db, user, token)
        assert user.is_verified is True

    def test_a_verification_token_is_single_use(self, db):
        user, token = service.register(db, "priya@acme.com", PASSWORD)
        service.verify_email(db, user, token)
        with pytest.raises(ValidationError):
            service.verify_email(db, user, token)

    def test_a_wrong_verification_token_is_refused(self, db):
        user, _ = service.register(db, "priya@acme.com", PASSWORD)
        with pytest.raises(ValidationError):
            service.verify_email(db, user, "not-the-token")

    def test_registering_a_taken_address_raises_an_opaque_conflict(self, db):
        """
        The message is a code, not prose: the API must answer identically for a taken
        and an untaken address, or signup becomes a way to enumerate our customers.
        """
        service.register(db, "priya@acme.com", PASSWORD)
        with pytest.raises(ConflictError) as caught:
            service.register(db, "priya@acme.com", PASSWORD)
        assert str(caught.value) == "account_exists"

    def test_the_address_is_normalised_before_storage(self, db):
        user, _ = service.register(db, "  Priya@ACME.com ", PASSWORD)
        assert user.email == "priya@acme.com"


class TestSignIn:
    def test_correct_credentials_return_the_user(self, db):
        make_user(db)
        assert service.authenticate(db, "priya@acme.com", PASSWORD).email == "priya@acme.com"

    def test_the_address_is_case_insensitive(self, db):
        make_user(db)
        assert service.authenticate(db, "PRIYA@acme.com", PASSWORD)

    def test_an_unknown_address_and_a_wrong_password_say_the_same_thing(self, db):
        """
        Different messages here turn the login form into a customer list. Both paths
        also run a real hash, so the timing does not give it away either.
        """
        make_user(db)
        with pytest.raises(AuthenticationFailed) as unknown:
            service.authenticate(db, "nobody@acme.com", PASSWORD)
        with pytest.raises(AuthenticationFailed) as wrong:
            service.authenticate(db, "priya@acme.com", "not the password")
        assert str(unknown.value) == str(wrong.value) == service.GENERIC_SIGNIN_FAILURE

    def test_a_deactivated_account_is_indistinguishable_from_a_wrong_password(self, db):
        user = make_user(db)
        user.status = "deactivated"
        db.flush()
        with pytest.raises(AuthenticationFailed) as caught:
            service.authenticate(db, "priya@acme.com", PASSWORD)
        assert str(caught.value) == service.GENERIC_SIGNIN_FAILURE

    def test_a_malformed_address_fails_like_any_other_bad_credential(self, db):
        with pytest.raises(AuthenticationFailed):
            service.authenticate(db, "not-an-email", PASSWORD)

    def test_repeated_failures_impose_a_wait(self, db):
        user = make_user(db)
        for _ in range(3):
            with pytest.raises(AuthenticationFailed):
                service.authenticate(db, "priya@acme.com", "wrong")
        assert user.locked_until is not None

    def test_the_wait_lengthens_with_more_failures(self, db):
        user = make_user(db)
        waits = []
        for attempt in range(1, 9):
            with pytest.raises(AuthenticationFailed):
                service.authenticate(db, "priya@acme.com", "wrong")
            if user.locked_until:
                waits.append((attempt, user.locked_until - utcnow()))
                user.locked_until = None  # let the next attempt through
        assert waits[0][1] < waits[-1][1]

    def test_the_wait_is_never_permanent(self, db):
        """
        A lock any stranger can trigger by typing a known address is a denial-of-service
        aimed at our own customers, so the longest backoff is still finite.
        """
        user = make_user(db)
        for _ in range(20):
            user.locked_until = None
            with pytest.raises(AuthenticationFailed):
                service.authenticate(db, "priya@acme.com", "wrong")
        assert user.locked_until - utcnow() < timedelta(hours=1)

    def test_a_success_clears_the_backoff(self, db):
        user = make_user(db)
        with pytest.raises(AuthenticationFailed):
            service.authenticate(db, "priya@acme.com", "wrong")
        service.authenticate(db, "priya@acme.com", PASSWORD)
        assert user.failed_attempts == 0
        assert user.locked_until is None

    def test_changing_a_password_ends_other_sessions(self, db):
        """A password change is usually a reaction to suspicion."""
        user = make_user(db)
        _, _, row = session_store.issue(
            db, user_id=user.id, tenant_id=None, auth_method="password", mfa_satisfied=False
        )
        service.change_password(db, user, "a-completely-different-phrase")
        db.refresh(row)
        assert row.revoked_at is not None
        assert row.revoked_reason == "password_changed"


# =======================================================================================
# Sessions
# =======================================================================================


class TestSessions:
    def test_issuing_returns_a_usable_access_token(self, db):
        user = make_user(db)
        access, refresh, row = session_store.issue(
            db, user_id=user.id, tenant_id=None, auth_method="password", mfa_satisfied=True
        )
        claims = session_store.decode_access(access)
        assert claims["sub"] == user.id
        assert claims["sid"] == row.id
        assert refresh

    def test_the_raw_refresh_token_is_never_stored(self, db):
        """A copy of the database must not be a set of live sessions."""
        user = make_user(db)
        _, refresh, row = session_store.issue(
            db, user_id=user.id, tenant_id=None, auth_method="password", mfa_satisfied=True
        )
        assert refresh not in row.refresh_hash
        assert row.refresh_hash != refresh

    def test_the_tenant_in_the_token_is_the_one_the_server_put_there(self, db):
        user = make_user(db)
        tenant = make_org(db, user)
        access, _, _ = session_store.issue(
            db, user_id=user.id, tenant_id=tenant.id, auth_method="password", mfa_satisfied=True
        )
        assert session_store.decode_access(access)["tid"] == tenant.id

    def test_rotating_returns_a_new_pair(self, db):
        user = make_user(db)
        _, refresh, _ = session_store.issue(
            db, user_id=user.id, tenant_id=None, auth_method="password", mfa_satisfied=True
        )
        _, rotated, _ = session_store.rotate(db, refresh)
        assert rotated != refresh

    def test_rotation_keeps_the_family(self, db):
        user = make_user(db)
        _, refresh, first = session_store.issue(
            db, user_id=user.id, tenant_id=None, auth_method="password", mfa_satisfied=True
        )
        _, _, second = session_store.rotate(db, refresh)
        assert second.family_id == first.family_id

    def test_replaying_a_rotated_token_revokes_the_whole_family(self, db):
        """
        The point of rotation. A token that has already been exchanged turning up again
        means it was stolen or cloned, and every token descended from that sign-in is
        suspect - so all of them go, not just this one.
        """
        user = make_user(db)
        _, refresh, first = session_store.issue(
            db, user_id=user.id, tenant_id=None, auth_method="password", mfa_satisfied=True
        )
        _, live, second = session_store.rotate(db, refresh)

        with pytest.raises(session_store.SessionError) as caught:
            session_store.rotate(db, refresh)
        assert "sign in elsewhere" in str(caught.value)

        db.refresh(first)
        db.refresh(second)
        assert first.revoked_at is not None
        assert second.revoked_at is not None, "the still-live token must go too"

        with pytest.raises(session_store.SessionError):
            session_store.rotate(db, live)

    def test_an_unknown_refresh_token_is_refused(self, db):
        with pytest.raises(session_store.SessionError):
            session_store.rotate(db, "not-a-real-token")

    def test_an_expired_session_cannot_be_refreshed(self, db):
        user = make_user(db)
        _, refresh, row = session_store.issue(
            db, user_id=user.id, tenant_id=None, auth_method="password", mfa_satisfied=True
        )
        row.expires_at = utcnow() - timedelta(minutes=1)
        db.flush()
        with pytest.raises(session_store.SessionError):
            session_store.rotate(db, refresh)

    def test_a_revoked_session_cannot_be_loaded(self, db):
        user = make_user(db)
        _, _, row = session_store.issue(
            db, user_id=user.id, tenant_id=None, auth_method="password", mfa_satisfied=True
        )
        session_store.revoke(db, row.id)
        with pytest.raises(session_store.SessionError):
            session_store.load_valid(db, row.id)

    def test_leaving_one_organisation_does_not_sign_you_out_of_another(self, db):
        """A consultant removed from one customer keeps working for the others."""
        user = make_user(db)
        acme = make_org(db, user, "acme")
        globex = make_org(db, user, "globex")

        _, _, acme_session = session_store.issue(
            db, user_id=user.id, tenant_id=acme.id, auth_method="password", mfa_satisfied=True
        )
        _, _, globex_session = session_store.issue(
            db, user_id=user.id, tenant_id=globex.id, auth_method="password", mfa_satisfied=True
        )

        session_store.revoke_for_membership(db, user.id, acme.id, reason="removed")
        db.refresh(acme_session)
        db.refresh(globex_session)
        assert acme_session.revoked_at is not None
        assert globex_session.revoked_at is None

    def test_active_sessions_exclude_rotated_and_revoked_ones(self, db):
        user = make_user(db)
        _, refresh, _ = session_store.issue(
            db, user_id=user.id, tenant_id=None, auth_method="password", mfa_satisfied=True
        )
        session_store.rotate(db, refresh)
        assert len(session_store.active_sessions(db, user.id)) == 1

    def test_an_expired_access_token_is_rejected(self, db, monkeypatch):
        user = make_user(db)
        monkeypatch.setattr(session_store, "ACCESS_TOKEN_MINUTES", -1)
        access, _, _ = session_store.issue(
            db, user_id=user.id, tenant_id=None, auth_method="password", mfa_satisfied=True
        )
        with pytest.raises(session_store.SessionError):
            session_store.decode_access(access)

    def test_a_token_signed_with_another_key_is_rejected(self, db, monkeypatch):
        from app.core.config import get_settings

        user = make_user(db)
        access, _, _ = session_store.issue(
            db, user_id=user.id, tenant_id=None, auth_method="password", mfa_satisfied=True
        )
        monkeypatch.setattr(get_settings(), "secret_key", "a-different-key-entirely")
        with pytest.raises(session_store.SessionError):
            session_store.decode_access(access)


# =======================================================================================
# Organisations and membership
# =======================================================================================


class TestOrganisations:
    def test_the_creator_becomes_the_owner(self, db):
        user = make_user(db)
        tenant = make_org(db, user)
        member = service.memberships_for(db, user)[0]
        assert member.tenant_id == tenant.id
        assert member.role == "owner"

    def test_the_catalog_and_container_come_from_the_slug(self, db):
        tenant = make_org(db, make_user(db), "acme")
        assert tenant.catalog == "assarium_dev_acme"
        assert tenant.storage_container == "tenant-acme"

    def test_a_taken_slug_is_refused(self, db):
        make_org(db, make_user(db), "acme")
        with pytest.raises(ConflictError):
            make_org(db, make_user(db, "raj@other.com"), "acme")

    def test_a_person_can_belong_to_several_organisations(self, db):
        user = make_user(db)
        make_org(db, user, "acme")
        make_org(db, user, "globex")
        assert len(service.memberships_for(db, user)) == 2

    def test_the_last_owner_cannot_be_removed(self, db):
        """
        An organisation with no owner cannot be administered or recovered by anybody.
        Refusing here is a small inconvenience; the alternative is a support escalation
        with no good ending.
        """
        user = make_user(db)
        tenant = make_org(db, user)
        with pytest.raises(ValidationError) as caught:
            service.remove_member(db, tenant, user.id)
        assert "only owner" in str(caught.value)

    def test_an_owner_can_leave_once_there_is_another(self, db):
        first = make_user(db)
        tenant = make_org(db, first)
        second = make_user(db, "raj@acme.com")
        service.invite(db, tenant, second.email, "owner")
        _, token = service.invite(db, tenant, second.email, "owner")
        service.accept_invitation(db, token, second)

        service.remove_member(db, tenant, first.id)
        assert service.memberships_for(db, first) == []

    def test_the_last_owner_cannot_be_demoted(self, db):
        user = make_user(db)
        tenant = make_org(db, user)
        with pytest.raises(ValidationError) as caught:
            service.change_role(db, tenant, user.id, "viewer")
        assert "without an owner" in str(caught.value)

    def test_removal_ends_that_workspace_session_immediately(self, db):
        """Not at token expiry - access should stop when the decision is made."""
        owner = make_user(db)
        tenant = make_org(db, owner)
        other = make_user(db, "raj@acme.com")
        _, token = service.invite(db, tenant, other.email, "analyst")
        service.accept_invitation(db, token, other)

        _, _, row = session_store.issue(
            db, user_id=other.id, tenant_id=tenant.id,
            auth_method="password", mfa_satisfied=True,
        )
        service.remove_member(db, tenant, other.id)
        db.refresh(row)
        assert row.revoked_at is not None

    def test_an_unknown_role_is_refused(self, db):
        user = make_user(db)
        tenant = make_org(db, user)
        with pytest.raises(ValidationError):
            service.change_role(db, tenant, user.id, "superuser")


# =======================================================================================
# Invitations
# =======================================================================================


class TestInvitations:
    def test_an_invitation_creates_a_membership_at_the_invited_role(self, db):
        tenant = make_org(db, make_user(db))
        guest = make_user(db, "raj@acme.com")
        _, token = service.invite(db, tenant, "raj@acme.com", "analyst")
        member = service.accept_invitation(db, token, guest)
        assert member.role == "analyst"
        assert member.tenant_id == tenant.id

    def test_an_invitation_is_single_use(self, db):
        tenant = make_org(db, make_user(db))
        guest = make_user(db, "raj@acme.com")
        _, token = service.invite(db, tenant, "raj@acme.com", "analyst")
        service.accept_invitation(db, token, guest)
        with pytest.raises(ValidationError) as caught:
            service.accept_invitation(db, token, guest)
        assert "already been used" in str(caught.value)

    def test_only_the_addressed_person_can_accept(self, db):
        """
        Otherwise a forwarded email is an access grant, and the invitation model stops
        meaning anything.
        """
        tenant = make_org(db, make_user(db))
        _, token = service.invite(db, tenant, "raj@acme.com", "analyst")
        interloper = make_user(db, "someone.else@acme.com")
        with pytest.raises(ValidationError) as caught:
            service.accept_invitation(db, token, interloper)
        assert "raj@acme.com" in str(caught.value)

    def test_an_expired_invitation_is_refused_with_the_window(self, db):
        tenant = make_org(db, make_user(db))
        guest = make_user(db, "raj@acme.com")
        invitation, token = service.invite(db, tenant, "raj@acme.com", "analyst")
        invitation.expires_at = utcnow() - timedelta(minutes=1)
        db.flush()
        with pytest.raises(ValidationError) as caught:
            service.accept_invitation(db, token, guest)
        assert "7 days" in str(caught.value)

    def test_re_inviting_invalidates_the_previous_token(self, db):
        """
        An admin re-sending because the first "did not arrive" should not be widening
        the window - two live tokens is one more than anybody intended.
        """
        tenant = make_org(db, make_user(db))
        guest = make_user(db, "raj@acme.com")
        _, first = service.invite(db, tenant, "raj@acme.com", "viewer")
        _, second = service.invite(db, tenant, "raj@acme.com", "analyst")

        with pytest.raises(ValidationError) as caught:
            service.accept_invitation(db, first, guest)
        assert "withdrawn" in str(caught.value)
        assert service.accept_invitation(db, second, guest).role == "analyst"

    def test_inviting_an_existing_member_is_refused(self, db):
        owner = make_user(db)
        tenant = make_org(db, owner)
        with pytest.raises(ConflictError):
            service.invite(db, tenant, owner.email, "analyst")

    def test_re_inviting_a_removed_person_reinstates_them(self, db):
        owner = make_user(db)
        tenant = make_org(db, owner)
        guest = make_user(db, "raj@acme.com")
        _, token = service.invite(db, tenant, "raj@acme.com", "analyst")
        service.accept_invitation(db, token, guest)
        service.remove_member(db, tenant, guest.id)

        _, again = service.invite(db, tenant, "raj@acme.com", "viewer")
        member = service.accept_invitation(db, again, guest)
        assert member.active is True
        assert member.role == "viewer"

    def test_a_garbage_token_is_refused(self, db):
        guest = make_user(db, "raj@acme.com")
        with pytest.raises(ValidationError):
            service.accept_invitation(db, "made-up", guest)


# =======================================================================================
# Where a new signup lands
# =======================================================================================


class TestSignupDestination:
    def test_a_personal_address_is_asked_to_create_an_organisation(self, db):
        user = make_user(db, "someone@gmail.com")
        outcome = service.signup_destination(db, user)
        assert outcome["action"] == "create_organisation"
        assert outcome["reason"] == "public_email_domain"

    def test_an_unrecognised_company_domain_creates_an_organisation(self, db):
        user = make_user(db, "priya@brand-new-reit.com")
        assert service.signup_destination(db, user)["action"] == "create_organisation"

    def test_a_verified_domain_admits_the_user(self, db):
        owner = make_user(db, "owner@acme.com")
        tenant = make_org(db, owner)
        db.add(VerifiedDomain(
            tenant_id=tenant.id, domain="acme.com",
            verification_token="t", verified_at=utcnow(), auto_join=True,
        ))
        db.flush()

        joiner = make_user(db, "raj@acme.com")
        outcome = service.signup_destination(db, joiner)
        assert outcome["action"] == "joined"
        assert outcome["membership"].role == "viewer"

    def test_an_unverified_domain_only_gets_a_request(self, db):
        """
        The whole reason domain verification exists. An unverified claim means somebody
        typed a matching address, not that they own the namespace.
        """
        owner = make_user(db, "owner@acme.com")
        tenant = make_org(db, owner)
        db.add(VerifiedDomain(
            tenant_id=tenant.id, domain="acme.com", verification_token="t", verified_at=None
        ))
        db.flush()

        joiner = make_user(db, "raj@acme.com")
        outcome = service.signup_destination(db, joiner)
        assert outcome["action"] == "requested"
        assert service.memberships_for(db, joiner) == []

    def test_a_verified_domain_with_auto_join_off_only_gets_a_request(self, db):
        owner = make_user(db, "owner@acme.com")
        tenant = make_org(db, owner)
        db.add(VerifiedDomain(
            tenant_id=tenant.id, domain="acme.com", verification_token="t",
            verified_at=utcnow(), auto_join=False,
        ))
        db.flush()
        outcome = service.signup_destination(db, make_user(db, "raj@acme.com"))
        assert outcome["action"] == "requested"

    def test_asking_twice_does_not_create_two_requests(self, db):
        owner = make_user(db, "owner@acme.com")
        tenant = make_org(db, owner)
        db.add(VerifiedDomain(
            tenant_id=tenant.id, domain="acme.com", verification_token="t", verified_at=None
        ))
        db.flush()
        joiner = make_user(db, "raj@acme.com")
        first = service.signup_destination(db, joiner)["request"]
        second = service.signup_destination(db, joiner)["request"]
        assert first.id == second.id

    def test_a_suspended_organisation_does_not_absorb_new_signups(self, db):
        owner = make_user(db, "owner@acme.com")
        tenant = make_org(db, owner)
        tenant.status = "suspended"
        db.add(VerifiedDomain(
            tenant_id=tenant.id, domain="acme.com", verification_token="t",
            verified_at=utcnow(), auto_join=True,
        ))
        db.flush()
        assert service.signup_destination(db, make_user(db, "raj@acme.com"))["action"] == (
            "create_organisation"
        )

    def test_an_existing_member_is_sent_to_their_workspace(self, db):
        user = make_user(db)
        make_org(db, user)
        assert service.signup_destination(db, user)["action"] == "existing"


# =======================================================================================
# Organisation sign-in policy
# =======================================================================================


def policy_for(db: Session, tenant: Tenant, **kwargs) -> AuthPolicy:
    policy = AuthPolicy(tenant_id=tenant.id, **kwargs)
    db.add(policy)
    db.flush()
    return policy


def enrol_totp(db: Session, user: User) -> MfaFactor:
    factor = MfaFactor(
        user_id=user.id, kind="totp", label="phone",
        secret=new_totp_secret(), confirmed_at=utcnow(),
    )
    db.add(factor)
    db.flush()
    return factor


class TestAuthPolicy:
    def test_with_no_policy_an_enrolled_factor_is_still_used(self, db):
        """Otherwise enrolling voluntarily leaves the account no safer."""
        user = make_user(db)
        tenant = make_org(db, user)
        assert service.mfa_is_required(db, tenant, user) is False
        enrol_totp(db, user)
        assert service.mfa_is_required(db, tenant, user) is True

    def test_enforcement_requires_a_factor_from_everyone(self, db):
        user = make_user(db)
        tenant = make_org(db, user)
        policy_for(db, tenant, require_mfa=True)
        assert service.mfa_is_required(db, tenant, user) is True

    def test_the_grace_window_gives_existing_members_time(self, db):
        """Switching enforcement on at 9am and locking out a customer at 9:01 is not
        an upgrade they will thank us for."""
        user = make_user(db)
        tenant = make_org(db, user)
        policy_for(db, tenant, require_mfa=True, mfa_grace_until=utcnow() + timedelta(days=7))
        assert service.mfa_is_required(db, tenant, user) is False

    def test_an_expired_grace_window_enforces(self, db):
        user = make_user(db)
        tenant = make_org(db, user)
        policy_for(db, tenant, require_mfa=True, mfa_grace_until=utcnow() - timedelta(days=1))
        assert service.mfa_is_required(db, tenant, user) is True

    def test_sso_enforcement_refuses_a_password(self, db):
        """
        Without this the customer believes their conditional access applies while a
        member quietly keeps signing in with a password that bypasses all of it.
        """
        user = make_user(db)
        tenant = make_org(db, user)
        policy_for(db, tenant, enforce_sso=True)
        with pytest.raises(AuthenticationFailed) as caught:
            service.check_sign_in_method(db, tenant, user, "password")
        assert "single sign-on" in str(caught.value)

    def test_sso_enforcement_permits_sso(self, db):
        user = make_user(db)
        tenant = make_org(db, user)
        policy_for(db, tenant, enforce_sso=True)
        service.check_sign_in_method(db, tenant, user, "sso")

    def test_the_break_glass_owner_keeps_a_way_in(self, db):
        """
        An expired IdP certificate otherwise locks out every person who could fix it,
        admins included. One named owner keeps password access, and its use is logged.
        """
        user = make_user(db)
        tenant = make_org(db, user)
        policy_for(db, tenant, enforce_sso=True, break_glass_user_id=user.id)
        service.check_sign_in_method(db, tenant, user, "password")

    def test_break_glass_applies_to_one_person_only(self, db):
        owner = make_user(db)
        tenant = make_org(db, owner)
        other = make_user(db, "raj@acme.com")
        policy_for(db, tenant, enforce_sso=True, break_glass_user_id=owner.id)
        with pytest.raises(AuthenticationFailed):
            service.check_sign_in_method(db, tenant, other, "password")

    def test_no_policy_permits_any_method(self, db):
        user = make_user(db)
        tenant = make_org(db, user)
        service.check_sign_in_method(db, tenant, user, "password")


class TestSecondFactorFlow:
    def test_a_valid_totp_code_is_accepted(self, db):
        user = make_user(db)
        factor = enrol_totp(db, user)
        code = totp_at(factor.secret, int(time.time() // 30))
        assert service.verify_second_factor(db, user, code).id == factor.id

    def test_the_same_totp_code_cannot_be_used_twice(self, db):
        user = make_user(db)
        factor = enrol_totp(db, user)
        code = totp_at(factor.secret, int(time.time() // 30))
        service.verify_second_factor(db, user, code)
        with pytest.raises(AuthenticationFailed):
            service.verify_second_factor(db, user, code)

    def test_a_recovery_code_works_once(self, db):
        user = make_user(db)
        code = new_recovery_codes(1)[0]
        db.add(MfaFactor(
            user_id=user.id, kind="recovery", secret=recovery_hash(code),
            confirmed_at=utcnow(),
        ))
        db.flush()
        service.verify_second_factor(db, user, code)
        with pytest.raises(AuthenticationFailed):
            service.verify_second_factor(db, user, code)

    def test_a_recovery_code_is_accepted_however_it_is_typed(self, db):
        user = make_user(db)
        code = new_recovery_codes(1)[0]
        db.add(MfaFactor(
            user_id=user.id, kind="recovery", secret=recovery_hash(code),
            confirmed_at=utcnow(),
        ))
        db.flush()
        assert service.verify_second_factor(db, user, code.lower().replace("-", " "))

    def test_an_unconfirmed_factor_does_not_count(self, db):
        """Half-finished enrolment must not be mistaken for protection."""
        user = make_user(db)
        db.add(MfaFactor(user_id=user.id, kind="totp", secret=new_totp_secret()))
        db.flush()
        assert service.has_mfa(db, user.id) is False

    def test_the_email_verification_factor_is_not_a_sign_in_factor(self, db):
        user, _ = service.register(db, "priya@acme.com", PASSWORD)
        assert service.has_mfa(db, user.id) is False
        assert service.active_factors(db, user.id) == []

    def test_a_wrong_code_is_refused(self, db):
        user = make_user(db)
        enrol_totp(db, user)
        with pytest.raises(AuthenticationFailed):
            service.verify_second_factor(db, user, "000000")
