from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from app.connectors.base import Connector
from app.connectors.registry import build
from app.connectors.types import DatasetSchema
from app.core.errors import AssariumError
from app.db.session import session_scope
from app.engine.arrow import arrow_schema
from app.engine.base import Engine, Layer, safe_identifier
from app.engine.factory import get_engine
from app.ingestion.store import (
    commit_state,
    known_file_etags,
    load_state,
    record_file,
)
from app.medallion.transforms import plan_gold, plan_silver
from app.models.base import utcnow
from app.models.entities import Connection, Dataset, PipelineRun, PipelineStep
from app.profiling.types import DatasetProfile
from app.quality.rules import derive_policy
from app.quality.sql import build as build_quality_sql
from app.secrets.connections import load_secrets

logger = logging.getLogger("assarium.pipeline")


@dataclass
class PipelineOptions:
    drop_ingestion_metadata: bool = True
    build_gold: bool = True
    batch_size: int = 50_000


def start_run(
    connection_id: str, tenant_id: str, dataset_ids: list[str] | None = None
) -> str:
    """
    Create the run record up front so the UI has something to follow immediately.

    The tenant is passed in rather than read from the connection: this runs outside a
    request, and a background job that infers its own tenant from the row it was handed
    would happily process whatever row it was handed.
    """
    with session_scope() as db:
        connection = db.execute(
            select(Connection).where(
                Connection.id == connection_id, Connection.tenant_id == tenant_id
            )
        ).scalar_one_or_none()
        if connection is None:
            raise AssariumError("That connection does not exist.")
        statement = select(Dataset).where(
            Dataset.connection_id == connection_id, Dataset.tenant_id == tenant_id
        )
        if dataset_ids:
            statement = statement.where(Dataset.id.in_(dataset_ids))
        datasets = list(db.execute(statement).scalars())

        run = PipelineRun(
            tenant_id=tenant_id,
            connection_id=connection_id,
            engine=get_engine(tenant_id).name,
            status="running",
            started_at=utcnow(),
            dataset_count=len(datasets),
        )
        db.add(run)
        db.flush()
        return run.id


def execute_run(run_id: str, dataset_ids: list[str] | None, options: PipelineOptions) -> None:
    """
    Run the refinement.

    Everything selected is landed in bronze first, even when its contents already look
    conformed. That single extra write buys reproducibility: refinement can be re-run,
    corrected and re-run again without going back to the source system, and every gold
    number stays traceable to bytes the platform actually received.

    What differs by classification is what happens *after* landing - a clean table gets a
    near-passthrough into silver instead of a full repair, and an already-aggregated table
    is carried into gold rather than being aggregated a second time.
    """
    # The tenant comes from the run row, which was written under a resolved tenant at
    # start_run. Reading it here rather than accepting it as an argument means a caller
    # cannot ask for one tenant's run to be executed against another's catalog.
    with session_scope() as db:
        opened = db.get(PipelineRun, run_id)
        if opened is None:
            raise AssariumError("That run does not exist.")
        tenant_id = opened.tenant_id

    engine = get_engine(tenant_id)
    engine.ensure_layers()
    sequence = 0
    failures = 0
    produced: dict[str, list[str]] = {"bronze": [], "silver": [], "gold": []}

    with session_scope() as db:
        run = db.get(PipelineRun, run_id)
        if run is None:
            return
        connection = db.get(Connection, run.connection_id)
        statement = select(Dataset).where(Dataset.connection_id == run.connection_id)
        if dataset_ids:
            statement = statement.where(Dataset.id.in_(dataset_ids))
        datasets = list(db.execute(statement).scalars())
        connection_config = dict(connection.config)
        connection_source = connection.source_id
        connection_secrets = load_secrets(connection.secret_refs or {})
        plan = [
            _DatasetPlan(
                id=dataset.id,
                name=dataset.name,
                path=list(dataset.path),
                table=safe_identifier(dataset.name),
                layer=dataset.effective_layer or "bronze",
                columns=list(dataset.column_schema or []),
                profile=dataset.profile,
                connection_id=dataset.connection_id,
                tenant_id=getattr(dataset, "tenant_id", "") or "",
                merge_keys=_merge_keys(dataset),
            )
            for dataset in datasets
        ]

    connector = build(connection_source, connection_config, connection_secrets)

    try:
        for item in plan:
            sequence += 1
            landed = _ingest(engine, connector, item, sequence, run_id, options)
            if landed is None:
                failures += 1
                continue
            produced["bronze"].append(item.table)

            sequence += 1
            silver_ok = _refine_silver(engine, item, sequence, run_id, options)
            if not silver_ok:
                failures += 1
                continue
            produced["silver"].append(item.table)

            if not options.build_gold:
                continue

            sequence += 1
            gold_table = _build_gold(engine, item, sequence, run_id)
            if gold_table:
                produced["gold"].append(gold_table)
    finally:
        connector.close()

    with session_scope() as db:
        run = db.get(PipelineRun, run_id)
        if run is None:
            return
        run.finished_at = utcnow()
        if failures and not produced["silver"]:
            run.status = "failed"
        elif failures:
            run.status = "partial"
        else:
            run.status = "succeeded"
        run.summary = {
            "tables": produced,
            "failures": failures,
            "engine": engine.name,
        }


