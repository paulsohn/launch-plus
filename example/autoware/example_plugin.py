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
connections is a list of entries.  Each entry must have a ``topic`` key plus a
``type`` key.  Additional optional keys: ``msg_type``, ``qos``, ``when``.

connections:
  - topic: ~/input/points
    type: subscription
    qos:
      reliability: best_effort
      durability: volatile
  - topic: ~/output/objects
    type: publisher
    qos:
      reliability: reliable
      durability: volatile
  - topic: /tf
    type: subscription
  - topic: ~/set_parameters
    type: service_server

Conditional connections
------------------------
An entry may carry an optional ``when`` key whose value is a Python expression
evaluated against the node's resolved parameters.  The expression receives a
single name ``params`` — the dict of parameter name → resolved value passed by
roscope.  The entry is included only when the expression evaluates to a truthy
value.

  - topic: ~/output/predicted_objects
    type: publisher
    when: "params.get('use_object_filter', False)"

  - topic: ~/input/map_based_prediction
    type: subscription
    when: "params.get('prediction_time_horizon_rate_for_validate_lane_changing_path', 0.0) > 0"

If the expression raises any exception (e.g. unexpected param type), the entry
is included conservatively.

The same topic name may appear more than once with mutually-exclusive ``when``
conditions (e.g. a topic that is a subscription in replay mode and a publisher
in hardware mode).  Two entries resolving to the *same* topic name after
``when`` filtering is an error — the plugin raises ``ValueError``.

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
        topic = entry.get("topic")
        if not topic:
            continue
        when = entry.get("when")
        if when is not None and not _evaluate_when(when, params):
            continue
        if topic in result:
            raise ValueError(f"{interface_file}: duplicate resolved connection topic {topic!r}")
        result[topic] = {k: v for k, v in entry.items() if k not in ("topic", "when")}
    return result
