"""Convert resolved action tree to a JSON-serializable graph structure.

The graph IR bridges between Python Action objects and the Cytoscape.js
frontend. It produces nodes (vertices), groups (compound nodes), topics,
and edges suitable for hierarchical graph layout.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def actions_to_graph(
    actions: list,
    package: str,
    launcher: str,
) -> dict:
    """Convert a resolved action list to a graph dict.

    Returns a dict with keys: metadata, nodes, groups, topics, edges.
    """
    builder = _GraphBuilder(package, launcher)
    builder.walk(actions, parent_id=None)
    builder.resolve_load_targets()
    return builder.to_dict()


class _GraphBuilder:
    """Stateful builder that walks the action tree and accumulates graph elements."""

    def __init__(self, package: str, launcher: str):
        self.package = package
        self.launcher = launcher

        self._nodes: list[dict] = []
        self._groups: list[dict] = []
        self._topics: dict[str, str] = {}  # topic_name -> topic_id
        self._edges: list[dict] = []

        # For resolving LoadComposableNodes targets
        self._containers: dict[str, str] = {}  # container FQN/name -> node_id
        self._pending_load_targets: list[tuple[str, str]] = []  # (lcn_id, target_name)

        self._id_counters: dict[str, int] = {}

    def _next_id(self, prefix: str) -> str:
        count = self._id_counters.get(prefix, 0)
        self._id_counters[prefix] = count + 1
        return f"{prefix}-{count}"

    def _get_or_create_topic(self, topic_name: str) -> str:
        if topic_name in self._topics:
            return self._topics[topic_name]
        tid = self._next_id("topic")
        self._topics[topic_name] = tid
        return tid

    def _package_color(self, pkg: str) -> str:
        """Deterministic color from package name."""
        h = int(hashlib.md5(pkg.encode()).hexdigest()[:6], 16)  # noqa: S324
        hue = h % 360
        return f"hsl({hue}, 60%, 70%)"

    def walk(self, actions: list, parent_id: str | None) -> None:
        from roscope.entities.actions.executable import ExecuteProcess
        from roscope.entities.actions.group import GroupAction
        from roscope.entities.actions.marker import ArgComment, SourceMarker
        from roscope.entities.actions.node import (
            ComposableNodeContainer,
            LoadComposableNodes,
            Node,
        )

        # SourceMarker is now the first child of the GroupAction (not a sibling).
        # ArgComment children are also inside the group; skip them at this level.
        for action in actions:
            if isinstance(action, (SourceMarker, ArgComment)):
                continue

            if isinstance(action, GroupAction):
                self._handle_group(action, parent_id)
            elif isinstance(action, ComposableNodeContainer):
                self._handle_container(action, parent_id)
            elif isinstance(action, LoadComposableNodes):
                self._handle_load_composable(action, parent_id)
            elif isinstance(action, Node):
                self._handle_node(action, parent_id)
            elif isinstance(action, ExecuteProcess):
                self._handle_executable(action, parent_id)

    def _handle_group(self, action, parent_id: str | None) -> None:
        from roscope.entities.actions.marker import SourceMarker

        children = action.resolved_children or []
        if not children:
            return

        gid = self._next_id("group")

        # SourceMarker is the first child of the GroupAction.
        source = None
        if children and isinstance(children[0], SourceMarker):
            source = children[0].label()

        self._groups.append(
            {
                "id": gid,
                "source": source,
                "parent": parent_id,
            }
        )

        self.walk(children, parent_id=gid)

    def _handle_node(self, action, parent_id: str | None) -> None:
        if not action.package:
            return
        nid = self._next_id("node")
        ns = action.namespace or ""
        name = action.name or ""
        fqn = f"{ns.rstrip('/')}/{name}" if ns else name

        node_entry = {
            "id": nid,
            "type": getattr(action, "_kind", "node"),
            "package": action.package,
            "executable": action.executable or "",
            "name": name,
            "namespace": ns,
            "fqn": fqn,
            "parent": parent_id,
            "color": self._package_color(action.package),
            "params": self._extract_params(action),
            "remaps": self._extract_remaps(action),
        }
        self._nodes.append(node_entry)
        self._add_topic_edges(nid, action)

    def _handle_container(self, action, parent_id: str | None) -> None:
        if not action.package:
            return
        cid = self._next_id("container")
        ns = action.namespace or ""
        name = action.name or ""
        fqn = f"{ns.rstrip('/')}/{name}" if ns else name

        container_entry = {
            "id": cid,
            "type": "container",
            "package": action.package,
            "executable": action.executable or "",
            "name": name,
            "namespace": ns,
            "fqn": fqn,
            "parent": parent_id,
            "color": self._package_color(action.package),
            "params": self._extract_params(action),
            "remaps": self._extract_remaps(action),
        }
        self._nodes.append(container_entry)

        # Register for LoadComposableNodes target resolution
        # Register both with and without leading slash for flexible matching
        self._containers[fqn] = cid
        if name:
            self._containers[name] = cid
        if fqn.startswith("/"):
            self._containers[fqn.lstrip("/")] = cid
        else:
            self._containers["/" + fqn] = cid

        # Add composable node children
        for desc in action.composable_node_descriptions or []:
            self._handle_composable_node(desc, parent_id=cid)

    def _handle_composable_node(self, desc, parent_id: str) -> None:
        data = getattr(desc, "_resolved_data", None)
        if data is None:
            return
        cnid = self._next_id("composable")
        node_entry = {
            "id": cnid,
            "type": "composable_node",
            "package": data.get("package", ""),
            "plugin": data.get("plugin", ""),
            "name": data.get("name", ""),
            "namespace": "",
            "fqn": data.get("name", ""),
            "parent": parent_id,
            "color": self._package_color(data.get("package", "")),
            "params": [
                {"name": k, "value": v} for k, v in sorted(data.get("parameters", {}).items())
            ],
            "remaps": [{"from": r[0], "to": r[1]} for r in data.get("remappings", [])],
        }
        self._nodes.append(node_entry)

        # Topic edges from composable node remaps
        for remap in data.get("remappings", []):
            if len(remap) == 2 and remap[1]:
                tid = self._get_or_create_topic(remap[1])
                self._edges.append({"source": cnid, "target": tid, "type": "remap"})

    def _handle_load_composable(self, action, parent_id: str | None) -> None:
        if not action.composable_node_descriptions:
            return
        if not action.target:
            return

        lcn_id = self._next_id("lcn")
        self._nodes.append(
            {
                "id": lcn_id,
                "type": "load_composable_node",
                "target": action.target,
                "namespace": action.namespace or "",
                "parent": parent_id,
                "color": "hsla(210, 50%, 80%, 0.5)",
                "params": [],
                "remaps": [],
            }
        )

        # Defer target resolution (container may not exist yet)
        self._pending_load_targets.append((lcn_id, action.target))

        # Add composable node children inside the LCN compound node
        for desc in action.composable_node_descriptions or []:
            self._handle_composable_node(desc, parent_id=lcn_id)

    def _handle_executable(self, action, parent_id: str | None) -> None:
        cmd = action.cmd if isinstance(action.cmd, str) else ""
        if not cmd:
            return
        eid = self._next_id("exec")
        name = action.name if isinstance(action.name, str) else ""
        self._nodes.append(
            {
                "id": eid,
                "type": "executable",
                "name": name or cmd.split()[0] if cmd else "",
                "cmd": cmd,
                "parent": parent_id,
                "color": "hsl(30, 60%, 70%)",
                "params": [],
                "remaps": [],
            }
        )

    def resolve_load_targets(self) -> None:
        """Second pass: resolve LCN target names to container IDs."""
        for lcn_id, target_name in self._pending_load_targets:
            container_id = self._containers.get(target_name)
            if container_id is None:
                # Try stripping/adding leading slash
                alt = target_name.lstrip("/") if target_name.startswith("/") else "/" + target_name
                container_id = self._containers.get(alt)

            if container_id:
                self._edges.append(
                    {
                        "source": lcn_id,
                        "target": container_id,
                        "type": "load_target",
                    }
                )

    def _extract_params(self, action) -> list[dict]:
        params = getattr(action, "parameters", {})
        if isinstance(params, dict):
            return [{"name": k, "value": v} for k, v in sorted(params.items())]
        return []

    def _extract_remaps(self, action) -> list[dict]:
        remaps = getattr(action, "remappings", [])
        return [{"from": r[0], "to": r[1]} for r in remaps if len(r) == 2]

    def _add_topic_edges(self, node_id: str, action) -> None:
        remaps = getattr(action, "remappings", [])
        for remap in remaps:
            if len(remap) == 2 and remap[1]:
                tid = self._get_or_create_topic(remap[1])
                self._edges.append({"source": node_id, "target": tid, "type": "remap"})

    def to_dict(self) -> dict:
        topic_list = [
            {"id": tid, "name": name}
            for name, tid in sorted(self._topics.items(), key=lambda x: x[1])
        ]
        return {
            "metadata": {
                "package": self.package,
                "launcher": self.launcher,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            },
            "nodes": self._nodes,
            "groups": self._groups,
            "topics": topic_list,
            "edges": self._edges,
        }