@dataclass
class _DatasetPlan:
    id: str
    name: str
    path: list[str]
    table: str
    layer: str
    columns: list[dict]
    profile: dict | None
    connection_id: str = ""
    tenant_id: str = ""
    merge_keys: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------------------


def _ingest(
    engine: Engine,
    connector: Connector,
    item: _DatasetPlan,
    sequence: int,
    run_id: str,
    options: PipelineOptions,
) -> int | None:
    step = _open_step(run_id, sequence, item, kind="ingest", layer="bronze")
    started = time.perf_counter()
    actions: list[str] = []

    try:
        schema = DatasetSchema.model_validate(
            {"path": item.path, "name": item.name, "columns": item.columns}
        )
        target = arrow_schema(schema.columns)

        # Decide how much to read before reading any of it, so the decision is loggable
        # and the reason is shown to the user rather than inferred from row counts.
        with session_scope() as db:
            state = load_state(db, item.id)
            known = known_file_etags(db, item.id)

        plan = _plan_for(connector, item, schema, state, known)
        actions.append(plan.describe())

        if plan.strategy == "file" and not plan.files:
            _close_step(
                step, "skipped",
                actions=actions,
                message="Nothing has changed at the source since the last load.",
                duration=started,
            )
            return 0

        batches = connector.read_plan(item.path, plan, batch_size=options.batch_size)
        rows_before = engine.row_count("bronze", item.table)

        if plan.full_refresh or not item.merge_keys:
            ref = engine.write("bronze", item.table, batches, target, mode="replace")
            if not item.merge_keys and not plan.full_refresh:
                actions.append(
                    "Replaced rather than merged: this table has no key, so an "
                    "incremental row cannot be matched to the row it supersedes."
                )
        else:
            # Merging makes a retry safe: re-reading a window that was already written
            # updates the same rows instead of duplicating them.
            ref = engine.merge("bronze", item.table, batches, target, item.merge_keys)
            actions.append(f"Merged on {', '.join(item.merge_keys)}")

        rows_read = max(ref.row_count - rows_before, 0) if not plan.full_refresh else ref.row_count

        # Only now, with the data written, is it safe to move the watermark.
        with session_scope() as db:
            commit_state(
                db,
                tenant_id=item.tenant_id,
                connection_id=item.connection_id,
                dataset_id=item.id,
                plan=plan,
                rows_read=rows_read,
                run_id=run_id,
            )
            for change in plan.files:
                record_file(
                    db,
                    tenant_id=item.tenant_id,
                    connection_id=item.connection_id,
                    dataset_id=item.id,
                    change=change,
                    run_id=run_id,
                    rows=rows_read,
                    status="LANDED",
                )

    except (AssariumError, NotImplementedError) as exc:
        _close_step(step, "failed", message=str(exc), actions=actions, duration=started)
        return None
    except Exception as exc:  # noqa: BLE001 - a driver fault must not abort the whole run
        logger.exception("Ingest failed for %s", item.name)
        _close_step(step, "failed", message=str(exc)[:400], actions=actions, duration=started)
        return None

    _close_step(
        step,
        "succeeded",
        rows_out=ref.row_count,
        target=ref.qualified,
        actions=actions,
        duration=started,
    )
    return ref.row_count


def _plan_for(connector, item, schema, state, known):
    """Ask the connector how much to read, falling back to a full load it can explain."""
    from app.ingestion.types import IngestionPlan

    planner = getattr(connector, "plan_ingestion", None)
    if planner is None:
        return IngestionPlan(
            strategy="full",
            full_refresh=True,
            reason=f"{connector.spec.name} does not support incremental reads yet.",
        )
    try:
        return planner(item.path, schema, state, known)
    except TypeError:
        # SQL connectors take no known-files map.
        return planner(item.path, schema, state)


