# Resolved Output Specification

## Overview

The launch resolver takes an **input launch description** (Python, XML, or YAML) containing control flow, substitutions, and nesting, and produces a **list of resolved action objects**. The renderer converts these actions into human-readable XML via `serialize_resolved()`.

There is no intermediate dict IR. Actions are the resolved representation.

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

Substitutions appear in attribute values and are resolved eagerly at `execute()` time.

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

**Exception:** `CommandSubstitution.perform()` returns `$(command resolved_args)` since commands can't be executed during static analysis.

### 1.4 Conditions

Any action may carry a condition that gates its execution.

```
Condition          ::= IfCondition                 (* if="expr" — execute when truthy *)
                      | UnlessCondition            (* unless="expr" — execute when falsy *)
```

---

## 2. Resolution Pipeline

### 2.1 Three-Phase Architecture

1. **Parse**: `@expose_action("tag")` class with `@classmethod parse(entity, parser)` → creates action with raw substitution tokens
2. **Execute**: `action.execute(context)` → resolves substitutions, performs side effects, returns new resolved action objects (or `[]` for side-effect-only actions)
3. **Serialize**: `action.serialize_resolved()` → returns `list[ET.Element]` for XML rendering

### 2.2 Action Class Structure

All action classes live in `launch_plus/entities/actions/`. Each has:

- `@classmethod parse(cls, entity, parser)` — parse XML/YAML Entity into action with unresolved tokens
- `__init__(**kwargs)` — stores raw substitution tokens (XML path) or resolved values (Python shim path)
- `execute(context) → list[Action]` — resolve substitutions, perform side effects, return resolved actions
- `serialize_resolved() → list[ET.Element]` — render to XML elements (only for actions that produce output)

### 2.3 Dispatch

`_resolve_element()` in resolver.py dispatches each parsed element through the action registry:

```python
def _resolve_element(elem, ctx, include_stack):
    tag = elem.type_name
    if tag in action_parse_methods:
        parser = _ActionParser(ctx, include_stack)
        action = action_parse_methods[tag](elem, parser)
        if action is not None:
            return action.execute(ctx)
    return []
```

### 2.4 Actions That Produce Output

These actions override `serialize_resolved()` and appear in the resolved tree:

| Action | XML Output |
|---|---|
| `Node` | `<node pkg="..." exec="..." ...>params, param_files</node>` |
| `ComposableNodeContainer` | `<node_container ...>composable_nodes</node_container>` |
| `LoadComposableNodes` | `<load_composable_node target="...">composable_nodes</load_composable_node>` |
| `ExecuteProcess` | `<executable cmd="..."/>` |
| `GroupAction` (with resolved_children) | `<group>children</group>` |
| `SourceMarker` | `<!-- source: pkg://path -->` |
| `ArgComment` | `<!-- arg name="..." value="..." -->` |
| Event handlers | `<on_process_exit>`, `<on_process_start>`, etc. |

### 2.5 Actions That Are Side-Effect Only

These actions return `[]` from `execute()` — their effects are consumed during resolution:

| Action | Side Effect |
|---|---|
| `DeclareLaunchArgument` | Sets default in `_launch_configurations` |
| `SetLaunchConfiguration` (let) | Sets variable |
| `PushRosNamespace` | Modifies `_launch_configurations['ros_namespace']` |
| `SetParameter` | Appends to `_launch_configurations['global_params']` |
| `SetRemap` | Appends to `_launch_configurations['ros_remaps']` |
| `SetEnvironmentVariable` / `UnsetEnvironmentVariable` | Modifies `context.environment` |

### 2.6 Actions That Delegate

| Action | Behavior |
|---|---|
| `GroupAction.execute()` | Push/pop scope, execute children, return results |
| `IncludeLaunchDescription.execute()` | Resolve file, execute children, wrap with markers in a GroupAction |
| `OpaqueFunction.execute()` | Call `fn(context)`, walk returned actions |

---

## 3. Rendering and Tracking

### 3.1 Renderer

The renderer (`renderer.py`) takes a list of resolved actions and produces XML:

1. Creates an `<launch>` root element
2. Appends each action's `serialize_resolved()` elements
3. Uses `ET.indent()` for formatting, `ET.tostring()` for output

### 3.2 Markers

`SourceMarker` and `ArgComment` are marker actions that appear in the resolved tree:

- `SourceMarker` is the first child inside a group created by `IncludeLaunchDescription`
- `ArgComment` follows `SourceMarker` when `--show-args` is enabled
- Both serialize to XML comments

### 3.3 ResolverState.tracked

`ResolverState.tracked` exists for **dependency tracking** (separate concern from rendering):

```python
tracked = {
    "packages": [str],              # referenced package names
    "includes": [str],              # included file paths
    "declared_args": [ArgEntry],
    "declared_args_by_file": {str: [ArgEntry]},
    "include_args": {file_path: {name: value}},
    "include_deps": [IncludeDepEntry],
    "param_file_deps": [ParamFileDepEntry],
    "param_files": [str],           # parameter file paths
    "global_params": [[name, value]],
    "set_launch_configurations": {name: value},
}
```

### 3.4 Handler Location

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
| `marker.py` | (not parsed from XML — created by `IncludeLaunchDescription`) |
