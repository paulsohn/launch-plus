# Project Context: launch-plus

## Overview

launch-plus is a **Bazel-like build/run system for ROS 2** that enables lazy, on-demand package fetching and building based on actual launch-time dependencies.

**Problem**: Traditional ROS 2 workflow (vcs → rosdep → colcon → ros2 launch) requires cloning and building everything upfront.

**Solution**: Treat launch files as build targets. Parse them, resolve dependencies, fetch/build only what's needed.

## Design Philosophy: Bazel for ROS 2

```
# Bazel                          # launch-plus
bazel build //pkg:target    →    launch-plus build <pkg> <launcher>
bazel test //pkg:target     →    launch-plus test <pkg> <launcher>
bazel run //pkg:target      →    launch-plus run <pkg> <launcher>
bazel query //pkg:target    →    launch-plus dry-run <pkg> <launcher>
```

Key insight: **A launch file IS a build target** that declares its dependencies through:
- `<include file="$(find-pkg-share ...)"/>` - package dependencies
- `<node pkg="..."/>` - executable dependencies
- Embedded `launch-plus:` comment blocks - explicit dependency override (inline)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         launch-plus                                  │
├─────────────────────────────────────────────────────────────────────┤
│  CLI Layer (Python - thin, replaceable)                             │
│  ├── ros2 launch-plus ... (ros2 verb integration)                   │
│  └── launch-plus ...      (standalone)                              │
├─────────────────────────────────────────────────────────────────────┤
│  Launch API Packages (Python - API-compatible with ros2/launch)     │
│  ├── launch_plus      - mirrors launch API, reports dependencies    │
│  └── launch_plus_ros  - mirrors launch_ros API                      │
│  (Exported as aliases: launch, launch_ros when in launch-plus env)  │
├─────────────────────────────────────────────────────────────────────┤
│  Core Library (Rust)                                                │
│  ├── indexer      - .repos → lockfile (pkg→repo+path+version map)   │
│  ├── resolver     - launch file → resolved structure + dep graph    │
│  │   ├── xml_parser   - parse launch.xml                            │
│  │   ├── substitution - $(arg), $(var), $(find-pkg-share), etc.     │
│  │   ├── conditional  - if/unless evaluation                        │
│  │   └── flattener    - inline all includes                         │
│  ├── fetcher      - partial git clone (sparse-checkout)             │
│  ├── builder      - native cmake/setuptools build backend          │
│  └── executor     - process spawning & lifecycle (OUR implementation)│
├─────────────────────────────────────────────────────────────────────┤
│  Python Bindings (PyO3)                                             │
│  ├── launch_plus_core - Rust core exposed to Python                 │
│  └── launch_py_runner - execute launch.py, introspect result        │
└─────────────────────────────────────────────────────────────────────┘

Note: We implement our own executor for full control over process lifecycle
and direct consumption of resolved launch structures.
```

## Data Flow

```
1. INDEX PHASE

   launch-plus index [file.repos...]           # Generate from scratch
   launch-plus index --append [file.repos...]  # Append to existing
        ↓
   [indexer] for each repo in .repos:
     - git ls-remote → resolve version to SHA
     - git archive → fetch package.xml files (no full clone)
     - parse package.xml → extract dependencies
        ↓
   manifest.lock.yaml
     - repos: {repo_key → url, sha, packages[]}
     - packages: {pkg_name → repo, path, deps}  ← O(1) lookup

2. UPDATE PHASE (optional, on-demand)

   launch-plus update [--branch]
        ↓
   [updater] for each repo in lockfile:
     - compare current SHA vs remote
     - if changed: re-fetch package.xml, update lockfile
        ↓
   manifest.lock.yaml (updated)

3. EXECUTION PHASE (resolve/build/test/run)

   launch-plus <mode> <package> <launch_file>
        ↓
   [resolver] lockfile.packages[package] → quick lookup
        ↓
   [resolver] parse launch file → dependency graph
        ↓
   [fetcher] for each needed package:
     - lockfile.packages[pkg].repo → lockfile.repos[repo]
     - sparse-checkout only needed directories
        ↓
   [builder] native cmake/setuptools build (skip if mode=resolve)
        ↓
   [launcher] ros2 launch <package> <launch_file>     (only if mode=run)
