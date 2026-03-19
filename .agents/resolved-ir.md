# Resolved IR Specification

## Overview

The launch resolver takes an **input launch description** (Python, XML, or YAML) containing control flow, substitutions, and nesting, and produces a **Resolved IR** — a flat list of concrete, declarative actions with no remaining control flow or unresolved substitutions.

This document defines both the input grammar (what actions exist) and the output IR (what the resolver produces) using an inductive BNF-style definition.

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
                      | OpaqueCoroutine            (* async callable → Future *)
                      | ForEach                    (* items, callback → Action* per iteration *)
                      | TimerAction                (* period, children: Action* *)
                      | Shutdown                   (* reason? *)

Process            ::= ExecuteLocal                (* process_description, shell?, on_exit?, respawn?, ... *)
                      | ExecuteProcess             (* cmd, name?, cwd?, output?, ... *)

Env                ::= SetEnvironmentVariable      (* name, value *)
                      | UnsetEnvironmentVariable   (* name *)
                      | AppendEnvironmentVariable  (* name, value, prepend?, separator? *)
                      | PushEnvironment            (* snapshot current env *)
                      | PopEnvironment             (* restore env from stack *)
                      | ResetEnvironment           (* reset to initial env *)
                      | ReplaceEnvironmentVariables (* replace all env with new dict *)

Config             ::= PushLaunchConfigurations    (* snapshot current configs *)
                      | PopLaunchConfigurations    (* restore configs from stack *)
                      | ResetLaunchConfigurations  (* clear or selectively reset configs *)
                      | Log                        (* message, level *)
                      | LogInfo | LogWarning | LogDebug | LogError  (* message *)

Event              ::= EmitEvent                   (* event object *)
                      | RegisterEventHandler       (* event_handler *)
                      | UnregisterEventHandler     (* event_handler *)
```

### 1.2 ROS-Specific Grammar

```
ROSAction          ::= ROSProcess | ROSConfig | ROSNamespace | ROSTimer

ROSProcess         ::= Node                        (* pkg, exec, name?, namespace?, params, remaps, envs *)
                      | LifecycleNode              (* + autostart? *)
                      | ComposableNodeContainer    (* + composable_nodes: ComposableNode* *)
                      | LoadComposableNodes        (* target, composable_nodes: ComposableNode* *)
                      | LifecycleTransition        (* lifecycle_node_names, transition_ids *)

ComposableNode     ::= ComposableNode              (* pkg, plugin, name?, namespace?, params, remaps *)
                      | ComposableLifecycleNode    (* + autostart? *)

ROSConfig          ::= SetParameter               (* name, value *)
                      | SetParametersFromFile      (* filename *)
                      | SetRemap                   (* from, to *)
                      | SetUseSimTime              (* value *)
                      | SetROSLogDir               (* new_log_dir *)

ROSNamespace       ::= PushROSNamespace            (* namespace *)

ROSTimer           ::= ROSTimer                    (* period, children: Action* *)
```

### 1.3 Substitutions

Substitutions appear in attribute values and are resolved during the walk.

```
Substitution       ::= TextSubstitution            (* literal string *)
                      | LaunchConfiguration        (* $(arg name) — access launch config *)
                      | EnvironmentVariable        (* $(env NAME default?) *)
                      | FindExecutable             (* locate on PATH *)
                      | FindPackageShare           (* $(find-pkg-share pkg) *)
                      | FindPackagePrefix          (* $(find-pkg-prefix pkg) *)
                      | PythonExpression           (* $(eval expr) *)
                      | Command                    (* $(command cmd) — shell command output *)
                      | ThisLaunchFile             (* absolute path to current file *)
                      | ThisLaunchFileDir          (* $(dirname) — directory of current file *)
                      | PathJoinSubstitution       (* join path components *)
                      | AnonName                   (* generate anonymous name *)
                      | FileContent                (* read file contents *)
                      | IfElseSubstitution         (* condition ? if_value : else_value *)
                      | EqualsSubstitution         (* left == right → "true"/"false" *)
                      | NotEqualsSubstitution      (* left != right → "true"/"false" *)
                      | NotSubstitution            (* !value *)
                      | AndSubstitution            (* left && right *)
                      | OrSubstitution             (* left || right *)
                      | AnySubstitution            (* any(values...) *)
                      | AllSubstitution            (* all(values...) *)
                      | LaunchLogDir               (* log directory path *)
                      | LocalSubstitution          (* access local context vars *)
                      | ForEachVar                 (* loop iteration variable *)
                      | ForLoopIndex               (* loop iteration index *)
