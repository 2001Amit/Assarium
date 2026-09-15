"""
Password hashing, one-time codes, and opaque token handling.

Everything here is stdlib. That is deliberate: the alternative was another native wheel
to install, and `hashlib.scrypt` is a memory-hard KDF that OWASP lists as an acceptable
choice. Nothing in this file rolls its own primitive - scrypt, HMAC-SHA1 (RFC 6238) and
`secrets` are all used as specified.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import unicodedata

# ---------------------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------------------

#: scrypt cost. n=2**16 with r=8, p=1 costs roughly 64 MiB per hash.
#:
#: OWASP suggests n=2**17. We sit one notch below on purpose: a sign-in endpoint that
#: allocates 128 MiB per attempt is itself a denial-of-service surface, and an attacker
#: does not need a valid account to make us do it. The backoff in `app.identity.service`
#: is what limits guessing; this parameter limits how expensive an *unauthenticated*
#: request can be. The cost is recorded in each hash, so raising it later re-hashes
#: users on their next sign-in instead of invalidating every password.
SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

#: Long passphrases beat short complex ones, which is also what NIST 800-63B says. The
#: only hard rule is a floor; there is no forced-rotation and no character-class theatre.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

#: Passwords seen so often that a per-account rate limit does not help: an attacker
#: sprays one of these across many accounts rather than many guesses at one account.
COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "passw0rd", "welcome1", "welcome123",
    "qwerty123", "letmein123", "changeme", "iloveyou", "admin123", "administrator",
    "123456789012", "1234567890123", "abcdefghijkl", "qwertyuiop12",
    # Twelve characters or more, so the length floor does not catch these. These are the
    # ones that matter: an attacker sprays a handful of them across many accounts, which
    # a per-account backoff does nothing about.
    "password1234", "password12345", "welcome123456", "qwerty123456",
    "letmein123456", "trustno1234567", "iloveyou1234",
    "monkey123456", "dragon123456", "football12345", "baseball12345",
    "sunshine12345", "princess12345", "superman12345", "starwars12345",
})


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def normalise_password(password: str) -> str:
    """
    NFKC-normalise so a password typed on a different keyboard still matches.

    Without this, an accented character entered as one code point on a Mac and as two on
    Windows hashes differently, and the user is told their correct password is wrong.
    """
    return unicodedata.normalize("NFKC", password)


def check_password_strength(password: str, email: str | None = None) -> None:
    """
    Raise if the password is unusable. Length first, then the things length cannot fix.
    """
    from app.core.errors import ValidationError

    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"Passwords need at least {MIN_PASSWORD_LENGTH} characters. "
            "A short phrase you will remember beats a short jumble you will not."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        # Not a strength rule - an unbounded input to a memory-hard KDF is a way to
        # make the server do arbitrary work.
        raise ValidationError("That password is too long.")

    lowered = password.lower()
    if lowered in COMMON_PASSWORDS:
        raise ValidationError(
            "That password appears on every breach list there is. Please pick another."
        )
    if email:
        local = email.split("@")[0].lower()
        if len(local) >= 4 and local in lowered:
            raise ValidationError(
                "That password contains your email address, which is the first thing "
                "anyone guessing would try."
            )


def hash_password(password: str) -> str:
    """Derive a storable hash. The format carries its own parameters."""
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        normalise_password(password).encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_N * SCRYPT_R * 256,
    )
    return f"scrypt$n={SCRYPT_N},r={SCRYPT_R},p={SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, stored: str | None) -> bool:
    """
    Check a password against a stored hash.

    Returns False rather than raising for anything malformed: a caller that has to catch
    exceptions to know a password was wrong will eventually get that wrong.
    """
    if not stored:
        return False
    try:
        scheme, params, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        parsed = dict(part.split("=") for part in params.split(","))
        derived = hashlib.scrypt(
            normalise_password(password).encode(),
            salt=_unb64(salt_b64),
            n=int(parsed["n"]),
            r=int(parsed["r"]),
            p=int(parsed["p"]),
            dklen=len(_unb64(hash_b64)),
            maxmem=int(parsed["n"]) * int(parsed["r"]) * 256,
        )
    except (ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(derived, _unb64(hash_b64))


def needs_rehash(stored: str | None) -> bool:
    """True when a hash was made with weaker parameters than we now use."""
    if not stored or not stored.startswith("scrypt$"):
        return True
    try:
        parsed = dict(p.split("=") for p in stored.split("$")[1].split(","))
        return int(parsed["n"]) < SCRYPT_N
    except (ValueError, KeyError, IndexError):
        return True


# ---------------------------------------------------------------------------------------
# Opaque tokens: sessions, invitations, verification links
# ---------------------------------------------------------------------------------------

TOKEN_BYTES = 32


def new_token() -> str:
    """A high-entropy token to hand out. Only ever returned once."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """
    What gets stored.

    Plain SHA-256, not scrypt: these tokens are 256 bits of randomness, so there is no
    guessing attack for a slow hash to frustrate, and refresh happens often enough that
    a memory-hard hash on the hot path would be felt. Storing the hash means a leaked
    database still does not contain a usable session.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_match(presented: str, stored_hash: str) -> bool:
    return hmac.compare_digest(token_hash(presented), stored_hash)


# ---------------------------------------------------------------------------------------
# TOTP (RFC 6238) and recovery codes
# ---------------------------------------------------------------------------------------

TOTP_DIGITS = 6
TOTP_PERIOD = 30
#: One step either side, to tolerate clock drift. Wider than this starts to matter: each
#: extra step is another code an attacker may relay.
TOTP_WINDOW = 1


def new_totp_secret() -> str:
    """A base32 secret, which is what authenticator apps accept."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_uri(secret: str, email: str, issuer: str = "Assarium") -> str:
    """The otpauth:// URI an authenticator app reads from a QR code."""
    from urllib.parse import quote

    label = quote(f"{issuer}:{email}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}"
    )


def totp_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**TOTP_DIGITS)).zfill(TOTP_DIGITS)


def verify_totp(
    secret: str, code: str, at: float | None = None, last_counter: int | None = None
) -> int | None:
    """
    Check a TOTP code, returning the counter it matched so the caller can store it.

    `last_counter` blocks replay inside the same window: without it, a code shoulder-
    surfed or relayed by a proxy stays usable for the rest of its thirty seconds.
    """
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return None

    now = int((at if at is not None else time.time()) // TOTP_PERIOD)
    for drift in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        counter = now + drift
        if last_counter is not None and counter <= last_counter:
            continue
        if hmac.compare_digest(totp_at(secret, counter), code):
            return counter
    return None


RECOVERY_CODE_COUNT = 10
RECOVERY_GROUP = 5


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """
    Human-transcribable codes: no vowels, so no accidental words, and no characters
    that get confused when read off a printout.
    """
    alphabet = "BCDFGHJKMNPQRTVWXY346789"
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(alphabet) for _ in range(RECOVERY_GROUP * 2))
        codes.append(f"{raw[:RECOVERY_GROUP]}-{raw[RECOVERY_GROUP:]}")
    return codes


def recovery_hash(code: str) -> str:
    """Recovery codes are stored hashed, and normalised so formatting never rejects one."""
    return hashlib.sha256(normalise_recovery(code).encode()).hexdigest()


def normalise_recovery(code: str) -> str:
    return code.strip().upper().replace("-", "").replace(" ", "")
