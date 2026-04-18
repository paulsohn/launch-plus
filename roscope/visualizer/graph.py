"""Convert resolved action tree to a JSON-serializable graph structure.

The graph IR bridges between Python Action objects and the Cytoscape.js
frontend. It produces nodes (vertices), groups (compound nodes), topics,
and edges suitable for hierarchical graph layout.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


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
        self._topics: dict[str, str] = {}  # expanded_topic_name -> topic_id
        self._topic_meta: dict[str, dict] = {}  # topic_id -> {name}
        self._edges: list[dict] = []

        # For resolving LoadComposableNodes targets
        self._containers: dict[str, str] = {}  # container FQN/name -> node_id
        self._pending_load_targets: list[tuple[str, str]] = []  # (lcn_id, target_name)
        # Deferred composable node children: (lcn_id, target_name, desc, fallback_parent)
        self._pending_composable_children: list[tuple[str, str, object, str | None]] = []

        self._uid_counters: dict[str, int] = {}

    def _uid(self, *parts: str) -> str:
        """Build a deterministic, stable ID from identity parts.

        If the same identity appears more than once (e.g. two groups
        from the same source file), a counter suffix is appended.
        """
        base = "/".join(p for p in parts if p)
        count = self._uid_counters.get(base, 0)
        self._uid_counters[base] = count + 1
        return base if count == 0 else f"{base}#{count}"

    def _get_or_create_topic(self, topic_name: str, node_fqn: str, node_ns: str) -> str:
        # Resolve topic name to absolute form following ROS 2 name rules:
        #   ~/foo  → <node_fqn>/foo   (private, node-scoped, best-effort)
        #   foo    → <node_ns>/foo    (relative, namespace-scoped)
        #   /foo   → /foo             (absolute, used as-is)
        if topic_name.startswith("~/"):
            topic_name = f"{node_fqn.rstrip('/')}/{topic_name[2:]}"
        elif not topic_name.startswith("/"):
            topic_name = f"{node_ns.rstrip('/')}/{topic_name}"
        if topic_name in self._topics:
            return self._topics[topic_name]
        tid = self._uid("topic", topic_name)
        self._topics[topic_name] = tid
        self._topic_meta[tid] = {"name": topic_name}
        return tid

    def _package_color(self, pkg: str) -> str:
        """Deterministic color from package name."""
        h = int(hashlib.md5(pkg.encode()).hexdigest()[:6], 16)  # noqa: S324
        hue = h % 360
        return f"hsl({hue}, 60%, 70%)"

    def walk(self, actions: list, parent_id: str | None) -> None:
        from roscope.entities.actions.composable_node_container import ComposableNodeContainer
        from roscope.entities.actions.executable import ExecuteProcess
        from roscope.entities.actions.group import GroupAction
        from roscope.entities.actions.load_composable_nodes import LoadComposableNodes
        from roscope.entities.actions.marker import ArgComment, SourceMarker
        from roscope.entities.actions.node import Node

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
        from roscope.entities.actions.marker import ArgComment, SourceMarker

        children = action.resolved_children or []
        if not children:
            return

        # SourceMarker is the first child of the GroupAction.
        source = None
        if children and isinstance(children[0], SourceMarker):
            source = children[0].file_path

        # Collect ArgComment entries from children
        args = []
        for child in children:
            if isinstance(child, ArgComment):
                args.append(
                    {"name": child.name, "value": child.value, "isDefault": child.is_default}
                )

        # Capture include_args from the first-child SourceMarker
        first = children[0] if children else None
        include_args = dict(first.include_args) if isinstance(first, SourceMarker) else None

        gid = self._uid("group", source or "")

        self._groups.append(
            {
                "id": gid,
                "source": source,
                "parent": parent_id,
                "groupType": "include",
                "args": args,
                "includeArgs": include_args,
            }
        )

        self.walk(children, parent_id=gid)

    def _handle_node(self, action, parent_id: str | None) -> None:
        if not action.package:
            return
        ns = action.namespace or "/"
        # Fallback to executable is best-effort; users should set name= explicitly.
        name = action.name or action.executable or ""
        if not action.name:
            logger.warning(
                "Node in package %r has no name set (executable=%r, namespace=%r); "
                "FQN is best-effort.",
                action.package,
                action.executable,
                ns,
            )
        fqn = f"{ns.rstrip('/')}/{name}"
        nid = self._uid("node", action.package, fqn)

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
        self._add_topic_edges(nid, fqn, ns, action)

    def _handle_container(self, action, parent_id: str | None) -> None:
        if not action.package:
            return
        ns = action.namespace or "/"
        # Fallback to executable is best-effort; users should set name= explicitly.
        name = action.name or action.executable or ""
        if not action.name:
            logger.warning(
                "Container in package %r has no name set (executable=%r, namespace=%r); "
                "FQN is best-effort.",
                action.package,
                action.executable,
                ns,
            )
        fqn = f"{ns.rstrip('/')}/{name}"
        cid = self._uid("container", action.package, fqn)

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

        # Register for LoadComposableNodes target resolution (exact FQN match)
        self._containers[fqn] = cid

        # Add composable node children
        for desc in action.composable_node_descriptions or []:
            self._handle_composable_node(desc, parent_id=cid)

    def _handle_composable_node(self, desc, parent_id: str) -> None:
        data = getattr(desc, "_resolved_data", None)
        if data is None:
            return
        pkg = data.get("package", "")
        plugin = data.get("plugin", "")
        cname = data.get("name", "")
        cns = data.get("namespace", "/") or "/"
        cfqn = f"{cns.rstrip('/')}/{cname}" if cname else cns
        if not cname:
            logger.warning(
                "ComposableNode with plugin %r has no name set; FQN is best-effort.",
                plugin,
            )
        cnid = self._uid("composable", parent_id, plugin, cname)
        node_entry = {
            "id": cnid,
            "type": "composable_node",
            "package": pkg,
            "plugin": plugin,
            "name": cname,
            "namespace": cns,
            "fqn": cfqn,
            "parent": parent_id,
            "color": self._package_color(pkg),
            "params": self._build_params(data.get("param_files", []), data.get("parameters", [])),
            "remaps": [{"from": r[0], "to": r[1]} for r in data.get("remappings", [])],
            "extraArgs": data.get("extra_arguments", []),
        }
        self._nodes.append(node_entry)

        # Topic edges from composable node remaps
        for remap in data.get("remappings", []):
            if len(remap) == 2 and remap[1]:
                tid = self._get_or_create_topic(remap[1], cfqn, cns)
                self._edges.append({"source": cnid, "target": tid, "type": "remap"})

    def _handle_load_composable(self, action, parent_id: str | None) -> None:
        if not action.composable_node_descriptions:
            return
        if not action.target:
            return

        lcn_id = self._uid("lcn", action.target, action.namespace or "")
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

        # Defer composable node children — they will be placed inside a
        # wrapper group in the resolved target container.
        for desc in action.composable_node_descriptions or []:
            self._pending_composable_children.append((lcn_id, action.target, desc, parent_id))

    def _handle_executable(self, action, parent_id: str | None) -> None:
        cmd = action.cmd if isinstance(action.cmd, str) else ""
        if not cmd:
            return
        name = action.name if isinstance(action.name, str) else ""
        cmd_parts = cmd.split()
        cmd0 = cmd_parts[0] if cmd_parts else ""
        eid = self._uid("exec", name or cmd0)
        self._nodes.append(
            {
                "id": eid,
                "type": "executable",
                "name": name or cmd0,
                "cmd": cmd,
                "parent": parent_id,
                "color": "hsl(30, 60%, 70%)",
                "params": [],
                "remaps": [],
            }
        )

    def resolve_load_targets(self) -> None:
        """Second pass: resolve LCN targets, create wrapper groups inside containers."""
        from collections import defaultdict

        # Group deferred composable children by lcn_id
        lcn_groups: dict[str, list[tuple[str, object, str | None]]] = defaultdict(list)
        for lcn_id, target_name, desc, fallback_parent in self._pending_composable_children:
            lcn_groups[lcn_id].append((target_name, desc, fallback_parent))

        # LCN ids that have children (will get a wrapper + edge to wrapper)
        lcn_with_children: set[str] = set()

        for lcn_id, children in lcn_groups.items():
            target_name = children[0][0]
            fallback_parent = children[0][2]
            container_id = self._containers.get(target_name)
            actual_parent = container_id if container_id is not None else fallback_parent

            # Create a wrapper group inside the container for this LCN
            wrapper_id = self._uid("lcn_wrapper", lcn_id)
            self._groups.append(
                {
                    "id": wrapper_id,
                    "source": None,
                    "parent": actual_parent,
                    "groupType": "lcn_wrapper",
                    "args": [],
                    "includeArgs": None,
                }
            )
            # Edge from the LCN leaf node to the wrapper inside the container
            self._edges.append({"source": lcn_id, "target": wrapper_id, "type": "load_target"})
            lcn_with_children.add(lcn_id)

            for _target, desc, _fallback in children:
                self._handle_composable_node(desc, parent_id=wrapper_id)

        # LCN nodes without children: edge directly to container
        for lcn_id, target_name in self._pending_load_targets:
            if lcn_id in lcn_with_children:
                continue
            container_id = self._containers.get(target_name)
            if container_id:
                self._edges.append(
                    {"source": lcn_id, "target": container_id, "type": "load_target"}
                )

    def _build_params(self, param_files: list[dict], inline: list) -> list[dict]:
        """Emit all param entries in source order (param_files then inline).

        Duplicates are preserved so the frontend can apply last-wins styling.
        """
        entries: list[dict] = []
        for pf in param_files:
            for k, v in pf.get("params", []):
                entries.append({"name": k, "value": v})
        for k, v in inline:
            entries.append({"name": k, "value": v})
        return entries

    def _extract_params(self, action) -> list[dict]:
        inline = getattr(action, "parameters", [])
        return self._build_params(
            getattr(action, "param_files", []),
            inline if isinstance(inline, list) else [],
        )

    def _extract_remaps(self, action) -> list[dict]:
        remaps = getattr(action, "remappings", [])
        return [{"from": r[0], "to": r[1]} for r in remaps if len(r) == 2]

    def _add_topic_edges(self, node_id: str, node_fqn: str, node_ns: str, action) -> None:
        remaps = getattr(action, "remappings", [])
        for remap in remaps:
            if len(remap) == 2 and remap[1]:
                tid = self._get_or_create_topic(remap[1], node_fqn, node_ns)
                self._edges.append({"source": node_id, "target": tid, "type": "remap"})

    def to_dict(self) -> dict:
        topic_list = [
            {"id": tid, **self._topic_meta[tid]} for tid in sorted(set(self._topics.values()))
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