```

## Key Files

### Input: .repos files (vcs format)

**Universal tool** - works with any .repos files, not Autoware-specific.

**Default**: `manifest.repos` → `manifest.lock.yaml`

```bash
launch-plus index                        # Uses manifest.repos
launch-plus index my.repos               # Appends my.repos → manifest.lock.yaml
launch-plus index a.repos b.repos        # Appends multiple → manifest.lock.yaml
```

**Example: manifest.repos** (union of autoware + simulator + tools, with branch fields from nightly):
```yaml
repositories:
  core/autoware_msgs:
    type: git
    url: https://github.com/autowarefoundation/autoware_msgs.git
    version: 1.11.0           # Can be tag, branch, or SHA
    branch: main              # For update tracking (from *-nightly.repos)

  universe/autoware_universe:
    type: git
    url: https://github.com/autowarefoundation/autoware_universe.git
    version: 0.49.0
    branch: main

  simulator/awsim:
    type: git
    url: https://github.com/autowarefoundation/awsim.git
    version: v1.2.0
    branch: main
```

**Version resolution** (indexer MUST resolve all to SHA):
- Tag `1.11.0` → SHA `abc123...`
- Branch `main` → HEAD SHA `def456...`
- SHA `abc123...` → unchanged

### Output: manifest.lock.yaml

Dual-indexed structure for efficient operations:
- `repos`: grouped by repository (for fetching)
- `packages`: indexed by package name (for O(1) lookup)

```yaml
version: 1

# Repository-centric view (for fetching)
repos:
  autowarefoundation/autoware_msgs:
    url: https://github.com/autowarefoundation/autoware_msgs.git
    version: 1.11.0               # Original version from .repos
    sha: abc123def456...          # Resolved SHA
    branch: main                  # For update tracking
    workspace_path: core/autoware_msgs  # Path in workspace
    packages:                     # List of packages in this repo
      - autoware_msgs
      - autoware_planning_msgs

# Package-centric view (for quick lookup by package name)
packages:
  autoware_msgs:
    repo: autowarefoundation/autoware_msgs   # Reference to repos entry
    path: autoware_msgs                       # Path within repo
    dependencies:
      build: [rosidl_default_generators]
      exec: [rosidl_default_runtime]
      test: [ament_lint_auto]
  autoware_planning_msgs:
    repo: autowarefoundation/autoware_msgs
    path: autoware_planning_msgs
    dependencies:
      build: [rosidl_default_generators]
      exec: [rosidl_default_runtime]
      test: []
```

**Two indexing modes:**
```bash
# Generate from scratch (overwrites)
launch-plus index autoware.repos simulator.repos tools.repos

# Append to existing lockfile
launch-plus index --append extra.repos
```

**Lookup flow:**
```
launch-plus run autoware_msgs node.launch.xml
  → packages["autoware_msgs"] → repo: "autowarefoundation/autoware_msgs"
  → repos["autowarefoundation/autoware_msgs"] → url, sha for fetching
```

### Per-launch dependency override (Embedded Comments)

Dependencies can be embedded as special comment blocks within launch files.

launch_plus preprocessor:
1. Scans for special comment blocks (prefix: `launch-plus:`)
2. Uncomments and parses them before standard parsing
3. Standard ros2 launch ignores these comments (backward compatible)

**XML example (launch.xml):**
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!-- launch-plus:
<deps>
  <build_depend>autoware_vehicle_msgs</build_depend>
  <build_depend>tier4_vehicle_msgs</build_depend>
  <exec_depend>vehicle_cmd_gate</exec_depend>
</deps>
-->
<launch>
  <node pkg="vehicle_cmd_gate" exec="vehicle_cmd_gate_node" name="gate"/>
</launch>
```

**YAML example (launch.yaml):**
```yaml
# launch-plus:
#   deps:
#     build_depend: [autoware_vehicle_msgs, tier4_vehicle_msgs]
#     exec_depend: [vehicle_cmd_gate]

launch:
  - node:
      pkg: vehicle_cmd_gate
      exec: vehicle_cmd_gate_node
```

**Compatibility modes:**
- **Commented (default)**: Works with both ros2 launch and launch_plus
- **Uncommented**: Only launch_plus can parse (for launch_plus-only projects)

**Fallback chain for launch.xml/launch.yaml:**
1. Embedded `launch-plus:` comment blocks (most specific)
2. Auto-detected from launch file parsing
3. `package.xml` (least specific)

### Automatic Dependency Tracking (launch.xml/launch.yaml)

The XML/YAML resolver automatically tracks dependencies during parsing - no manual declaration needed for most cases.

