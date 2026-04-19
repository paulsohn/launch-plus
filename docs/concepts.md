# Core Concepts

## The launch language and resolution semantics

### Launch files as a Python-embedded DSL

The ROS 2 launch system is designed as an **extensible scripting framework**.
The `launch` and `launch_ros` packages provide a set of `Action` and
`Substitution` types, but the system is explicitly open: projects can define
their own by subclassing, and each ROS 2 distribution adds new types.  In that
design, the answer to "what language are launch files written in?" is simply
Python.

roscope takes a different view.  It treats the standard `launch` / `launch_ros`
API as a **domain-specific language (DSL) embedded in Python**.  The DSL has a
defined, closed vocabulary — `Node`, `GroupAction`, `IncludeLaunchDescription`,
`DeclareLaunchArgument`, `OpaqueFunction`, and so on — and roscope gives that
vocabulary a dedicated **operational semantics** for extracting the runtime
system topology without instantiating the system.

### Resolution as partial evaluation

Resolution is not purely static analysis in the traditional sense of inspecting
an AST without executing anything.  It is more precisely **partial evaluation**:
roscope maintains its own resolver context — current namespace stack, argument
bindings, resolved substitutions — and evolves that context as it walks the
launch description.  It follows `<include>` chains, binds arguments, evaluates
conditionals, and executes `OpaqueFunction` bodies by calling `fn(context)` with
the resolver's context and collecting the resulting actions.

The critical distinction is that roscope's context is **not a ROS 2 runtime**.
There is no DDS middleware, no running nodes, no service calls, no process
management.  Seen from the ROS 2 system, roscope is static: it produces a
description of what the system would look like without instantiating any of it.
Seen from a programming-languages perspective, it is dynamic: it executes Python
code and evolves state.  roscope is a partial evaluator whose "runtime" is
intentionally limited to the semantics needed for topology extraction —
substitution resolution, argument binding, include traversal, opaque callable
execution — while leaving everything that requires a live ROS 2 system to
`ros2 launch`.

### OpaqueFunction and IncludeLaunchDescription as Oracles

Launch constructs whose internal computation roscope does not model —
`OpaqueFunction` bodies, dynamically constructed includes — are treated as
**oracles**: roscope executes them and takes their outputs as given.

The oracle escape is bounded.  OpaqueFunction outputs must be `Action` objects
from the launch API: the oracle can produce any combination of `Node`,
`GroupAction`, `IncludeLaunchDescription`, etc., but it cannot produce terms
outside the launch vocabulary.  This is a contract that launch file authors are
expected to uphold.

When this contract holds, the resolved graph is expected to be *sound* — nothing
in it contradicts what `ros2 launch` would produce — even if it may be
*incomplete*: a branch the oracle did not take at evaluation time is absent, but
every entry present reflects a real runtime element.  Formal verification of this
soundness property, via the work-in-progress operational semantics of the launch
API, is ongoing work (see the note on formal foundations below).

### Language closure and static verification

The standard `launch` / `launch_ros` API forms a **closed** language in
roscope's model: its terms, rules, and evaluation order are defined.  This
closure is what makes static verification possible beyond topology extraction.
Argument completeness checking, namespace collision detection, and connectivity
analysis all require a language with fixed rules.  An open scripting system that
allows arbitrary subclassing cannot support these analyses statically.

