from __future__ import annotations

import json
import logging
from typing import Any

from app.ai import provider
from app.ai.types import ChatMessage, ChatQueryResult, ChatResponse
from app.engine.base import Engine
from app.semantic.compiler import QueryCompiler, SemanticError
from app.semantic.types import CompiledQuery, Filter, MetricQuery, SemanticModel

logger = logging.getLogger("assarium.ai.chat")

# One correction round. The compiler's refusals name exactly what was wrong, which is
# usually enough for the model to fix itself; a second round rarely helps and doubles
# the latency and cost of every failed question.
MAX_REPAIR_ATTEMPTS = 1


def _model_context(model: SemanticModel) -> str:
    """
    Describe the model to the LLM in the vocabulary it has to answer in.

    Identifiers matter more than prose here. A measure is only usable if the model emits
    its exact id, so ids lead and human labels follow as context. Listing labels alone
    reads better and produces queries the compiler rejects every single time.
    """
    lines: list[str] = []

    for entity in model.entities:
        kind = "fact table" if entity.is_fact else "dimension table"
        lines.append(
            f"\nENTITY {entity.id} — {entity.label} ({kind}, {entity.row_count:,} rows)"
        )

        groupable = [
            a
            for a in entity.attributes
            if not a.hidden and a.role in {"dimension", "key", "time"}
        ]
        if groupable:
            lines.append("  Group or filter by:")
            for attribute in groupable:
                note = " [date — pass as time_dimension]" if attribute.role == "time" else ""
                if attribute.contains_pii:
                    note += " [personal data]"
                cardinality = (
                    f", {attribute.cardinality} distinct"
                    if attribute.cardinality is not None
                    else ""
                )
                lines.append(
                    f"    {entity.id}.{attribute.name} — {attribute.label}"
                    f"{cardinality}{note}"
                )

        measures = [m for m in model.measures if m.entity_id == entity.id]
        if measures:
            lines.append("  Measures:")
            for measure in measures:
                source = (
                    f"{measure.aggregation}({measure.column})"
                    if measure.column
                    else "count(*)"
                )
                lines.append(
                    f"    {measure.id} — {measure.label} = {source} [{measure.format}]"
                )

    if model.joins:
        lines.append("\nJOINS (only these paths may be traversed, in this direction):")
        for join in model.joins:
            lines.append(
                f"  {join.from_entity} -> {join.to_entity} "
                f"({join.from_column} = {join.to_column}, {join.cardinality})"
            )

    return "\n".join(lines)


SYSTEM_PROMPT = """\
You are Assarium, an analyst working over a governed semantic model. You never write SQL.
You express questions as metric queries, which a compiler validates and executes.

Reply with a single JSON object, one of these two shapes.

A data question:
{{"action": "query",
  "measures": ["<exact measure id from the model below>"],
  "dimensions": ["<entity_id.attribute_name>"],
  "time_dimension": "<entity_id.attribute_name>" or null,
  "time_grain": "day" | "week" | "month" | "quarter" | "year",
  "filters": [{{"field": "<entity_id.attribute_name>", "operator": "<op>", "values": [...]}}],
  "limit": 20,
  "intent": "<one short sentence saying what you asked for>"}}

Anything else:
{{"action": "answer", "text": "<your reply>"}}

RULES — the compiler enforces these, so breaking one wastes the turn:
- Use ONLY the exact ids listed below. Never invent a measure, attribute or entity, and
  never pass a human label where an id is required.
- Every measure in a single query must belong to the SAME entity. To compare measures
  from different entities, answer with two separate questions instead.
- You may group by another entity's attribute only when a JOIN below runs FROM your
  measure's entity TO that entity. The direction matters.
- Operators: eq, ne, in, not_in, gt, gte, lt, lte, contains, starts_with, between,
  is_null, is_not_null. "in"/"not_in" take a list; "between" takes exactly two values;
  "is_null"/"is_not_null" take none.
- Prefer time_dimension + time_grain over grouping by a raw date column.
- If the model genuinely cannot answer the question, use "answer" and say plainly what
  is missing. Never substitute a measure that means something else.

THE MODEL:
{context}
"""


