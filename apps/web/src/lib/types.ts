// Mirrors app/connectors/types.py. Kept hand-written and small rather than generated,
// so the contract stays readable at the boundary.

export type FieldType =
  | "text"
  | "password"
  | "textarea"
  | "number"
  | "select"
  | "boolean"
  | "file";

export interface FieldOption {
  value: string;
  label: string;
}

export interface SpecField {
  name: string;
  label: string;
  type: FieldType;
  required: boolean;
  secret: boolean;
  placeholder: string | null;
  help: string | null;
  default: unknown;
  options: FieldOption[] | null;
  show_if: Record<string, string[]> | null;
  group: string;
}

export interface AuthMethod {
  id: string;
  label: string;
  description: string;
  fields: SpecField[];
  recommended: boolean;
  deprecated: boolean;
  notice: string | null;
}

export type SourceCategory =
  | "warehouse"
  | "database"
  | "lakehouse"
  | "object_store"
  | "saas"
  | "file";

export interface BrowseLevel {
  key: string;
  label: string;
  plural: string;
}

export interface SourceSpec {
  source_id: string;
  name: string;
  category: SourceCategory;
  summary: string;
  icon: string;
  docs_url: string | null;
  capabilities: {
    sql: boolean;
    incremental: boolean;
    row_count_estimate: boolean;
    levels: BrowseLevel[];
  };
  auth_methods: AuthMethod[];
  fields: SpecField[];
  available: boolean;
  install_hint: string | null;
}

export interface ConnectionTestResult {
  ok: boolean;
  message: string;
  latency_ms: number | null;
  server_version: string | null;
  details: Record<string, string>;
}

export interface Connection {
  id: string;
  name: string;
  source_id: string;
  source_name: string;
  auth_method: string;
  config: Record<string, unknown>;
  status: "untested" | "ok" | "failed";
  status_message: string | null;
  last_tested_at: string | null;
  created_at: string;
  dataset_count: number;
}

export type NodeKind = "container" | "namespace" | "dataset" | "folder";

export interface BrowseNode {
  id: string;
  name: string;
  kind: NodeKind;
  path: string[];
  has_children: boolean;
  row_estimate: number | null;
  size_bytes: number | null;
  meta: Record<string, string | null>;
}

export interface ColumnSchema {
  name: string;
  native_type: string;
  logical_type: string;
  nullable: boolean;
  position: number;
  primary_key: boolean;
  comment: string | null;
}

export interface DatasetSchema {
  path: string[];
  name: string;
  columns: ColumnSchema[];
  row_estimate: number | null;
  comment: string | null;
}

export interface SampleResult {
  columns: string[];
  rows: unknown[][];
  truncated: boolean;
}

export interface ApiError {
  code: string;
  message: string;
  details: Record<string, unknown>;
}

// --- Phase 2: selection, profiling, inference ----------------------------------------

export type Layer = "bronze" | "silver" | "gold";

export interface Dataset {
  id: string;
  connection_id: string;
  name: string;
  path: string[];
  row_estimate: number | null;
  column_count: number;
  detected_layer: Layer | null;
  layer_confidence: number | null;
  layer_override: Layer | null;
  effective_layer: Layer | null;
  status: string;
  profiled_at: string | null;
}

export interface LayerSignal {
  id: string;
  label: string;
  observation: string;
  verdict: Layer | "neutral";
  weight: number;
  detail: string | null;
}

export interface LayerVerdict {
  layer: Layer;
  confidence: number;
  scores: Record<Layer, number>;
  signals: LayerSignal[];
  summary: string;
  recommended_action: string;
}

export interface PiiFinding {
  kind: string;
  confidence: number;
  basis: "name" | "value" | "name+value";
  matched_ratio: number;
}

export interface TopValue {
  value: string;
  count: number;
  pct: number;
}

export interface ColumnProfile {
  name: string;
  native_type: string;
  logical_type: string;
  position: number;
  null_count: number;
  null_pct: number;
  distinct_count: number | null;
  distinct_pct: number | null;
  is_unique: boolean;
  is_constant: boolean;
  minimum: string | null;
  maximum: string | null;
  mean: number | null;
  stddev: number | null;
  p25: number | null;
  p50: number | null;
  p75: number | null;
  mean_length: number | null;
  max_length: number | null;
  blank_count: number;
  top_values: TopValue[];
  shadow_type: string | null;
  shadow_ratio: number;
  pii: PiiFinding | null;
  exact_counts: boolean;
}

