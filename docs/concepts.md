# Core Concepts

## Lockfile: why `.repos` is not enough

The ROS 2 community uses `.repos` files (vcstool format) to describe workspaces.
A typical `.repos` entry looks like:

```yaml
repositories:
  autoware/universe:
    type: git
    url: https://github.com/autowarefoundation/autoware_universe.git
    version: v0.49.0
```

This has a fundamental problem: **`version: v0.49.0` is a moving target**.  Tags
can be force-pushed, branches advance with every commit, and there is no record
of *which packages* a repository contains or what their dependencies are.  Two
developers running `vcs import` a week apart may get different source code with
no way to detect or reproduce the difference.

Other ecosystems solved this long ago:
- **npm** has `package-lock.json`
- **Cargo** has `Cargo.lock`
- **pip** has `pip freeze` / `requirements.txt` with pinned hashes
- **Pixi** (conda) has `pixi.lock`

ROS 2 has had no equivalent — until now.

### The launch-plus lockfile

A **lockfile** (`manifest.lock.repos`) is a snapshot of your workspace that pins
every repository to a concrete commit SHA and records the ROS packages each
repository contains.

```yaml
repositories:
  core/autoware_msgs:
    type: git
    url: https://github.com/autowarefoundation/autoware_msgs.git
    version: 588f00df3acca009ea709a74acd6538897eef424  # pinned SHA
    ref: 1.11.0                                        # original tag/branch
    packages:
      - autoware_common_msgs
      - autoware_msgs
      - autoware_planning_msgs

packages:
  autoware_msgs:
    repo: core/autoware_msgs
    path: autoware_msgs
```

The lockfile is dual-indexed:
- **`repositories`** — grouped by repository, used during fetching
- **`packages`** — indexed by package name, used for O(1) dependency lookup

This gives you:
- **Reproducibility** — SHA pins mean identical source code every time
- **Sparse clone map** — the lockfile records which packages live in which
  repository and at what path, so launch-plus can sparse-checkout *only* the
  packages it needs without cloning entire repositories
- **Offline dependency graph** — transitive build closures can be computed from
  the lockfile alone, without cloning anything
- **`.repos` compatibility** — the lockfile is a valid `.repos` file.  You can
  pass it to `vcs import` as a drop-in replacement for your original manifest,
  getting the same repos at the exact pinned SHAs.  This means adopting
  launch-plus does not require abandoning your existing vcstool workflow

Generate a lockfile with `launch-plus index`.  Commit it alongside your
`.repos` manifest — it serves the same role as `Cargo.lock` or
`package-lock.json`.

## Sparse checkout