**Tracked patterns:**
| Pattern | Tracked Package |
|---------|-----------------|
| `$(find-pkg-share pkg)` | `pkg` |
| `$(find-pkg-prefix pkg)` | `pkg` |
| `<node pkg="pkg" .../>` | `pkg` |
| `<include file="$(find-pkg-share pkg)/..."/>` | `pkg` |
| `<composable_node pkg="pkg" .../>` | `pkg` |

**Example - all dependencies auto-detected:**
```xml
<launch>
  <!-- my_msgs auto-tracked from find-pkg-share -->
  <let name="config" value="$(find-pkg-share my_msgs)/config/params.yaml"/>

  <!-- my_driver auto-tracked from node pkg -->
  <node pkg="my_driver" exec="driver_node" name="driver">
    <param from="$(var config)"/>
  </node>

  <!-- other_pkg auto-tracked from include -->
  <include file="$(find-pkg-share other_pkg)/launch/sub.launch.xml"/>
</launch>
```

Embedded `launch-plus:` comments are only needed for dependencies that can't be auto-detected (e.g., message types used at runtime but not referenced in launch file).

### launch.py Support (Dynamic Dependency Generation)

Dependencies are generated dynamically at runtime.

**How it works:**

1. **launch_plus / launch_plus_ros packages**: API-compatible with `launch` / `launch_ros`
   - When launch.py uses these packages, dependencies are tracked automatically
   - Each `IncludeLaunchDescription`, `Node`, etc. registers its package dependency

2. **Detection flow:**
   ```python
   # In user's launch.py
   from launch_plus import LaunchDescription  # or: from launch import ...
   from launch_plus_ros.actions import Node   # or: from launch_ros.actions import ...

   def generate_launch_description():
       return LaunchDescription([
           Node(package='my_pkg', executable='my_node', ...)
       ])
   ```

3. **Resolution:**
   - launch-plus loads the launch.py and calls `generate_launch_description()`
   - If using `launch_plus`/`launch_plus_ros`: dependencies collected via API hooks
   - If using original `launch`/`launch_ros`: dependencies extracted via introspection of returned LaunchDescription

### Dependency Tracking Hooks

`launch_plus` and `launch_plus_ros` wrap the original APIs to automatically track package dependencies.

**Substitutions (package references):**
| Class | Tracks |
|-------|--------|
| `FindPackageShare(pkg)` | Records `pkg` when evaluating share path |
| `FindPackagePrefix(pkg)` | Records `pkg` when evaluating prefix path |
| `PathJoinSubstitution([FindPackageShare(...), ...])` | Inherits from nested substitutions |

**Actions (executables/nodes):**
| Class | Tracks |
|-------|--------|
| `Node(package=pkg, ...)` | Records `pkg` for the node |
| `LifecycleNode(package=pkg, ...)` | Records `pkg` for lifecycle node |
| `ComposableNodeContainer(package=pkg, ...)` | Records container `pkg` |
| `LoadComposableNodes(...)` | Records each component's package |
| `IncludeLaunchDescription(source)` | Records package from source path |

**Implementation pattern:**
```python
# launch_plus/_tracker.py
class DependencyTracker:
    _current: ClassVar[Optional['DependencyTracker']] = None

    def __init__(self):
        self.packages: set[str] = set()

    @classmethod
    def record(cls, package: str) -> None:
        if cls._current:
            cls._current.packages.add(package)

# launch_plus/substitutions/find_package_share.py
class FindPackageShare(launch.substitutions.FindPackageShare):
    def __init__(self, package: SomeSubstitutionType) -> None:
        super().__init__(package)
        if isinstance(package, str):
            DependencyTracker.record(package)

    def perform(self, context: LaunchContext) -> str:
        result = super().perform(context)
        # Also record at evaluation time for dynamic package names
        DependencyTracker.record(self._package_name)
        return result
```

**Future tracking opportunities:**
- Message type inference from typed parameters (e.g., `geometry_msgs/Pose` → `geometry_msgs`)
- Plugin package references (`pluginlib` plugins)
- URDF/Xacro model packages

**Fallback chain for launch.py:**
1. Dynamic tracking via API hooks (preferred)
2. Introspection of LaunchDescription (for original launch/launch_ros)
3. `package.xml` (fallback)

### Seamless Switching Between launch_plus and ros2 launch

**Goal:** Users can resolve with launch_plus and execute with either launch_plus or ros2 launch.

**Strategy:**
- `launch_plus` and `launch_plus_ros` provide the same API as `launch` and `launch_ros`
- Launch files written for one work with the other
- No special environment sourcing required - launch-plus handles paths internally
- `launch-plus resolve` outputs a format consumable by both executors