export interface DatasetProfile {
  row_count: number | null;
  row_count_exact: boolean;
  sampled_rows: number;
  duplicate_row_ratio: number;
  columns: ColumnProfile[];
  key_candidates: string[][];
  warnings: string[];
}

export interface DatasetDetail extends Dataset {
  columns: ColumnSchema[];
  profile: DatasetProfile | null;
  layer_evidence: LayerVerdict | null;
}

export interface Relationship {
  id: string;
  from_dataset_id: string;
  from_dataset: string;
  from_column: string;
  to_dataset_id: string;
  to_dataset: string;
  to_column: string;
  kind: "declared" | "inferred";
  confidence: number;
  overlap: number | null;
  cardinality: "many_to_one" | "one_to_one" | "unknown";
  accepted: boolean;
}

// --- Phase 3: medallion pipeline ------------------------------------------------------

export interface PipelineStep {
  id: string;
  sequence: number;
  dataset_name: string;
  kind: "ingest" | "silver" | "gold";
  layer: Layer;
  status: "running" | "succeeded" | "failed" | "skipped";
  target_table: string | null;
  rows_out: number | null;
  duration_ms: number | null;
  actions: string[] | null;
  notes: string[] | null;
  message: string | null;
  sql: string | null;
}

export interface PipelineRun {
  id: string;
  connection_id: string;
  engine: string;
  status: "running" | "succeeded" | "failed" | "partial";
  started_at: string;
  finished_at: string | null;
  dataset_count: number;
  summary: { tables: Record<Layer, string[]>; failures: number; engine: string } | null;
  steps: PipelineStep[];
}

export interface WarehouseTable {
  name: string;
  rows: number;
  columns: { name: string; type: string }[];
}

export interface Warehouse {
  engine: string;
  layers: Record<Layer, WarehouseTable[]>;
}

// --- Phase 4: semantic model and ontology --------------------------------------------

export type AttributeRole = "key" | "foreign_key" | "dimension" | "time" | "attribute";
export type Aggregation =
  | "sum" | "avg" | "min" | "max" | "count" | "count_distinct" | "median" | "ratio";
export type TimeGrain = "day" | "week" | "month" | "quarter" | "year";

export interface SemanticAttribute {
  name: string;
  label: string;
  column: string;
  logical_type: string;
  role: AttributeRole;
  description: string | null;
  cardinality: number | null;
  contains_pii: boolean;
  hidden: boolean;
}

export interface SemanticEntity {
  id: string;
  name: string;
  label: string;
  description: string | null;
  layer: Layer;
  table: string;
  primary_key: string[];
  attributes: SemanticAttribute[];
  is_fact: boolean;
  row_count: number;
}

export interface SemanticMeasure {
  id: string;
  name: string;
  label: string;
  description: string | null;
  entity_id: string;
  aggregation: Aggregation;
  column: string | null;
  format: "number" | "currency" | "percent" | "duration";
  decimals: number;
  constraint: string | null;
  generated: boolean;
}

export interface SemanticJoin {
  id: string;
  from_entity: string;
  from_column: string;
  to_entity: string;
  to_column: string;
  cardinality: "many_to_one" | "one_to_one" | "one_to_many";
  confidence: number;
  kind: "declared" | "inferred" | "manual";
}

export interface SemanticModel {
  connection_id: string;
  entities: SemanticEntity[];
  measures: SemanticMeasure[];
  joins: SemanticJoin[];
  notes: string[];
}

export interface MetricFilter {
  field: string;
  operator: string;
  values: unknown[];
}

export interface MetricQuery {
  measures: string[];
  dimensions?: string[];
  filters?: MetricFilter[];
  time_dimension?: string | null;
  time_grain?: TimeGrain | null;
  order_by?: { field: string; direction: "asc" | "desc" }[];
  limit?: number;
}

export interface MetricResult {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  elapsed_ms: number;
  truncated: boolean;
  sql: string;
  dimension_columns: string[];
  measure_columns: string[];
  notes: string[];
}

export interface OntologyNode {
  id: string;
  label: string;
  kind: "fact" | "dimension" | "reference";
  table: string;
  layer: Layer;
  row_count: number;
  measure_count: number;
  attribute_count: number;
  dimension_count: number;
  time_columns: string[];
  primary_key: string[];
  pii_attributes: string[];
  degree: number;
}

