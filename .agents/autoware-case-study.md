# Autoware Launch Architecture Case Study

Defects and refactoring suggestions discovered while running `roscope check` against
`autoware_launch`.  Each entry is self-contained: it describes the problem, shows the
exact lines that demonstrate it, and gives a concrete code sketch so any contributor
(human or AI agent) can implement a fix and open a PR without additional research.

This document is aimed at autoware upstream.  For roscope internals see
`edge-cases.md`.

---

## Defect 1 — `global_params.launch.py` is included 7 times; only 1 is needed

### Affected files

| File | Line |
|---|---|
| `autoware_launch/launch/autoware.launch.xml` | 54–59 |
| `autoware_launch/launch/components/tier4_sensing_component.launch.xml` | 16–20 |
| `autoware_launch/launch/components/tier4_localization_component.launch.xml` | 28–32 |
| `autoware_launch/launch/components/tier4_perception_component.launch.xml` | 23–27 |
| `autoware_launch/launch/components/tier4_planning_component.launch.xml` | 15–20 |
| `autoware_launch/launch/components/tier4_control_component.launch.xml` | 20–24 |
| `autoware_launch/launch/components/tier4_autoware_api_component.launch.xml` | 11–15 |

### Problem

`global_params.launch.py` spawns two actions every time it is included:
1. `SetParameter(name="use_sim_time", ...)` — sets a ROS parameter globally
2. An include of `vehicle_info.launch.py` which starts a loader node

`autoware.launch.xml` already includes it once at the top.  Every component file then
includes it again independently, so in a full-stack launch the loader node is started
7 times and `use_sim_time` is overwritten 7 times in sequence.

The design intent is correct (each component should be independently launchable without
the top-level context), but the execution creates redundant node instances in the normal
case.

### Fix

Add a guard arg to `global_params.launch.py` so each component can opt out when called
from a parent that already loaded global params:

**`autoware_global_parameter_loader/launch/global_params.launch.py`** — add a guard:
```python
DeclareLaunchArgument("load_global_params", default_value="true"),
# Wrap the SetParameter and IncludeLaunchDescription in an IfCondition on load_global_params
```

**Each component file** — pass `load_global_params:=false` when the top-level entry
point is handling it:

```xml
<!-- autoware.launch.xml: no change — it owns the one real include -->

<!-- tier4_planning_component.launch.xml and the other 5 components: -->
<include file="$(find-pkg-share autoware_global_parameter_loader)/launch/global_params.launch.py">
  <arg name="use_sim_time"       value="$(var use_sim_time)"/>
  <arg name="vehicle_model"      value="$(var vehicle_model)"/>
  <arg name="load_global_params" value="$(var load_global_params)"/>  <!-- new -->
</include>
```

**`autoware.launch.xml`** passes `load_global_params:=false` to all component includes:

```xml
<include file="...tier4_planning_component.launch.xml">
  <arg name="load_global_params" value="false"/>
  <!-- existing args unchanged -->
</include>
```

Each component's default keeps it independently launchable; when composed the top-level
entry point suppresses duplicates.

---

## Defect 2 — `vehicle_model` used in `global_params.launch.py` without a declaration

### Affected file

`autoware_global_parameter_loader/launch/global_params.launch.py`

### Problem

Every component passes `vehicle_model` explicitly to `global_params.launch.py`:

```xml
<include file="...global_params.launch.py">
  <arg name="use_sim_time" value="$(var use_sim_time)"/>
  <arg name="vehicle_model" value="$(var vehicle_model)"/>  <!-- forwarded by caller -->
</include>
```

Inside `global_params.launch.py`, `vehicle_model` is consumed in `launch_setup`:

```python
vehicle_description_pkg = FindPackageShare(
    [LaunchConfiguration("vehicle_model"), "_description"]
).perform(context)
```

But the `generate_launch_description` only declares `use_sim_time`:

```python
def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        # vehicle_model: NOT declared — consumed silently from global context
        OpaqueFunction(function=launch_setup),
    ])
```

Consequences:
- `ros2 launch global_params.launch.py --show-args` does not list `vehicle_model`
- Launched standalone without a parent that set `vehicle_model`, it silently uses `""`
  and produces a broken path to `_description` package
- Static analysis flags every caller with an "excessive include arg" warning for
  `vehicle_model`

### Fix

**`autoware_global_parameter_loader/launch/global_params.launch.py`** — add one line:

