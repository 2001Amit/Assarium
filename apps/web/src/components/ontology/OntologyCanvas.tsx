"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import { cn } from "@/lib/cn";
import { formatCount } from "@/lib/format";
import type { OntologyEdge, OntologyGraph, OntologyNode } from "@/lib/types";

interface LaidOutNode extends SimulationNodeDatum, OntologyNode {
  radius: number;
}

type LaidOutEdge = SimulationLinkDatum<LaidOutNode> & OntologyEdge;

const KIND_STYLE = {
  fact: { fill: "var(--accent)", label: "Fact" },
  dimension: { fill: "var(--color-silver)", label: "Dimension" },
  reference: { fill: "var(--text-subtle)", label: "Reference" },
} as const;

/** Node size tracks how much you can ask of an entity, not how many rows it holds. */
function radiusFor(node: OntologyNode): number {
  return 20 + Math.min(18, Math.sqrt(node.measure_count + node.dimension_count) * 4);
}

export function OntologyCanvas({
  graph,
  selected,
  onSelect,
}: {
  graph: OntologyGraph;
  selected: string | null;
  onSelect: (id: string | null) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 900, height: 560 });
  const [nodes, setNodes] = useState<LaidOutNode[]>([]);
  const [edges, setEdges] = useState<LaidOutEdge[]>([]);
  const [hovered, setHovered] = useState<string | null>(null);
  const simulationRef = useRef<Simulation<LaidOutNode, LaidOutEdge> | null>(null);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setSize({ width, height });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const signature = useMemo(
    () => graph.nodes.map((n) => n.id).join("|") + "::" + graph.edges.map((e) => e.id).join("|"),
    [graph],
  );

  useEffect(() => {
    const simulationNodes: LaidOutNode[] = graph.nodes.map((node) => ({
      ...node,
      radius: radiusFor(node),
    }));
    const simulationEdges: LaidOutEdge[] = graph.edges.map((edge) => ({ ...edge }));

    const simulation = forceSimulation(simulationNodes)
      .force(
        "link",
        forceLink<LaidOutNode, LaidOutEdge>(simulationEdges)
          .id((node) => node.id)
          .distance(170)
          .strength(0.7),
      )
      .force("charge", forceManyBody().strength(-620))
      .force("center", forceCenter(size.width / 2, size.height / 2))
      .force("collide", forceCollide<LaidOutNode>().radius((node) => node.radius + 26))
      // Gentle centring keeps disconnected entities from drifting off the canvas.
      .force("x", forceX(size.width / 2).strength(0.045))
      .force("y", forceY(size.height / 2).strength(0.06))
      .stop();

    // Run the layout to completion up front: a graph that settles while you read it is
    // harder to use than one that is simply correct when it appears.
    simulation.tick(320);
    simulationRef.current = simulation;
    setNodes([...simulationNodes]);
    setEdges([...simulationEdges]);

    return () => {
      simulation.stop();
      simulationRef.current = null;
    };
  }, [signature, size.width, size.height, graph]);

  const connected = useMemo(() => {
    const focus = selected ?? hovered;
    if (!focus) return null;
    const ids = new Set<string>([focus]);
    for (const edge of graph.edges) {
      const source = typeof edge.source === "string" ? edge.source : (edge.source as LaidOutNode).id;
      const target = typeof edge.target === "string" ? edge.target : (edge.target as LaidOutNode).id;
      if (source === focus) ids.add(target);
      if (target === focus) ids.add(source);
    }
    return ids;
  }, [selected, hovered, graph.edges]);

  return (
    <div ref={containerRef} className="relative h-full w-full overflow-hidden">
      <svg
        width={size.width}
        height={size.height}
        className="hairline-grid absolute inset-0"
        style={{ backgroundSize: "28px 28px" }}
        onClick={() => onSelect(null)}
        role="img"
        aria-label="Entity relationship graph"
      >
        <defs>
          <marker
            id="arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="5"
            markerHeight="5"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--line-strong)" />
          </marker>
        </defs>

        {edges.map((edge) => {
          const source = edge.source as LaidOutNode;
          const target = edge.target as LaidOutNode;
          if (!source?.x || !target?.x) return null;
          const dimmed =
            connected !== null && !(connected.has(source.id) && connected.has(target.id));

          // Stop the line at the node boundary so the arrowhead is not buried.
          const dx = (target.x ?? 0) - (source.x ?? 0);
          const dy = (target.y ?? 0) - (source.y ?? 0);
          const length = Math.hypot(dx, dy) || 1;
          const x1 = (source.x ?? 0) + (dx / length) * source.radius;
          const y1 = (source.y ?? 0) + (dy / length) * source.radius;
          const x2 = (target.x ?? 0) - (dx / length) * (target.radius + 6);
          const y2 = (target.y ?? 0) - (dy / length) * (target.radius + 6);

          return (
            <g key={edge.id} opacity={dimmed ? 0.18 : 1}>
              <line
                x1={x1}
                y1={y1}
                x2={x2}
                y2={y2}
                stroke="var(--line-strong)"
                strokeWidth={1}
                strokeDasharray={edge.kind === "inferred" ? "4 3" : undefined}
                markerEnd="url(#arrow)"
              />
              <text
                x={(x1 + x2) / 2}
                y={(y1 + y2) / 2 - 5}
                textAnchor="middle"
                className="fill-[var(--text-subtle)] font-mono"
                style={{ fontSize: 9.5 }}
              >
                {edge.label}
              </text>
            </g>
          );
        })}

        {nodes.map((node) => {
          const style = KIND_STYLE[node.kind];
          const dimmed = connected !== null && !connected.has(node.id);
          const isSelected = selected === node.id;
          return (
            <g
              key={node.id}
              transform={`translate(${node.x ?? 0}, ${node.y ?? 0})`}
              opacity={dimmed ? 0.22 : 1}
              className="cursor-pointer"
              onMouseEnter={() => setHovered(node.id)}
              onMouseLeave={() => setHovered(null)}
              onClick={(event) => {
                event.stopPropagation();
                onSelect(isSelected ? null : node.id);
              }}
            >
              <circle
                r={node.radius}
                fill="var(--surface)"
                stroke={style.fill}
                strokeWidth={isSelected ? 2 : 1.25}
              />
              <circle r={node.radius - 5} fill={style.fill} opacity={isSelected ? 0.2 : 0.1} />
              <text
                textAnchor="middle"
                y={4}
                className="fill-[var(--text)] font-medium"
                style={{ fontSize: 11 }}
              >
                {node.measure_count > 0 ? node.measure_count : ""}
              </text>
              <text
                textAnchor="middle"
                y={node.radius + 14}
                className="fill-[var(--text)] font-medium"
                style={{ fontSize: 11.5 }}
              >
                {node.label}
              </text>
              <text
                textAnchor="middle"
                y={node.radius + 26}
                className="fill-[var(--text-subtle)]"
                style={{ fontSize: 10 }}
              >
                {formatCount(node.row_count)} rows
              </text>
            </g>
          );
        })}
      </svg>

      <Legend />
    </div>
  );
}

