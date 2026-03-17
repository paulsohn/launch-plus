# Launch File Edge Cases

Based on analysis of Autoware reference codebase. These patterns must be supported by launch-plus.

## 1. Complex Substitution Patterns

### Environment Variables with Defaults
```xml
<arg name="vehicle_id" default="$(env VEHICLE_ID default)"/>
<arg name="data_path" default="$(env HOME)/autoware_data"/>
```

### Eval Expressions with Nested Variables
```xml
<group if="$(eval '\'$(var simulator_type)\' == \'carla\'')">
<group if="$(eval &quot;'$(var launch_driver)' and '$(var gnss_receiver)'=='septentrio'&quot;)">
```
- Escaped quotes within eval
- Boolean operators (and, or)
- HTML entity encoding (`&quot;`)

### Variables in Path Components
```xml
<arg name="lat_controller_param_path"
     value="$(find-pkg-share autoware_launch)/config/control/$(var controller_dir)/$(var mode).param.yaml"/>
```
- Multiple variables in single path
- Dynamic file selection

## 2. Conditional Patterns

### Multiple Let with If/Unless
```xml
<let name="launch_dummy" value="false" if="$(var scenario_simulation)"/>
<let name="launch_dummy" value="true" unless="$(var scenario_simulation)"/>
```
- Standard ROS 2 idiom for conditional variable assignment
- Conditions are fully evaluated: at most one branch executes
- Real-world example: `tier4_control_component.launch.xml` sets `latlon_controller_param_path_dir`
  to `$(var vehicle_id)` when `use_individual_control_param` is true (and `""` otherwise)
- **`<set_env>` and `<unset_env>` also support `if=`/`unless=` on the same basis**

### Scoped Groups

`scoped="false"` is a **legitimate language feature**, not an anti-pattern per se.
It allows a group's internal `<let>` assignments to propagate to the enclosing scope —
equivalent to an `if`-block in a programming language.

**Idiomatic use — conditional multi-variable assignment:**
```xml
<group scoped="false" if="$(var use_camera)">
  <let name="driver_exec" value="camera_driver"/>
  <let name="driver_pkg"  value="camera_pkg"/>
</group>
<!-- driver_exec and driver_pkg are visible here when use_camera is true -->
```
Without `scoped="false"` the `<let>` values die when the group closes.  The alternative
(repeating the `if` condition on every `<let>`) is more verbose and harder to maintain.

**Anti-pattern — `<include>` inside `scoped="false">`:**
```xml
<group scoped="false">
  <include file="sub.launch.xml"/>
  <!-- All variables set *inside sub.launch.xml* now bleed into this scope — invisible! -->
</group>
```
This causes cross-file, invisible state mutation: the caller has no way to know which
variables from `sub.launch.xml` are now in scope without opening the file.
Prefer explicit forwarding instead.

### Allow / deny table for `scoped="false">` children

The governing principle: an element is problematic inside `scoped="false">` if it
introduces variable side-effects that are **invisible at the call site** (defined in
another file or hidden behind a recursive include chain).

