# roscope

**ROS 2 launch system inspector and targeted builder** — evaluate your full
system topology and build only what you need, without a ROS 2 runtime.

![Visualizer screenshot](docs/visualizer-screenshot.png)

roscope evaluates launch descriptions following ROS 2 semantics — resolving
substitutions, evaluating conditionals, and executing Python
`generate_launch_description()` callables — without a running ROS environment
or a built workspace. From a single launch file it derives every node,
parameter, remap, topic, package dependency, and include boundary that would be
active at runtime, and can then fetch and build exactly those packages — nothing
more.

## The problem

A typical ROS 2 workspace like [Autoware](https://github.com/autowarefoundation/autoware)
contains **200+ packages** across dozens of repositories. Before you can answer
"what nodes does this launch file actually start?", the standard workflow demands:

```
# Traditional ROS 2 workflow
vcs import src < autoware.repos      # clone ~50 repos
rosdep install --from-paths src      # install ALL system deps
colcon build                         # build ALL ~235 packages (30+ min)
ros2 launch autoware_launch ...      # finally launch
```

This is slow, wasteful, and makes it hard to audit or iterate on a subset of
the system. The topology — which nodes run, which topics they use, which
parameters are set — is locked away behind a full build.

## The solution

roscope makes the **launch file the source of truth**. It evaluates the launch
description following the same semantics as `ros2 launch`, but without a
running ROS environment, fetching only the packages it needs on demand:

```
# roscope workflow
roscope index autoware.repos     # generate lockfile (one-time)
roscope resolve autoware_launch autoware.launch.xml \
  sensor_model:=sample_sensor_kit \
  vehicle_model:=sample_vehicle \
  map_path:=/path/to/map \
  --visualize                        # interactive graph in your browser
```

The resolved output is a **flattened, fully-resolved XML** with all includes
inlined, conditionals evaluated, and variables substituted. The visualizer turns
that into an interactive compound graph — nodes, containers, topics, remaps, and
include boundaries — all explorable before a single package is built.

Targeted builds are also supported: from the same launch evaluation, roscope
knows exactly which packages are needed and can fetch and build only those.

## Key features

- **No-runtime topology** — evaluate the full launch graph and inspect nodes,
  parameters, remaps, and topics without a build or running ROS environment
- **Interactive visualizer** — compound graph view of the full launch structure,
  with selection, detail panel, and topic tracking
- **On-demand sparse checkout** — packages are sparse-cloned only when
  referenced by the launch file, via
  [git sparse-checkout](https://git-scm.com/docs/git-sparse-checkout)
- **Targeted builds** — the packages required by your launch target are derived
  from the evaluation; only those are fetched and built
- **Python launch support** — executes `generate_launch_description()` with
  shimmed `launch`/`launch_ros` imports; no installed ROS 2 Python packages needed
- **OpaqueFunction handling** — executes arbitrary Python callables,
  transparently fetching packages as they are accessed
- **Lockfile pinning** — reproducible analysis and builds via commit-SHA-pinned lockfiles
- **rosdep integration** — automatically installs system dependencies for the
  packages being built

> **Current scope:** roscope covers launch evaluation without a ROS runtime and
> targeted builds today. Execution support — a built-in executor and integration
> with `ros2 launch` — is on the roadmap.

## Quick start

### Prerequisites

- **Python 3.10+** — used to evaluate Python launch files and `$(eval ...)` substitutions in XML
- **Git** — for sparse-checkout operations
- **ROS 2** — source your ROS 2 environment (`source /opt/ros/<distro>/setup.bash`)
- **pnpm** — required to build the web visualizer ([install](https://pnpm.io/installation))
- **rosdep** — required for `--rosdep` (part of `ros-dev-tools`, not the base ROS 2 runtime)
- **colcon** — required for `build` / `test` commands (part of `ros-dev-tools`)

### Install

```bash
pip install git+https://github.com/paulsohn/roscope.git
```

Or for development:

```bash
git clone https://github.com/paulsohn/roscope.git
cd roscope
pip install -e .
```

### Try the bundled Autoware example

A pre-generated lockfile for a full [Autoware workspace](https://github.com/autowarefoundation/autoware)
is included in [`example/autoware/`](example/autoware/).  You can run the
resolver immediately without writing a manifest or running `index` first.

```bash
# Source ROS 2 first (only needed for --rosdep)
source /opt/ros/humble/setup.bash

cd example/autoware

# Preview-resolve: produces flattened XML without building
roscope resolve -d autoware_launch autoware.launch.xml \
  sensor_model:=sample_sensor_kit \
  vehicle_model:=sample_vehicle \
  map_path:=/path/to/map \
  --show-args \
  --rosdep \
  --preview \
  > resolved.launch.xml
```

On first run, the resolver sparse-clones only the packages it needs into
`example/autoware/src/` (this may take a few minutes).  Subsequent runs reuse
the already-fetched packages and are fast.

The pre-generated output files are included for reference:
- [`example/autoware/resolved.launch.xml`](example/autoware/resolved.launch.xml)
- [`example/autoware/resolver.log`](example/autoware/resolver.log)

To build the resolved packages (requires a sourced ROS 2 environment and `colcon`):

```bash
roscope build -d autoware_launch autoware.launch.xml \
  sensor_model:=sample_sensor_kit \
  vehicle_model:=sample_vehicle \
  map_path:=/path/to/map \
  --rosdep \
  --colcon-flagfile colcon-flags.txt
```

### Use your own project

1. Write a `.repos` file listing your repositories (standard
   [vcstool](https://github.com/dirk-thomas/vcstool) format)
2. Generate a lockfile: `roscope index`
3. Resolve: `roscope resolve <pkg> <launcher> [args...]`
4. Build: `roscope build <pkg> <launcher> [args...] --clean --rosdep`

See the [Getting Started guide](docs/getting-started.md) for a full walkthrough.

## Commands

| Command | Description |
|---|---|
| `index` | Parse `.repos` files and generate a lockfile |
| `update` | Re-resolve refs and update lockfile SHAs |
| `resolve` | Resolve and flatten a launch file to XML (no build) |
| `check` | Like `resolve` but exits non-zero on warnings/errors |
| `build` | Resolve, fetch dependencies, and run `colcon build` |
| `build-pkg` | Build package(s) by name with transitive dependency fetching |
| `test` | Like `build` but includes `test_depend` packages |
| `fetch` | Sparse-checkout specific packages from the lockfile |
| `clean` | Remove fetched packages |

Run `roscope <command> --help` for detailed usage of each command.

## Visualizer

`resolve` accepts a `--visualize` flag that opens an interactive
graph view of the resolved launch structure in your browser:

```bash
roscope resolve -d autoware_launch autoware.launch.xml \
  sensor_model:=sample_sensor_kit \
  vehicle_model:=sample_vehicle \
  map_path:=/path/to/map \
  --visualize
```

The visualizer shows the full node graph — groups (include boundaries),
containers, composable nodes, topics, and remaps — as a compound graph with
interactive selection and detail panel.

Use `--viz-id <name>` to label the snapshot. Snapshots are cached in
`~/.cache/roscope-viz/`.

## Workspace state flags

Commands that refer to source code support workspace state flags:

| Flag | Short | When to use |
|---|---|---|
| *(default)* | | Verifies HEAD matches lockfile SHA; errors on mismatch |
| `--clean` | `-c` | CI / reproducible runs — resets repos to lockfile SHAs |
| `--dirty` | `-d` | No lockfile required — scans `src/` for packages as-is |

In dirty mode roscope does not read a lockfile.  Instead it walks the source
directory (default: `src/`) looking for `package.xml` files.  This supports
workflows where `src/` is populated by `vcs import` without generating a
lockfile first.

## Documentation

| Document | Description |
|---|---|
| [Motivation](docs/motivation.md) | Why roscope exists and what problems it solves |
| [Core Concepts](docs/concepts.md) | Lockfiles, sparse checkout, package resolution, and more |
| [Getting Started](docs/getting-started.md) | Step-by-step tutorial for your own project |
| [Architecture](docs/architecture.md) | How the resolver, fetcher, and builder work internally |
| [Supported Environments](docs/supported-environments.md) | Platforms, ROS distros, and known limitations |
| [FAQ](docs/faq.md) | Common questions and answers |
| [Contributing](CONTRIBUTING.md) | Development setup and contribution guidelines |

## Who is this for?

- **ROS 2 developers** working with large multi-repository workspaces who want
  faster iteration cycles
- **CI/CD pipelines** that need to build and test only the packages affected by
  a launch configuration change
- **System integrators** who want a clear, auditable view of what a launch file
  actually does — every node, parameter, and remap in one flat XML
- **Anyone** tired of waiting 30+ minutes for a full workspace build when they
  only need a handful of packages

## License

Apache-2.0 — see [LICENSE](LICENSE).
