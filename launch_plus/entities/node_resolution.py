"""Node detail resolution helpers.

Resolves deferred node details (package, executable, name, namespace,
parameters, remappings, environment) and composable node plugins from
raw substitution objects to concrete strings.
"""

from __future__ import annotations

import launch_plus.resolver as _R
from launch_plus.entities.actions.node import _TrackedComposableNode

# ─── Node detail resolution helpers ──────────────────────────────────────────


def _env_overrides(state):
    """Return a copy of the current env overrides for per-node output."""
    return dict(state.env)


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
        if _R._is_substitution(raw):
            resolved, is_fallback = _R._resolve_substitution_ex(raw, context)
            if resolved is not None:
                entry[field_name] = resolved
                if field_name == "package" and not is_fallback:
                    _R._track_package(state, resolved)

    # Namespace: emit raw inputs — effective_namespace is computed downstream
    ns = (
        _R._resolve_substitution(node._raw_namespace, context)
        if node._raw_namespace is not None
        else None
    )
    entry["namespace_stack"] = list(state.namespace_stack)
    entry["explicit_namespace"] = ns

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
                        expanded = _R._read_and_expand_param_file(path)
                        if expanded is not None:
                            pf_entry["params"] = expanded
                    pf_list.append(pf_entry)
        elif isinstance(p, dict):
            for k, v in p.items():
                resolved_v = _R._resolve_substitution(v, context)
                params[str(k)] = resolved_v if resolved_v is not None else ""
    # Merge global params from the launch context (global first, node-local overrides).
    # In ROS 2, Node.execute() reads global_params from context._launch_configurations.
    # ComposableNodeContainer inherits from Node, so it also gets global params.
    ctx_global_params = context._launch_configurations.get("global_params", [])
    merged_params = {k: str(v) for k, v in ctx_global_params}
    merged_params.update(params)
    entry["parameters"] = merged_params
    entry["param_files"] = list(state.global_param_files) + pf_list

    # Remappings: list of [src, dst] pairs — merge global remaps first
    remaps = list(state.global_remaps)
    for r in node._raw_remappings:
        if isinstance(r, (tuple, list)) and len(r) == 2:
            src = _R._resolve_substitution(r[0], context)
            dst = _R._resolve_substitution(r[1], context)
            remaps.append([src or str(r[0]), dst or str(r[1])])
    entry["remappings"] = remaps

    # Env vars: start with inherited env diff, then node-local overrides
    env = _env_overrides(state)
    raw_env = node._raw_env
    if isinstance(raw_env, dict):
        for k, v in raw_env.items():
            env[_R._resolve_substitution(k, context) or str(k)] = (
                _R._resolve_substitution(v, context) or ""
            )
    elif isinstance(raw_env, (list, tuple)):
        for item in raw_env:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                k_str = _R._resolve_substitution(item[0], context) or str(item[0])
                v_str = _R._resolve_substitution(item[1], context) or ""
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
            resolved = _R._resolve_substitution(raw, context)
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
                            expanded = _R._read_and_expand_param_file(path)
                            if expanded is not None:
                                pf_entry["params"] = expanded
                    pf_list.append(pf_entry)
            elif isinstance(p, dict):
                for k, v in p.items():
                    resolved_v = _R._resolve_substitution(v, context)
                    params[str(k)] = resolved_v if resolved_v is not None else ""
        remaps = []
        for r in desc._raw_remappings:
            if isinstance(r, (tuple, list)) and len(r) == 2:
                src = _R._resolve_substitution(r[0], context)
                dst = _R._resolve_substitution(r[1], context)
                remaps.append(
                    [
                        src if src is not None else str(r[0]),
                        dst if dst is not None else str(r[1]),
                    ]
                )
        # Resolve package/plugin/name substitutions with the live context
        pkg = desc._package
        if _R._is_substitution(desc._raw_package):
            resolved_pkg, is_fallback = _R._resolve_substitution_ex(desc._raw_package, context)
            if resolved_pkg is not None:
                pkg = resolved_pkg
                if not is_fallback:
                    _R._track_package(state, resolved_pkg)
        plg = desc._plugin
        if _R._is_substitution(desc._raw_plugin):
            resolved_plg = _R._resolve_substitution(desc._raw_plugin, context)
            if resolved_plg is not None:
                plg = resolved_plg
        nm = desc._name
        if _R._is_substitution(desc._raw_name):
            resolved_nm = _R._resolve_substitution(desc._raw_name, context)
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
