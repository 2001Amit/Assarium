from __future__ import annotations

import re
from collections.abc import Sequence

from app.profiling.types import PiiFinding, PiiKind

# --------------------------------------------------------------------------------------
# Detection is two-sided: a column name suggests a kind, values confirm it. Either alone
# yields a low-confidence finding; agreement yields a high one. The result is always
# advisory and carries its basis, because a false positive on a business column is as
# costly as a miss on a personal one.
# --------------------------------------------------------------------------------------

VALUE_PATTERNS: list[tuple[PiiKind, re.Pattern[str]]] = [
    ("email", re.compile(r"^[^@\s]+@[^@\s.]+\.[a-z]{2,}$", re.I)),
    (
        "ip_address",
        re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$|^[0-9a-f:]{6,}:[0-9a-f:]+$", re.I),
    ),
    ("iban", re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")),
    ("credit_card", re.compile(r"^(?:\d[ -]?){13,19}$")),
    ("phone", re.compile(r"^\+?\d[\d\s().-]{7,17}\d$")),
    ("national_id", re.compile(r"^\d{3}-\d{2}-\d{4}$|^[A-Z]{5}\d{4}[A-Z]$")),
    ("coordinates", re.compile(r"^-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}$")),
]

NAME_PATTERNS: list[tuple[PiiKind, re.Pattern[str]]] = [
    ("email", re.compile(r"\b(e?mail|email_address)\b")),
    ("phone", re.compile(r"\b(phone|mobile|telephone|msisdn|contact_no|cell)\b")),
    (
        "national_id",
        re.compile(r"\b(ssn|social_security|aadhaar|pan_no|nino|national_id|tax_id)\b"),
    ),
    ("credit_card", re.compile(r"\b(card_no|card_number|credit_card|pan|cc_num)\b")),
    ("iban", re.compile(r"\b(iban|bank_account|account_no|routing|swift|bic)\b")),
    ("ip_address", re.compile(r"\b(ip|ip_address|client_ip|remote_addr)\b")),
    ("date_of_birth", re.compile(r"\b(dob|date_of_birth|birth_date|birthdate)\b")),
    (
        "person_name",
        re.compile(
            r"\b(first_name|last_name|full_name|surname|given_name|fname|lname|"
            r"customer_name|employee_name)\b"
        ),
    ),
    ("street_address", re.compile(r"\b(address|street|addr_line|address_line|postal_address)\b")),
    ("postal_code", re.compile(r"\b(zip|zipcode|postal_code|postcode|pin_code)\b")),
    ("coordinates", re.compile(r"\b(lat_long|latlng|geo_point|coordinates)\b")),
]

# Kinds a column name alone can establish, because no value shape distinguishes them.
NAME_ONLY_KINDS = {"person_name", "street_address", "date_of_birth", "postal_code"}


def _normalise(column_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", column_name.lower()).strip("_")


def detect_pii(column_name: str, values: Sequence[object], logical_type: str) -> PiiFinding | None:
    """Classify a column as personal data using its name and a sample of its values."""
    normalised = _normalise(column_name)
    name_kind = next((kind for kind, pattern in NAME_PATTERNS if pattern.search(normalised)), None)

    strings = [str(v).strip() for v in values if v is not None and str(v).strip()]
    value_kind: PiiKind | None = None
    matched_ratio = 0.0

    # Value matching only makes sense for text; a numeric column is not an email.
    if strings and logical_type in {"string", "unknown"}:
        sample = strings[:500]
        for kind, pattern in VALUE_PATTERNS:
            hits = sum(1 for value in sample if pattern.match(value))
            ratio = hits / len(sample)
            if ratio >= 0.7:
                value_kind, matched_ratio = kind, ratio
                break

    if name_kind and value_kind:
        if name_kind == value_kind:
            return PiiFinding(
                kind=name_kind, confidence=0.97, basis="name+value", matched_ratio=matched_ratio
            )
        # Disagreement: the values are evidence, the name is a label someone chose.
        return PiiFinding(
            kind=value_kind, confidence=0.8, basis="value", matched_ratio=matched_ratio
        )
    if value_kind:
        return PiiFinding(
            kind=value_kind, confidence=0.85, basis="value", matched_ratio=matched_ratio
        )
    if name_kind:
        # Without value confirmation, only name-decidable kinds are worth reporting.
        if name_kind in NAME_ONLY_KINDS:
            return PiiFinding(kind=name_kind, confidence=0.6, basis="name")
        return PiiFinding(kind=name_kind, confidence=0.4, basis="name")
    return None
