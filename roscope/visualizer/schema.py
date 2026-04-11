"""Graph IR schema — single source of truth for Python and TypeScript.

These dataclasses define the JSON structure exchanged between the Python
server and the TypeScript frontend.  Run ``generate_types.py`` to emit
the corresponding TypeScript interfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ── Graph IR ──────────────────────────────────────────────────────────


@dataclass
class ParamEntry:
    name: str
    value: str


@dataclass
class RemapEntry:
    from_: str
    to: str


@dataclass
class GraphNode:
    id: str
    type: Literal[
        "node",
        "lifecycle_node",
        "container",
        "composable_node",
        "load_composable_node",
        "executable",
    ]
    parent: str | None
    color: str
    params: list[ParamEntry] = field(default_factory=list)
    remaps: list[RemapEntry] = field(default_factory=list)
    package: str | None = None
    executable: str | None = None
    plugin: str | None = None
    name: str | None = None
    namespace: str | None = None
    fqn: str | None = None
    cmd: str | None = None
    target: str | None = None


@dataclass
class ArgEntry:
    name: str
    value: str
    is_default: bool


@dataclass
class GraphGroup:
    id: str
    source: str | None
    parent: str | None
    group_type: Literal["include", "lcn_wrapper"] = "include"
    args: list[ArgEntry] = field(default_factory=list)
    include_args: dict[str, str] | None = None


@dataclass
class GraphTopic:
    id: str
    name: str
    is_private: bool = False


@dataclass
class GraphEdge:
    source: str
    target: str
    type: Literal["remap", "load_target"]


@dataclass
class GraphMetadata:
    package: str
    launcher: str
    timestamp: str


@dataclass
class GraphData:
    metadata: GraphMetadata
    nodes: list[GraphNode]
    groups: list[GraphGroup]
    topics: list[GraphTopic]
    edges: list[GraphEdge]


# ── REST API types ────────────────────────────────────────────────────


@dataclass
class Snapshot:
    """A single graph snapshot as stored in cache and returned by /api/catalog."""

    viz_id: str
    timestamp: str
    graph: GraphData
