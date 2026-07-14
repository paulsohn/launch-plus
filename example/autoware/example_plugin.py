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

Having both ``name`` and ``name_expr`` on the same entry is an error.

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

  - loop: "to_list(params.get('input_topics', []))"
    name_expr: "item"
    type: subscription
    msg_type: sensor_msgs/msg/PointCloud2

``to_list`` converts a ROS 2 string-serialised list (``"[a,b,c]"``) or an
actual Python list to ``list[str]``.  It is available in all expressions.

``args`` follows the ``argv`` convention: ``args[0]`` is the executable name,
``args[1]`` is the first user-supplied argument (same index as ``argv[1]``).
``args`` is always a list; it is empty for composable nodes (which have no
standalone process argv).

Available names in all expressions: ``params`` (always), ``args`` (always),
``item`` (inside ``loop``).
Available built-ins: format, len, str, int, float, bool, range, list, tuple,
                     enumerate, zip, to_list, to_bool, to_snake_case.
``to_bool`` converts a ROS 2 param value to bool robustly: Python booleans
pass through; strings are matched case-insensitively against ``"true"``
(necessary because YAML-loaded params may arrive as ``"false"`` rather than
``False``, and Python's built-in ``bool("false")`` incorrectly returns ``True``).
``to_snake_case`` converts a CamelCase string to snake_case.  Useful with
``str.split`` and ``str.removesuffix`` for deriving per-module topic names from
plugin class strings, e.g.:
``to_snake_case(cls.split("::")[-1].removesuffix("ModuleManager"))``.

Error policy
------------
Any evaluation failure (``loop``, ``name_expr``, ``when``) raises an exception
rather than silently skipping.  The caller is expected to surface these as
configuration errors.

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

import re
from pathlib import Path

import yaml

_INTERFACES_DIR = Path(__file__).parent / "interfaces"

_SKIP_KEYS = {"name", "name_expr", "loop", "when"}


def _to_list(v: object) -> list:
    """Convert a ROS 2 string-serialised list ``"[a,b,c]"`` or a Python list to list[str]."""
    if isinstance(v, list):
        return v
    s = str(v).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [item.strip() for item in s.split(",") if item.strip()]


def _to_bool(v: object) -> bool:
    """Convert a ROS 2 param value to bool.

    ROS 2 params loaded from YAML may arrive as strings (``"true"``/``"false"``)
    rather than Python booleans.  ``bool("false")`` is ``True`` in Python, so
    use this helper instead of the built-in ``bool`` when testing param flags.
    """
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == "true"


def _to_snake_case(s: str) -> str:
    """Convert a CamelCase string to snake_case."""
    return re.sub(r"(?<=[a-z\d])([A-Z])", r"_\1", s).lower()


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
    "to_list": _to_list,
    "to_bool": _to_bool,
    "to_snake_case": _to_snake_case,
}


def _eval(expr: str, local: dict):
    return eval(expr, {"__builtins__": _SAFE_BUILTINS}, local)


def get_connections(
    *,
    pkg_share_path: str,
    params: dict,
    pkg_name: str,
    executable: str | None = None,
    plugin_name: str | None = None,
    args: list[str] | None = None,
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
    base_local: dict = {"params": params, "args": args or []}

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

        items = list(_eval(entry["loop"], base_local)) if has_loop else [None]
        attrs = {k: v for k, v in entry.items() if k not in _SKIP_KEYS}

        for item in items:
            local = {**base_local, "item": item} if has_loop else base_local

            conn_name: str = (
                str(_eval(entry["name_expr"], local)) if has_expr else (entry.get("name") or "")
            )
            if not conn_name:
                continue

            when = entry.get("when")
            if when is not None and not bool(_eval(when, local)):
                continue

            if conn_name in result:
                raise ValueError(
                    f"{interface_file}: duplicate resolved connection name {conn_name!r}"
                )
            result[conn_name] = attrs

    return result
