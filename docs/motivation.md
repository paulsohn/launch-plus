# Motivation

## The scaling problem in ROS 2 workspaces

ROS 2 projects tend to grow into large multi-repository workspaces.  A production
autonomous driving stack may contain 400+ packages across ~30 repositories.  The
standard development workflow looks like this:

```bash
vcs import src < project.repos       # clone ~30 repositories
rosdep install --from-paths src      # install ALL system dependencies
colcon build                         # build ALL packages
source install/setup.bash
ros2 launch my_package my_launch.xml # finally launch
```

Every developer, every CI job, and every deployment target repeats this process
in full — even when only a fraction of the workspace is actually needed.

## Pain points of the traditional workflow

### Each step is a separate, uncoordinated tool

The `vcs import → rosdep install → colcon build → ros2 launch` pipeline is a
chain of independent tools that share no context:

1. **`vcs import`** clones everything in the `.repos` file.  It has no knowledge
   of which packages you actually need.
2. **`rosdep install --from-paths src`** scans *every* `package.xml` under `src/`
   and installs system dependencies for *all* of them — including packages you
   will never build, and test-only dependencies you don't need.
3. **`colcon build`** builds everything.  You can use `--packages-up-to` to
   build a subset, but you need to know which packages to list, all source
   code must already be cloned, and colcon still pulls in `exec_depend`
   packages — runtime dependencies that don't need to be built from source.
   The Autoware project maintains a dedicated CI action
   ([`remove-exec-depend`](https://github.com/autowarefoundation/autoware-github-actions/tree/main/remove-exec-depend))
   that strips `<exec_depend>` from every `package.xml` before building, just
   to work around this.
4. **`ros2 launch`** finally runs the system — but at this point you've already
   paid the full cost.

There is no way to say "I want to launch *this* launch file — give me exactly
what I need and nothing more."

### No lockfile, no reproducibility

The `.repos` format uses mutable references (`version: main`, `version: v1.0.0`)
that can change between runs.  A tag can be force-pushed, a branch advances with
every commit.  Two developers running `vcs import` on the same `.repos` file a
week apart may get different source code with no way to detect the difference.

Other ecosystems solved this with lockfiles long ago (npm's `package-lock.json`,
Cargo's `Cargo.lock`, Pixi's `pixi.lock`).  ROS 2 has had no equivalent.

### Whole-workspace operations are expensive

A full workspace build takes **1+ hours** on a reasonably powerful
machine.  During active development, most changes affect only a handful of
packages, yet the entire workspace must be cloned, dependency-resolved, and
built before anything can run.

### Multi-ECU / multi-target deployments

Production robotics systems often run across **multiple compute units** (ECUs),
each responsible for a different subsystem — perception on a GPU node, planning
on a CPU node, logging/streaming on a third.

Each ECU needs only a subset of the full workspace, but the standard ROS 2
tooling provides no way to express "build only the packages needed for *this*
launch configuration."  Teams end up maintaining **per-ECU build configurations**
— manually curated lists of which repositories or packages to clone and build
for each target.

The specifics vary across organizations: some use label-based repository
filtering, some maintain separate `.repos` files per ECU, some use custom
scripts that hardcode package lists.  Regardless of the mechanism, the
fundamental problem is the same:

- **Manual maintenance** — someone must keep the per-ECU package list in sync
  with the actual launch-time dependencies.  When a launch file starts including
  a new package, someone must remember to update the build configuration.
- **Coarse granularity** — these lists typically operate at the repository level,
  not the package level.  A repository with 20 packages gets cloned and built in
  full even if only 1 package is needed.
- **No single source of truth** — the real dependency information is in the
  launch files and `package.xml` files, but the build system ignores it and
  relies on manually-curated lists instead.
- **Drift** — over time, build configurations accumulate stale entries.  Nobody
  removes a package when a dependency is dropped.  Nobody adds one until CI
  breaks.

### The launch topology is opaque

Beyond the build pipeline, the launch system itself creates a visibility
problem.  A production ROS 2 stack like Autoware involves many levels of nested
include files spread across multiple repositories.  There is no way to see which
nodes actually run, which parameters take effect, or how topics are remapped —
without manually tracing through the entire include chain.

This opacity is a recognized and actively discussed problem in the ROS 2
community.  Proposed solutions range from flattening the hierarchy into a single
consolidated launcher to introducing a new DSL that compiles to launch files.
Both directions acknowledge the same root cause — the topology is locked inside
the launcher and cannot be examined without running it — but neither evaluates
the launcher you already have.  No new DSL is needed: the launch file is already
a sufficient specification of the system.

The specific pain points observed in large-scale deployments, and how roscope
addresses each:

- **Deep include nesting across multiple repositories** makes it impossible to
  know which nodes actually run without manually tracing every file.
  roscope inlines the full include chain into a single flat document; the
  interactive visualizer makes the same structure explorable as a graph.
- **Implicit argument scope**: arguments propagate downstream by default, making
  it hard to reason about which values reach which nodes.  roscope resolves and
  shows all argument values explicitly at each include boundary (`--show-args`,
  automatically enabled in visualizer mode).
- **Scattered, overriding parameters**: parameter files are spread across packages
  and silently overridden at multiple levels; the final runtime state is uncertain.
  roscope inlines all parameter files at resolve time, with override order visible.
- **Startup latency obscures topology**: ROS 2 argument validation alone adds
  13+ seconds in Autoware before any node starts — this is before the build,
  and before any node actually runs.  roscope resolves the full Autoware topology
  in under 2 seconds on a pre-fetched workspace, with no build or launch required.
- **No audit trail**: system behavior cannot be reviewed without running it.
  roscope's resolved XML is a complete static record of what the system would do
  at runtime — every node, namespace, remap, and parameter in one document.
  In practice the launch topology is the right level of detail for change
  detection: nodes, parameters, and remaps are what differ between
  configurations, while infrastructure topics remain constant across them.
- **Cross-repository opacity**: include chains span multiple repositories; no
  single view shows the full picture.  A single `roscope resolve` traces the
  chain across all repository boundaries.
- **Late error detection**: wrong remaps, missing arguments, and broken includes
  surface only at runtime.  `roscope check` catches these at resolve time, before
  any build.
- **Invisible runtime impact of launch changes**: when a pull request touches
  launch files or parameters, there is no easy way to see what changes to the
  running system it would introduce — without building and running it.  Resolving
  the base branch and the PR branch in dirty mode and diffing the two XML outputs
  makes the runtime impact immediately visible: every node addition, parameter
  change, and remap modification, before the PR is merged.  Staging the "before"
  output and overwriting with "after" lets an IDE highlight the diff inline.
  Dedicated structured diffing (both XML output and visualizer) for CI is planned.

### The insight: launch files already declare runtime dependencies

A ROS 2 launch file is, in effect, a **runtime dependency manifest**.  It
declares which packages to load, which nodes to run, which config files to read,
and which other launch files to include.  This is the same information that
`exec_depend` tries to capture in `package.xml` — but the launch file expresses
it more precisely, because it reflects the *actual* runtime composition rather
than a manually-maintained, often over-inclusive list.

All the information needed to determine the minimal build set is already there —
it just needs to be extracted.

This is the core insight behind roscope: **treat the launch file as a build
target**, like Bazel treats a `BUILD` file.  Instead of relying on `exec_depend`
or manually labeling repositories, let the tool trace the launch graph and
determine exactly which packages are needed.

## What roscope provides

roscope's primary contribution is the **resolver**: a launch file evaluator
that derives the launch topology — every node, parameter, remap, and
include boundary declared in the launch description — without a build or
a running ROS environment.  No new format needs to be learned, and no build
system needs to be replaced.  Launch files that use the standard
`launch`/`launch_ros` API work without modification.  Where project-specific
patterns reach outside that API — direct calls to `get_package_share_directory()`
or custom `Action` subclasses — targeted refactoring to the standard equivalents
is straightforward and results in more portable launch files.  The launch files
you already have are the system description; roscope evaluates them.

```
# Traditional: build first, then launch to see the topology
vcs import src < project.repos            # clone everything
rosdep install --from-paths src           # install all deps
colcon build                              # build everything
ros2 launch my_pkg my_launch.xml          # topology visible only now

# roscope: inspect topology immediately, no build required
vcs import src < project.repos            # clone repos (as usual)
roscope resolve -d my_pkg my_launch.xml \
  --visualize                             # interactive graph — before any build
```

Targeted builds are also supported from the same launch evaluation, using
`colcon build` as the backend:

```
roscope build my_pkg my_launch.xml \
  --clean --rosdep                        # fetch + deps + build (only what's needed)
ros2 launch my_pkg my_launch.xml          # launch
```

This build approach mirrors how Bazel treats `BUILD` files: the launch file
is the entry point, and the tool derives the minimal build set from it — no
manually-curated package lists required.  Unlike a full Bazel migration,
roscope requires zero changes to your existing `colcon`/`ament`/`rosdep`
setup.  See [FAQ: Is this Bazel but for ROS?](../docs/faq.md#is-this-bazel-but-for-ros)
for a fuller comparison.

Because roscope works on any launch file — hand-written or generated by
another tool — it can also serve as a **verification layer**.  If a launcher
is rewritten or generated by a higher-level tool, resolving both the original
and the new version and comparing the outputs gives a concrete, auditable
answer to "are these two launch systems equivalent?" without repeating the
full simulation test suite.

| Traditional workflow | roscope workflow |
|---|---|
| Mutable `.repos` refs | SHA-pinned lockfile (reproducible) |
| Clone all repos | Sparse-checkout only needed packages |
| Install all system deps | Install deps for needed packages only |
| Build all packages | Build minimal transitive closure |
| Manual per-ECU configs | Automatic dependency tracing |
| Separate build lists per target | Per-ECU launch file = per-ECU build set |
| Topology hidden behind full build | Interactive graph visualizer — no build needed |

With roscope, the multi-ECU problem reduces to pointing at the right launch
file.  To inspect the topology each ECU will run:

```bash
roscope resolve -d perception_launch perception.launch.xml --visualize
roscope resolve -d planning_launch planning.launch.xml --visualize
roscope resolve -d logging_launch logging.launch.xml --visualize
```

And to build only the packages each ECU needs:

```bash
roscope build perception_launch perception.launch.xml --clean --rosdep
roscope build planning_launch planning.launch.xml --clean --rosdep
roscope build logging_launch logging.launch.xml --clean --rosdep
```

No labels.  No manual filtering.  The launch file *is* the system specification.

## Beyond Autoware

roscope was developed and validated against Autoware, one of the largest and
most complex ROS 2 workspaces in production.  The tool has no Autoware-specific
logic — it operates on standard ROS 2 conventions and is intended to work with
any project that follows them:

- `.repos` files (vcstool format)
- `package.xml` (REP-149 / REP-127)
- XML and Python launch files (ROS 2 launch API)
- `colcon` for building
- `rosdep` for system dependency resolution

The one precondition is that launch files use the standard `launch`/`launch_ros`
API.  Patterns that reach outside it — such as direct calls to
`get_package_share_directory()` or custom `Action` subclasses — require targeted
refactoring to standard equivalents before roscope can resolve them fully
(see [Supported Environments](supported-environments.md#known-limitations)).

Validation beyond Autoware is ongoing.  Nav2 is the next planned target.
If your project follows these conventions, roscope should work — feedback on
projects other than Autoware is very welcome.