def _refine_silver(
    engine: Engine, item: _DatasetPlan, sequence: int, run_id: str, options: PipelineOptions
) -> bool:
    step = _open_step(run_id, sequence, item, kind="silver", layer="silver")
    started = time.perf_counter()
    actions: list[str] = []
    notes: list[str] = []
    transform = None

    try:
        profile = DatasetProfile.model_validate(item.profile) if item.profile else DatasetProfile()
        source = engine.qualified("bronze", item.table)

        # Order matters, and getting it wrong defeats the whole mechanism: the rules run
        # against RAW bronze, before any cast. Casting first would turn an unconvertible
        # value into NULL, and the type rule would then find a perfectly clean column and
        # quarantine nothing - which is the silent data loss this replaces. It also means
        # a quarantined row keeps its original value, so it can actually be diagnosed.
        policy = derive_policy(profile, key_columns=item.merge_keys or None)
        checked = build_quality_sql(engine, f"SELECT * FROM {source}", policy, run_id)

        # The silver transform then applies to the rows that passed.
        transform = plan_silver(
            engine,
            f"({checked.clean}) AS _clean",
            item.table,
            profile,
            drop_ingestion_metadata=options.drop_ingestion_metadata,
        )
        actions.extend(transform.actions)
        notes.extend(transform.notes)

        quarantine_table = f"{item.table}_quarantine"
        target = engine.qualified("silver", item.table)
        quarantine_target = engine.qualified("silver", quarantine_table)

        total = engine.row_count("bronze", item.table)

        rejected = 0
        if checked.rejecting_rules:
            engine.execute_ddl(
                f"CREATE TABLE IF NOT EXISTS {quarantine_target} AS "
                f"SELECT * FROM ({checked.quarantine}) AS _q WHERE 1 = 0"
            )
            engine.execute_ddl(
                f"INSERT INTO {quarantine_target} BY NAME {checked.quarantine}"
                if engine.name == "duckdb"
                else f"INSERT INTO {quarantine_target} {checked.quarantine}"
            )
            rejected = engine.row_count("silver", quarantine_table)

        # Publishing a partial table as though it were complete is the failure mode this
        # guards against: stop, and say how many rows and why.
        breach = policy.breached(total, rejected)
        if breach:
            raise AssariumError(breach)

        engine.execute_ddl(f"CREATE OR REPLACE TABLE {target} AS {transform.sql}")
        rows = engine.row_count("silver", item.table)

        if rejected:
            reasons = engine.execute(
                f"SELECT {engine.quote('_dq_rule')}, count(*) FROM {quarantine_target} "
                f"WHERE {engine.quote('_dq_run_id')} = '{run_id}' GROUP BY 1 ORDER BY 2 DESC"
            ).rows
            summary = ", ".join(f"{rule} ({count})" for rule, count in reasons[:4])
            actions.append(
                f"Quarantined {rejected:,} row(s) rather than dropping them: {summary}. "
                f"They are in {quarantine_table} with the rule that caught them."
            )
        elif checked.rejecting_rules:
            actions.append(
                f"Every row passed all {len(checked.rejecting_rules)} quality rules"
            )
        else:
            # Silence here would read as "quality checks passed" when in fact none ran.
            actions.append(
                "No rejecting quality rules apply to this table yet, so no row could be "
                "quarantined. Profiling found no key and no mistyped column to check."
            )

        if not item.merge_keys:
            notes.append(
                "This table has no key, so each run replaces it rather than merging. "
                "Incremental loading needs a column that identifies a row."
            )

        if checked.warning_summary:
            warned = engine.execute(checked.warning_summary).rows
            for rule, count in warned:
                notes.append(f"{count:,} row(s) triggered the warning '{rule}' but were kept.")

    except AssariumError as exc:
        _close_step(step, "failed", message=exc.message, actions=actions, duration=started)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.exception("Silver refinement failed for %s", item.name)
        _close_step(step, "failed", message=str(exc)[:400], actions=actions, duration=started)
        return False

    _close_step(
        step,
        "succeeded",
        rows_out=rows,
        target=target,
        sql=transform.sql,
        actions=actions,
        notes=notes,
        duration=started,
    )
    return True


