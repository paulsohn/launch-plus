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

Expected YAML format
---------------------
connections:
  ~/input/points:
    type: subscription
    qos:
      reliability: best_effort
      durability: volatile
  ~/output/objects:
    type: publisher
    qos:
      reliability: reliable
      durability: volatile
  /tf:
    type: subscription
  ~/set_parameters:
    type: service_server

Conditional connections
------------------------
A connection entry may carry an optional ``when`` key whose value is a Python
expression evaluated against the node's resolved parameters.  The expression
receives a single name ``params`` — the dict of parameter name → resolved value
passed by roscope.  The connection is included only when the expression
evaluates to a truthy value.

  ~/output/predicted_objects:
    type: publisher
    when: "params.get('use_object_filter', False)"

  ~/input/map_based_prediction:
    type: subscription
    when: "params.get('prediction_time_horizon_rate_for_validate_lane_changing_path', 0.0) > 0"

If the expression raises any exception (e.g. unexpected param type), the
connection is included conservatively.

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
    raw = data.get("connections") or {}

    result = {}
    for topic, meta in raw.items():
        if not isinstance(meta, dict):
            result[topic] = meta
            continue
        when = meta.get("when")
        if when is not None and not _evaluate_when(when, params):
            continue
        # Strip `when` from the metadata returned to roscope — it's plugin-internal.
        result[topic] = {k: v for k, v in meta.items() if k != "when"}
    return result
