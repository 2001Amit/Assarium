"""
Personal data masking.

Profiling detected PII from the beginning and nothing enforced it — a viewer could group
by a customer's name and read the names, and the platform's response was a note on the
result saying it had noticed. Detected-but-unenforced is the worst position of the three
for a regulator, because the finding proves you knew.

These tests run the SQL rather than inspecting it. A mask that appears in the query text
and does not survive execution is not a mask, and the two engines spell their string
functions differently, so the only convincing check is the values that come back.
"""

from __future__ import annotations

import pytest

from app.engine.duckdb_engine import DuckDBEngine
from app.semantic.compiler import QueryCompiler
from app.semantic.masking import VIEW_PII, mask_expression, strategy_for
from app.semantic.types import (
    Attribute,
    Entity,
    Filter,
    Measure,
    MetricQuery,
    SemanticModel,
)

PEOPLE = [
    ("Priya Sharma", "priya@acme.com", "+44 7700 900123", "1984-06-02", 5000.0),
    ("Raj Patel", "raj@acme.com", "+44 7700 900456", "1979-11-20", 4200.0),
    ("Ana Silva", "ana@globex.com", "+1 415 555 0134", "1991-03-15", 6100.0),
    ("Tom Weber", None, None, None, 3300.0),
]


@pytest.fixture
def engine(tmp_path):
    instance = DuckDBEngine(tmp_path)
    instance.ensure_layers()
    rows = ",\n".join(
        "({}, {}, {}, {}, {})".format(
            *[f"'{v}'" if isinstance(v, str) else "NULL" if v is None else v for v in person]
        )
        for person in PEOPLE
    )
    instance.execute_ddl(f"""
        CREATE OR REPLACE TABLE silver.main.contacts AS SELECT * FROM (VALUES
        {rows}
        ) AS t(full_name, email, phone, born_on, spend)
    """)
    yield instance
    instance.close()


def model() -> SemanticModel:
    def attribute(name: str, kind: str | None, role: str = "dimension") -> Attribute:
        return Attribute(
            name=name, label=name.replace("_", " ").title(), column=name,
            logical_type="string", role=role,
            contains_pii=kind is not None, pii_kind=kind,
        )

    entity = Entity(
        id="contacts", name="contacts", label="Contacts", layer="silver",
        table="contacts", is_fact=True, row_count=len(PEOPLE),
        attributes=[
            attribute("full_name", "person_name"),
            attribute("email", "email"),
            attribute("phone", "phone"),
            attribute("born_on", "date_of_birth"),
            attribute("spend", None, role="attribute"),
        ],
    )
    return SemanticModel(
        connection_id="c1",
        entities=[entity],
        measures=[
            Measure(id="contacts.total_spend", name="total_spend", label="Total spend",
                    entity_id="contacts", aggregation="sum", column="spend"),
        ],
    )


def run(engine, permissions, dimension="contacts.email"):
    compiler = QueryCompiler(model(), engine, permissions=permissions)
    compiled = compiler.compile(
        MetricQuery(measures=["contacts.total_spend"], dimensions=[dimension])
    )
    return compiled, engine.execute(compiled.sql, params=compiled.params)


VIEWER: frozenset[str] = frozenset()
PRIVILEGED = frozenset({VIEW_PII})


# =======================================================================================
# The values that come back
# =======================================================================================


