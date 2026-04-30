"""Example connection metadata plugin that reads per-executable interface definitions.

This example shows one possible convention (a per-executable YAML file under
``interface/``). The plugin interface is intentionally open-ended — projects can
use any lookup strategy: a single registry file, a database, generated stubs, or
anything else, as long as ``get_connections`` returns the expected dict.

Usage:
    roscope resolve ... --plugin example/autoware/example_plugin.py

Expected package directory structure
-------------------------------------
<pkg_share_path>/
└── interface/
    ├── my_node.yaml
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

Recognized types: publisher, subscription, service_client, service_server,
                  action_client, action_server.
Any other type string is warned and ignored by the resolver.
If the interface file does not exist, the node is silently skipped.
"""

from pathlib import Path

import yaml


def get_connections(pkg_share_path: str, executable: str, params: dict) -> dict:
    interface_file = Path(pkg_share_path) / "interface" / f"{executable}.yaml"
    if not interface_file.exists():
        return {}
    data = yaml.safe_load(interface_file.read_text())
    if not isinstance(data, dict):
        return {}
    return data.get("connections") or {}