```python
def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        DeclareLaunchArgument("vehicle_model"),  # ← add this
        OpaqueFunction(function=launch_setup),
    ])
```

No other files need to change.

---

## Defect 3 — `<group scoped="false">` wrapper around `global_params` includes has no effect

### Affected files

Same 6 component files as Defect 1 (lines immediately around the `global_params` include
in each file).

### Problem

Every component wraps the `global_params` include in `<group scoped="false">`:

```xml
<group scoped="false">
  <include file="...global_params.launch.py">
    <arg name="use_sim_time" value="$(var use_sim_time)"/>
    <arg name="vehicle_model" value="$(var vehicle_model)"/>
  </include>
</group>
```

`scoped="false"` causes any `<let>` or `<arg>` assignments made inside the group to
"leak" into the parent scope.  Its legitimate use is conditional multi-variable
assignment (see `edge-cases.md §2`).

`global_params.launch.py` does not produce any launch-scope variables.  It produces:
- A `SetParameter` action → goes to the ROS parameter server (not launch scope)
- A node include → spawns a loader node (not launch scope)

`scoped="false"` therefore has **zero effect** here.  It misleads readers into thinking
global variables are being injected, and it triggers a roscope warning about
`<include>` inside `scoped="false">` (which is flagged as a general anti-pattern because
in other contexts it causes invisible variable leakage).

### Fix

**Each of the 6 component files** — remove the `<group scoped="false">` wrapper, keep
the `<include>` bare:

```xml
<!-- Before -->
<group scoped="false">
  <include file="...global_params.launch.py">
    <arg name="use_sim_time" value="$(var use_sim_time)"/>
    <arg name="vehicle_model" value="$(var vehicle_model)"/>
  </include>
</group>

<!-- After -->
<include file="...global_params.launch.py">
  <arg name="use_sim_time" value="$(var use_sim_time)"/>
  <arg name="vehicle_model" value="$(var vehicle_model)"/>
</include>
```

---

## Defect 4 — Component file owns the subsystem's internal path structure (57 forwarded args)

### Affected files

| File | Lines |
|---|---|
| `autoware_launch/launch/components/tier4_planning_component.launch.xml` | 24–124 |
| `tier4_planning_launch/launch/planning.launch.xml` | entire file (no `<arg>` declarations for the 57 args it consumes) |

The same structural problem exists in:
- `tier4_perception_component.launch.xml` → `perception.launch.xml`
- `tier4_localization_component.launch.xml` → `localization.launch.xml`

### Problem

`tier4_planning_component.launch.xml` computes every param file path for the entire
planning subsystem and passes them individually to `planning.launch.xml`:

```xml
<arg name="behavior_path_config_path"
     value="$(find-pkg-share autoware_launch)/config/planning/scenario_planning/..."/>
<arg name="behavior_path_planner_common_param_path"
     value="$(var behavior_path_config_path)/behavior_path_planner.param.yaml"/>
<arg name="behavior_velocity_planner_crosswalk_module_param_path"
     value="$(var behavior_velocity_config_path)/crosswalk.param.yaml"/>
<!-- 54 more args -->
<include file="...planning.launch.xml">
  <!-- all 57 args forwarded here -->
</include>
```

`planning.launch.xml` declares **none** of these 57 args.  It consumes them via global
`LaunchConfiguration` cascade.

This means the component file (`tier4_planning_component`) knows the full directory
layout of `tier4_planning_launch`, which is an internal concern of the planning package.
Any change to the planning config directory structure requires editing the component file
in a different package (`autoware_launch`).

### Fix

Move path computation into `planning.launch.xml`.  The component passes one config
root dir:

**`tier4_planning_component.launch.xml`** (100 lines of arg forwarding → ~8 lines):

```xml
<include file="$(find-pkg-share tier4_planning_launch)/launch/planning.launch.xml">
  <arg name="planning_config_dir"
       value="$(find-pkg-share autoware_launch)/config/planning"/>
  <arg name="vehicle_param_file"
       value="$(find-pkg-share $(var vehicle_model)_description)/config/vehicle_info.param.yaml"/>
  <arg name="pointcloud_container_name"       value="$(var pointcloud_container_name)"/>
  <arg name="enable_all_modules_auto_mode"    value="$(var enable_all_modules_auto_mode)"/>
  <arg name="is_simulation"                   value="$(var is_simulation)"/>
  <arg name="input_objects_topic_name"        value="$(var planning_input_objects_topic_name)"/>
  <arg name="input_pointcloud_topic_name"     value="$(var planning_input_pointcloud_topic_name)"/>
</include>
```

