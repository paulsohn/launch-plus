# launch-plus

Static analyser and lazy resolver for ROS 2 launch files.

Instead of cloning and building an entire workspace upfront, launch-plus
reads a `.repos` manifest, generates a lockfile, and resolves a launch
file on demand — fetching only the packages actually needed, then
producing a single flattened XML that shows every node, parameter, and
remap that would be active at runtime.

> **Status:** early development — API and CLI flags may change.

## Prerequisites

- Rust toolchain (`cargo`) — https://rustup.rs
- `python3` in `PATH` (used to evaluate `$(eval ...)` substitutions and to resolve Python launch files)
- ROS 2 sourced — only required for `--rosdep` (system package resolution); the resolver itself does not depend on any ROS 2 Python packages

## Quickstart

### 1. Clone and build

```bash
git clone https://github.com/paulsohn/launch-plus.git
cd launch-plus
cargo build --bin launch-plus --release
# binary: target/release/launch-plus
# or use `cargo run --bin launch-plus --` in place of the binary below
```

### 2. Try the bundled Autoware example

A pre-generated lockfile for a full [Autoware workspace](https://github.com/autowarefoundation/autoware/tree/a06b188d3275da9de561ef4aa5ce0c4bfd87d716/repositories) is included in
[`example/autoware/`](example/autoware/).  You can run the resolver against it
immediately without writing a manifest or running `index` first.

```bash
# Source ROS 2 first so that rosdep and ament can locate system packages.
source /opt/ros/humble/setup.bash

cd example/autoware

cargo run --bin launch-plus -- resolve -c autoware_launch autoware.launch.xml \
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

On first run, the resolver will sparse-clone only the packages it needs into
`example/autoware/src/` (this may take a few minutes). Subsequent runs reuse
the already-fetched packages and are fast.

`--preview` keeps `$(find-pkg-share pkg)/...` placeholders in the output
instead of absolute filesystem paths, making the result portable across machines.

The pre-generated output files (paths anonymized, `[home]` replaces the home
directory) are included for reference:
- [`example/autoware/resolved.launch.xml`](example/autoware/resolved.launch.xml)
- [`example/autoware/resolver.log`](example/autoware/resolver.log)

To build the resolved packages with colcon (requires a sourced ROS 2 environment
and `colcon` installed):

```bash
cd example/autoware

cargo run --bin launch-plus -- build -c autoware_launch autoware.launch.xml \
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

`colcon-flags.txt` enables `--symlink-install` by default; edit it to add
`--cmake-args`, `--parallel-workers`, etc.

### 3. Use your own manifest

Write a `.repos` file listing the repositories you need — see
[`example/autoware/manifest.repos`](example/autoware/manifest.repos) for the
format — then generate a lockfile:

```bash
cd example/autoware

cargo run --bin launch-plus -- index
```

The lockfile pins every repository to a concrete commit SHA and records the ROS
packages it contains.  Commit it alongside your manifest, then run `resolve` as
above pointing `--lockfile` at it.

## Key commands

| Command | Description |
|---|---|
| `index <manifest.repos>` | Parse `.repos` file and generate a lockfile |
| `update [REPOS...]` | Re-resolve refs and update lockfile SHAs |
| `resolve <pkg> <launcher> [args...]` | Resolve launch file to a flat XML |
| `check <pkg> <launcher> [args...]` | Like `resolve` but exits non-zero on errors |
| `fetch <pkg>...` | Sparse-checkout specific packages from the lockfile |
| `build <pkg> <launcher> [args...]` | Resolve, plan dependencies, and run `colcon build` |
| `test <pkg> <launcher> [args...]` | Like `build` but includes `test_depend` packages |
| `clean` | Remove fetched packages |

## Notable `resolve` / `check` flags

### Workspace state (required — exactly one)

| Flag | Short | Description |
|---|---|---|
| `--clean` | `-c` | Reset every repository to the pinned lockfile SHA; discard local modifications |
| `--dirty` | `-d` | Use whatever is on disk; skip all git operations for existing repos |

Use `--dirty` during local iteration (edits survive).
Use `--clean` for reproducible CI runs.

### Resolution options

| Flag | Description |
|---|---|
| `--lockfile <path>` | Lockfile to use (default: `manifest.lock.repos`) |
| `--src <dir>` | Directory where packages are fetched (default: `src/`) |
| `--preview` | Use portable `$(find-pkg-share ...)` paths in output |
| `--inline-params` | Expand `<param from="file.yaml"/>` entries inline |
| `--flatten-namespaces` | Fold namespace into each node's name/topic |
| `--show-args` | Emit `<!-- arg name=... -->` comments at include boundaries |
| `--apply-opaque-file-access` | Let OpaqueFunction bodies read param files |
| `--apply-launch-arg-defaults` | Fill unset args from their declared defaults |
| `--allow-global-arg-cascade` | Propagate parent args into included files |
| `--rosdep` | Resolve system packages via rosdep (requires sourced ROS 2) |

## `build` / `test` flags

`build` resolves the launch file, computes the transitive build-dependency closure
(`build_depend`, `buildtool_depend`, `<depend>`, …), and calls `colcon build
--packages-select <exact list>`.  `test` does the same but also pulls in
`test_depend` packages.

### Workspace state (required — exactly one)

| Flag | Short | Description |
|---|---|---|
| `--clean` | `-c` | Reset every repository to the pinned lockfile SHA |
| `--dirty` | `-d` | Use whatever is on disk; skip git operations |

### Build / install directories

| Flag | Default | Description |
|---|---|---|
| `--build-base <dir>` | `build` | Colcon build output directory |
| `--install-base <dir>` | `install` | Colcon install prefix |

### Extra colcon flags (flagfile)

Pass arbitrary colcon flags via a flagfile — one shell token per line,
`#` comments allowed:

```bash
launch-plus build autoware_launch autoware.launch.xml \
  sensor_model:=sample_sensor_kit vehicle_model:=sample_vehicle map_path:=/ \
  --clean --colcon-flagfile example/colcon-flags.example.txt
```

Each line of the flagfile is inserted verbatim into `colcon build` before
`--packages-select`.  Flags that conflict with launch-plus-managed arguments
(`--packages-*`, `--base-paths`, `--build-base`, `--install-base`) are
rejected as errors.  See [`example/colcon-flags.example.txt`](example/colcon-flags.example.txt)
for an annotated template.

| Flag | Description |
|---|---|
| `--colcon-flagfile <file>` | Path to a flagfile with extra colcon arguments |
| `--dry-run` | Print the colcon command without running it |

## How the resolver works

**XML launch files** are parsed and resolved in Rust. Substitution expressions
(`$(find-pkg-share ...)`, `$(var ...)`, `$(eval ...)`) are evaluated, `<include>`
tags are followed recursively, and all `<node>`, `<param>`, and `<remap>` elements
are collected into the output.

**Python launch files** are executed with `importlib`, but before the file loads,
a `MetaPathFinder` intercepts all imports of `launch`, `launch_ros`, and
`ament_index_python` and replaces them with shim modules built entirely from the
standard library.  The shims record constructor arguments (package, executable,
parameters, remaps, included files) into a structured trace instead of scheduling
anything for execution.  No ROS 2 packages need to be installed.

**OpaqueFunction** bodies are arbitrary Python callables and cannot be statically
analysed — they are executed directly.  `open()`, `yaml.safe_load()`, and
`os.path.*` are patched so filesystem reads go through portable
`$(find-pkg-share pkg)/...` paths; the package is sparse-checked out on demand if
not yet present locally.

## Development

```bash
cargo test --workspace
cargo clippy --all-targets
cargo fmt --check
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
