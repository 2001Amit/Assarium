from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.semantic.types import SemanticModel

NodeKind = Literal["fact", "dimension", "reference"]


class GraphNode(BaseModel):
    id: str
    label: str
    kind: NodeKind
    table: str
    layer: str
    row_count: int
    measure_count: int
    attribute_count: int
    dimension_count: int
    time_columns: list[str] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    pii_attributes: list[str] = Field(default_factory=list)
    # How many edges touch this node, so layout can weight the hubs.
    degree: int = 0


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str
    cardinality: str
    kind: str
    confidence: float


class OntologyGraph(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    # Entities nothing joins to. Usually the most useful thing on the screen: it means a
    # question spanning them cannot be answered yet.
    isolated: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def build_graph(model: SemanticModel) -> OntologyGraph:
    """
    Render the semantic model as a graph.

    A node's kind comes from how it is used, not from what it is called: a table other
    tables point at is a dimension, a table carrying measures is a fact, and a table doing
    neither is a reference list that nothing can currently be asked about.
    """
    measures_by_entity: dict[str, int] = {}
    for measure in model.measures:
        measures_by_entity[measure.entity_id] = measures_by_entity.get(measure.entity_id, 0) + 1

    pointed_at = {join.to_entity for join in model.joins}
    degrees: dict[str, int] = {}
    for join in model.joins:
        degrees[join.from_entity] = degrees.get(join.from_entity, 0) + 1
        degrees[join.to_entity] = degrees.get(join.to_entity, 0) + 1

    nodes: list[GraphNode] = []
    for entity in model.entities:
        # record_count exists on every entity, so a fact needs more than that alone.
        measure_count = measures_by_entity.get(entity.id, 0)
        if measure_count > 1:
            kind: NodeKind = "fact"
        elif entity.id in pointed_at:
            kind = "dimension"
        else:
            kind = "reference"

        nodes.append(
            GraphNode(
                id=entity.id,
                label=entity.label,
                kind=kind,
                table=entity.table,
                layer=entity.layer,
                row_count=entity.row_count,
                measure_count=measure_count,
                attribute_count=len(entity.attributes),
                dimension_count=len(
                    [a for a in entity.attributes if a.role == "dimension" and not a.hidden]
                ),
                time_columns=[a.name for a in entity.attributes if a.role == "time"],
                primary_key=entity.primary_key,
                pii_attributes=[a.name for a in entity.attributes if a.contains_pii],
                degree=degrees.get(entity.id, 0),
            )
        )

    edges = [
        GraphEdge(
            id=join.id,
            source=join.from_entity,
            target=join.to_entity,
            label=f"{join.from_column} → {join.to_column}",
            cardinality=join.cardinality,
            kind=join.kind,
            confidence=join.confidence,
        )
        for join in model.joins
    ]

    isolated = [node.id for node in nodes if node.degree == 0]
    notes = list(model.notes)
    if isolated and len(nodes) > 1:
        labels = ", ".join(
            next(n.label for n in nodes if n.id == entity_id) for entity_id in isolated
        )
        notes.append(
            f"Nothing joins to {labels}. Questions that combine it with another entity "
            "cannot be answered until a relationship is defined."
        )

    return OntologyGraph(nodes=nodes, edges=edges, isolated=isolated, notes=notes)