**`planning.launch.xml`** — add the declaration and compute paths internally:

```xml
<arg name="planning_config_dir"/>
<let name="common_config_path"
     value="$(var planning_config_dir)/scenario_planning/common"/>
<let name="behavior_path_config_path"
     value="$(var planning_config_dir)/scenario_planning/lane_driving/behavior_planning/behavior_path_planner"/>
<let name="behavior_velocity_config_path"
     value="$(var planning_config_dir)/scenario_planning/lane_driving/behavior_planning/behavior_velocity_planner"/>
<let name="motion_config_path"
     value="$(var planning_config_dir)/scenario_planning/lane_driving/motion_planning"/>
<!-- all 57 path args become internal <let>s; no longer in the public interface -->
```

The subsystem now owns its path conventions.  The component file becomes a thin adapter
that maps top-level config roots to subsystem entries.

Apply the same refactoring to `perception.launch.xml` and `localization.launch.xml`.

---

## Defect 5 — YAML preset injects variables that consuming files never declare

### Affected files

| File | Role |
|---|---|
| `autoware_launch/launch/components/tier4_planning_component.launch.xml` line 22 | Includes the preset YAML |
| `autoware_launch/config/planning/preset/default_preset.yaml` | Sets ~30 toggle args |
| `tier4_planning_launch/launch/scenario_planning/parking.launch.xml` | Uses `$(var launch_parking_module)` without declaring it |
| (and ~10 other files in `tier4_planning_launch/`) | Same pattern |

Same pattern in `tier4_control_component.launch.xml` → `control.launch.xml`.

### Problem

The preset include:

```xml
<include file="$(find-pkg-share autoware_launch)/config/planning/preset/$(var module_preset)_preset.yaml"/>
```

sets 30+ toggle args (e.g., `launch_parking_module`, `motion_path_smoother_type`) via the
YAML launch format.  These flow into scope via normal scope inheritance — the downstream
files access them without declaring them.

The dynamic filename `$(var module_preset)_preset.yaml` makes the dependency graph
invisible to static analysis: the set of variables injected is unknown until the arg is
resolved at runtime.

Files like `parking.launch.xml` have a hidden contract — they only work correctly when
called through the preset include chain.  Calling them standalone or through a different
parent produces undefined `$(var launch_parking_module)`.

### Fix

Keep the YAML preset mechanism (it is idiomatic for profile switching with a single CLI
arg).  Add no-default `<arg>` declarations in each consuming file to make the contract
explicit:

**`tier4_planning_launch/launch/scenario_planning/parking.launch.xml`**:

```xml
<!-- Declare args received from the preset; no default = required input -->
<arg name="launch_parking_module"/>
<arg name="freespace_planner_type"/>

<group if="$(var launch_parking_module)">
  ...
</group>
```

Benefits:
- `ros2 launch parking.launch.xml --show-args` now surfaces `launch_parking_module`
- Files are usable standalone: the caller must explicitly pass preset args
- `roscope check` stops flagging these as undeclared variable uses
- A contributor reading `parking.launch.xml` sees what inputs it requires

---

## Anti-Pattern Warnings from `roscope check`

Running `roscope check autoware_launch autoware.launch.xml` emits a set of static
anti-pattern warnings in addition to the structural defects above.  The table below shows
the four categories added in M8 Phase 3, which warnings fire against the current
`autoware_launch` sources, and where to look.

### W1 — `<include>` inside `<group scoped="false">`

**Warning text:** `<include> inside <group scoped="false"> — 'X.launch.py' leaks its
internal variables into the parent scope; move the <include> out of the group`

**Fires on:** All 6 component files (same files as Defect 3 above).

```
autoware://components/tier4_sensing_component.launch.xml
autoware://components/tier4_localization_component.launch.xml
autoware://components/tier4_perception_component.launch.xml
autoware://components/tier4_planning_component.launch.xml
autoware://components/tier4_control_component.launch.xml
autoware://components/tier4_autoware_api_component.launch.xml
```

**Root cause:** Each component wraps its `global_params.launch.py` include in
`<group scoped="false">` (see Defect 3).  The warning fires because `<include>` inside
an unscoped group is a general anti-pattern — when the included file *does* emit
`<let>`/`<arg>` values, those values leak invisibly into the parent scope.

