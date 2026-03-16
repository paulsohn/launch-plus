# Motivation

## The scaling problem in ROS 2 workspaces

ROS 2 projects tend to grow into large multi-repository workspaces.  A production
autonomous driving stack may contain 200+ packages across 50+ repositories.  The
standard development workflow looks like this:

```bash
vcs import src < project.repos       # clone all repositories
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

A full workspace build takes **30–60 minutes** on a reasonably powerful
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

### The insight: launch files already declare runtime dependencies

A ROS 2 launch file is, in effect, a **runtime dependency manifest**.  It
declares which packages to load, which nodes to run, which config files to read,
and which other launch files to include.  This is the same information that
`exec_depend` tries to capture in `package.xml` — but the launch file expresses
it more precisely, because it reflects the *actual* runtime composition rather
than a manually-maintained, often over-inclusive list.

All the information needed to determine the minimal build set is already there —
it just needs to be extracted.

This is the core insight behind launch-plus: **treat the launch file as a build
target**, like Bazel treats a `BUILD` file.  Instead of relying on `exec_depend`
or manually labeling repositories, let the tool trace the launch graph and
determine exactly which packages are needed.

## What launch-plus provides

launch-plus replaces the four-step pipeline with a single command that
understands the full picture:

```
# Traditional: 4 tools, no shared context
vcs import src < project.repos            # clone everything
rosdep install --from-paths src           # install all deps
colcon build                              # build everything
ros2 launch my_pkg my_launch.xml          # launch

# launch-plus: 1 tool, launch-file-driven
launch-plus build my_pkg my_launch.xml \
  --clean --rosdep                        # fetch + deps + build (only what's needed)
ros2 launch my_pkg my_launch.xml          # launch
```

| Traditional workflow | launch-plus workflow |
|---|---|
| Mutable `.repos` refs | SHA-pinned lockfile (reproducible) |
| Clone all repos | Sparse-checkout only needed packages |
| Install all system deps | Install deps for needed packages only |
| Build all packages | Build minimal transitive closure |
| Manual per-ECU configs | Automatic dependency tracing |
| Separate build lists per target | Per-ECU launch file = per-ECU build set |

With launch-plus, the multi-ECU problem reduces to:

```bash
# Perception ECU — just point at the perception launch file
launch-plus build perception_launch perception.launch.xml --clean --rosdep

# Planning ECU — point at the planning launch file
launch-plus build planning_launch planning.launch.xml --clean --rosdep

# Logging ECU — point at the logging launch file
launch-plus build logging_launch logging.launch.xml --clean --rosdep
```

No labels.  No manual filtering.  The launch file *is* the build specification.

## Beyond Autoware

While launch-plus was developed with Autoware as the primary test case, it is
designed to work with **any ROS 2 project** that uses standard `.repos` manifests
and launch files.  The tool has no Autoware-specific logic — it operates on
standard ROS 2 conventions:

- `.repos` files (vcstool format)
- `package.xml` (REP-149 / REP-127)
- XML and Python launch files (ROS 2 launch API)
- `colcon` for building
- `rosdep` for system dependency resolution

If your project follows these conventions, launch-plus can help.
