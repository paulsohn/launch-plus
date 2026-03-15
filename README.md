# launch-plus

**Bazel-like build system for ROS 2** — resolve, fetch, and build only what your
launch file actually needs.

Instead of cloning and building an entire workspace upfront, launch-plus reads a
`.repos` manifest, generates a lockfile, and resolves a launch file on demand —
fetching only the packages actually referenced, then producing a single flattened
XML that shows every node, parameter, and remap that would be active at runtime.

## The problem

A typical ROS 2 workspace like [Autoware](https://github.com/autowarefoundation/autoware)
contains **200+ packages** across dozens of repositories.  The standard workflow
requires cloning everything, installing all system dependencies, and building the
full workspace before you can launch a single node — even if your launch file
only touches 30 of those packages.

```
# Traditional ROS 2 workflow
vcs import src < autoware.repos      # clone ~50 repos
rosdep install --from-paths src      # install ALL system deps
colcon build                         # build ALL ~235 packages (30+ min)
ros2 launch autoware_launch ...      # finally launch
```

This is slow, wasteful, and makes it hard to iterate on a subset of the system.

## The solution

launch-plus treats **launch files as build targets**.  It parses the launch
graph statically, determines exactly which packages are needed, fetches only
those via [git sparse-checkout](https://git-scm.com/docs/git-sparse-checkout),
and builds the minimal set:

```
# launch-plus workflow
launch-plus index                    # generate lockfile (one-time)
launch-plus build autoware_launch autoware.launch.xml \
  sensor_model:=sample_sensor_kit \
  vehicle_model:=sample_vehicle \
  map_path:=/path/to/map \
  --clean --rosdep                   # fetch + build only what's needed
```

The resolver also produces a **flattened, fully-resolved XML** that shows the
complete launch graph with all includes inlined, conditionals evaluated, and
variables substituted — invaluable for debugging and CI validation.

## Key features

- **Lazy fetching** — packages are sparse-checked out on demand; no full clone required
- **Minimal builds** — only the transitive dependencies of your launch target are built
- **Static analysis** — resolve launch files without building or executing anything
- **Python launch support** — executes `generate_launch_description()` with shimmed
  `launch`/`launch_ros` imports; no ROS 2 Python packages needed on the resolver host
- **OpaqueFunction handling** — executes arbitrary Python callables with patched
  filesystem access, transparently fetching packages as they are accessed
- **Portable output** — resolved XML uses `$(find-pkg-share pkg)/...` paths that
  work across machines and environments
- **Lockfile pinning** — reproducible builds via commit-SHA-pinned lockfiles
- **rosdep integration** — automatically installs system dependencies for the
  packages being built

> **Current scope:** launch-plus is a build-time tool today (resolve + build).
> Execution support — both a built-in executor and integration with `ros2 launch`
> and third-party launchers — is on the roadmap.

## Quick start

### Prerequisites

- **Rust toolchain** — [rustup.rs](https://rustup.rs) (edition 2024, MSRV 1.85)
- **Python 3** in `PATH` — used to evaluate Python launch files
- **Git** — for sparse-checkout operations
- **ROS 2** — source your ROS 2 environment (`source /opt/ros/<distro>/setup.bash`)

### Install

```bash
git clone https://github.com/paulsohn/launch-plus.git
cd launch-plus
cargo build --bin launch-plus --release
# binary: target/release/launch-plus
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
cargo run --bin launch-plus -- resolve -d autoware_launch autoware.launch.xml \
  sensor_model:=sample_sensor_kit \
  vehicle_model:=sample_vehicle \
  map_path:="[map_path]" \
  --allow-global-arg-cascade \
  --apply-launch-arg-defaults \
  --apply-opaque-file-access \
  --allow-including-unportable-path \
  --show-args \
  --inline-params \
  --flatten-namespaces \
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
cargo run --bin launch-plus -- build -d autoware_launch autoware.launch.xml \
  sensor_model:=sample_sensor_kit \
  vehicle_model:=sample_vehicle \
  map_path:="[map_path]" \
  --allow-global-arg-cascade \
  --apply-launch-arg-defaults \
  --apply-opaque-file-access \
  --allow-including-unportable-path \
  --rosdep \
  --colcon-flagfile colcon-flags.txt
```

### Use your own project

1. Write a `.repos` file listing your repositories (standard
   [vcstool](https://github.com/dirk-thomas/vcstool) format)
2. Generate a lockfile: `launch-plus index`
3. Resolve: `launch-plus resolve <pkg> <launcher> [args...]`
4. Build: `launch-plus build <pkg> <launcher> [args...] --clean --rosdep`

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

Run `launch-plus <command> --help` for detailed usage of each command.

## Workspace state flags

Commands that refer to source code support workspace state flags:

| Flag | Short | When to use |
|---|---|---|
| `--clean` | `-c` | CI / reproducible runs — resets repos to lockfile SHAs |
| `--dirty` | `-d` | Local iteration — uses whatever is on disk |
| *(default)* | | Verifies HEAD matches lockfile SHA; errors on mismatch |

## Documentation

| Document | Description |
|---|---|
| [Motivation](docs/motivation.md) | Why launch-plus exists and what problems it solves |
| [Core Concepts](docs/concepts.md) | Lockfiles, sparse checkout, portable paths, and more |
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