def chat(
    model: SemanticModel,
    engine: Engine,
    messages: list[ChatMessage],
    permissions: frozenset[str] | set[str] | None = None,
) -> ChatResponse:
    """
    Answer one conversational turn.

    The model proposes a metric query; the compiler is the authority on whether it can
    be run. When the compiler refuses, its reason goes back to the model for one
    correction attempt - refusals name the exact problem, which is precisely the
    feedback needed to fix a query, and is why the semantic layer is worth having in
    front of an LLM at all.
    """
    context = _model_context(model)
    conversation: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(context=context)}
    ]
    for message in messages:
        conversation.append({"role": message.role, "content": message.content})

    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        raw = provider.complete(
            conversation,
            temperature=0.1,
            max_tokens=900,
            response_format={"type": "json_object"},
        )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return ChatResponse(message=ChatMessage(role="assistant", content=raw.strip()))

        if parsed.get("action") != "query":
            text = parsed.get("text") or raw.strip()
            return ChatResponse(message=ChatMessage(role="assistant", content=text))

        try:
            query = _to_query(parsed)
            compiled = QueryCompiler(model, engine, permissions=permissions).compile(query)
        except (SemanticError, ValueError) as exc:
            reason = exc.message if isinstance(exc, SemanticError) else str(exc)
            if attempt >= MAX_REPAIR_ATTEMPTS:
                return ChatResponse(
                    message=ChatMessage(
                        role="assistant",
                        content=(
                            "I could not answer that from the model as it stands. "
                            f"{reason}"
                        ),
                    )
                )
            # Hand the refusal back and let the model correct itself once.
            conversation.append({"role": "assistant", "content": raw})
            conversation.append(
                {
                    "role": "user",
                    "content": (
                        f"That query was rejected: {reason}\n"
                        "Correct it using only the ids in the model, or reply with "
                        '{"action":"answer"} explaining what is missing.'
                    ),
                }
            )
            continue

        return _run(model, engine, query, compiled, parsed.get("intent"))

    # Unreachable: the loop either returns or exhausts its attempts above.
    return ChatResponse(
        message=ChatMessage(role="assistant", content="I could not answer that question.")
    )


def _to_query(parsed: dict[str, Any]) -> MetricQuery:
    filters = [
        Filter(
            field=item["field"],
            operator=item.get("operator", "eq"),
            values=item.get("values", []),
        )
        for item in parsed.get("filters", [])
        if isinstance(item, dict) and item.get("field")
    ]
    return MetricQuery(
        measures=parsed.get("measures", []),
        dimensions=parsed.get("dimensions", []) or [],
        time_dimension=parsed.get("time_dimension"),
        time_grain=parsed.get("time_grain") or "month",
        filters=filters,
        limit=min(int(parsed.get("limit", 50) or 50), 200),
    )


def _run(
    model: SemanticModel,
    engine: Engine,
    query: MetricQuery,
    compiled: CompiledQuery,
    intent: str | None,
) -> ChatResponse:
    result = engine.execute(compiled.sql, params=compiled.params)

    query_result = ChatQueryResult(
        columns=result.columns,
        rows=result.rows,
        row_count=result.row_count,
        elapsed_ms=result.elapsed_ms,
        truncated=result.truncated,
        sql=compiled.sql,
        dimension_columns=compiled.dimension_columns,
        measure_columns=compiled.measure_columns,
        notes=compiled.notes,
        chart_hint=_chart_hint(query, result.row_count),
    )
    narrative = _narrate(model, query, compiled, result.rows, intent)

    return ChatResponse(
        message=ChatMessage(
            role="assistant", content=narrative, query_result=query_result
        ),
        query_result=query_result,
    )


