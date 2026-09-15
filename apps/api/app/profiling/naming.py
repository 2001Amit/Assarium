from __future__ import annotations

import re

# Shared naming vocabulary. Lives here rather than in the classifier because profiling
# needs it too: a continuous measure must never be accepted as a primary key.

MEASURE_NAMES = re.compile(
    r"(^|_)(total|sum|amount|amt|revenue|cost|qty|quantity|count|cnt|avg|average|mean|"
    r"rate|ratio|pct|percent|margin|score|balance|value|ytd|mtd|qtd|wtd|rolling|"
    r"cumulative|net|gross|price|salary|weight|height|temperature|duration|latency|"
    r"rent|noi|opex|capex|sqft|sqm|area|footage|occupancy|yield|escalation)(_|$)"
)

IDENTIFIER_NAMES = re.compile(r"(^|_)(id|key|code|no|num|number|uuid|guid|sk|pk)$|^id$")

# Types an identifier can plausibly have. Continuous numerics are excluded: a float
# column of random amounts is distinct in every row without identifying anything.
KEY_ELIGIBLE_TYPES = {"string", "integer", "date", "timestamp"}


def normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def is_key_eligible(
    name: str, logical_type: str, shadow_type: str | None = None
) -> bool:
    """
    Whether a column could serve as an identifier rather than carry a measurement.

    A text column whose values are really numbers is a measurement that has not been
    typed yet, not an identifier - `monthly_rent` held as text is unique across rows
    by accident, not because it identifies anything.
    """
    if logical_type not in KEY_ELIGIBLE_TYPES:
        return False
    if shadow_type in {"integer", "float", "decimal"}:
        return False
    return not MEASURE_NAMES.search(normalise(name))