**Fix:** Remove the `<group scoped="false">` wrapper as described in Defect 3.  The
warning will disappear because `global_params.launch.py` produces no launch-scope
variables; the wrapper was dead code all along.

---

### W2 — Deep include-chain nesting (depth > 5)

**Warning text:** `include chain depth N — consider flattening the launch hierarchy`

**Fires on:** Any include resolved at depth 6 or deeper when starting from
`autoware.launch.xml`.

**Typical chain in autoware:**

```
autoware.launch.xml                         [depth 1]
  └─ tier4_planning_component.launch.xml    [depth 2]
       └─ planning.launch.xml               [depth 3]
            └─ scenario_planning.launch.xml [depth 4]
                 └─ lane_driving.launch.xml [depth 5]
                      └─ behavior_planning.launch.xml  ← WARNING fires [depth 6]
                           └─ behavior_path_planner.launch.xml [depth 7]
```

**Fix:** Apply the path-encapsulation refactoring from Defect 4.  Moving config-path
computation into `planning.launch.xml` collapses several intermediate files that exist
purely to thread path args down the chain, reducing typical nesting by 1–2 levels.
Alternatively, flatten the most-nested chains by inlining shallow sub-launchers directly
into their parent.

---

### W3 — `$(env X)` without a fallback default

**Warning text:** `$(env VAR_NAME) has no fallback — will fail if the environment
variable is unset; use $(env VAR_NAME <default>)`

**Fires on:** Any launch file that references `$(env FOO)` without a word-form default
(`$(env FOO bar)` or `$(env FOO "")`).

**Common pattern in autoware:**

```xml
<!-- Risky: launch aborts at startup if ROS_HOME is unset -->
<arg name="log_dir" value="$(env ROS_HOME)/log"/>

<!-- Safe: falls back to empty string if unset -->
<arg name="log_dir" value="$(env ROS_HOME )"/>
```

**Fix:** Add an explicit fallback in every bare `$(env ...)` substitution.  Choose a
sensible default (empty string, a path relative to `$(find-pkg-share ...)`, or a
hard-coded fallback) rather than letting the runtime abort with an opaque error.

---

### W4 — Complex `$(eval ...)` expression

**Warning text:** `complex eval expression '...' — difficult to analyse statically;
consider simplifying or splitting into multiple conditions`

**Fires when** any `$(eval ...)` expression is longer than 60 characters, contains a
literal double-quote character (`"`), or uses escaped single-quotes (`\'`).

**Example in autoware:**

```xml
<!-- Flagged: expression uses double-quoted string literal -->
<group if="$(eval '&quot;' + arg('vehicle_model') + '&quot;' == '&quot;sample_vehicle&quot;')">
```

After XML entity decoding the eval expression contains `"`, which triggers the warning.

**Fix:** Decompose complex conditionals into named `<let>` variables:

```xml
<let name="is_sample_vehicle"
     value="$(eval arg('vehicle_model') == 'sample_vehicle')"/>
<group if="$(var is_sample_vehicle)">
  ...
</group>
```

This makes each step testable and readable, and eliminates the quoting gymnastics that
trigger the warning.

---

## Defects at a glance

### Structural defects

| # | Defect | Severity | Fix scope |
|---|--------|----------|-----------|
| 1 | `global_params.launch.py` included 7× — spawns loader node 7× | High | `global_params.launch.py` + 6 component files |
| 2 | `vehicle_model` consumed without `DeclareLaunchArgument` | Medium | 1 line in `global_params.launch.py` |
| 3 | `<group scoped="false">` around `global_params` has no effect | Low | Remove 6 wrappers |
| 4 | Component file owns subsystem's internal path structure (57 forwarded args) | High | `tier4_planning_component.launch.xml` + `planning.launch.xml` (and perception, localization) |
| 5 | YAML preset variables undeclared in consuming files | Medium | ~10 files in `tier4_planning_launch/` |

### Anti-pattern warnings (`roscope check`)

| Warning | Description | Typical fix |
|---------|-------------|-------------|
| W1 | `<include>` inside `<group scoped="false">` — 6 component files | Remove the `<group scoped="false">` wrapper (Defect 3) |
| W2 | Include-chain depth > 5 — deepest chains in planning subsystem | Encapsulate paths in the subsystem (Defect 4) |
| W3 | `$(env X)` without fallback — bare env substitutions | Add explicit fallback word to each `$(env ...)` |
| W4 | Complex `$(eval ...)` expression — quoted string comparisons | Decompose into named `<let>` variables |