launch-plus uses [git sparse-checkout](https://git-scm.com/docs/git-sparse-checkout)
to fetch only the files it needs from each repository.  When the resolver first
encounters a package, it checks out just that package's directory — not the
entire repository.

This is additive: once a package is fetched, it stays on disk until you run
`launch-plus clean`.  Over multiple resolve/build cycles, only the packages
actually used accumulate on disk.

The `--src` flag controls where packages are fetched (default: `src/`).

## Portable paths

The resolver produces output using **portable paths** — substitution expressions
like `$(find-pkg-share pkg)/config/params.yaml` that are valid across machines
and environments.

In `--preview` mode, these paths remain as substitutions in the output XML.
Without `--preview` (post-build), paths are expanded using `AMENT_PREFIX_PATH` to
point at the installed package locations.

Portable paths are the canonical output format.  They ensure that:
- Resolved XML can be shared between developers
- CI artifacts are not tied to a specific filesystem layout
- `diff` between preview and post-build resolutions shows only semantic differences

### Source/install path equivalence convention

For preview mode to produce correct results, launch-plus assumes that **launch
files and parameter files have the same relative path within a package in both
the source directory and the install directory**.  Concretely:

```
# Source path
src/my_pkg/launch/bringup.launch.xml
src/my_pkg/config/params.yaml

# Install path (after colcon build --symlink-install)
install/my_pkg/share/my_pkg/launch/bringup.launch.xml
install/my_pkg/share/my_pkg/config/params.yaml
```

Both are reachable via `$(find-pkg-share my_pkg)/launch/bringup.launch.xml`.
In preview mode, `$(find-pkg-share my_pkg)` is interpreted as `src/my_pkg`
(the source directory); post-build, it resolves to the installed share path
via `AMENT_PREFIX_PATH`.

This means packages that **transform or generate** launch files or parameter
files during the build (e.g. template expansion, code generation) are not
supported in preview mode — the resolver needs to read the files as they exist
in the source tree.  Standard `ament_cmake` / `ament_python` packages that
simply install files to the share directory work correctly.

## Workspace state: clean vs dirty

Every command that refers to source code supports **workspace state flags** that
control how launch-plus treats the on-disk state of fetched repositories:

- **`--clean` (`-c`)** — Resets every fetched repository to the exact SHA
  recorded in the lockfile.  Guarantees reproducible output.  Use in CI.
- **`--dirty` (`-d`)** — Uses whatever is currently on disk.  Only fetches
  repositories that are completely missing.  Use during local development to
  preserve your edits.
- **Default (no flag)** — Verifies that each repository's HEAD matches the
  lockfile SHA and that the working tree is clean.  Errors if either check
  fails.  This is the safest mode: it refuses to proceed on ambiguous state
  without forcing any changes.

## Resolution modes

### Preview resolution

With `--preview`, the resolver works against the **source workspace** (`src/`)
without requiring packages to be built or installed.  The output carries a
`<!-- PREVIEW -->` header and uses portable `$(find-pkg-share ...)` paths.

This is the primary mode for static analysis: you can see the full launch graph
without building anything.

### Post-build resolution

Without `--preview`, the resolver works against **installed packages** via
`AMENT_PREFIX_PATH`.  Source your workspace's `install/setup.bash` first.
The output uses absolute installed paths and represents what `ros2 launch`
would actually see.

You can verify consistency between the two by resolving in both modes and
comparing the output.

## OpaqueFunction handling

Python launch files can contain `OpaqueFunction` — arbitrary Python callables
that generate launch actions at runtime.  These cannot be statically analyzed.

launch-plus handles them by **executing the Python callable** with patched
filesystem access:
- `open()`, `yaml.safe_load()`, `os.path.*`, and `pathlib.Path.open` are
  intercepted
- File reads go through portable `$(find-pkg-share pkg)/...` paths
- If a package hasn't been fetched yet, it is sparse-checked out on demand

The `--apply-opaque-file-access` flag enables this.  Without it, file access in
OpaqueFunction bodies is recorded as an error (strict mode).

## Python launch shims

When resolving Python launch files, launch-plus does **not** import the real
`launch` or `launch_ros` packages.  Instead, it injects lightweight shim modules
that record constructor arguments (package, executable, parameters, remaps)
into a structured trace.

This means:
- No ROS 2 Python packages need to be installed on the resolver host
- The resolver captures the launch description structure without scheduling
  anything for execution
- All standard `launch` / `launch_ros` API patterns are supported

## Dependency closure

When building (`launch-plus build`), the tool computes the **transitive
build-dependency closure** from the resolved launch graph:

1. **Direct packages** — every package referenced in the launch file
   (`<node pkg="...">`, `$(find-pkg-share ...)`, `<include>`)
2. **Transitive build deps** — `build_depend`, `buildtool_depend`, and `<depend>`
   from each package's `package.xml`, expanded recursively
3. **System deps** — packages not in the lockfile are resolved via `rosdep`
   (with `--rosdep`)

Only the resulting minimal set is passed to `colcon build --packages-select`.

### Why `exec_depend` is excluded

A critical difference from `colcon build --packages-up-to` is that launch-plus
**does not follow `exec_depend`** when computing the build closure.

`exec_depend` declares packages needed at *runtime* — not at build time.  When
you run `colcon build --packages-up-to my_node`, colcon treats `exec_depend` as
an implicit build dependency and pulls those packages (and their transitive
dependencies) into the build set.  This is conservative but wasteful: runtime
dependencies like message transport libraries or shared utility packages are
often already installed as system packages and don't need to be built from
source.

This is a well-known pain point.  The Autoware project, for instance, maintains
a dedicated CI action
([`remove-exec-depend`](https://github.com/autowarefoundation/autoware-github-actions/tree/main/remove-exec-depend))
that strips `<exec_depend>` entries from every `package.xml` before building —
a workaround for colcon's inability to distinguish build-time from runtime
dependencies.

launch-plus's goal is to avoid this entirely: because the resolver already knows
which packages are actually needed (it read the launch file), it *should* compute
the build closure using only `build_depend` and `buildtool_depend`.

**Current status:** today, launch-plus still includes `exec_depend` in the build
set because it delegates to `colcon build`, which validates that all
`package.xml` dependencies — including `exec_depend` — have install artifacts
before running cmake.  There is no colcon flag to disable this check.  Replacing
colcon with direct ament invocations (see [#18](https://github.com/paulsohn/launch-plus/issues/18))
will remove this constraint, allowing the build closure to use only true
build-time dependencies.

## Static analysis: what the resolver can verify

The `resolve` and `check` commands perform static analysis on the launch graph.
Here is what the resolver can and cannot verify.

### What it verifies

- **Package existence** — every `$(find-pkg-share pkg)`, `<node pkg="...">`,
  and `<include>` target must reference a package that exists in the lockfile
  or on `AMENT_PREFIX_PATH`
- **Launch file existence** — included launch files must exist on disk (or be
  fetchable from the lockfile)
- **Argument completeness** — every `$(var name)` / `$(arg name)` reference must
  have a corresponding `<arg name="...">` declaration or be supplied on the
  command line (in strict mode without `--apply-launch-arg-defaults`)
- **Argument forwarding** — in strict mode (without `--allow-global-arg-cascade`),
  arguments used in an included file must be explicitly forwarded via
  `<arg name="..." value="..."/>` in the `<include>` tag
- **Conditional evaluation** — `if="..."` and `unless="..."` attributes are
  fully evaluated, so only the active branches appear in the output
- **Substitution resolution** — all `$(var)`, `$(arg)`, `$(env)`, `$(eval)`,
  `$(find-pkg-share)` expressions are resolved; unresolvable substitutions are
  errors
- **Parameter file validity** — with `--inline-params`, YAML parameter files are
  parsed and their structure validated (must follow ROS 2 `ros__parameters`
  conventions)
- **Namespace composition** — with `--flatten-namespaces`, the full namespace
  stack is computed for every node
- **OpaqueFunction execution** — Python callables are executed (not just parsed),
  so runtime errors in OpaqueFunction bodies are caught during resolution

### What it does not verify

- **Runtime type correctness** — parameter types are not checked against what
  the node code expects
- **Topic/service connectivity** — the resolver does not verify that publishers
  match subscribers or that service servers exist for clients
- **Executable existence** — `<node pkg="..." exec="...">` does not verify that
  the executable exists in the installed package
- **Message type compatibility** — no check that connected topics use compatible
  message types
- **Resource availability** — hardware devices, network interfaces, GPU
  resources, etc. are not verified
- **Non-launch-graph side effects** — if an OpaqueFunction spawns a subprocess,
  modifies global state, or performs network I/O, those effects are not analyzed

### The `check` command

`launch-plus check` is a thin alias over `resolve` that exits non-zero on any
warning or error.  Use it in CI to catch:
- Missing packages or launch files
- Undefined or unforwarded arguments
- Broken `$(eval ...)` expressions
- OpaqueFunction failures
- Unresolvable `$(find-pkg-share ...)` references

```bash
# CI validation example
launch-plus check -c my_pkg my_launch.xml \
  arg1:=value1 \
  --preview --rosdep
```

## rosdep integration

The `--rosdep` flag enables automatic system dependency resolution.  When a
package required by the build is not in the lockfile (e.g. a ROS buildfarm
package like `rosbridge_server`), launch-plus:

1. Checks `AMENT_PREFIX_PATH` — if the package is already installed, it's used
   directly
2. Runs `rosdep resolve` to map rosdep keys to system package names
3. Installs via `apt-get` (for `#apt`) or `pip` (for `#pip`)

This uses `rosdep resolve` + manual install rather than `rosdep install` directly,
because `rosdep install` cannot handle an explicit list of rosdep keys without
either scanning an entire source tree (`--from-paths`) or misinterpreting keys
as ROS package names.