def _project_profile(profile: DatasetProfile, engine: Engine, table: str) -> DatasetProfile:
    """
    Re-express a bronze profile in terms of the silver table that was actually built.

    Silver renames columns and drops ingestion metadata, so planning gold straight from
    the bronze profile references columns that no longer exist.
    """
    surviving = {name for name, _ in engine.describe("silver", table)}
    projected = profile.model_copy(deep=True)
    columns = []
    for column in projected.columns:
        alias = safe_identifier(column.name)
        if alias not in surviving:
            continue
        column.name = alias
        # Silver applied the cast, so the column now genuinely holds that type.
        if column.shadow_type:
            column.logical_type = column.shadow_type
            column.shadow_type = None
        columns.append(column)
    projected.columns = columns
    return projected


def _build_gold(engine: Engine, item: _DatasetPlan, sequence: int, run_id: str) -> str | None:
    raw_profile = DatasetProfile.model_validate(item.profile) if item.profile else DatasetProfile()
    profile = _project_profile(raw_profile, engine, item.table)
    source = engine.qualified("silver", item.table)

    # Data that is already at a reporting grain is carried across rather than aggregated
    # again; summarising a summary would silently change what the numbers mean.
    if item.layer == "gold":
        step = _open_step(run_id, sequence, item, kind="gold", layer="gold")
        started = time.perf_counter()
        try:
            target = engine.qualified("gold", item.table)
            engine.execute_ddl(f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {source}")
            rows = engine.row_count("gold", item.table)
        except AssariumError as exc:
            _close_step(step, "failed", message=exc.message, duration=started)
            return None
        _close_step(
            step,
            "succeeded",
            rows_out=rows,
            target=target,
            actions=["Carried through unchanged: this data is already at a reporting grain"],
            duration=started,
        )
        return item.table

    name = f"{item.table}_summary"
    transform = plan_gold(engine, source, name, profile)
    if transform is None:
        step = _open_step(run_id, sequence, item, kind="gold", layer="gold")
        _close_step(
            step,
            "skipped",
            message="No measures and grain to summarise by. This table is a dimension or "
            "reference list; it joins to a fact rather than becoming one.",
        )
        return None

    step = _open_step(run_id, sequence, item, kind="gold", layer="gold")
    started = time.perf_counter()
    try:
        target = engine.qualified("gold", name)
        engine.execute_ddl(f"CREATE OR REPLACE TABLE {target} AS {transform.sql}")
        rows = engine.row_count("gold", name)
    except AssariumError as exc:
        _close_step(step, "failed", message=exc.message, duration=started)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Gold build failed for %s", item.name)
        _close_step(step, "failed", message=str(exc)[:400], duration=started)
        return None

    _close_step(
        step,
        "succeeded",
        rows_out=rows,
        target=target,
        sql=transform.sql,
        actions=transform.actions,
        notes=transform.notes,
        duration=started,
    )
    return name


# --------------------------------------------------------------------------------------
# step bookkeeping
# --------------------------------------------------------------------------------------


def _open_step(run_id: str, sequence: int, item: _DatasetPlan, *, kind: str, layer: Layer) -> str:
    with session_scope() as db:
        step = PipelineStep(
            tenant_id=item.tenant_id,
            run_id=run_id,
            sequence=sequence,
            dataset_id=item.id,
            dataset_name=item.name,
            kind=kind,
            layer=layer,
            status="running",
        )
        db.add(step)
        db.flush()
        return step.id


def _close_step(
    step_id: str,
    status: str,
    *,
    rows_in: int | None = None,
    rows_out: int | None = None,
    target: str | None = None,
    sql: str | None = None,
    actions: list[str] | None = None,
    notes: list[str] | None = None,
    message: str | None = None,
    duration: float | None = None,
) -> None:
    with session_scope() as db:
        step = db.get(PipelineStep, step_id)
        if step is None:  # pragma: no cover
            return
        step.status = status
        step.rows_in = rows_in
        step.rows_out = rows_out
        step.target_table = target
        step.sql = sql
        step.actions = actions
        step.notes = notes
        step.message = message
        if duration is not None:
            step.duration_ms = int((time.perf_counter() - duration) * 1000)


def _merge_keys(dataset) -> list[str]:
    """
    The columns an incremental load matches on.

    Taken from the key profiling actually established, not guessed: merging on the wrong
    column silently overwrites unrelated rows, which is worse than not merging at all.
    """
    profile = dataset.profile or {}
    candidates = profile.get("key_candidates") or []
    if not candidates:
        return []
    return [safe_identifier(column) for column in candidates[0]]