def _chart_hint(query: MetricQuery, row_count: int) -> str:
    """Pick the form from the data's job, the same way the dashboard generator does."""
    if row_count == 0:
        return "none"
    if not query.dimensions and not query.time_dimension:
        # A single number is a stat tile, never a one-bar bar chart.
        return "stat"
    if query.time_dimension:
        return "line"
    if len(query.dimensions) == 1 and row_count <= 24 and len(query.measures) == 1:
        return "bar"
    return "table"


def _narrate(
    model: SemanticModel,
    query: MetricQuery,
    compiled: CompiledQuery,
    rows: list[list[Any]],
    intent: str | None,
) -> str:
    """
    Summarise the result in a sentence.

    Written from the returned rows rather than asked of the LLM: the numbers are already
    known here, and a second model call to describe them can only introduce a figure
    that does not match the table underneath it.
    """
    if not rows:
        return "That query returned no rows for the current filters."

    measures = [model.measure(measure_id) for measure_id in query.measures]
    lead = measures[0] if measures else None
    dimension_count = len(compiled.dimension_columns)

    if dimension_count == 0:
        parts = []
        for index, measure in enumerate(measures):
            if measure is None:
                continue
            parts.append(f"**{measure.label}** is {_format(rows[0][index], measure.format)}")
        return (intent + ". " if intent else "") + ", ".join(parts) + "."

    top = rows[0]
    label = " · ".join(str(value) for value in top[:dimension_count])
    detail = ""
    if lead is not None and len(top) > dimension_count:
        detail = f", led by **{label}** at {_format(top[dimension_count], lead.format)}"

    opener = intent.rstrip(".") + ". " if intent else ""
    return (
        f"{opener}{len(rows):,} row{'s' if len(rows) != 1 else ''}{detail}."
    )


def _format(value: Any, fmt: str = "number") -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if fmt == "percent":
        scaled = number * 100 if abs(number) <= 1 else number
        return f"{scaled:,.1f}%"
    if fmt == "currency":
        return f"{number:,.2f}"
    if number.is_integer():
        return f"{number:,.0f}"
    return f"{number:,.2f}"


def explain_kpi(
    model: SemanticModel,
    engine: Engine,
    measure_id: str,
    value: float | None,
    delta: float | None,
    direction: str | None,
    breakdown: list[tuple[str, float | None]] | None = None,
) -> str:
    """
    Explain one tile in plain English.

    The model is given the figures and the measure's definition and asked to describe
    them - not to explain *why* they moved. Cause is not in this data, and a confident
    invented reason on a dashboard card is worse than no card at all.
    """
    measure = model.measure(measure_id)
    if measure is None:
        return f"There is no measure called '{measure_id}' any more."

    entity = model.entity(measure.entity_id)
    source = f"{measure.aggregation} of {measure.column}" if measure.column else "row count"

    facts = [
        f"- Measure: {measure.label} ({source})",
        f"- Source: {entity.label if entity else measure.entity_id}",
    ]
    if value is not None:
        facts.append(f"- Current value: {_format(value, measure.format)}")
    if delta is not None:
        facts.append(
            f"- Change on the previous period: "
            f"{_format(delta, measure.format)} ({direction})"
        )
    if measure.description:
        facts.append(f"- Definition: {measure.description}")
    if breakdown:
        top = ", ".join(
            f"{label} {_format(amount, measure.format)}" for label, amount in breakdown[:5]
        )
        facts.append(f"- Largest contributors: {top}")

    prompt = (
        "Describe this figure for a business reader in two or three sentences.\n"
        + "\n".join(facts)
        + "\n\nState what the number measures and what it is doing. Do NOT speculate "
        "about causes, and do not invent any figure that is not listed above."
    )

    return provider.complete(
        [
            {
                "role": "system",
                "content": "You are Assarium, a data analyst. Be factual and concise.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=260,
    )
