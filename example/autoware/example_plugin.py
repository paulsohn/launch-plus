"""Example connection metadata plugin that reads per-executable interface definitions.

This example shows one possible convention (a per-executable YAML file under
``interface/``). The plugin interface is intentionally open-ended — projects can
use any lookup strategy: a single registry file, a database, generated stubs, or
anything else, as long as ``get_connections`` returns the expected dict.

Usage:
    roscope resolve ... --plugin example/autoware/example_plugin.py

Expected package directory structure for this example
-------------------------------------
<pkg_share_path>/
└── interface/
    ├── MyComponent.yaml
    ├── my_node.yaml
    └── other_node.yaml

Expected YAML format for this example
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

Recognized types: publisher, subscription, service_client, service_server,
                  action_client, action_server.
Any other type string is warned and ignored by the resolver.
If the interface file does not exist, the node is silently skipped.

Plugin function signature
--------------------------
get_connections(pkg_share_path, params, *, executable=None, plugin_name=None)

Exactly one of *executable* (standalone node) or *plugin_name* (composable node)
is provided, so the plugin can adapt its lookup strategy accordingly.
"""

from pathlib import Path

import yaml


def get_connections(
    pkg_share_path: str,
    params: dict,
    *,
    executable: str | None = None,
    plugin_name: str | None = None,
) -> dict:
    # For composable nodes the plugin class name (e.g. "my_pkg::MyComponent") is
    # provided. Use only the final component after "::" as the file name.
    name = executable or (plugin_name.split("::")[-1] if plugin_name else None)
    if not name:
        return {}
    interface_file = Path(pkg_share_path) / "interface" / f"{name}.yaml"
    if not interface_file.exists():
        return {}
    data = yaml.safe_load(interface_file.read_text())
    if not isinstance(data, dict):
        return {}
    return data.get("connections") or {}