function Legend() {
  return (
    <div className="pointer-events-none absolute bottom-3 left-3 flex flex-col gap-1 rounded-[var(--radius-md)] border border-[var(--line)] bg-[var(--surface)]/92 px-2.5 py-2 backdrop-blur-sm">
      {(["fact", "dimension", "reference"] as const).map((kind) => (
        <span key={kind} className="flex items-center gap-1.5 text-[10.5px] text-[var(--text-muted)]">
          <span
            className="size-2 rounded-full border"
            style={{
              borderColor: KIND_STYLE[kind].fill,
              background: `color-mix(in srgb, ${KIND_STYLE[kind].fill} 25%, transparent)`,
            }}
            aria-hidden
          />
          {KIND_STYLE[kind].label}
        </span>
      ))}
      <span className="mt-0.5 flex items-center gap-1.5 text-[10.5px] text-[var(--text-subtle)]">
        <svg width="16" height="4" aria-hidden>
          <line x1="0" y1="2" x2="16" y2="2" stroke="var(--line-strong)" strokeDasharray="4 3" />
        </svg>
        inferred join
      </span>
    </div>
  );
}

export function NodeDetail({
  node,
  edges,
  nodes,
  onClose,
}: {
  node: OntologyNode;
  edges: OntologyEdge[];
  nodes: OntologyNode[];
  onClose: () => void;
}) {
  const label = (id: string) => nodes.find((n) => n.id === id)?.label ?? id;
  const outgoing = edges.filter((e) => e.source === node.id);
  const incoming = edges.filter((e) => e.target === node.id);

  return (
    <aside className="w-[300px] shrink-0 overflow-y-auto border-l border-[var(--line)] bg-[var(--surface)] p-4">
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <h2 className="text-[13px] font-semibold text-[var(--text)]">{node.label}</h2>
          <p className="font-mono text-[11px] text-[var(--text-subtle)]">
            {node.layer}.{node.table}
          </p>
        </div>
        <button
          onClick={onClose}
          className="text-[11px] text-[var(--text-subtle)] hover:text-[var(--text)]"
        >
          Close
        </button>
      </div>

      <dl className="mb-4 grid grid-cols-2 gap-y-2">
        {[
          ["Rows", formatCount(node.row_count)],
          ["Measures", String(node.measure_count)],
          ["Dimensions", String(node.dimension_count)],
          ["Attributes", String(node.attribute_count)],
        ].map(([term, value]) => (
          <div key={term}>
            <dt className="text-[11px] text-[var(--text-subtle)]">{term}</dt>
            <dd className="text-[13px] font-medium tabular-nums text-[var(--text)]">{value}</dd>
          </div>
        ))}
      </dl>

      {node.primary_key.length > 0 && (
        <Section title="Identified by">
          <code className="font-mono text-[11.5px] text-[var(--text-muted)]">
            {node.primary_key.join(" + ")}
          </code>
        </Section>
      )}

      {node.time_columns.length > 0 && (
        <Section title="Can be trended on">
          <code className="font-mono text-[11.5px] text-[var(--text-muted)]">
            {node.time_columns.join(", ")}
          </code>
        </Section>
      )}

      {node.pii_attributes.length > 0 && (
        <Section title="Personal data">
          <p className="text-[11.5px] leading-relaxed text-[var(--color-critical)]">
            {node.pii_attributes.join(", ")}
          </p>
        </Section>
      )}

      {(outgoing.length > 0 || incoming.length > 0) && (
        <Section title="Joins">
          <ul className="space-y-1">
            {outgoing.map((edge) => (
              <li key={edge.id} className="text-[11.5px] text-[var(--text-muted)]">
                <span className="text-[var(--text-subtle)]">many &rarr; one</span>{" "}
                {label(edge.target)}
                <span className="block font-mono text-[10.5px] text-[var(--text-subtle)]">
                  {edge.label}
                </span>
              </li>
            ))}
            {incoming.map((edge) => (
              <li key={edge.id} className="text-[11.5px] text-[var(--text-muted)]">
                <span className="text-[var(--text-subtle)]">one &larr; many</span>{" "}
                {label(edge.source)}
                <span className="block font-mono text-[10.5px] text-[var(--text-subtle)]">
                  {edge.label}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {node.degree === 0 && (
        <p
          className={cn(
            "rounded-[var(--radius-md)] border border-[var(--color-caution)]/25",
            "bg-[var(--color-caution)]/6 px-2.5 py-2 text-[11.5px] leading-relaxed text-[var(--text-muted)]",
          )}
        >
          Nothing joins to this entity, so it cannot be combined with any other in one
          question yet.
        </p>
      )}
    </aside>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-3.5">
      <p className="mb-1 text-[10.5px] font-semibold uppercase tracking-[0.06em] text-[var(--text-subtle)]">
        {title}
      </p>
      {children}
    </section>
  );
}
