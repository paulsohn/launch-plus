// AUTO-GENERATED — do not edit manually.
// Source: roscope/visualizer/schema.py
// Run: python -m roscope.visualizer.generate_types

export interface ParamEntry {
  name: string;
  value: string;
}

export interface RemapEntry {
  from: string;
  to: string;
}

export interface GraphNode {
  id: string;
  type: "node" | "lifecycle_node" | "container" | "composable_node" | "load_composable_node" | "executable";
  parent?: string | null;
  color: string;
  params: ParamEntry[];
  remaps: RemapEntry[];
  package?: string | null;
  executable?: string | null;
  plugin?: string | null;
  name?: string | null;
  namespace?: string | null;
  fqn?: string | null;
  cmd?: string | null;
  target?: string | null;
}

export interface GraphGroup {
  id: string;
  source?: string | null;
  parent?: string | null;
}

export interface GraphTopic {
  id: string;
  name: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  type: "remap" | "load_target";
}

export interface GraphMetadata {
  package: string;
  launcher: string;
  timestamp: string;
}

export interface GraphData {
  metadata: GraphMetadata;
  nodes: GraphNode[];
  groups: GraphGroup[];
  topics: GraphTopic[];
  edges: GraphEdge[];
}

export interface Snapshot {
  vizId: string;
  timestamp: string;
  graph: GraphData;
}