export interface OntologyEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  cardinality: string;
  kind: string;
  confidence: number;
}

export interface OntologyGraph {
  nodes: OntologyNode[];
  edges: OntologyEdge[];
  isolated: string[];
  notes: string[];
}

// --- Phase 5: dashboards --------------------------------------------------------------

export type TileType =
  | "stat"
  | "line"
  | "area"
  | "bar"
  | "stacked_bar"
  | "combo"
  | "donut"
  | "scatter"
  | "table";

export interface Tile {
  id: string;
  type: TileType;
  title: string;
  subtitle: string | null;
  measures: string[];
  dimension: string | null;
  time_dimension: string | null;
  time_grain: TimeGrain;
  filters: MetricFilter[];
  limit: number;
  drill_path: string[];
  width: number;
  height: number;
}

export interface Dashboard {
  id: string;
  connection_id: string;
  name: string;
  description: string | null;
  tiles: Tile[];
  filters: MetricFilter[];
  time_dimension: string | null;
  time_grain: TimeGrain;
  generated: boolean;
  notes: string[];
}

export interface DashboardSummary {
  id: string;
  connection_id: string;
  name: string;
  tile_count: number;
  generated: boolean;
}

export interface Point {
  label: string;
  raw: unknown;
  values: Record<string, number | null>;
}

export interface StatSummary {
  measure: string;
  value: number | null;
  previous: number | null;
  delta: number | null;
  delta_pct: number | null;
  direction: "up" | "down" | "flat" | "unknown";
  sparkline: (number | null)[];
  period_label: string | null;
}

export interface TileResult {
  tile_id: string;
  type: TileType;
  title: string;
  columns: string[];
  dimension_columns: string[];
  measure_columns: string[];
  /** Measure ids behind those columns, in the same order. Column names are not unique. */
  measure_ids?: string[];
  /** Measures the server placed on a second axis. Combo tiles only. */
  secondary_measure_columns?: string[];
  points: Point[];
  rows: unknown[][];
  stats: StatSummary[];
  sql: string[];
  elapsed_ms: number;
  truncated: boolean;
  notes: string[];
  error: string | null;
}

export interface DashboardData {
  dashboard_id: string;
  tiles: TileResult[];
  elapsed_ms: number;
}

// --- Phase 6: AI / chat ---------------------------------------------------------------

export type ChatChartHint = "bar" | "line" | "table" | "stat" | "none";

export interface ChatQueryResult {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  elapsed_ms: number;
  truncated: boolean;
  sql: string;
  dimension_columns: string[];
  measure_columns: string[];
  notes: string[];
  chart_hint: ChatChartHint;
}

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  query_result?: ChatQueryResult | null;
}

export interface ChatResponse {
  message: ChatMessage;
  query_result?: ChatQueryResult | null;
}

export interface KpiExplanation {
  tile_id: string;
  measure: string;
  explanation: string;
  suggestions: string[];
}


// --- Phase 7: orchestration & schedules -----------------------------------------------

export interface Schedule {
  id: string;
  connection_id: string;
  connection_name: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_id: string | null;
  last_status: "succeeded" | "failed" | null;
  last_error: string | null;
  consecutive_failures: number;
  max_retries: number;
  locked_at: string | null;
  locked_by: string | null;
  created_at: string;
}

export interface ScheduleOverview {
  total: number;
  active: number;
  paused: number;
  failing: number;
  schedules: Schedule[];
}

// --- Filter-bar support ---------------------------------------------------------------

export interface DimensionValues {
  field: string;
  label: string;
  values: (string | number | null)[];
  truncated: boolean;
}

// --- Lineage ---------------------------------------------------------------------------

export interface LineageNode {
  kind: "source" | "bronze" | "silver" | "gold" | "measure" | "tile";
  table: string;
  column: string;
  id: string;
}

export interface LineageEdge {
  source: LineageNode;
  target: LineageNode;
  via: string;
  transform: string | null;
}

export interface LineageCoverage {
  complete: boolean;
  unresolved_steps: { step: string; reason: string }[];
  edge_count: number;
}

export interface LineageGraph {
  edges: LineageEdge[];
  coverage: LineageCoverage;
}

export interface LineageTrace {
  of: LineageNode;
  direction: "upstream" | "downstream";
  edges: LineageEdge[];
  origins?: string[];
  tiles?: string[];
  measures?: string[];
  tables?: string[];
  coverage: LineageCoverage;
}