**Import compatibility:**
```python
# These should work interchangeably:
from launch import LaunchDescription           # Original ros2
from launch_plus import LaunchDescription      # launch-plus

from launch_ros.actions import Node            # Original ros2
from launch_plus_ros.actions import Node       # launch-plus
```

**Execution modes:**
- `launch-plus run`: Uses launch_plus executor (full control)
- `launch-plus run --use-ros2-launch`: Delegates to ros2 launch (compatibility mode)

## Directory Structure

```
launch-plus/
├── Cargo.toml                    # Rust workspace
├── crates/
│   ├── launch-plus-core/         # Core Rust library
│   │   ├── src/
│   │   │   ├── lib.rs
│   │   │   ├── indexer.rs
│   │   │   ├── resolver.rs
│   │   │   ├── fetcher.rs
│   │   │   ├── builder/          # native build backend
│   │   │   └── launcher.rs
│   │   └── Cargo.toml
│   └── launch-plus-cli/          # Rust CLI (optional standalone)
│       ├── src/main.rs
│       └── Cargo.toml
├── python/
│   ├── launch_plus/              # Python bindings + ros2 verb + CLI
│   │   ├── __init__.py
│   │   ├── _core.pyi             # Type stubs for Rust bindings
│   │   ├── cli.py                # Standalone CLI entry
│   │   └── verb/                 # ros2 CLI verb
│   │       └── launch_plus.py
│   ├── launch_plus_launch/       # API-compatible with ros2/launch
│   │   ├── __init__.py           # Re-exports launch API
│   │   ├── actions/              # LaunchDescription, etc.
│   │   ├── substitutions/        # LaunchConfiguration, etc.
│   │   └── _tracker.py           # Dependency tracking hooks
│   └── launch_plus_ros/          # API-compatible with ros2/launch_ros
│       ├── __init__.py
│       ├── actions/              # Node, ComposableNodeContainer, etc.
│       └── descriptions/         # Parameter, etc.
├── pyproject.toml                # Python package (maturin)
└── .agents/                      # AI agent instructions
```

## Technology Stack

- **Rust**: Core logic (git operations, YAML parsing, dependency resolution)
- **PyO3/Maturin**: Rust → Python bindings
- **Python**: Thin CLI layer, ros2 verb integration
- **Git sparse-checkout**: Partial cloning
- **cmake/make**: Direct build invocation for ament_cmake packages

## ROS 2 Integration

- **Target: ROS 2 Jazzy** (Ubuntu 24.04)
- Integrates as `ros2 launch-plus` verb
- Respects `AMENT_PREFIX_PATH`, `COLCON_PREFIX_PATH`
- Uses standard `package.xml` for dependency info

## Workspace Layout

**Fetch location** (configurable, default: `src/`):
```
workspace/
├── manifest.lock.yaml
├── src/                          # Default fetch location
│   ├── core/autoware_msgs/       # workspace_path from lockfile
│   │   ├── autoware_msgs/
│   │   └── autoware_planning_msgs/
│   └── universe/autoware_universe/
├── build/                        # colcon build output
└── install/                      # colcon install output
```

Configuration via CLI:
```bash
launch-plus --fetch-path=src/ run my_pkg my_launch.xml
```

## Conflict Resolution

**Package name conflicts** (same package in multiple repos): **Error**
- Indexer fails if duplicate package names detected
- User must resolve manually (remove from one .repos file)
- Clear error message with both sources listed

## Reference Repositories

Clone reference repos into `reference/<repo-name>/` for code study:
- `reference/launch/` - https://github.com/ros2/launch
- `reference/launch_ros/` - https://github.com/ros2/launch_ros
- `reference/autoware/` - https://github.com/autowarefoundation/autoware

These are read-only reference repositories for study.

## Execution Modes

| Mode | Fetch | Build | Test | Launch | Output |
|------|-------|-------|------|--------|--------|
| `resolve` | Yes* | No | No | No | Resolved XML/YAML |
| `build` | Yes | Yes | No | No | Build artifacts |
| `test` | Yes | Yes | Yes | No | Test results |
| `run` | Yes | Yes | No | Yes | Running system |

*Fetch required if lockfile not available or launch file not yet fetched.

### Mode: resolve

Resolves and flattens the launch file without building or execution:
- All `<include>` inlined
- All conditionals evaluated
- All variables substituted
- Output as XML or YAML

### Mode: build

