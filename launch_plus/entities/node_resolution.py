"""Node detail resolution helpers.

Resolves deferred node details (package, executable, name, namespace,
parameters, remappings, environment) and composable node plugins from
raw substitution objects to concrete strings.
"""

from __future__ import annotations

from launch_plus.entities.actions.node import _TrackedComposableNode
from launch_plus.entities.helpers import (
    _is_substitution,
    _read_and_expand_param_file,
    _track_package,
)

# ─── Node detail resolution helpers ──────────────────────────────────────────


def _env_overrides(context):
    """Return env vars explicitly set via SetEnvironmentVariable (overrides only).

    Compares context.environment against os.environ to extract only the diff.
    """
    import os

    overrides = {}
    for k, v in context.environment.items():
        if k not in os.environ or os.environ[k] != v:
            overrides[k] = v
    return overrides


def _resolve_node_details(state, node, context):
    """Fill in deferred details (package, executable, name, namespace, params, remaps, env).

    Works for both ``_TrackedNode`` / ``_TrackedLifecycleNode`` and
    ``_TrackedComposableNodeContainer`` — both expose the same raw fields.
    """
    entry = state.tracked["nodes"][node._idx]

    # Package / executable / name: resolve substitutions (e.g. LaunchConfiguration)
    # that could not be resolved at construction time.
    for field_name in ("package", "executable", "name"):
        raw = getattr(node, f"_raw_{field_name}", None)
        if _is_substitution(raw):
            resolved, is_fallback = context.perform_substitution_ex(raw)
            if resolved is not None:
                entry[field_name] = resolved
                if field_name == "package" and not is_fallback:
                    _track_package(state, resolved)

    # Namespace: emit raw inputs — effective_namespace is computed downstream
    ns = (
        context.perform_substitution(node._raw_namespace)
        if node._raw_namespace is not None
        else None
    )
    entry["explicit_namespace"] = ns
    ros_ns = context._launch_configurations.get("ros_namespace")
    if ros_ns:
        entry["ros_namespace"] = ros_ns

    # Parameters and param files
    params = {}
    pf_list: list[dict] = []
    seen_pf: set[str] = set()
    for p in node._raw_parameters:
        if hasattr(p, "_param_file"):
            path = p._param_file
            # Deferred resolution: try again with live context if not resolved eagerly
            if path is None and hasattr(p, "_raw_param_file") and p._raw_param_file is not None:
                raw = p._raw_param_file
                if hasattr(raw, "perform"):
                    try:
                        result = raw.perform(context)
                        if result is not None:
                            path = str(result)
                    except Exception:
                        pass
                elif not isinstance(raw, str):
                    path = str(raw)
            if path:
                path = str(path)
                if path not in seen_pf:
                    seen_pf.add(path)
                    pf_entry: dict = {"path": path}
                    if state.inline_params:
                        expanded = _read_and_expand_param_file(path, state=state)
                        if expanded is not None:
                            pf_entry["params"] = expanded
                    pf_list.append(pf_entry)
        elif isinstance(p, dict):
            for k, v in p.items():
                resolved_v = context.perform_substitution(v)
                params[str(k)] = resolved_v if resolved_v is not None else ""
    # Global params from launch_configurations (matching official Node)
    ctx_global_params = context._launch_configurations.get("global_params", [])
    merged_params = {k: str(v) for k, v in ctx_global_params}
    merged_params.update(params)
    entry["parameters"] = merged_params
    global_pf = context._launch_configurations.get("global_param_files", [])
    entry["param_files"] = list(global_pf) + pf_list

    # Remappings from launch_configurations (matching official Node)
    remaps = list(context._launch_configurations.get("ros_remaps", []))
    for r in node._raw_remappings:
        if isinstance(r, (tuple, list)) and len(r) == 2:
            src = context.perform_substitution(r[0])
            dst = context.perform_substitution(r[1])
            remaps.append([src or str(r[0]), dst or str(r[1])])
    entry["remappings"] = remaps

    # Env vars: start with inherited env diff, then node-local overrides
    env = _env_overrides(context)
    raw_env = node._raw_env
    if isinstance(raw_env, dict):
        for k, v in raw_env.items():
            env[context.perform_substitution(k) or str(k)] = context.perform_substitution(v) or ""
    elif isinstance(raw_env, (list, tuple)):
        for item in raw_env:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                k_str = context.perform_substitution(item[0]) or str(item[0])
                v_str = context.perform_substitution(item[1]) or ""
                env[k_str] = v_str
    entry["env"] = env

    # output / arguments / respawn / respawn_delay
    for attr, key in (
        ("_raw_output", "output"),
        ("_raw_arguments", "args"),
        ("_raw_respawn", "respawn"),
        ("_raw_respawn_delay", "respawn_delay"),
    ):
        raw = getattr(node, attr, None)
        if raw is not None:
            resolved = context.perform_substitution(raw)
            if resolved is not None:
                entry[key] = resolved
            elif isinstance(raw, str):
                entry[key] = raw
            else:
                entry[key] = str(raw)


