# Resolved Output Specification

## Overview

The launch resolver takes an **input launch description** (Python, XML, or YAML) containing control flow, substitutions, and nesting, and produces a **resolved output** — a flat collection of concrete node descriptions, event handlers, and metadata with no remaining control flow or unresolved substitutions.

The output is stored in the `_state.tracked` dict during resolution. The renderer converts this into human-readable XML.

---

## 1. Input Grammar: Launch Actions

A launch description is a sequence of **actions**. Actions come from two packages: `launch` (core) and `launch_ros` (ROS-specific).

> **Note:** This section is not exhaustive. The full set of action types, attributes, and their possible value types is large and varies across ROS 2 distributions. This grammar captures the most common actions for orientation; the authoritative sources are [ros2/launch](https://github.com/ros2/launch) and [ros2/launch_ros](https://github.com/ros2/launch_ros).

### 1.1 Core Grammar

```
LaunchDescription  ::= Action*

Action             ::= ControlFlow | Process | Env | Config | Event | Meta

 ControlFlow        ::= DeclareLaunchArgument      (* arg name, default?, description?, choices? *)
                      | SetLaunchConfiguration     (* let: name, value *)
                      | UnsetLaunchConfiguration   (* name *)
                      | GroupAction                (* scoped?, forwarding?, children: Action* *)
                      | IncludeLaunchDescription   (* file, launch_arguments *)
                      | OpaqueFunction             (* python callable → Action* *)

Process            ::= ExecuteProcess             (* cmd, name?, cwd?, output?, ... *)

Env                ::= SetEnvironmentVariable      (* name, value *)
                      | UnsetEnvironmentVariable   (* name *)

Config             ::= Log                        (* message, level *)

Event              ::= EmitEvent                   (* event object *)
                      | RegisterEventHandler       (* event_handler *)
```

### 1.2 ROS-Specific Grammar

```
ROSAction          ::= ROSProcess | ROSConfig | ROSNamespace

ROSProcess         ::= Node                        (* pkg, exec, name?, namespace?, params, remaps, envs *)
                      | LifecycleNode              (* + autostart? *)
                      | ComposableNodeContainer    (* + composable_nodes: ComposableNode* *)
                      | LoadComposableNodes        (* target, composable_nodes: ComposableNode* *)

ComposableNode     ::= ComposableNode              (* pkg, plugin, name?, namespace?, params, remaps *)

ROSConfig          ::= SetParameter               (* name, value *)
                      | SetParametersFromFile      (* filename *)
                      | SetRemap                   (* from, to *)

ROSNamespace       ::= PushROSNamespace            (* namespace *)
```

### 1.3 Substitutions

Substitutions appear in attribute values and are resolved during the walk.

```
Substitution       ::= TextSubstitution            (* literal string *)
                      | LaunchConfiguration        (* $(arg name) — access launch config *)
                      | EnvironmentVariable        (* $(env NAME default?) *)
                      | FindPackageShare           (* $(find-pkg-share pkg) *)
                      | FindPackagePrefix          (* $(find-pkg-prefix pkg) *)
                      | PythonExpression           (* $(eval expr) *)
                      | Command                    (* $(command cmd) — shell command output *)
                      | ThisLaunchFileDir          (* $(dirname) — directory of current file *)
                      | PathJoinSubstitution       (* join path components *)
                      | IfElseSubstitution         (* condition ? if_value : else_value *)
                      | EqualsSubstitution         (* left == right → "true"/"false" *)
                      | NotEqualsSubstitution      (* left != right → "true"/"false" *)
                      | NotSubstitution            (* !value *)
                      | AndSubstitution            (* left && right *)
                      | OrSubstitution             (* left || right *)
```

### 1.4 Conditions

Any action may carry a condition that gates its execution.

```
Condition          ::= IfCondition                 (* if="expr" — execute when truthy *)
                      | UnlessCondition            (* unless="expr" — execute when falsy *)
```

---

## 2. Resolution Output: `_state.tracked`

The resolver populates `_state.tracked`, a dict with the following structure:

```python
tracked = {
    "packages": [str],              # all referenced package names
    "includes": [str],              # all included file paths (portable)
    "nodes": [NodeDict],            # resolved node descriptions
    "warnings": [str],
    "errors": [str],
    "declared_args": [ArgEntry],    # from <arg> declarations
    "declared_args_by_file": {str: [ArgEntry]},
    "global_params": [[name, value]],
    "include_args": {file_path: {name: value}},
    "param_files": [str],           # parameter file paths
    "set_launch_configurations": {name: value},
    "include_deps": [IncludeDepEntry],
    "param_file_deps": [ParamFileDepEntry],
    "event_handlers": [EventHandlerDict],
}
```

### 2.1 Node Dict Format

Each entry in `tracked["nodes"]` is a flat dict:

```python
{
    "package": str,                # e.g. "my_pkg"
    "executable": str,             # e.g. "my_node"
    "name": str,                   # node name (may be "")
    "namespace_stack": [str],      # accumulated push-ros-namespace entries
    "explicit_namespace": str | None,  # namespace= attribute on the element
    "parameters": {str: str},      # merged: global params + node params
    "param_files": [ParamFileEntry],
    "remappings": [[str, str]],    # merged: global remaps + node remaps
    "env": {str: str},             # accumulated set_env overrides
    "kind": str,                   # "node" | "lifecycle_node" | "container" |
                                   # "load_composable" | "executable" | "log" |
                                   # "event_handler"
    "plugins": [PluginDict],       # composable node plugins (containers only)
    "target": str | None,          # load_composable_node target

    # Kind-specific fields:
    "output": str | None,          # node: output attribute
    "args": str | None,            # node: args attribute
    "respawn": str | None,         # node: respawn attribute
    "respawn_delay": str | None,   # node: respawn_delay attribute
    "cmd": str,                    # executable: command string
    "shell": bool,                 # executable: shell execution flag
    "message": str,                # log: message text

    # Metadata (added by _track_node):
    "source": str,                 # source key (e.g. "pkg://launch/file.xml")
    "include_chain": [[str, str]], # [[pkg, path], ...] of include ancestry
}
```

### 2.2 Plugin Dict Format

Each composable node plugin:

```python
{
    "package": str,
    "plugin": str,                 # plugin class name
    "name": str | None,
    "parameters": {str: str},
    "remappings": [[str, str]],
    "param_files": [ParamFileEntry],
}
```

### 2.3 Event Handler Dict Format

Each entry in `tracked["event_handlers"]`:

```python
{
    "handler_kind": str,           # "on_process_start" | "on_process_exit" |
                                   # "on_state_transition" | "on_shutdown" |
                                   # "emit_event"
    "target": str | None,
    "target_node": str | None,
    "start_state": str | None,     # on_state_transition only
    "goal_state": str | None,      # on_state_transition only
    "namespace_stack": [str],
    "explicit_namespace": str | None,
    "actions": [EventActionDict],
}
```

### 2.4 What is Consumed During Resolution

These input actions are **consumed during resolution** and do not appear in the output:

| Input Action | Resolution |
|---|---|
| `DeclareLaunchArgument` | Default inserted into substitution context; recorded in `declared_args` |
| `SetLaunchConfiguration` / `let` | Variable binding consumed during walk |
| `IncludeLaunchDescription` / `include` | Child file parsed and inlined; recorded in `includes` |
| `OpaqueFunction` | Called; returned actions walked and inlined |
| `GroupAction` / `group` | Children resolved; scope effects (env, namespace, params, remaps) applied to child nodes |
| `SetEnvironmentVariable` / `set_env` | Applied to env context; reflected in child nodes' `env` |
| `UnsetEnvironmentVariable` / `unset_env` | Applied to env context |
| `PushROSNamespace` | Applied to namespace stack; reflected in child nodes' `namespace_stack` |
| `SetParameter` / `set_parameter` | Merged into child nodes' `parameters` |
| `SetParametersFromFile` | Merged into child nodes' `param_files` |
| `SetRemap` / `set_remap` | Merged into child nodes' `remappings` |
| Conditions (`if=`/`unless=`) | Evaluated; element present iff condition was true |
| All substitutions (`$(...)`) | Resolved to concrete `str`. **Exception:** `$(find-pkg-share pkg)` is preserved as-is in preview mode to keep paths portable. |

### 2.5 Invariants

1. **Flat** — All nodes are collected into a flat list. Group structure is not preserved in the output; scope effects are baked into child nodes.
2. **Concrete** — Every `str` field is a resolved value; no `$(...)` substitutions remain (except `$(find-pkg-share ...)` in preview mode).
3. **No conditions** — Every node in the output is unconditional. Conditions were evaluated during resolution.
4. **Deterministic** — Same inputs → same output.
5. **Namespace and env are effective** — Each node carries its computed namespace stack and environment overrides. Procedural scope actions are consumed; only their effects survive.

---

## 3. Action Handler Architecture

### Registration

Action handlers are registered via `@expose_action("tag_name")` decorators from `entities/expose.py`. Each handler receives an `Entity` (parsed XML/YAML element) and an `_ActionParser` (access to resolver state).

```python
@expose_action("node")
@expose_action("lifecycle_node")
def _action_node(entity: Entity, parser: _ActionParser) -> None:
    ...
    parser.track_node({...})
```

### Dispatch

`_resolve_element()` in resolver.py dispatches each parsed element through the action registry:

```python
def _resolve_element(elem, ctx, include_stack):
    tag = elem.type_name
    if tag in action_parse_methods:
        parser = _ActionParser(ctx, include_stack)
        action_parse_methods[tag](elem, parser)
```

### Handler Location

All `@expose_action` handlers live in `launch_plus/entities/actions/`:

| Module | Tags |
|---|---|
| `arg.py` | `arg`, `let` |
| `group.py` | `group` |
| `include.py` | `include` |
| `node.py` | `node`, `lifecycle_node`, `node_container`, `composable_node_container`, `load_composable_node` |
| `env.py` | `set_env`, `unset_env`, `push-ros-namespace`, `set_parameter`, `set_remap` |
| `log.py` | `log` |
| `executable.py` | `executable` |
| `event_handler.py` | `on_process_start`, `on_process_exit`, `on_state_transition`, `on_shutdown`, `emit_event` |