| Child element | Verdict | Reason |
|---|---|---|
| `<let>` | ✅ Allow — **idiomatic** | Leaked variable is right there in the group; reader can see exactly what propagates |
| `<arg>` | ✅ Allow — unusual but readable | Arg declaration is visible; reader knows what becomes available |
| `<node>` | ✅ Allow — neutral | Nodes produce no launch-scope variables; `scoped` attribute has no effect |
| `<set_env>` / `<unset_env>` | ✅ Allow — scoped | Env vars are tracked in `ctx.env`; `scoped=true` groups save/restore env (mutations don't leak); `scoped=false` lets mutations propagate |
| `<push-ros-namespace>` | ✅ Allow — neutral | Namespace pushes are always group-scoped in our resolver regardless of `scoped` flag; the push does not leak |
| `<group scoped="true">` (default) | ✅ Allow | Acts as a scoping barrier; nothing inside can leak further |
| `<group scoped="false">` (nested) | ⚠️ Inherit | Apply the same rules recursively; warn if the nested unscoped group contains `<include>` |
| `<include>` | ❌ **Warn** | Leaks **all** internal `<let>` / `<arg>` from the included file; invisible at the call site |

**Why `<push-ros-namespace>` is neutral:** our resolver saves `ctx.namespace_stack.len()`
before the unscoped group's children and truncates to that length afterward.  This is an
intentional design choice — namespace scope is always group-bounded regardless of the
`scoped` attribute.

**Resolution support:** our resolver correctly handles both cases — `scoped=false` shares
the parent `SubstitutionContext` so `<let>` assignments propagate, while `scoped=true`
(default) creates a new context that is discarded on exit.

### Conditional Includes
```xml
<include file="..." if="$(var launch_driver)">
  <arg name="..." value="..."/>
</include>
```

## 3. Namespace Stacking

```xml
<group>
  <push-ros-namespace namespace="lidar"/>
  <group>
    <push-ros-namespace namespace="top"/>
    <!-- Results in /lidar/top -->
  </group>
</group>
```

## 4. Python Launch Patterns

### DeclareLaunchArgument Defaults

M4 implements "easy" defaults (plain strings, simple substitutions). Three hard cases are
deferred to M9:

**Circular / forward-reference defaults:**
```python
# B's default references A, but A is declared after B
DeclareLaunchArgument("B", default_value=LaunchConfiguration("A"))
DeclareLaunchArgument("A", default_value="hello")
```
M4 two-pass processes in list order; if A isn't in context yet when B is processed, B's
default resolves to `""`. M9 should implement a dependency-sorted resolution pass.

**Conditional declaration:**
```python
DeclareLaunchArgument("use_feature", default_value="true",
    condition=IfCondition(LaunchConfiguration("enable_extras")))
```
If `enable_extras` itself has no default (or its own default is not yet resolved), our
`.evaluate()` call on the condition fails silently and the declaration is skipped entirely.

**Declarations inside OpaqueFunction:**
```python
def launch_setup(context, *args, **kwargs):
    DeclareLaunchArgument("dynamic_arg", default_value="foo")  # inside opaque fn
    ...
generate_launch_description():
    return LaunchDescription([OpaqueFunction(function=launch_setup)])
```
The two-pass pre-pass (Pass 1) only sees top-level entities, not those returned by
`OpaqueFunction`. The declaration is only applied if the OpaqueFunction succeeds in Pass 2,
but by then the pre-pass context has already been used to resolve other args.

### OpaqueFunction with Context
```python
def launch_setup(context, *args, **kwargs):
    sensor_model = LaunchConfiguration("sensor_model").perform(context)
    # Dynamic node creation based on runtime value
    return [container]

generate_launch_description():
    return LaunchDescription([OpaqueFunction(function=launch_setup)])
```

### SetLaunchConfiguration with Conditions
```python
SetLaunchConfiguration("container_executable", "component_container",
    condition=UnlessCondition(LaunchConfiguration("use_multithread")))
SetLaunchConfiguration("container_executable", "component_container_mt",
    condition=IfCondition(LaunchConfiguration("use_multithread")))
```

### YAML Loading in Launch Scripts
```python
def get_vehicle_info(context):
    path = LaunchConfiguration("vehicle_param_file").perform(context)
    with open(path, "r") as f:
        p = yaml.safe_load(f)["/**"]["ros__parameters"]
    return p
```

### ParameterFile with Substitutions
```python
ParameterFile(param_file=path, allow_substs=True)
```
- Substitutions inside YAML parameter files

## 5. Composable Nodes

### LoadComposableNodes with Dynamic Container
```python
LoadComposableNodes(
    composable_node_descriptions=[...],
    target_container=LaunchConfiguration("container_name"),
    condition=IfCondition(LaunchConfiguration("use_filter")),
)
```

### Complex Remappings
```python
remappings=[
    ("~/input/twist", "/sensing/vehicle_velocity_converter/twist_with_covariance"),
    ("output", "concatenated/pointcloud"),
]
```
- Tilde notation for private topics

## 6. Parameter File Patterns

### allow_substs in XML
```xml
<param from="$(var config_file)" allow_substs="true"/>
```

### Preset Loading via Include
```xml
<include file="$(find-pkg-share pkg)/config/preset/$(var preset)_preset.yaml"/>
```
- YAML files included as launch resources

## 7. Node Configuration

### Respawn with Variables
```xml
<node respawn="$(var rviz_respawn)" respawn_delay="1.0">
```

### Command Arguments with Substitutions
```xml
<node args="-d $(var rviz_config) -s $(find-pkg-share pkg)/image/logo.png">
  <env name="QT_QPA_PLATFORMTHEME" value="qt5ct"/>
</node>
```

## 8. Include Patterns

### Deep Nesting
```
autoware.launch.xml
  → tier4_vehicle_launch/vehicle.launch.xml
    → sensor_kit/sensing.launch.xml
      → lidar.launch.xml
        → velodyne_VLS128.launch.xml
```
- 5+ levels of nesting common

### Argument Forwarding
```xml
<include file="...">
  <arg name="vehicle_model" value="$(var vehicle_model)"/>
  <arg name="sensor_model" value="$(var sensor_model)"/>
  <!-- 20+ args forwarded -->
</include>
```

## 9. Build-Generated Config/Param Files Accessed in Python Launch Scripts

### Problem

Some Python launch scripts eagerly read config/param files at launch-description construction
time — before any node is started — to compute derived argument defaults:

```python
def get_vehicle_info(context):
    path = LaunchConfiguration("vehicle_param_file").perform(context)
    with open(path, "r") as f:                          # reads at analysis time
        p = yaml.safe_load(f)["/**"]["ros__parameters"]
    return p
```

Some of these files are **generated during the build step** (e.g., CMake `configure_file`,
Python `setup.py` data generation, or `xacro`-processed YAMLs) and do **not exist in the
source tree**. They live in `install/share/<pkg>/config/...` only after `colcon build`.

### Current Behaviour (M4/M9 py_resolver)

- `FindPackageShare("pkg")` / `get_package_share_directory("pkg")` → the package IS tracked
  as a dependency even before the file open attempt (tracking happens at import time)
- The subsequent `open(path)` raises `FileNotFoundError` inside the OpaqueFunction
- py_resolver catches ALL OpaqueFunction exceptions as **warnings**, continues processing
- Net effect: package dependency recorded ✓, nodes/includes inside the OpaqueFunction are
  **lost** (the function never returned), recorded as a warning

This is acceptable for static analysis: the launch file cannot be fully resolved without
a build, but we don't fail fatally and we do capture the direct package dependency.

### Distinguishing "build-generated" from "wrong path"

Currently there's no distinction. A specific warning category helps the user understand:

| Scenario | Ideal warning |
|---|---|
| File path looks like install/ but doesn't exist | "File may need `colcon build` to generate" |
| File path looks like source/ but doesn't exist | "File not found — possible misconfiguration" |
| File path is a `PathJoinSubstitution` that was not yet performed | "Substitution not evaluated" |

### Colcon Install Convention Support

All three colcon install conventions are handled transparently:

| Convention | `AMENT_PREFIX_PATH` after sourcing | Resolved share path |
|---|---|---|
| Isolated (default) | `install/pkg1:install/pkg2:...` | `install/<pkg>/share/<pkg>/` |
| `--merge-install` | `install/` | `install/share/<pkg>/` |
| `--symlink-install` | `install/<pkg>:...` | symlink → source file |

This works because both the Rust `PackageLocator` (Strategy 3 in `locate_package_share`)
and Python `_real_get_package_share_directory` resolve via `AMENT_PREFIX_PATH` directly.
The `share/<pkg>/` tree structure is identical across all conventions.

**Residual gap — source-first priority:**
`all_package_shares()` fills the package map with **source paths first** (from lockfile),
and AMENT_PREFIX_PATH entries only cover packages not already present (`or_insert_with`).
So for workspace packages, `_resolve_pkg_share` returns the source path even when a build
is present, missing generated files that only exist in the install space.

After M5 (Builder), we should add a `--post-build` mode (or `--install-base <path>`) that
inverts this priority: prefer installed paths so generated files are accessible.

### Planned (M9)

- Detect `open()` / `yaml.safe_load()` calls in OpaqueFunction bodies where the path
  resolves to a build-output location (contains `install/`, `build/`, or the package is
  in the workspace but has no such file in its source tree)
- Emit a specific warning: `"[pkg] config/foo.yaml not found in source — may require build"`
- Continue tracking the package dependency regardless
- Do NOT attempt to locate the file via `ament_index_python` at analysis time (the path
  resolution already went through `FindPackageShare`, so the package is tracked)
- For `--post-build` / `--install-base` mode: prefer install path over source path in
  `_resolve_pkg_share` and `all_package_shares()`, so generated files are accessible

### Workaround for Launch File Authors

Prefer reading files lazily (inside `OpaqueFunction`) over reading them at module load or at
`generate_launch_description()` time. If the file is generated, add a guard:

```python
def get_vehicle_info(context):
    path = LaunchConfiguration("vehicle_param_file").perform(context)
    if not os.path.exists(path):
        return {}           # safe default — node still launches, uses its own defaults
    with open(path) as f:
        return yaml.safe_load(f)["/**"]["ros__parameters"]
```

This makes the launch file work both with and without a pre-built install tree.

---

## 10. Resolved XML as Execution Artifact ("resolve then run")

### Use Case

During migration or debugging, teams may want to:

1. Use `launch-plus resolve` to produce a flattened launch file
2. Run that file directly with `ros2 launch flat.launch.xml`

This "resolve then run" workflow is valuable during migration phases — it simplifies a
complex multi-package include hierarchy into a single, auditable file that can be compared
against the original runtime behavior.

### Path Resolution Concern

`launch-plus resolve` resolves `$(find-pkg-share pkg)` to **source paths** by default:

```
$(find-pkg-share autoware_launch) → /workspace/src/autoware_launch
```

When running the resolved XML with `ros2 launch`, param files pointing to source paths
may fail if:

1. **Copy-install (default colcon)**: files live only in `install/share/<pkg>/config/...`,
   not in source.  The resolved path is valid but does not exist at `ros2 launch` time.
2. **Build-generated files**: YAML/headers produced by `cmake configure_file`, `xacro`,
   or Python codegen do not exist in source at all — they appear only in `install/` after
   `colcon build`.  The resolved path will be missing regardless.

Works correctly when:
- **`--symlink-install`** was used: `install/share/<pkg>/` symlinks back to source files,
  so source paths and install paths are the same file.
- All referenced param files are hand-written and checked in to the source tree.

### Bare `<group>` Safety

The resolved XML uses bare `<group>` elements as visual containers for include boundaries.
A bare `<group>` (no `ns` attribute) is **inert in `ros2 launch`**: it does not push a
namespace or alter execution order.  The resolved XML is therefore safe to feed directly
to `ros2 launch` without any semantic difference.

### Proposed Solution: `--output-paths=install`

A flag that resolves `$(find-pkg-share pkg)` against `AMENT_PREFIX_PATH` (install space)
instead of the source workspace:

```bash
# Source paths (default) — for inspection, dependency analysis
launch-plus resolve autoware_launch autoware.launch.xml > inspect.launch.xml

# Install paths — for direct execution with ros2 launch
launch-plus resolve autoware_launch autoware.launch.xml --output-paths=install > run.launch.xml
ros2 launch run.launch.xml
```

Requires the workspace to be built and sourced (`source install/setup.bash`) before
resolving so that `AMENT_PREFIX_PATH` is populated.

Implementation: in `--output-paths=install` mode, `PackageLocator` and `py_resolver` use
AMENT_PREFIX_PATH-first priority (inverse of the current source-first order in
`all_package_shares()`).

### Status

| Phase | Behaviour |
|-------|-----------|
| M4 (current) | Source paths only.  Works with `--symlink-install`; may fail for generated files with copy-install. |
| M5 (planned) | Add `--output-paths=install` flag.  Integrate with the Builder so the resolved XML is immediately runnable after `launch-plus build`. |

---

## 11. Generated Launch Files

### Problem
Some launch files don't exist in source code but are generated during build:

```python
# setup.py or CMakeLists.txt
# Generates launch files from templates or Python scripts
generate_launch_description_from_config("config.yaml", "generated.launch.py")
```

These patterns are common when:
- Launch files are templated from YAML configs
- Multiple similar launch files generated from a single source
- Launch files generated based on hardware configuration

### Resolution Strategy

| Scenario | Behavior |
|----------|----------|
| Launcher found in `src/` | Resolve directly (no build needed) |
| Launcher not found in `src/` | **Fail by default** |
| Launcher not found + `--build-if-missing` | (**Not yet implemented**) Trigger build, then resolve from `install/` |

### CLI Options (planned)

```bash
# Default: fail if launcher not found
launch-plus resolve my_pkg launcher.launch.xml

# Build if needed (resolve mode with build fallback)
launch-plus resolve my_pkg launcher.launch.xml --build-if-missing

# Or use build/run which always builds first
launch-plus build my_pkg launcher.launch.xml
launch-plus run my_pkg launcher.launch.xml
```

### Detection Heuristics

To detect if a package might generate launch files:
1. Check `CMakeLists.txt` for `generate_launch_description` or similar
2. Check `setup.py` for launch file generation in `data_files`
3. Check for `*.launch.py.in` template files
4. If package has `launch/` dir but requested file missing → might be generated

### Warning
```
Warning: Launch file 'generated.launch.py' not found in source.
  Package 'my_pkg' may generate this file during build.
  Use --build-if-missing to build first, or use 'launch-plus build' command.
```

## 12. Build Modes

Two distinct build commands with dedicated verbs:

### `build` - Target-Focused (Bazel-like)
Build only what's required for a specific launcher:

```bash
launch-plus build my_pkg my_launcher.launch.xml
```

**Process:**
1. Parse launch file to find all nodes/includes
2. Resolve package dependencies (from lockfile)
3. Build only required packages
4. Install to isolated prefix

**Advantages:**
- Fast incremental builds
- Minimal disk usage
- Clear dependency graph

### `build-pkg` - Package-Wide (colcon-like)
Build everything in one or more packages:

```bash
# Build entire package
launch-plus build-pkg my_pkg

# Build multiple packages
launch-plus build-pkg pkg1 pkg2 pkg3

# Build all packages in lockfile
launch-plus build-pkg --all
```

**Use cases:**
- CI/CD full builds
- Package development/testing
- When launcher is unknown

### Command Summary

| Command | What gets built |
|---------|-----------------|
| `build pkg launcher` | Only packages needed for launcher |
| `build-pkg pkg` | Everything in package |
| `build-pkg --all` | All packages in lockfile |
| `resolve pkg launcher --build-if-missing` | Minimal (just target pkg to generate launcher) |

### Minimal Build for Generated Launchers

When `--build-if-missing` triggers a build, it should be minimal:
1. Build only the target package (to generate the launcher)
2. Don't build dependencies yet (resolve will figure those out after)
3. If resolve then finds more dependencies, those are built in the actual `build` phase

```
resolve --build-if-missing flow:
  1. Launcher not found in src/
  2. build-pkg target_pkg (generates launcher)
  3. Parse generated launcher
  4. Return resolved launch graph (dependency builds deferred to build phase)

build flow:
  1. Resolve launcher → get dependency graph
  2. Build all required packages (target-focused)
  3. Ready to run
```

## 13. YAML-as-Launch-File (Preset Pattern)

### Pattern

Autoware includes YAML files directly from XML launch files to inject arg defaults into scope:

```xml
<!-- tier4_planning_component.launch.xml -->
<arg name="module_preset" default="default"/>
<include file="$(find-pkg-share autoware_launch)/config/planning/preset/$(var module_preset)_preset.yaml"/>

<!-- All nested includes now see $(var launch_parking_module), $(var motion_path_smoother_type), etc. -->
<include file="$(find-pkg-share tier4_planning_launch)/launch/planning.launch.xml">
  ...
</include>
```

The YAML file uses the standard ROS 2 YAML launch format — the same schema supported natively by `ros2 launch`:

```yaml
# config/planning/preset/default_preset.yaml
launch:
  - arg:
      name: launch_parking_module
      default: "true"
  - arg:
      name: motion_path_smoother_type
      default: elastic_band
  - arg:
      name: launch_static_obstacle_avoidance
      default: "true"
  # ... ~30 more args
```

Variables propagate to all downstream includes via normal scope inheritance:
```xml
<!-- scenario_planning/parking.launch.xml -->
<group if="$(var launch_parking_module)">
  ...
</group>
```

### Why This Pattern Exists

Without it, the alternative would be forwarding all 30+ preset args explicitly through every
`<include>` in the chain — extremely verbose.  The preset YAML provides a flat, single-file
place to define an entire module configuration profile, and the implicit scope inheritance
(not explicit arg passing) does the propagation.

The dynamic filename `$(var module_preset)_preset.yaml` allows swapping configurations:
```bash
ros2 launch autoware_launch autoware.launch.xml planning_module_preset:=minimal
```

### Is It Idiomatic?

It **works and is used widely in Autoware**, but has downsides:
- Variables appear "from nowhere" in downstream files — no `<arg>` declaration in the file that uses them
- `ros2 launch --show-args` does not surface preset variables
- Files like `parking.launch.xml` silently depend on being included through the preset chain
- The dynamic filename makes the dependency graph invisible to static analysis without knowing the arg value

The idiomatic alternative is explicit arg passing through each `<include>`, which is more
verbose but makes dependencies legible.

### Support Status (as of M4.5)

`parse_launch_yaml()` (resolver.rs) replaces the former `apply_yaml_preset_args()` and
produces the same `LaunchElement` AST as XML parsing — all downstream resolution is shared:

- ✅ `arg`, `let`, `set_env`, `unset_env`, `push-ros-namespace`
- ✅ `group` with children (recursive)
- ✅ `include` with arg list
- ✅ `node` (basic fields: pkg, exec, name, namespace, param, remap, env)
- ✅ Conditionals (`if`/`unless`) on all elements
- ✅ YAML files recorded in `required_files` with `DependencyKind::Launch`
- ✅ Substitution resolution in all string fields
- ✅ Silent no-op when file not found (fetch-on-demand compatibility)
- ❌ → M12: `composable_node_container`, `load_composable_nodes`
- ❌ → M12: Unknown element types (silently skipped for now)

---

## 14. Python Event-Driven Launch Patterns

### Overview

Python launch files support event-driven composition via `RegisterEventHandler` with handlers
like `OnProcessStart`, `OnProcessExit`, `OnProcessIO`, and `OnStateTransition`. These are
evaluated at runtime by the `ros2 launch` framework and are difficult to model statically.

Two patterns account for virtually all event-handler usage in the Autoware reference codebase.
In **both patterns** the nodes are always **explicitly declared** in the `LaunchDescription` —
event handlers only drive operational behaviour (lifecycle transitions, shutdown propagation),
never node creation.

---

### Pattern A: Lifecycle Auto-Manage (configure → activate)

**Used in:** `ros2_socketcan` (socket_can_receiver, socket_can_sender), `serial_driver`

```python
socket_can_receiver_node = LifecycleNode(
    package="ros2_socketcan",
    executable="socket_can_receiver_node_exe",
    ...)

return LaunchDescription([
    socket_can_receiver_node,
    # Automatically configure when process starts
    RegisterEventHandler(
        OnProcessStart(
            target_action=socket_can_receiver_node,
            on_start=[EmitEvent(event=ChangeState(
                lifecycle_node_matcher=MatchesNode(socket_can_receiver_node),
                transition_id=Transition.TRANSITION_CONFIGURE,
            ))],
        )
    ),
    # Automatically activate when configure completes
    RegisterEventHandler(
        OnStateTransition(
            matcher=MatchesStateTransition(
                lifecycle_node_matcher=MatchesNode(socket_can_receiver_node),
                start_state="configuring",
                goal_state="inactive",
            ),
            entities=[EmitEvent(event=ChangeState(
                lifecycle_node_matcher=MatchesNode(socket_can_receiver_node),
                transition_id=Transition.TRANSITION_ACTIVATE,
            ))],
        )
    ),
])
```

The `LifecycleNode` is always explicitly declared. The two `RegisterEventHandler` blocks only
automate the configure→activate state machine on startup.

`serial_driver` adds a third handler for clean shutdown:
```python
RegisterEventHandler(
    OnShutdown(
        on_shutdown=[EmitEvent(event=ChangeState(
            lifecycle_node_matcher=...,
            transition_id=Transition.TRANSITION_ACTIVE_SHUTDOWN,
        ))]
    )
)
```

---

### Pattern B: Exit-Propagates-Shutdown

**Used in:** `autoware_pointcloud_merger`, `random_test_runner`

```python
merger_node = Node(package="autoware_pointcloud_merger", ...)

merger_shutdown = RegisterEventHandler(
    event_handler=OnProcessExit(
        target_action=merger_node,
        on_exit=[
            LogInfo(msg="shutdown launch"),
            EmitEvent(event=Shutdown()),
        ],
    )
)

return LaunchDescription([merger_node, merger_shutdown])
```

When the target node exits, this handler shuts down the entire launch process. Used for
single-node utilities where the launch file should terminate together with the node.

---

### Current Handling in py_resolver

`RegisterEventHandler`, `OnProcessStart`, `OnProcessExit`, and `OnStateTransition` are all
stubbed to return `None`. The `_walk_actions` loop skips `None` entries silently:

```python
mod.RegisterEventHandler = lambda *a, **kw: None
mod.OnProcessExit = lambda *a, **kw: None
mod.OnProcessStart = lambda *a, **kw: None
```

Because all nodes are declared **outside** the event handlers, **no nodes are dropped**.
The stubs are safe for static dependency analysis.

**Effect on output:**

| Aspect | Status |
|--------|--------|
| Node declarations | ✅ Fully captured (declared outside event handlers) |
| Lifecycle state transitions | ✅ Correctly ignored (operational only; no impact on dep graph) |
| Shutdown propagation | ✅ Correctly ignored (operational only) |
| Spurious errors | ✅ None — stubs return None, None entries are silently skipped |

---

### Proposed XML Serialization (M9/M10)

When the resolver detects these patterns, the resolved XML can encode the operational intent
using node-level attributes — keeping the output readable and avoiding new element types.

#### Chosen: Explicit event-handler elements

```xml
<lifecycle_node pkg="ros2_socketcan" exec="socket_can_receiver_node_exe" name="socket_can_receiver">
    <param name="interface" value="can0"/>
</lifecycle_node>

<on_process_start target="socket_can_receiver">
    <emit_event event="configure" target_node="socket_can_receiver" />
</on_process_start>

<on_state_transition target_node="socket_can_receiver" start_state="configuring" goal_state="inactive">
    <emit_event event="activate" target_node="socket_can_receiver" />
</on_state_transition>
```

These are launch-plus XML extensions — `ros2 launch` does not support them. They serve as
documentation in the resolved output. Actual lifecycle management (calling `/change_state`
services) is deferred to a future `launch-plus run` implementation.

For Pattern B (exit-propagates-shutdown):
```xml
<on_process_exit target="merger_node">
    <emit_event event="shutdown" />
</on_process_exit>
```

---

### Implementation

**Parser:** `<lifecycle_node>` parsed like `<node>` but produces `LaunchElement::LifecycleNode`.
`<on_process_start>`, `<on_state_transition>`, `<on_process_exit>` produce
`LaunchElement::EventHandler { kind, children }`. `<emit_event>` produces
`LaunchElement::EmitEvent` (only valid inside event handlers).

**Resolver:** `NodeKind::LifecycleNode` (unit variant, like `Node`).
`NodeKind::EventHandler { handler_kind, actions }` carries the resolved event data.
Shared node-resolution logic extracted to avoid duplication between `Node` and `LifecycleNode`.

**Python resolver:** `_TrackedLifecycleNode` emits `"kind": "lifecycle_node"`.
Replace `RegisterEventHandler`/`OnProcessStart`/`OnStateTransition`/`EmitEvent` stubs with
tracked classes that record handler structure into `_tracked["event_handlers"]`.

**Rendering:** Parameterize `render_node()` with a tag name string. Add `render_event_handler()`
for `<on_*>` wrappers containing `<emit_event />` children.

---

### Design Note: Static vs. Runtime Semantics

Event handlers affect **runtime behaviour** only — when nodes become active, what happens when
they exit. They do **not** affect the dependency graph (which packages are needed, which nodes
exist, which topics are remapped). For the primary purpose of `launch-plus` (dependency
indexing and workspace management), the current silent-drop behaviour is correct and complete.

The event-handler elements in the resolved XML are **informational**: they make the output
more faithful to the original Python intent. Actual lifecycle management at execution time
is deferred to future work.

---

## 15. `--flatten-namespaces`: Inline Namespace Stack into Node Attributes

### Feature Overview

By default, `launch-plus resolve` preserves `<push-ros-namespace>` wrappers inside
`<group>` elements.  The `--flatten-namespaces` flag changes this: the full namespace
is eagerly applied as a `namespace=` attribute directly on each `<node>` element, and
`<push-ros-namespace>` is omitted entirely from the output.

This means each launch-file boundary produces **one `<group>` element** (as a pure
source-traceability container) rather than splitting into sub-groups per namespace.

```bash
# Default: preserve push-ros-namespace
launch-plus resolve autoware_launch autoware.launch.xml ...

# Flatten: inline namespace into each node
launch-plus resolve autoware_launch autoware.launch.xml --flatten-namespaces ...
```

**Default output (without flag):**
```xml
<group>
  <!-- source: tier4_sensing_launch://launch/sensing.launch.xml -->
  <push-ros-namespace namespace="/sensing/lidar"/>
  <node pkg="lidar_driver" exec="driver_node"/>
  <node pkg="lidar_filter" exec="filter_node"/>
</group>
```

**Flattened output (with `--flatten-namespaces`):**
```xml
<!-- source: tier4_sensing_launch://launch/sensing.launch.xml -->
<group>
  <node pkg="lidar_driver" exec="driver_node" namespace="/sensing/lidar"/>
  <node pkg="lidar_filter" exec="filter_node" namespace="/sensing/lidar"/>
</group>
```

### Semantic Equivalence

In ROS 2 launch XML, these two forms are **fully equivalent at runtime**:

| Form | Mechanism |
|------|-----------|
| `<push-ros-namespace namespace="/sensing/lidar"/>` inside `<group>` | Dynamically pushes namespace onto stack; restored when group exits |
| `namespace="/sensing/lidar"` on `<node>` | Statically sets effective namespace directly |

Both produce the same effective node namespace.  The `<group>` element without a
`<push-ros-namespace>` child is inert (no behavioral effect; it is a visual container only).

### Implementation Notes

- `node.namespace` already holds the **fully composed** effective namespace
  (`effective_namespace(namespace_stack, node_explicit_ns)`), computed during resolution.
  Flatten mode simply uses this value directly as the `namespace=` attribute.
- Grouping key changes from `(namespace_stack, source)` to `source` only, enabling
  "one group per launch file" even when that file spans multiple namespace contexts.

### Edge Cases

#### EC-15.1: Node with explicit `namespace=` attribute beyond the stack

```xml
<!-- sensing.launch.xml — stack is ["sensing"] -->
<node pkg="x" exec="y" namespace="lidar"/>  <!-- relative: appended → /sensing/lidar -->
<node pkg="a" exec="b" namespace="/override"/> <!-- absolute: replaces stack → /override -->
```

- **Normal mode:** `<push-ros-namespace namespace="/sensing"/>` in `<group>`; nodes emit
  only the _extra_ part: `namespace="lidar"` or `namespace="/override"` respectively.
- **Flatten mode:** each node gets its fully composed `node.namespace` value:
  `namespace="/sensing/lidar"` and `namespace="/override"`.  No information is lost.

#### EC-15.2: Absolute `namespace=` on `<push-ros-namespace>` resets the stack

In ROS 2, `<push-ros-namespace namespace="/abs"/>` replaces the accumulated stack
with `/abs` (absolute).  Our resolver models this by accumulating raw components;
`effective_namespace()` joins them with `/` and strips leading `/` from interior
components.

**Example:** Stack `["sensing", "/abs"]` currently produces `/sensing/abs`, not `/abs`.

> **Known gap** (pre-existing): absolute namespace reset within nested
> `<push-ros-namespace>` stacks is not modeled correctly.  This affects both normal and
> flatten mode equally; `--flatten-namespaces` does not worsen the situation.
>
> Tracked as part of M9.5 (Unified Resolver IR).

#### EC-15.3: Nodes with no namespace (empty stack, no explicit `namespace=`)

- `node.namespace` is `None`.
- In flatten mode: `namespace=` attribute is omitted.  Output is identical in both modes.

#### EC-15.4: Remappings with relative topic names

```python
remappings=[("~/input/twist", "/sensing/velocity"), ("output", "concat/pc")]
```

- **Normal mode:** Relative topics (`output`) are resolved at runtime by ROS 2 relative
  to the node's effective namespace (set by `<push-ros-namespace>`).
- **Flatten mode:** Same runtime semantics, since the node's effective namespace is now
  set by the `namespace=` attribute instead.  **No behavioral difference.**

#### EC-15.5: OpaqueFunction-derived namespace pushes (Python launch files)

Python launch files may call `PushRosNamespace(...)` inside an `OpaqueFunction`.
If the function succeeds, the namespace is captured in `namespace_stack`.
If the function fails (missing params, missing files), the namespace is **not** captured.

- In both modes this limitation is the same; `--flatten-namespaces` does not worsen it.
- When the namespace IS captured, flatten mode correctly inlines it.

#### EC-15.6: Non-consecutive nodes from the same file

If nodes from file A are interleaved with nodes from file B:

```
[fileA node1, fileB node1, fileA node2]
```

Both modes will produce two `<group>` elements for file A (one before and one after
file B's group) rather than one.  This is a source-ordering artifact, not a bug —
the namespace-flattened output cannot merge non-adjacent groups without reordering
nodes.

#### EC-15.7: Grouping "one group per file" guarantee

The flag achieves *at most* one group per contiguous run from the same source file.
Files that produce nodes interleaved with other files (rare in practice) will still
produce multiple groups.  The typical Autoware pattern (include → all nodes from
that file immediately) produces exactly one group per include boundary.

#### EC-15.8: Resolved XML validity for `ros2 launch`

`<node namespace="..."/>` is standard in ROS 2 XML launch format (supported since
ROS 2 Dashing).  Bare `<group>` without `<push-ros-namespace>` is inert.  The
flattened output is safe to feed directly to `ros2 launch` without any semantic
difference from the default output.

---

## 16. Arg-Lifecycle Diagnostics — Common Warning Patterns

Observed when running against real Autoware `autoware.launch.xml` (211 warnings total;
208 were excessive-include-arg).  Two distinct root causes, each with a different fix.

---

### 16.1 Python Interface Gap (false positives — now fixed)

**Symptom:** Every arg forwarded to a `.launch.py` file is flagged as excessive even though
the Python file does declare the arg via `DeclareLaunchArgument`.

**Root cause:** The `declared_args_by_file` map was only populated for XML files.
Python files were resolved by the `py_resolver` shim, which *does* return
`declared_args: Vec<PyDeclaredArg>` — but that list was never inserted into the map.
Consequently, the parent's excessive-include-arg check always found an empty child set
and flagged every forwarded arg.

**Fix (applied):** After `resolve_python_file_recursive` succeeds, the Python-declared
args are now inserted into `declared_args_by_file`:

```rust
let py_declared: HashSet<String> = py_output.declared_args.iter()
    .map(|a| a.name.clone())
    .collect();
result.declared_args_by_file
    .insert((package.to_string(), share_path.to_path_buf()), py_declared);
```

**Affected top Python targets in Autoware (pre-fix):**

| Python file | Warning count |
|---|---|
| `nebula_node_container.launch.py` | 34 |
| `traffic_light_node_container.launch.py` | 19 |
| `global_params.launch.py` | 14 |
| `ground_segmentation.launch.py` | 8 |
| `sensing_component.launch.py` | 5 |

---

### 16.2 Skip-a-Level Forwarding (true positives — idiomatic Autoware pattern)

**Symptom:** An intermediate XML file explicitly passes 20-60 args to a child include,
but the child XML file never declares those args via `<arg name="..."/>`.

**Pattern:**

```
autoware.launch.xml
  → tier4_planning_component.launch.xml   ← forwards all 57 config args
      → planning.launch.xml               ← DOES NOT declare them; relies on
                                             ROS 2's LaunchConfiguration cascade
          → scenario_planning.launch.xml
          → parking.launch.xml
          ...
```

`tier4_planning_component.launch.xml` passes args like `use_surround_obstacle_check`,
`traffic_light_recognition_package`, etc. to `planning.launch.xml`.
`planning.launch.xml` does not declare these args because it uses the implicit
`LaunchConfiguration` global context — when `ros2 launch` is used, any configuration
set by the parent is visible to all descendants without explicit forwarding.

`launch-plus` resolves files independently, so this "cascade via context" is only
honoured when `--allow-global-arg-cascade` is passed.

**Top XML targets in Autoware:**

| XML file | Warning count |
|---|---|
| `planning.launch.xml` | 57 |
| `detection.launch.xml` | 13 |
| `localization.launch.xml` | 10 |
| `perception.launch.xml` | 9 |
| `sensing.launch.xml` | 7 |

**Guidance for users seeing this warning:**

| Situation | Recommended action |
|---|---|
| File tree that intentionally relies on `LaunchConfiguration` cascade | Pass `--allow-global-arg-cascade` to suppress these warnings |
| Migrating to explicit arg passing | Add `<arg name="X"/>` (no default) to each intermediate file so the chain is self-documenting |
| Quick audit — only care about errors | Run without `--strict`; excessive-include-arg warnings are non-fatal by default |

**Why explicit arg passing is better:**

```xml
<!-- Before: planning.launch.xml silently depends on parent cascade -->
<!-- No <arg> declarations at the top — args arrive "from nowhere" -->

<!-- After: self-documenting -->
<arg name="use_surround_obstacle_check"/>
<arg name="traffic_light_recognition_package"/>
<!-- ... -->
```

Explicit declarations make `ros2 launch --show-args` surface the full interface,
enable static analysis, and prevent hard-to-debug surprises when files are reused
outside the original include hierarchy.

---

## Priority Implementation Order

| Priority | Pattern | Reason |
|----------|---------|--------|
| P0 | Basic substitutions (`$(arg)`, `$(var)`, `$(find-pkg-share)`) | Core functionality |
| P0 | Node/include elements | Core functionality |
| P0 | If/unless conditions | Very common |
| P1 | Nested namespace stacking | Common in sensor setups |
| P1 | Multiple let with conditions | Common pattern |
| P1 | `$(env VAR default)` | Used for vehicle ID |
| P1 | `scoped="false"` for `<let>` (conditional assignment) | Fully supported; `<include>` inside `scoped="false"` emits a warning (M8) |
| P2 | `$(eval ...)` expressions | Complex conditionals |
| P2 | OpaqueFunction in Python | Dynamic launch.py |
| P2 | `allow_substs=true` | Parameter file substitutions |
| P3 | YAML loading in Python | Advanced pattern |
| P3 | Composable node dynamic targeting | Advanced pattern |
| P3 | Generated launch files (`--build-if-missing`) | Edge case fallback |

## Anti-Patterns and Warnings

Support these patterns when possible, but emit warnings to help users identify potential issues.

### Scope Leakage via Include

`scoped="false"` is **legitimate for conditional variable assignment** (see Section 2).
The anti-pattern is specifically `<include>` inside a `scoped="false">` group, where the
included file's *internal* variables invisibly bleed into the caller's scope:

```xml
<group scoped="false">
  <include file="sub.launch.xml"/>
</group>
```
**Warning:** `<include> inside scoped="false"> leaks sub.launch.xml's internal variables into this scope — prefer explicit argument passing`

### Default Value in Non-Top-Level Launcher
```xml
<!-- In included file: sub.launch.xml -->
<arg name="vehicle_model" default="sample_vehicle"/>
```
**Warning:** `Argument 'vehicle_model' uses default value in included file - should be passed explicitly from parent`

**Detection strategy:** Invalidate default values when resolving includes; if default is used, warn.

### Unused Arguments
```xml
<launch>
  <arg name="debug_mode"/>  <!-- declared but never referenced -->
  <node pkg="my_pkg" exec="node"/>
</launch>
```
**Warning:** `Argument 'debug_mode' declared but never used`

### Shadowed Arguments
```xml
<!-- parent.launch.xml -->
<arg name="config" value="a"/>
<include file="child.launch.xml">
  <arg name="config" value="b"/>  <!-- shadows parent -->
</include>
```
**Warning:** `Argument 'config' shadows parent scope value`

### Excessive Arguments Not Passed
```xml
<include file="child.launch.xml">
  <arg name="mode" value="..."/>
  <!-- child.launch.xml also has 'debug' arg with default -->
</include>
```
**Warning:** `Include does not pass argument 'debug' - child uses default value`

### Complex Eval Expressions
```xml
<group if="$(eval &quot;'$(var a)' and '$(var b)' or not '$(var c)'&quot;)">
```
**Warning:** `Complex eval expression - consider simplifying or using multiple conditions`

### Deep Include Nesting
```
a.launch.xml → b.launch.xml → c.launch.xml → d.launch.xml → e.launch.xml → f.launch.xml
```
**Warning:** `Include depth exceeds 5 levels - consider flattening launch structure`

### Environment Variable Without Default
```xml
<arg name="path" default="$(env CUSTOM_PATH)"/>
```
**Error** at resolve time if the variable is not in `ctx.env` and no default is provided
(matching ROS 2 behavior). The parse-time lint (`collect_env_without_fallback`) separately
flags `$(env VAR)` expressions that lack a default — that's informational, not an error.

Safe pattern with fallback:
```xml
<arg name="path" default="$(env CUSTOM_PATH /default/path)"/>
```

### Warning Levels

| Level | Behavior |
|-------|----------|
| `--warn=all` | Show all warnings (default) |
| `--warn=error` | Treat warnings as errors |
| `--warn=none` | Suppress warnings |
| `--warn=anti-pattern` | Only anti-pattern warnings |

---

## 17. Preview Mode vs Full Resolution for `$(find-pkg-share ...)`

### Design Decision

`$(find-pkg-share <pkg>)` substitutions have two resolution modes:

| Mode | `ctx.preview_mode` | Behaviour |
|------|--------------------|-----------|
| **Full** | `false` (default) | Expands to a real filesystem path (workspace source path → AMENT_PREFIX_PATH → fallback). Used when a real path is needed to open a file. |
| **Preview** | `true` | Keeps the portable `$(find-pkg-share pkg)` form. Used for all output values so the resolved XML/YAML is machine-independent. |

**API contract (Rust `resolver.rs`):**

- `resolve_substitutions(input, ctx)` — mode-aware: calls `resolve_substitutions_inner` with `resolve_pkg_share = !ctx.preview_mode`.  Use for **every value except include file paths**.
- `resolve_substitutions_full(input, ctx)` — private, always full: calls `resolve_substitutions_inner` with `resolve_pkg_share = true`.  Use **only** for `<include file=...>` paths that must be real filesystem paths so the file can be opened.

`resolve_substitutions_full` is intentionally private and used in exactly one place (the `<include>` path where the file must be opened for recursive parsing).  All other callers use the mode-aware public API.

When `resolve_pkg_share = true` and the package resolver cannot find the package, the output is also `$(find-pkg-share pkg)` (lines 505–511) — the same portable form as preview mode.  The output is never a wrong hardcoded path.

### Multi-value Strings

A single substitution string can contain multiple `$(find-pkg-share ...)` tokens embedded in other text:

```
"[$(find-pkg-share pkg1)/path/to/resource1, $(find-pkg-share pkg2)/path/to/resource2]"
```

**Rust resolver:** `parse_substitutions` tokenises the string character-by-character into `Substitution` variants:
```
[Literal("["), FindPkgShare("pkg1"), Literal("/path/to/resource1, "), FindPkgShare("pkg2"), Literal("/path/to/resource2]")]
```
`resolve_substitutions_inner` processes each token independently and accumulates the output string.  The `resolve_pkg_share` flag is propagated through all recursive calls (including `$(arg ...)` and `$(var ...)` expansion), so every `FindPkgShare` token anywhere in the string respects the mode.

**Python resolver (`_resolve_ros_substitutions`):** Uses `re.sub` with a global match, finding every `$(find-pkg-share ...)` independently:
```python
re.sub(r'\$\(find-pkg-share ([^)]+)\)', lambda m: _resolve_pkg_share(m.group(1).strip()), value)
```
For each match: if the package is found, the token is replaced with the real source path; if not found, `_resolve_pkg_share` returns `$(find-pkg-share pkg)` (the fallback introduced after M4), so the replacement equals the original token and the portable form is preserved in-place.

**Result for partially-resolvable strings:** if `pkg1` is found and `pkg2` is not:
```
"[/workspace/src/pkg1/path/to/resource1, $(find-pkg-share pkg2)/path/to/resource2]"
```
This is the maximally-resolved form for that environment; the not-found token remains portable.

### Python Resolver Fallback

`_resolve_pkg_share(package)` in `py_resolver.py` follows this chain:
1. Workspace source packages (from orchestrator lockfile) → real path
2. Installed packages via `ament_index_python` (AMENT_PREFIX_PATH) → real path
3. **Not found anywhere: returns `$(find-pkg-share {package})`** — the portable form

The Python resolver has no explicit `preview_mode` flag: it always resolves found packages to real paths (needed for `open()` calls and include tracking) and falls back to the portable form only when the package is genuinely unknown.  Portability of the final output XML/YAML is the responsibility of the Rust output layer (`ctx.preview_mode`).

### Known Limitation: Nested Substitutions in Python Regex

`_resolve_ros_substitutions` uses `[^)]+` to match the package name, which stops at the first `)`.  Nested substitutions of the form `$(find-pkg-share $(var pkg_name))` would be mismatched.  This syntax appears only in XML/YAML launch files; arg values cascaded from the Rust orchestrator to the Python resolver have already had `$(var ...)` expanded by the Rust layer, so this pattern is not encountered in practice.

### Where the Rule Is Applied

| Location | Call | Mode |
|----------|------|------|
| `<include file=...>` open path | `resolve_substitutions_full` | Always full (needs real path) |
| `<arg default=...>` | `resolve_substitutions` | Mode-aware |
| `<let value=...>` | `resolve_substitutions` | Mode-aware |
| `<node pkg=...>`, `exec=...`, etc. | `resolve_substitutions` | Mode-aware |
| `<param value=...>`, `from=...` | `resolve_substitutions` | Mode-aware |
| `<remap from=...>`, `to=...` | `resolve_substitutions` | Mode-aware |
| `<push-ros-namespace namespace=...>` | `resolve_substitutions` | Mode-aware |
| `include_args` forwarded to child | `resolve_substitutions` | Mode-aware |
| Condition expressions | `resolve_substitutions` | Mode-aware |
| Python `_resolve_ros_substitutions` | regex replace | Always resolves if found; portable fallback if not found |

## 15. Unresolvable Constructs (static-analysis limitations)

Several launch-file constructs execute code at ROS 2 launch runtime and cannot be evaluated
during static analysis.  launch-plus now reports each as an error or warning rather than
silently discarding the value.

### `$(command 'shell cmd' ['on_error'])` — **error**

```xml
<param name="robot_description"
       value="$(command 'xacro $(var model_file) vehicle_model:=$(var vehicle_model)' 'warn')"/>
```

The shell command is executed by ROS 2 at launch time.  Static analysis cannot run it.

**Behaviour:**
- Reported as an **error**: `$(command '...') cannot be evaluated during static analysis; value
  will be empty (resolved at ROS 2 launch runtime)`.
- In `--preview` mode the original `$(command '...')` expression is preserved as the
  parameter value so the output is informative rather than blank.
- In full (non-preview) mode the value is an empty string, matching the ROS 2 fallback
  when the command fails with the `'warn'` on-error policy.

**Rust:** `Substitution::Command(String)` variant in the `Substitution` enum.
`parse_substitution_expr` populates it; `resolve_substitutions_inner` reports the error and
emits the placeholder.  The error is propagated via `SubstitutionResult::errors` to
`ResolvedLaunch::errors`.

### `$(eval 'python_expr')` failure — **warning**

```xml
<group if="$(eval '\'$(var simulator_type)\' == \'carla\'')">
```

`$(eval ...)` spawns a `python3` subprocess.  If the expression fails (undefined variable,
syntax error, etc.) the resolver falls back to `"false"`.

**Behaviour:**
- Reported as a **warning**: `$(eval <expr>) failed: <reason>; substituted "false"`.
- Previously only emitted as `tracing::warn!` (developer log); now also appears in the
  user-facing `[warning]` summary.

**Rust:** In the `Eval` arm of `resolve_substitutions_inner`, the `Err` branch pushes to
`SubstitutionResult::warnings` in addition to `tracing::warn!`.

### Unknown Python action type — **warning**

```python
# Custom action returned from OpaqueFunction
return [MyCustomLoadAction(package="foo", plugin="Foo::Bar")]
```

`_walk_action` in `py_resolver.py` recognises a fixed set of launch action classes.
Any class whose `__name__` is not in that set may represent a node or include that
will not appear in the resolved output.

**Behaviour:**
- Reported as a **warning**: `Unrecognised action type '<ClassName>' — any nodes or
  includes it declares may not appear in the resolved output`.
- Safe infrastructure classes (`LogInfo`, `RegisterEventHandler`, `Shutdown`, etc.) are in
  the known-safe set and do not trigger the warning.

**Python:** `_KNOWN_ACTION_CLASSES` set defined at module level; checked at the tail of
`_walk_action`.

---

## Build Edge Cases

### Metapackages using `exec_depend` instead of `<depend>`

Some metapackages (e.g. `autoware_internal_msgs`) declare their sub-packages only as
`<exec_depend>`, not `<depend>` or `<build_depend>`.  This means that colcon's
`--packages-select` and `--packages-up-to` do not include the sub-packages in the
build set, but colcon's `ament_cmake` install step validates that **all** declared
dependencies (including `exec_depend`) are findable — causing a build failure.

**Root cause:** The metapackage pattern implies "building me means building my contents."
The correct `package.xml` tag should be `<depend>` (which expands to `build_depend` +
`build_export_depend` + `exec_depend`), since the semantic intent is that the
sub-packages must be present in the install space when the metapackage is installed.

**launch-plus stance:** This is an upstream packaging issue.  launch-plus uses
`DependencyMode::Build` which walks `build_depend`, `build_export_depend`,
`buildtool_depend`, and `<depend>` — matching the standard ROS 2 build dependency
model.  Packages that use only `exec_depend` for sub-packages that must be built
need to be fixed upstream to use `<depend>`.

**Workaround:** Users can add `--packages-select <missing_pkg>` via the colcon flagfile
to manually include the missing sub-packages in the build set.

### `BUILD_TESTING` and unconditional `find_package` of test dependencies

Some packages (e.g. `autoware_trajectory`) place `autoware_ament_auto_package()` or
`find_package(<test_dep> REQUIRED)` inside `if(BUILD_TESTING)` or outside any guard,
respectively.  This causes two classes of failures:

1. **Install step inside `if(BUILD_TESTING)`:** With `-DBUILD_TESTING=OFF`, the package
   compiles but never installs headers or CMake config, breaking downstream packages
   that `#include` its headers.

2. **Unconditional `find_package` of a `test_depend`:** Example code or debug tools
   that `find_package` a test-only dependency outside `if(BUILD_TESTING)` cause CMake
   to fail when the test package is not in the build set.

Both are upstream CMakeLists.txt bugs.  launch-plus recommends `-DBUILD_TESTING=OFF`
in the colcon flagfile for production builds, which is standard Autoware CI practice.