```

### 1.4 Conditions

Any action may carry a condition that gates its execution.

```
Condition          ::= IfCondition                 (* if="expr" — execute when truthy *)
                      | UnlessCondition            (* unless="expr" — execute when falsy *)
```

### 1.5 Events

Events are emitted at runtime and trigger registered handlers.

```
Event              ::= Shutdown                    (* reason?, due_to_sigint? *)
                      | ExecutionComplete          (* action *)
                      | TimerEvent                 (* timer_action *)
                      | ProcessStarted             (* action, name, cmd, cwd, env, pid *)
                      | ProcessExited              (* + returncode *)
                      | ProcessStdout | ProcessStderr | ProcessStdin  (* + text, fd *)
                      | ShutdownProcess            (* process_matcher *)
                      | SignalProcess              (* signal, process_matcher *)
                      | IncludeLaunchDescriptionEvent  (* launch_description *)
```

---

## 2. Output Grammar: Resolved IR

The resolver consumes the full input grammar and produces a resolved subset. All substitutions are resolved to concrete strings, all conditions are evaluated, but **group structure is preserved** so that scope boundaries (env, namespace, config) remain explicit.

The IR is defined inductively — `IRGroupAction` contains `[IRAction]`, making the IR a tree.

```
IRAction           ::= IRGroupAction
                      | IRNode
                      | IRLifecycleNode
                      | IRComposableNodeContainer
                      | IRLoadComposableNode
                      | IRExecutable
                      | IRLog
                      | IROpaque

IRGroupAction      ::= GroupAction(
                          children : [IRAction],    (* recursive *)
                          source   : str?,          (* include origin, e.g. "pkg://launch/file.launch.xml" *)
                        )
                        (* Structural grouping for annotation.  Scope effects (env,
                           namespace, params, remaps) are fully consumed during resolution
                           and baked into child nodes.  scoped/forwarding/launch_configurations
                           are resolution-time concepts that do not appear in the output.
                           source records include provenance for rendering comment markers. *)

IRNode             ::= Node(
                          package       : str,
                          executable    : str,
                          name          : str?,
                          namespace     : str?,
                          parameters    : {str: str},
                          param_files   : [str],
                          remappings    : [(str, str)],
                          env           : {str: str},
                          output        : str?,
                          args          : str?,
                          respawn       : str?,
                          respawn_delay : str?,
                        )

IRLifecycleNode    ::= LifecycleNode(
                          IRNode fields,
                          autostart : bool?,
                        )

IRComposableNodeContainer
                   ::= ComposableNodeContainer(
                          IRNode fields,
                          plugins : [IRComposablePlugin],
                        )

IRComposablePlugin ::= ComposablePlugin(
                          package    : str,
                          plugin     : str,
                          name       : str?,
                          namespace  : str?,
                          parameters : {str: str},
                          param_files: [str],
                          remappings : [(str, str)],
                        )

IRLoadComposableNode
                   ::= LoadComposableNode(
                          target    : str,
                          namespace : str?,
                          plugins   : [IRComposablePlugin],
                        )

IRExecutable       ::= Executable(
                          cmd       : str,
                          name      : str?,
                          shell     : bool,
                          namespace : str?,
                          env       : {str: str},
                        )

IRLog              ::= Log(
                          message : str,
                          level   : str?,
                        )

IROpaque           ::= Opaque(
                          description   : str,
                          python_object : Any,
                        )