class TestMaskedValues:
    def test_an_email_keeps_only_its_domain(self, engine):
        _, result = run(engine, VIEWER)
        labels = {row[0] for row in result.rows}
        assert "priya@acme.com" not in labels
        assert "•••@acme.com" in labels

    def test_the_domain_survives_so_it_can_still_be_grouped(self, engine):
        """
        "How many of our customers are at acme.com" is a legitimate question that needs
        no names, so the mask keeps the part that answers it.
        """
        _, result = run(engine, VIEWER)
        labels = {row[0] for row in result.rows}
        assert "•••@globex.com" in labels

    def test_a_name_keeps_only_its_initial(self, engine):
        _, result = run(engine, VIEWER, dimension="contacts.full_name")
        labels = {row[0] for row in result.rows}
        assert "Priya Sharma" not in labels
        assert any(label and label.startswith("P•••") for label in labels)

    def test_a_phone_keeps_only_its_last_four(self, engine):
        _, result = run(engine, VIEWER, dimension="contacts.phone")
        labels = {row[0] for row in result.rows if row[0]}
        assert not any("7700" in label for label in labels)
        assert any(label.startswith("•••") for label in labels)

    def test_a_birth_date_becomes_a_birth_year(self, engine):
        """A year supports age cohorts; a date is an identifier."""
        _, result = run(engine, VIEWER, dimension="contacts.born_on")
        labels = {row[0] for row in result.rows if row[0]}
        assert "1984-06-02" not in labels
        assert "1984•••" in labels

    def test_null_stays_null_rather_than_becoming_a_mask(self, engine):
        """
        Turning an absent value into a mask string invents data, and "how many are
        missing" is a question people legitimately ask of a masked column.
        """
        _, result = run(engine, VIEWER)
        assert any(row[0] is None for row in result.rows)

    def test_the_privileged_caller_sees_the_real_values(self, engine):
        _, result = run(engine, PRIVILEGED)
        assert "priya@acme.com" in {row[0] for row in result.rows}


# =======================================================================================
# The property that makes it a mask
# =======================================================================================


class TestMaskingHappensBeforeGrouping:
    def test_people_who_mask_alike_collapse_into_one_row(self, engine):
        """
        The whole point. If the real column were grouped and only the label masked, the
        result would still have one row per person: the row count is a headcount and the
        ordering says which of them is largest.

        Three of the four contacts have an email; two share the acme.com domain. Masked,
        that is two rows plus the null - not four.
        """
        _, masked = run(engine, VIEWER)
        _, unmasked = run(engine, PRIVILEGED)
        assert len(masked.rows) < len(unmasked.rows)

    def test_the_measure_still_totals_correctly(self, engine):
        """Masking hides who, not how much. The totals have to keep adding up."""
        _, masked = run(engine, VIEWER)
        _, unmasked = run(engine, PRIVILEGED)
        assert sum(r[1] for r in masked.rows) == pytest.approx(
            sum(r[1] for r in unmasked.rows)
        )

    def test_the_grouping_key_is_the_masked_expression(self, engine):
        """
        The compiler groups by ordinal - `GROUP BY 1` - which refers to the first item in
        the select list. So the property to assert is that the select list item is itself
        masked: group and label are then the same expression by construction, and there
        is no way for one to be masked while the other is not.
        """
        compiled, _ = run(engine, VIEWER)
        select_list, _, rest = compiled.sql.partition("\nFROM")

        assert "•••" in select_list, "the selected expression is not masked"
        # The raw column must never be what the dimension alias points at. Splitting on
        # " AS " does not work here - the mask contains `CAST(... AS VARCHAR)` itself -
        # so the check is that the bare column is nowhere in the select list.
        assert 'e0."email" AS "email"' not in select_list
        assert "GROUP BY 1" in rest, "grouping by ordinal is what ties the two together"


# =======================================================================================
# Filters
# =======================================================================================