def _resolve_composable_plugins(state, descs, context):
    """Convert ``_TrackedComposableNode`` descriptions to serialisable plugin dicts."""
    plugins = []
    for desc in descs:
        if not isinstance(desc, _TrackedComposableNode):
            # Fallback for real/unknown description objects
            pkg = getattr(desc, "package", None) or getattr(desc, "_package", None)
            plugin = getattr(desc, "plugin", None) or getattr(desc, "_plugin", None)
            if pkg or plugin:
                plugins.append(
                    {
                        "package": str(pkg) if pkg else "",
                        "plugin": str(plugin) if plugin else "",
                        "name": None,
                        "parameters": {},
                        "remappings": [],
                    }
                )
            continue
        params = {}
        pf_list: list[dict] = []
        seen_pf: set[str] = set()
        for p in desc._raw_parameters:
            if hasattr(p, "_param_file"):
                path = p._param_file
                # Deferred resolution: try again with live context if not resolved eagerly
                if path is None and hasattr(p, "_raw_param_file") and p._raw_param_file is not None:
                    raw = p._raw_param_file
                    if hasattr(raw, "perform"):
                        try:
                            result = raw.perform(context)
                            if result is not None:
                                path = str(result)
                        except Exception:
                            pass
                    elif not isinstance(raw, str):
                        path = str(raw)
                if path:
                    path = str(path)
                    if path not in seen_pf:
                        seen_pf.add(path)
                        pf_entry: dict = {"path": path}
                        if state.inline_params:
                            expanded = _read_and_expand_param_file(path, state=state)
                            if expanded is not None:
                                pf_entry["params"] = expanded
                    pf_list.append(pf_entry)
            elif isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = context.perform_substitution(v)
                    params[str(k)] = resolved_v if resolved_v is not None else ""
        remaps = []
        for r in desc._raw_remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = context.perform_substitution(r[0])
                dst = context.perform_substitution(r[1])
                remaps.append(
                    [
                        src if src is not None else str(r[0]),
                        dst if dst is not None else str(r[1]),
                    ]
                )
        # Resolve package/plugin/name substitutions with the live context
        pkg = desc._package
        if _is_substitution(desc._raw_package):
            resolved_pkg, is_fallback = context.perform_substitution_ex(desc._raw_package)
            if resolved_pkg is not None:
                pkg = resolved_pkg
                if not is_fallback:
                    _track_package(state, resolved_pkg)
        plg = desc._plugin
        if _is_substitution(desc._raw_plugin):
            resolved_plg = context.perform_substitution(desc._raw_plugin)
            if resolved_plg is not None:
                plg = resolved_plg
        nm = desc._name
        if _is_substitution(desc._raw_name):
            resolved_nm = context.perform_substitution(desc._raw_name)
            if resolved_nm is not None:
                nm = resolved_nm
        plugins.append(
            {
                "package": pkg,
                "plugin": plg,
                "name": nm or None,
                "parameters": params,
                "remappings": remaps,
                "param_files": pf_list,
            }
        )
    return plugins
