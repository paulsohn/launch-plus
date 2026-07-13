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
``msg_type``, ``qos``, ``when``.  The name (however derived) identifies the
topic, service, or action depending on ``type``.

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

Conditional connections
------------------------
An entry may carry an optional ``when`` key whose value is a Python expression
evaluated against the node's resolved parameters.  The expression receives a
single name ``params`` — the dict of parameter name → resolved value passed by
roscope.  The entry is included only when the expression evaluates to a truthy
value.

  - name: ~/output/predicted_objects
    type: publisher
    when: "params.get('use_object_filter', False)"

  - name_expr: "params.get('output_topic', '~/output/trajectory')"
    type: publisher
    when: "params.get('publish_output', True)"

If the ``when`` expression raises any exception, the entry is included
conservatively.

The same name may appear more than once with mutually-exclusive ``when``
conditions (e.g. a topic that is a subscription in replay mode and a publisher
in hardware mode).  Two entries resolving to the *same* name after ``when``
filtering is an error — the plugin raises ``ValueError``.

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


def _evaluate_when(expr: str, params: dict) -> bool:
    """Evaluate a ``when`` expression against resolved node parameters.

    Returns True (include the connection) on any evaluation error so that
    missing or unexpected parameter values never silently drop connections.
    """
    try:
        return bool(eval(expr, {"__builtins__": {}}, {"params": params}))
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
    name = executable or (plugin_name.split("::")[-1] if plugin_name else None)
    if not name:
        return {}
    interface_file = _INTERFACES_DIR / pkg_name / f"{name}.yaml"
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

    result = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        has_name = "name" in entry
        has_expr = "name_expr" in entry
        if has_name and has_expr:
            raise ValueError(f"{interface_file}: entry has both 'name' and 'name_expr'")
        if has_expr:
            try:
                conn_name: str = str(
                    eval(entry["name_expr"], {"__builtins__": {}}, {"params": params})
                )
            except Exception:
                continue
        else:
            conn_name = entry.get("name") or ""
        if not conn_name:
            continue
        when = entry.get("when")
        if when is not None and not _evaluate_when(when, params):
            continue
        if conn_name in result:
            raise ValueError(f"{interface_file}: duplicate resolved connection name {conn_name!r}")
        skip = {"name", "name_expr", "when"}
        result[conn_name] = {k: v for k, v in entry.items() if k not in skip}
    return result
