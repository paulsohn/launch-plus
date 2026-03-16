# Architecture

## Overview

launch-plus is a Rust application that shells out to an external `python3`
process for resolving Python launch files.  The Python resolver script
(`py_resolver.py`) is embedded in the Rust binary via `include_str!` at compile
time, written to a temporary file, and invoked as a subprocess.  The core
library (`launch-plus-core`) contains all logic; the CLI (`launch-plus-cli`) is
a thin `clap`-based wrapper.

```
┌───────────────────────────────────────────────────────┐
│  CLI (launch-plus-cli)                                │
│  clap argument parsing → delegates to core            │
├───────────────────────────────────────────────────────┤
│  Core Library (launch-plus-core)                      │
│  ├── indexer    — .repos → lockfile                   │
│  ├── fetcher    — git sparse-checkout on demand       │
│  ├── resolver   — launch file → resolved XML          │
│  │   ├── XML parser (quick-xml)                       │
│  │   ├── substitution engine ($(var), $(eval), etc.)  │
│  │   └── Python resolver (embedded py_resolver.py)    │
│  ├── orchestrator — coordinates resolve + fetch loop  │
│  ├── builder    — colcon build orchestration          │
│  └── rosdep     — system dependency resolution        │
└───────────────────────────────────────────────────────┘
```

## Data flow

### 1. Index phase

```
.repos file(s)
    │
    ▼
[indexer]
    ├── git ls-remote → resolve versions to SHAs
    ├── git archive → fetch package.xml files (no full clone)
    └── parse package.xml → extract dependencies
    │
    ▼
manifest.lock.repos (lockfile)
```

The indexer reads one or more `.repos` files, resolves each version reference
(tag, branch, or SHA) to a concrete commit SHA via `git ls-remote`, fetches
`package.xml` files via `git archive` (without cloning), and writes the
dual-indexed lockfile.

### 2. Resolve phase

```
launch-plus resolve <pkg> <launcher>
    │
    ▼
[orchestrator]
    ├── lockfile lookup: pkg → repo + path
    ├── fetch launch file via sparse-checkout
    │
    ▼
[resolver] — recursive launch file processing
    │
    ├─── XML launch file?
    │    ├── parse with quick-xml
    │    ├── evaluate substitutions: $(var), $(arg), $(find-pkg-share), $(eval)
    │    ├── evaluate conditionals: if="...", unless="..."
    │    ├── follow <include> tags → recurse
    │    └── collect <node>, <param>, <remap>, <composable_node>
    │
    └─── Python launch file?
         ├── invoke py_resolver.py via subprocess
         ├── py_resolver imports shimmed launch/launch_ros modules
         ├── calls generate_launch_description()
         ├── walks the LaunchDescription tree
         ├── executes OpaqueFunction bodies with patched filesystem
         └── returns structured JSON to Rust
    │
    ├── fetch additional packages on demand (sparse-checkout)
    ├── retry if _PackageNotFetchedError signals missing package
    │
    ▼
resolved launch XML (stdout)
```

### 3. Build phase

```
launch-plus build <pkg> <launcher>
    │
    ▼
[orchestrator]
    ├── resolve launch file (preview mode)
    ├── collect direct packages from launch graph
    │
    ▼
[dependency planner]
    ├── expand transitive build deps from package.xml
    ├── separate lockfile packages from system packages
    │
    ▼
[fetcher] — fetch any missing build dependencies
    │
    ▼
[rosdep] — install system packages (with --rosdep)
    ├── rosdep resolve → apt/pip package names
    ├── apt-get install
    └── pip install
    │
    ▼
[builder]
    └── colcon build --packages-select <minimal set>
```

## Key components

### Indexer (`indexer.rs`)

Generates lockfiles from `.repos` manifests.  Uses `git ls-remote` for version
resolution and `git archive` to fetch `package.xml` files without full clones.
Parses `package.xml` to extract dependency information including REP-149
condition attributes for conditional dependencies.

### Fetcher (`fetcher.rs`)

Manages git sparse-checkout.  The primary API is:
- **`fetch_packages()`** — full sparse-checkout of one or more package subtrees,
  including `package.xml` and all files
- **`is_package_fetched()`** — checks whether a package has been fetched
  (sentinel: `package.xml` exists on disk)

Sparse-checkout is additive: new paths are added without removing previously
checked-out files.

### Resolver (`resolver.rs`, `orchestrator.rs`)

The XML resolver is implemented in Rust using `quick-xml`.  It handles:
- Substitution expressions: `$(var ...)`, `$(arg ...)`, `$(find-pkg-share ...)`,
  `$(find-pkg-prefix ...)`, `$(env ...)`, `$(eval ...)`
- Conditional attributes: `if="..."`, `unless="..."`
- `<include>` expansion with argument forwarding
- `<group>` scoping
- `<push-ros-namespace>` namespace composition

### Python resolver (`py_resolver.py`)

An embedded Python script (`include_str!` at compile time) that resolves Python
launch files.  It:

1. Installs a `MetaPathFinder` that intercepts imports of `launch`, `launch_ros`,
   and `ament_index_python`
2. Replaces them with shim modules that record constructor arguments
3. Loads the target launch file via `importlib`
4. Calls `generate_launch_description()`
5. Walks the resulting `LaunchDescription` tree
6. Returns a JSON structure describing all actions (nodes, includes, params, etc.)

The shims require no ROS 2 Python packages to be installed.  `OpaqueFunction`
bodies are executed directly with patched `open()`, `yaml.safe_load()`,
`os.path.*`, and `pathlib.Path.open`.

### Orchestrator (`orchestrator.rs`)

Coordinates the resolve-fetch loop.  When the Python resolver encounters a
package that hasn't been fetched yet, it signals via `_PackageNotFetchedError`.
The orchestrator catches this, fetches the missing package, and retries (up to
3 times).

### Builder (`builder.rs`)

Computes the transitive build-dependency closure and invokes `colcon build`.
Reads extra arguments from a flagfile.  Validates that flagfile tokens don't
conflict with launch-plus-managed arguments.

### Rosdep (`rosdep.rs`)

Resolves rosdep keys to system package names via `rosdep resolve`, then installs
via `apt-get` or `pip` directly.  Caches the set of already-installed ROS
packages per-process to skip redundant resolution.

## Design decisions

### Why `rosdep resolve` instead of `rosdep install`?

`rosdep install` without `--from-paths` treats its arguments as ROS package
names (via `rospkg.expand_to_packages()`) and fails on pure system keys like
`asio`.  With `--from-paths`, it scans every `package.xml` and pulls in
unresolvable test-only dependencies.  `rosdep resolve` maps keys to system
packages cleanly without either problem.

### Why shim modules instead of real launch/launch_ros?

Installing `launch` and `launch_ros` would require a full ROS 2 Python
environment on the resolver host.  The shims let launch-plus work with just
Python 3 and no ROS 2 installation — the resolver is a standalone tool.

### Why Rust?

- Performance: parsing and resolving large launch graphs (200+ packages) needs
  to be fast
- Single binary: no Python environment setup required on the target machine
  (Python is only invoked as a subprocess for `.launch.py` files)
- Strong typing: the dependency graph and lockfile schema benefit from
  compile-time checks

### Why embed py_resolver.py via `include_str!`?

The Python resolver is embedded at compile time so the Rust binary is fully
self-contained.  No external Python file needs to be distributed alongside the
binary.  The trade-off is that changes to `py_resolver.py` require a Rust
rebuild.
