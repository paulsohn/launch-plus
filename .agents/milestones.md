# Project Milestones

## Vision

launch-plus is a Bazel-like build and run system for ROS 2 that enables:
- **Static analysis** of launch file dependency graphs without executing them
- **Selective building** — only compile the packages referenced by a launch target
- **On-demand fetching** — sparse-checkout packages from git as they are needed

## Implemented

### Indexer
Parse `.repos` manifests and generate lockfiles that map every package to its
repository, path, and pinned SHA.  Supports blobless clone for fast initial
indexing and `update` for re-resolving refs.

### Fetcher
Sparse-checkout individual packages from lockfile repositories on demand.
Supports shallow clone, submodule recursion, and incremental sparse-checkout
path addition.

### Parser
XML and YAML launch file parser producing a common AST (`LaunchElement` tree).
Handles all standard ROS 2 launch XML elements: `<node>`, `<include>`,
`<group>`, `<arg>`, `<let>`, `<param>`, `<set_env>`, `<unset_env>`,
`<push_ros_namespace>`, `<node_container>`, `<load_composable_node>`.

### Resolver
Substitution engine supporting `$(arg ...)`, `$(var ...)`, `$(env ...)`,
`$(find-pkg-share ...)`, `$(find-pkg-prefix ...)`, `$(dirname)`, and
`$(eval ...)`.  Evaluates conditions (`if=`, `unless=`), resolves includes
recursively, tracks file and package dependencies, computes effective
namespaces, and renders resolved XML with source annotations.

Environment variable override tracking: `<set_env>` (XML) and
`SetEnvironmentVariable` (Python) add entries to an override-only map
(`ctx.env` / `_env`) that starts empty.  `$(env X)` checks overrides
first, then falls back to the real process environment.  Scoped groups
(`scoped="true"`) save/restore overrides; unscoped groups let mutations
propagate.  Per-node `<env>` children are exactly the override map —
process env vars are never copied or exposed.  `<unset_env>` is only
accepted for override-only vars not in process env; otherwise it errors.
Net-zero check reports an error for overrides leaked from file scope.

Preview mode emits portable `$(find-pkg-share ...)` tokens; non-preview mode
resolves to actual AMENT install paths.

### Locator
Package location from three sources: lockfile (source workspace), AMENT prefix
path (installed packages), and workspace overlay.  Provides `find-pkg-share`
and `find-pkg-prefix` resolution for the substitution engine.

### Python Resolver
Embedded Python script (`py_resolver.py`) that evaluates `.launch.py` files
using shim modules for `launch`, `launch_ros`, and `ament_index_python`.
Extracts nodes, includes, parameters, and remappings from Python launch
descriptions — including `OpaqueFunction` bodies — without requiring ROS 2
packages to be installed.

### Orchestrator
Unified resolution pipeline (`resolve_launch_recursive`) that handles both
XML and Python launch files.  Manages the fetch-on-demand retry loop for
`_PackageNotFetchedError`, global parameter propagation, and recursive include
expansion into a flat `ParsedLaunchFile` intermediate representation.

### Builder
Selective colcon build driven by resolved launch dependencies.
`plan_build_from_packages` computes the transitive dependency closure from
on-disk `package.xml` files, fetches any missing packages, and
`execute_build` drives `colcon build --packages-select <exact list>`.

### CLI
Full command set: `index`, `update`, `fetch`, `clean`, `resolve`, `check`,
`build`, `build-pkg`, `test`.

## Future

### Executor
Process spawning, lifecycle management, and signal handling for running
resolved launch graphs directly.

### Direct CMake Builds
Replace colcon with direct CMake/ament_cmake invocations for finer control
over the build process.

### CI Integration
GitHub Actions workflow for automated testing and release.