Fetches and builds only the packages required to run the launch target:

1. **Resolve** the launch file (always in preview / source mode) → `direct_packages`
2. **Expand** transitively using build deps only (`build_depend`, `build_export_depend`,
   `buildtool_depend`, `buildtool_export_depend`); `exec_depend` is excluded because
   launch-plus tracks runtime requirements through the launch graph itself
3. **Fetch** any plan packages not yet on disk (e.g. pure build deps not reached by the
   launch graph traversal)
4. **Verify** on-disk `package.xml` dependencies match the lockfile for every build package
5. **Build**: native cmake/setuptools backend builds each package in topological order
   using a greedy parallel scheduler

Build options are passed directly as CLI flags:

```bash
launch-plus build autoware_launch autoware.launch.xml \
  sensor_model:=sample_sensor_kit vehicle_model:=sample_vehicle map_path:=/ \
  --allow-global-arg-cascade --apply-launch-arg-defaults \
  --lockfile manifest.lock.repos --src src \
  --dirty \
  --symlink-install \
  --cmake-args -DCMAKE_BUILD_TYPE=Release
```

`BUILD_TESTING` is managed automatically (`OFF` for build, `ON` for test);
manually passing `-DBUILD_TESTING=...` via `--cmake-args` is rejected with an error.

### Mode: test

Same pipeline as `build`, but `DependencyMode::BuildAndTest` is used so `test_depend` packages are
included in the build set.  These packages are never visited during launch-graph traversal
and are fetched on demand by `fetch_plan_packages`.

The `test` command currently builds the full test dependency set with `BUILD_TESTING=ON`;
running `ctest`/`pytest` is a planned future step.

```bash
launch-plus test autoware_launch autoware.launch.xml \
  sensor_model:=sample_sensor_kit vehicle_model:=sample_vehicle map_path:=/ \
  --allow-global-arg-cascade --apply-launch-arg-defaults \
  --lockfile manifest.lock.repos --src src \
  --dirty \
  --symlink-install
```

### Mode: run (default)

Full execution: fetch → build → launch:
- Uses `exec_depend` for runtime dependencies
- Equivalent to traditional `ros2 launch`

## Terminology

### Portable Path

A path string in `$(find-pkg-share <pkg>)/...` format — an *unresolved substitution* that
references a package resource without binding to any specific filesystem layout.  Portable
paths are valid inputs to the ROS 2 launch substitution engine and are the **canonical output
format of launch-plus in all modes**.

`FindPackageShare("pkg").perform(context)` is intended to always return
`$(find-pkg-share pkg)` — never a machine-specific path such as
`src/autoware/.../pkg` or `/opt/ros/humble/share/pkg`.  Actual filesystem resolution
happens only at the two sites that require it:

1. **Include expansion** — the tool resolves the portable path internally to read and recurse
   into an included launch file.  The resolved path is not written to output.
2. **Execution** — at launch time, the ROS 2 runtime or launch-plus executor resolves
   `$(find-pkg-share)` to the installed or source path as appropriate.

A consequence of this invariant is that the `--preview` flag no longer distinguishes an
output format — it becomes implicit in whether `--src`/`--lockfile` are supplied (source
checkout) or not (AMENT_PREFIX_PATH).  The flag may be deprecated once the invariant is
fully implemented.

### Verbose Flag

Flags that enable non-default resolver behaviour that would otherwise discourage users from
leaving upstream launch files as-is.  Examples: `--allow-global-arg-cascade`,
`--apply-launch-arg-defaults`.  A verbose flag is opt-in — the tool functions correctly
without it, but users who need to handle launch files that rely on the behaviour must pass it
explicitly.  The naming convention (long flag, no short alias) signals that the behaviour is
intentional but not recommended for clean launch-file design.

## Key Features

### Resolve Mode

The `resolve` command flattens a launch file into a fully-resolved form:
- All `<include>` files inlined
- All conditionals (`if`, `unless`) evaluated
- All variables (`$(arg ...)`, `$(var ...)`) substituted
- Output as clean XML or YAML

```bash
launch-plus resolve pkg file.launch.xml --arg1=value1 --output-format=yaml
```

Use cases:
- Visualization of actual launch graph
- Testing without execution
- Debugging argument propagation
- CI validation

### Launch Validation

The `check` command validates launch files without running:
- Missing required arguments (no default)
- Unused arguments (passed but not consumed)
- Undefined variable references
- Argument propagation through includes

```bash
launch-plus check pkg file.launch.xml --strict
```
