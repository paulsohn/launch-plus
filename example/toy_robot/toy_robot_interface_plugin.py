"""Connection metadata plugin for the toy_robot example.

Lookup order
------------
1. Primary: ``<pkg_share>/interface/<name>.yaml``
   For packages that install their own interface definitions (e.g. custom nodes).
2. Fallback: ``interfaces/<pkg_name>/<name>.yaml`` (relative to this file)
   For third-party packages whose share directory we cannot modify
   (e.g. ``robot_state_publisher`` from the ROS 2 distribution).

Expected YAML format
--------------------
connections:
  /joint_states:
    type: subscription
  ~/cmd_vel:
    type: subscription
  ~/odom:
    type: publisher

Recognized types: publisher, subscription, service_client, service_server,
                  action_client, action_server.

Plugin function signature
-------------------------
get_connections(*, pkg_share_path, params, pkg_name, executable=None, plugin_name=None)
"""

from pathlib import Path

import yaml

_LOCAL_INTERFACES_DIR = Path(__file__).parent / "interfaces"


def get_connections(
    *,
    pkg_share_path: str,
    params: dict,
    pkg_name: str,
    executable: str | None = None,
    plugin_name: str | None = None,
    args: list[str] | None = None,
) -> dict | None:
    name = executable or (plugin_name.split("::")[-1] if plugin_name else None)
    if not name:
        return None

    # Primary: interface definitions shipped inside the package
    primary = Path(pkg_share_path) / "interface" / f"{name}.yaml"
    if primary.exists():
        data = yaml.safe_load(primary.read_text())
        if isinstance(data, dict):
            return data.get("connections") or {}

    # Fallback: local interface registry for third-party packages
    fallback = _LOCAL_INTERFACES_DIR / pkg_name / f"{name}.yaml"
    if fallback.exists():
        data = yaml.safe_load(fallback.read_text())
        if isinstance(data, dict):
            return data.get("connections") or {}

    return None