```

### 2.1 What is NOT in the output

These input actions are **consumed during resolution** and never appear in the output IR:

| Input Action | Resolution |
|---|---|
| `DeclareLaunchArgument` | Default inserted into substitution context; recorded in `declared_args` metadata |
| `SetLaunchConfiguration` / `Unset` | Variable binding consumed during walk |
| `IncludeLaunchDescription` | Child file parsed and inlined; recorded in `includes` metadata |
| `OpaqueFunction` | Called; returned actions walked and inlined |
| `OpaqueCoroutine` | Called; result inlined or becomes `IROpaque` |
| `ForEach` | Unrolled; body actions inlined per iteration |
| `TimerAction` / `ROSTimer` | Deferred actions inlined (timing lost) |
| `PushEnvironment` / `Pop` / `Reset` / `Replace` | Env stack operations applied and consumed |
| `PushLaunchConfigurations` / `Pop` / `Reset` | Config stack operations applied and consumed |
| `AppendEnvironmentVariable` | Applied and consumed |
| `RegisterEventHandler` / `Unregister` | Extracted to `ResolvedLaunch.event_handlers` metadata |
| `LifecycleTransition` | Becomes `IREventHandler` in `event_handlers` metadata |
| `Shutdown` | Runtime event; consumed |
| `SetUseSimTime` | Becomes `IRSetParameter(name="use_sim_time", ...)` |
| `SetROSLogDir` | Applied to env context |
| `SetEnvironmentVariable` / `UnsetEnvironmentVariable` | Applied to env context; reflected in child nodes' `env` |
| `PushROSNamespace` | Applied to namespace stack; reflected in child nodes' `namespace` |
| `SetParameter` / `SetParametersFromFile` | Merged into child nodes' `parameters` / `param_files` |
| `SetRemap` | Merged into child nodes' `remappings` |
| Conditions (`if=`/`unless=`) | Evaluated; element present iff condition was true |
| All substitutions (`$(...)`) | Resolved to concrete `str`. **Exception:** `$(find-pkg-share pkg)` is preserved as-is in preview mode to keep paths portable. |

### 2.2 What DOES survive (inside groups)

| IR Action | Why it survives |
|---|---|
| `IRGroupAction` | Defines scope boundary; children inherit scoped state |

All scope-level mutations (`PushROSNamespace`, `SetEnv`, `UnsetEnv`, `SetParameter`, `SetRemap`, `SetParametersFromFile`) are **consumed** during resolution. Their effects are merged into child nodes' fields (`namespace`, `env`, `parameters`, `param_files`, `remappings`). The IR shows what will eventually run — each node carries its complete effective state.

### 2.3 Opaque actions

`IROpaque` is the escape hatch for actions that cannot be statically resolved:

- An `OpaqueFunction` returns an action type the resolver doesn't recognize
- An `EventHandler` callback is a Python closure that can't be introspected
- A custom action subclass not in the known set

`IROpaque` carries a `description` and the original `python_object`. Serialization to XML/YAML will skip or warn on `IROpaque` — this is a renderer concern, not a resolver concern.

### 2.4 Invariants

1. **Tree-structured** — `IRGroupAction` contains `[IRAction]` recursively. All other actions are leaves.
2. **Concrete** — Every `str` field is a resolved value; no `$(...)` substitutions remain.
3. **No conditions** — Every action in the output is unconditional. Conditions were evaluated during resolution.
4. **Deterministic** — Same inputs → same IR.
5. **Namespace and env are effective** — Each node carries its computed effective namespace and environment. Procedural scope actions (`PushROSNamespace`, `SetEnv`, `UnsetEnv`) are consumed; only their effects survive in node fields. Namespaces are always flattened (`--flatten-namespaces` is implicit).
6. **Declarative only** — No procedural actions (push/pop/set/unset for scope) survive in the IR. Only declarative actions (nodes, parameters, remaps, groups) remain.

---

## 3. Metadata (alongside actions)

The `ResolvedLaunch` container carries metadata that is not part of the action sequence but is needed by the orchestrator:

```
ResolvedLaunch     ::= {
                          actions        : [IRAction],
                          event_handlers : [IREventHandler],
                          packages       : [str],           (* all referenced packages *)
                          includes       : [ResolvedInclude],
                          declared_args  : [DeclaredArg],
                          warnings       : [str],
                          errors         : [str],
                        }

IREventHandler     ::= EventHandler(
                          kind        : EventHandlerKind,
                          target      : str?,
                          target_node : str?,
                          namespace   : str?,
                          start_state : str?,
                          goal_state  : str?,
                          actions     : [IREventAction],
                        )

EventHandlerKind   ::= "on_process_start"
                      | "on_process_exit"
                      | "on_state_transition"
                      | "on_shutdown"

IREventAction      ::= EmitEvent(
                          event       : str,
                          target_node : str?,
                          namespace   : str?,
                          metadata    : {str: str},
                        )
                        (* Events carry structured metadata beyond a name — e.g. Shutdown
                           has reason, SignalProcess has signal.  metadata captures
                           statically-known fields.  Events referencing runtime context
                           (ExecuteLocal, ExecuteProcess, TimerAction) cannot be fully
                           resolved; their metadata will be partial or empty. *)
                      | IROpaque

ResolvedInclude    ::= { package : str, share_path : str, args : {str: str} }
DeclaredArg        ::= { name : str, default : str, description : str? }
```

`includes` exists because even though child actions are inlined, the orchestrator needs the include graph for:
- Dependency tracking (which packages to fetch/build)
- Separate-file resolution (each included file may be resolved independently first)
- Include argument forwarding
