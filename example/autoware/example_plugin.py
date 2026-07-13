"""Example connection metadata plugin that reads per-executable interface definitions.

This example stores all interface definitions locally under ``interfaces/``,
keyed by package name.  This works for both first-party and third-party packages
— no files need to be written into the package's own share directory.

Usage:
    roscope resolve ... --plugin example/autoware/example_plugin.py

Expected directory structure
-----------------------------
example/autoware/interfaces/
└── <pkg_name>/
    ├── MyComponent.yaml     (composable node, matched by plugin class suffix)
    ├── my_node.yaml         (standalone node executable)
    └── other_node.yaml

When a node is available as both a standalone executable and a composable plugin
(both are launched depending on configuration), the composable YAML should be a
relative symlink pointing to the executable YAML so that only one file is maintained:

    ln -sr interfaces/<pkg>/my_node.yaml interfaces/<pkg>/MyComponent.yaml

Expected YAML format
---------------------
connections is a list of entries.  Each entry must have exactly one of
``name`` or ``name_expr``, plus a ``type`` key.  Additional optional keys:
``msg_type``, ``qos``, ``when``, ``loop``.  The name (however derived) identifies
the topic, service, or action depending on ``type``.

``name`` is a literal string:

connections:
  - name: ~/input/points
    type: subscription
    qos:
      reliability: best_effort
      durability: volatile
  - name: ~/output/objects
    type: publisher
  - name: /tf
    type: subscription
  - name: ~/set_parameters
    type: service_server

``name_expr`` is a Python expression evaluated against the node's resolved
parameters (same ``params`` dict as ``when``).  Use it when the connection
name contains a runtime-determined segment:

  - name_expr: "'/api/manual/' + params.get('mode', 'joy') + '/velocity'"
    type: subscription

On evaluation error, the entry is skipped (no conservative fallback — there
is no default name to substitute).  Having both ``name`` and ``name_expr`` on
the same entry is an error.

Loop expansion
--------------
``loop`` is a Python expression that evaluates to an iterable.  When present,
the entry is expanded once per item; the current item is bound to ``item`` and
is available in both ``name_expr`` and ``when``.  ``loop`` requires ``name_expr``
(``name`` is a literal and would produce duplicate-name errors).

  - loop: "range(1, 13)"
    name_expr: "'~/input/detection%02d/objects' % item"
    type: subscription
    msg_type: autoware_perception_msgs/msg/DetectedObjects
    when: "params.get('input/detection%02d/channel' % item, 'none') not in ('none', '')"

Available names in all expressions: ``params`` (always), ``item`` (inside ``loop``).
Available built-ins: format, len, str, int, float, bool, range, list, tuple,
                     enumerate, zip.

If ``loop`` evaluation fails the entry is skipped entirely.  If ``name_expr``
fails for a particular item that item is skipped.

Conditional connections
------------------------
An entry may carry an optional ``when`` key whose value is a Python expression.
The entry (or loop item) is included only when the expression is truthy.
If the expression raises any exception, the entry is included conservatively.

The same name may appear more than once with mutually-exclusive ``when``
conditions.  Two entries resolving to the *same* name after ``when`` filtering
is an error — the plugin raises ``ValueError``.

Recognized types: publisher, subscription, service_client, service_server,
                  action_client, action_server.
Any other type string is warned and ignored by the resolver.
If the interface file does not exist, the node is silently skipped.

Plugin function signature
--------------------------
get_connections(*, pkg_share_path, params, pkg_name, executable=None, plugin_name=None)

Exactly one of *executable* (standalone node) or *plugin_name* (composable node)
is provided.  *pkg_name* is always provided and is the ROS package name.
"""

from pathlib import Path

import yaml

_INTERFACES_DIR = Path(__file__).parent / "interfaces"

_SAFE_BUILTINS = {
    "format": format,
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "range": range,
    "list": list,
    "tuple": tuple,
    "enumerate": enumerate,
    "zip": zip,
}

_SKIP_KEYS = {"name", "name_expr", "loop", "when"}


def _eval(expr: str, local: dict):
    return eval(expr, {"__builtins__": _SAFE_BUILTINS}, local)


def _evaluate_when(expr: str, local: dict) -> bool:
    """Evaluate a ``when`` expression; returns True conservatively on any error."""
    try:
        return bool(_eval(expr, local))
    except Exception:
        return True


def get_connections(
    *,
    pkg_share_path: str,
    params: dict,
    pkg_name: str,
    executable: str | None = None,
    plugin_name: str | None = None,
) -> dict:
    # For composable nodes the plugin class name (e.g. "my_pkg::MyComponent") is
    # provided.  Use only the final part after "::" as the file name.
    node_name = executable or (plugin_name.split("::")[-1] if plugin_name else None)
    if not node_name:
        return {}
    interface_file = _INTERFACES_DIR / pkg_name / f"{node_name}.yaml"
    if not interface_file.exists():
        return {}
    data = yaml.safe_load(interface_file.read_text())
    if not isinstance(data, dict):
        return {}
    raw = data.get("connections") or []
    if not isinstance(raw, list):
        raise TypeError(
            f"{interface_file}: 'connections' must be a list of entries, got {type(raw).__name__}"
        )

    result: dict = {}
    base_local: dict = {"params": params}

    for entry in raw:
        if not isinstance(entry, dict):
            continue

        has_name = "name" in entry
        has_expr = "name_expr" in entry
        has_loop = "loop" in entry

        if has_name and has_expr:
            raise ValueError(f"{interface_file}: entry has both 'name' and 'name_expr'")
        if has_loop and has_name:
            raise ValueError(f"{interface_file}: entry has both 'loop' and 'name'")
        if has_loop and not has_expr:
            raise ValueError(f"{interface_file}: entry has 'loop' but no 'name_expr'")

        if has_loop:
            try:
                items = list(_eval(entry["loop"], base_local))
            except Exception:
                continue
        else:
            items = [None]

        attrs = {k: v for k, v in entry.items() if k not in _SKIP_KEYS}

        for item in items:
            local = {**base_local, "item": item} if has_loop else base_local

            if has_expr:
                try:
                    conn_name: str = str(_eval(entry["name_expr"], local))
                except Exception:
                    continue
            else:
                conn_name = entry.get("name") or ""

            if not conn_name:
                continue

            when = entry.get("when")
            if when is not None and not _evaluate_when(when, local):
                continue

            if conn_name in result:
                raise ValueError(
                    f"{interface_file}: duplicate resolved connection name {conn_name!r}"
                )
            result[conn_name] = attrs

    return result