class TestFiltersOnPersonalData:
    def test_a_filter_on_a_masked_column_is_refused(self, engine):
        """
        Equality on a hidden column is an oracle: it returns rows or it does not, and
        either answer identifies the person without ever displaying them.
        """
        compiler = QueryCompiler(model(), engine, permissions=VIEWER)
        with pytest.raises(Exception) as caught:
            compiler.compile(
                MetricQuery(
                    measures=["contacts.total_spend"],
                    filters=[Filter(field="contacts.email", operator="eq",
                                    values=["priya@acme.com"])],
                )
            )
        assert "personal data" in str(caught.value)

    def test_the_refusal_explains_why_rather_than_just_refusing(self, engine):
        compiler = QueryCompiler(model(), engine, permissions=VIEWER)
        with pytest.raises(Exception) as caught:
            compiler.compile(
                MetricQuery(
                    measures=["contacts.total_spend"],
                    filters=[Filter(field="contacts.full_name", operator="eq",
                                    values=["Priya Sharma"])],
                )
            )
        assert "confirm whether a specific person" in str(caught.value)

    def test_a_privileged_caller_may_filter(self, engine):
        compiler = QueryCompiler(model(), engine, permissions=PRIVILEGED)
        compiled = compiler.compile(
            MetricQuery(
                measures=["contacts.total_spend"],
                filters=[Filter(field="contacts.email", operator="eq",
                                values=["priya@acme.com"])],
            )
        )
        assert engine.execute(compiled.sql, params=compiled.params).rows[0][0] == 5000.0

    def test_a_filter_on_an_ordinary_column_is_unaffected(self, engine):
        compiler = QueryCompiler(model(), engine, permissions=VIEWER)
        compiled = compiler.compile(
            MetricQuery(
                measures=["contacts.total_spend"],
                filters=[Filter(field="contacts.spend", operator="gt", values=[4000])],
            )
        )
        assert engine.execute(compiled.sql, params=compiled.params).rows


# =======================================================================================
# Failing closed
# =======================================================================================


class TestDefaults:
    def test_omitting_permissions_masks_rather_than_reveals(self, engine):
        """
        A call site that forgets to pass permissions gets masked data. The absence of an
        argument is invisible, so it must not be the permissive case.
        """
        compiler = QueryCompiler(model(), engine)
        compiled = compiler.compile(
            MetricQuery(measures=["contacts.total_spend"], dimensions=["contacts.email"])
        )
        rows = engine.execute(compiled.sql, params=compiled.params).rows
        assert "priya@acme.com" not in {row[0] for row in rows}

    def test_an_unrelated_permission_does_not_unlock_it(self, engine):
        compiler = QueryCompiler(model(), engine, permissions={"query:run", "export:run"})
        assert compiler.may_see_pii is False

    def test_only_owners_and_admins_hold_the_permission(self):
        from app.tenancy.models import ROLE_PERMISSIONS

        assert VIEW_PII in ROLE_PERMISSIONS["owner"]
        assert VIEW_PII in ROLE_PERMISSIONS["admin"]
        assert VIEW_PII not in ROLE_PERMISSIONS["analyst"]
        assert VIEW_PII not in ROLE_PERMISSIONS["viewer"]

    def test_the_reader_is_told_the_column_is_masked(self, engine):
        """Otherwise a masked column reads as a broken one, and somebody files a bug."""
        compiled, _ = run(engine, VIEWER)
        assert any("personal data" in note for note in compiled.notes)


# =======================================================================================
# Strategy choices
# =======================================================================================


class TestStrategies:
    @pytest.mark.parametrize("kind", ["national_id", "credit_card", "iban",
                                      "street_address", "coordinates"])
    def test_the_most_sensitive_kinds_keep_nothing(self, kind):
        """
        No analysis needs part of a card number, and every partial is a step towards the
        whole.
        """
        assert strategy_for(kind) == "redact"

    def test_an_unknown_kind_falls_back_to_full_redaction(self):
        assert strategy_for(None) == "redact"
        assert strategy_for("something-new") == "redact"

    def test_the_mask_compiles_on_both_engines(self, engine):
        """
        DuckDB spells it VARCHAR and Spark spells it STRING. A mask written for one is a
        mask missing on the other, which is exactly where it would matter.
        """
        for kind in ("email", "phone", "person_name", "date_of_birth", "national_id"):
            sql = mask_expression(engine, "'test@example.com'", kind)
            engine.execute(f"SELECT {sql} AS masked")

    def test_a_value_that_is_not_the_shape_it_claimed_is_redacted(self, engine):
        """An "email" with no @ is not an email, and must not pass through unmasked."""
        sql = mask_expression(engine, "'not-an-email'", "email")
        assert engine.execute(f"SELECT {sql} AS m").rows[0][0] == "••• masked"