Third-party extensions — custom `Action` subclasses, non-standard substitutions
defined outside `launch` / `launch_ros` — fall outside the closed language by
definition and are not resolved.  In XML launch files, unknown elements are
skipped with a warning.  In Python launch files, importing or instantiating
unrecognized types typically causes an `ImportError` or resolution failure for
that file; see [Supported Environments → Known limitations](supported-environments.md#known-limitations)
for the exact failure modes.

> **Note on formal foundations:** The semantics described above — partial evaluation, oracle treatment of OpaqueFunction, and the expected soundness property — are stated informally here.
> Precise formal definitions (operational semantics of the launch language) and machine-checked proofs of soundness are ongoing and planned work.
> In the meantime, empirical validation against large-scale systems like Autoware provides confidence in the implementation.
> The gap between roscope's behavior and the official `ros2 launch` semantics is tracked and continuously narrowed.

## Resolution modes

### Preview resolution

With `--preview`, the resolver works against the **source workspace** (`src/`)
without requiring packages to be built or installed.  The output carries a
`<!-- PREVIEW -->` header and paths resolve to source workspace locations.

This is the primary mode for topology inspection: you can see the full launch
graph without building anything.

### Post-build resolution

Without `--preview`, the resolver works against **installed packages** via
`AMENT_PREFIX_PATH`.  Source your workspace's `install/setup.bash` first.
The output uses absolute installed paths and represents what `ros2 launch`
would actually see.

You can verify consistency between the two by resolving in both modes and
comparing the output.

## OpaqueFunction handling

Python launch files can contain `OpaqueFunction` — arbitrary Python callables
that generate launch actions at runtime.

roscope handles them by **executing the Python callable** directly — calling
`fn(context)` with the resolver's launch context.  If the function reads files
or generates actions, those are captured through the normal resolution pipeline.
If a package hasn't been fetched yet, it is sparse-checked out on demand.

## Python launch shims

When resolving Python launch files, roscope does **not** import the real
`launch` or `launch_ros` packages.  Instead, it injects lightweight shim modules
that record constructor arguments (package, executable, parameters, remaps)
into a structured trace.

This means:
- No ROS 2 Python packages need to be installed on the resolver host
- The resolver captures the launch description structure without scheduling
  anything for execution
- The standard `launch` / `launch_ros` API patterns needed for Autoware are
  supported; see [Supported Environments → Known limitations](supported-environments.md#known-limitations)
  for types not yet covered

## Package path resolution

`FindPackageShare` (and `$(find-pkg-share ...)` in XML) resolves to real
filesystem paths.

- **Preview mode** (`--preview`) — For packages in the lockfile,
  `FindPackageShare` resolves to the package's **source directory** in the
  workspace (e.g. `src/my_pkg`), fetching on demand if needed.  Packages not
  in the lockfile fall back to `AMENT_PREFIX_PATH`.  No build step is required.
- **Post-build mode** (no `--preview`) — `FindPackageShare` resolves to the
  installed share path via `AMENT_PREFIX_PATH` (e.g.
  `install/my_pkg/share/my_pkg`).

### Source/install path equivalence assumption

Preview mode resolves `FindPackageShare("my_pkg")` to the source directory
(e.g. `src/my_pkg`); post-build mode resolves it to the installed share
directory (e.g. `install/my_pkg/share/my_pkg`).  For this to produce
equivalent results, all in-package resources — launch files, parameter files,
config files — must exist at the **same relative path** in both locations:

```
# Source
src/my_pkg/launch/bringup.launch.xml
src/my_pkg/config/params.yaml

# Install (after colcon build)
install/my_pkg/share/my_pkg/launch/bringup.launch.xml
install/my_pkg/share/my_pkg/config/params.yaml
```

Standard `ament_cmake` and `ament_python` packages satisfy this — they
install resource files to `share/<pkg>/` preserving the directory structure.
In Autoware, every launch file and parameter file follows this convention.

Packages that **generate or transform** files during the build (e.g. template
expansion, code generation) may not have the generated files in the source
tree.  These packages are not fully supported in preview mode — the resolver
reads files as they exist in the source directory.

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
  command line
- **Argument forwarding** — arguments used in an included file must be
  explicitly forwarded via `<arg name="..." value="..."/>` in the `<include>`
  tag
- **Conditional evaluation** — `if="..."` and `unless="..."` attributes are
  fully evaluated, so only the active branches appear in the output
- **Substitution resolution** — all `$(var)`, `$(arg)`, `$(env)`, `$(eval)`,
  `$(find-pkg-share)` expressions are resolved; unresolvable substitutions are
  errors
- **Parameter file inlining** — YAML parameter files referenced by
  `<param from="..."/>` are always read and inlined at resolve time; their
  structure is validated (must follow ROS 2 `ros__parameters` conventions)
- **Namespace composition** — the full namespace stack is computed for every
  node and flattened onto the `namespace=` attribute
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

`roscope check` is a thin alias over `resolve` that exits non-zero on any
**error**.  Warnings alone do not cause a non-zero exit unless `--strict` is
also passed, which promotes warnings to errors.  Use it in CI to catch:
- Missing packages or launch files
- Undefined or unforwarded arguments
- Broken `$(eval ...)` expressions
- OpaqueFunction failures
- Unresolvable `$(find-pkg-share ...)` references

```bash
# CI validation example
roscope check -c my_pkg my_launch.xml \
  arg1:=value1 \
  --preview --rosdep
```

## Workspace state: clean vs dirty

Every command that refers to source code supports **workspace state flags** that
control how roscope locates and validates source packages.

- **Default (no flag)** — Requires a lockfile.  Verifies that each fetched
  repository's HEAD matches the lockfile SHA and that the working tree is clean.
  Errors if either check fails.  This is the safest mode: it refuses to proceed
  on ambiguous state without forcing any changes.
- **`--clean` (`-c`)** — Requires a lockfile.  Resets every fetched repository
  to the exact SHA recorded in the lockfile.  Guarantees reproducible output.
  Use in CI.
- **`--dirty` (`-d`)** — **Does not require a lockfile.**  Instead, roscope
  walks the `src/` directory (or the path given by `--src`) looking for
  `package.xml` files.  Every directory containing a `package.xml` is treated
  as a package at exactly the version on disk — no git operations are performed.
  Packages not found in `src/` and not installable via rosdep are an error.
  Use this mode when you have already populated `src/` with `vcs import` and
  want to resolve without generating a lockfile first.

### Dirty mode and the `vcs import` workflow

Dirty mode is designed for teams that manage their source directory with
`vcs import` but do not (yet) use a roscope lockfile:

```bash
vcs import src < my_project.repos   # populate src/ from your .repos file
roscope resolve -d my_bringup robot.launch.xml
```

roscope scans `src/` exhaustively — recursing into subdirectories until it
finds a `package.xml`, then treating that directory as a package root (without
recursing further into it).  Only `.git/` directories are skipped.

If `--lockfile` is also passed with `--dirty`, roscope emits a warning and
ignores the lockfile entirely.

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

### The roscope lockfile

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
  repository and at what path, so roscope can sparse-checkout *only* the
  packages it needs without cloning entire repositories
- **On-demand dependency expansion** — the lockfile maps every package to its
  repository and path, enabling roscope to fetch only the `package.xml`
  files it needs and expand the dependency graph incrementally
- **`.repos` compatibility** — the lockfile is a valid `.repos` file.  You can
  pass it to `vcs import` as a drop-in replacement for your original manifest,
  getting the same repos at the exact pinned SHAs.  This means adopting
  roscope does not require abandoning your existing vcstool workflow

Generate a lockfile with `roscope index`.  Commit it alongside your
`.repos` manifest — it serves the same role as `Cargo.lock` or
`package-lock.json`.

## Sparse checkout

roscope uses [git sparse-checkout](https://git-scm.com/docs/git-sparse-checkout)
to fetch only the files it needs from each repository.  When the resolver first
encounters a package, it checks out just that package's directory — not the
entire repository.

This is additive: once a package is fetched, it stays on disk until you run
`roscope clean`.  Over multiple resolve/build cycles, only the packages
actually used accumulate on disk.

The `--src` flag controls where packages are fetched (default: `src/`).

## Dependency closure

When building (`roscope build`), the tool computes the **transitive
build-dependency closure** from the resolved launch graph:

1. **Direct packages** — every package referenced in the launch file
   (`<node pkg="...">`, `$(find-pkg-share ...)`, `<include>`)
2. **Transitive build deps** — `build_depend`, `buildtool_depend`,
   `build_export_depend`, `buildtool_export_depend`, and `<depend>` from each
   package's `package.xml`, expanded recursively
3. **System deps** — packages not in the lockfile are resolved via `rosdep`
   (with `--rosdep`)

Only the resulting minimal set is passed to `colcon build --packages-select`.

### The `exec_depend` problem

A key design goal is to exclude `exec_depend` from the build closure — a critical
difference from `colcon build --packages-up-to`.

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

roscope's goal is to avoid this entirely: because the resolver already knows
which packages are actually needed (it read the launch file), it *should* compute
the build closure using only `build_depend` and `buildtool_depend`.

**Current status:** today, roscope still includes `exec_depend` in the build
set because it delegates to `colcon build`, which validates that all
`package.xml` dependencies — including `exec_depend` — have install artifacts
before running cmake.  There is no colcon flag to disable this check.  Replacing
colcon with direct ament invocations (see [#18](https://github.com/paulsohn/roscope/issues/18))
will remove this constraint, allowing the build closure to use only true
build-time dependencies.

## rosdep integration

The `--rosdep` flag enables automatic system dependency resolution.  When a
package required by the build is not in the lockfile (e.g. a ROS buildfarm
package like `rosbridge_server`), roscope:

1. Checks `AMENT_PREFIX_PATH` — if the package is already installed, it's used
   directly
2. Runs `rosdep resolve` to map rosdep keys to system package names
3. Installs via `apt-get` (for `#apt`) or `pip` (for `#pip`)

This uses `rosdep resolve` + manual install rather than `rosdep install` directly,
because `rosdep install` cannot handle an explicit list of rosdep keys without
either scanning an entire source tree (`--from-paths`) or misinterpreting keys
as ROS package names.
