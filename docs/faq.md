# FAQ

## General

### How is this different from just using colcon with `--packages-up-to`?

`colcon build --packages-up-to <pkg>` builds a package and its transitive
dependencies, but it requires:
1. All source code to already be cloned
2. You to know which packages to list
3. All system dependencies to already be installed

launch-plus automates all three: it determines the package list by analyzing the
launch file, sparse-checks out only those packages, and installs their system
dependencies.

### Is this Bazel but for ROS?

There are real similarities.  Like Bazel, launch-plus treats a **target** (in our
case, a launch file) as the entry point and computes the minimal set of things that
need to be built.  Both aim for reproducible, hermetic builds — Bazel through its
sandbox and content-addressable cache, launch-plus through its SHA-pinned lockfile.

But launch-plus is **not** a general-purpose build system.  It is a
ROS 2-specific tool that understands launch files, `package.xml`, and the
`colcon`/`ament` ecosystem natively.

Bazel rules for ROS 2 do exist (e.g. the ones provided by Apex.AI), and they
work well for teams that can commit to a full Bazel migration.  However, adopting
Bazel in an existing ROS 2 project is a significant undertaking:

- Every package needs a `BUILD` file — `package.xml` and `CMakeLists.txt` are not
  enough
- The entire build toolchain changes: `colcon`, `ament_cmake`, and `rosdep` are
  replaced by Bazel equivalents
- Third-party ROS packages (from the buildfarm or other `.repos` sources) must be
  wrapped with Bazel build rules
- Teams must learn and maintain a parallel build system

launch-plus takes the opposite approach: **zero migration**.  It works with the
ROS 2 conventions your project already uses — `.repos` manifests, `package.xml`,
`colcon build`, `rosdep` — and layers the dependency-tracing and sparse-checkout
capabilities on top.  You can adopt it incrementally without changing any existing
build files.

Additionally, launch-plus provides **launch-graph-aware static analysis** that
Bazel does not: argument validation, namespace composition, conditional evaluation,
OpaqueFunction execution, and parameter file verification.  These are
ROS 2-specific concerns that a general-purpose build system has no reason to
address.

In short: if your team has already invested in Bazel for ROS 2, you may not need
launch-plus for the build subset.  But if you use the standard `colcon`/`ament`
toolchain — as most ROS 2 projects do — launch-plus gives you Bazel-like
targeted builds and reproducibility without requiring a build system migration.

### Do I need ROS 2 installed to use the resolver?

Yes — you should have ROS 2 installed and sourced.  Since launch-plus operates on
ROS 2 projects, a sourced ROS 2 environment (`AMENT_PREFIX_PATH`) is the expected
baseline.  The resolver uses `AMENT_PREFIX_PATH` to locate installed ROS packages
(e.g. buildfarm packages like `tf2_ros` or `rosbridge_server`), so packages that
are only available through the ROS 2 overlay won't be found without sourcing.

That said, the resolver itself (without `--rosdep`) does not link against any
ROS 2 libraries — it only needs Python 3, Git, and the environment variables that
`source /opt/ros/<distro>/setup.bash` provides.

### Can I use this with my existing `.repos` files?

Yes.  launch-plus reads standard [vcstool](https://github.com/dirk-thomas/vcstool)
`.repos` files.  You can point `launch-plus index` at your existing manifests.

### Does this replace `ros2 launch`?

Not yet.  Today launch-plus is a **build-time** tool: it resolves and builds the
packages you need, but you still use `ros2 launch` (or your preferred launcher)
to actually run the system.  The `resolve` command produces a flattened XML that
shows what `ros2 launch` would do — useful for debugging and CI.

**Execution support is planned.**  The roadmap includes:
- A **built-in executor** that directly consumes the resolved launch structure
  to spawn and manage processes
- **Integration with existing launchers** — `ros2 launch`, and third-party
  reimplementations — so you can choose the execution backend that fits your
  project

The goal is a complete `resolve → build → launch` pipeline in a single tool,
while remaining compatible with the broader ROS 2 launch ecosystem.

## Resolution

### What does "preview" mode mean?

Preview mode (`--preview`) resolves the launch file against the **source
workspace** without requiring packages to be built.  The output uses portable
`$(find-pkg-share ...)` paths.  This is the fastest way to see the full launch
graph.

Without `--preview`, resolution uses `AMENT_PREFIX_PATH` (installed packages).

### Why do some flags have such long names?

Flags like `--allow-global-arg-cascade` and `--apply-launch-arg-defaults` are
intentionally verbose.  They enable behaviors that work around common anti-
patterns in legacy launch files (implicit argument propagation, reliance on
defaults).  The verbose names encourage fixing the root cause in the launch files
rather than permanently depending on the workaround.

### What happens with OpaqueFunction?

`OpaqueFunction` is a ROS 2 launch construct that wraps an arbitrary Python
callable.  launch-plus **executes** these callables with patched filesystem
access so that:
- `open()` calls are intercepted and resolved via portable paths
- Missing packages are fetched on demand
- Parameter YAML files are read and their contents captured

Use `--apply-opaque-file-access` to enable this.  Without it, file access in
OpaqueFunction bodies produces an error (strict mode).

### Why is the resolver redirecting my print() output?

The Python resolver communicates with the Rust host via JSON on stdout.  Any
`print()` in your launch file (including inside OpaqueFunction bodies) would
corrupt this channel.  launch-plus redirects `sys.stdout` to `sys.stderr` before
executing your launch file, so `print()` still works — it just goes to stderr.

## Building

### Why does launch-plus still build `exec_depend` packages?

It shouldn't have to — `exec_depend` declares packages needed at *runtime*, not
at build time.  However, launch-plus currently delegates to `colcon build`, which
validates that **all** `package.xml` dependencies (including `exec_depend`) have
install artifacts before running cmake.  There is no colcon flag to disable this
check, so launch-plus must include `exec_depend` in the build set today.

This is a well-known pain point.  The Autoware project maintains
[`remove-exec-depend`](https://github.com/autowarefoundation/autoware-github-actions/tree/main/remove-exec-depend),
a CI action that strips `<exec_depend>` from every `package.xml` before
building, just to work around colcon's behavior.

Once colcon is replaced with direct ament invocations
([#18](https://github.com/paulsohn/launch-plus/issues/18)), the build closure
will use only `build_depend`, `buildtool_depend`, `build_export_depend`, and
`buildtool_export_depend`.  Runtime dependencies will
be expected to be satisfied by installed system packages — exactly the role
`exec_depend` was designed to express.  See
[Dependency closure](concepts.md#dependency-closure) for details.

### How does `--colcon-flagfile` work?

The flagfile contains extra arguments for `colcon build`, one shell token per
line.  Comments (`#`) are supported:

```txt
--symlink-install
--cmake-args
-DCMAKE_BUILD_TYPE=Release
--parallel-workers
4
```

Flags that conflict with launch-plus-managed arguments (`--packages-select`,
`--base-paths`, `--build-base`, `--install-base`) are rejected.

### Can I build for a different ROS distro?

The resolver uses the currently-sourced ROS 2 environment (`ROS_DISTRO`,
`AMENT_PREFIX_PATH`).  To build for a different distro, source that distro's
setup file before running launch-plus.

### What about cross-compilation?

launch-plus itself is a native tool.  Cross-compilation of ROS 2 packages
depends on your colcon/CMake cross-compilation setup.  You can pass
cross-compilation flags via the colcon flagfile.

## Lockfile

### Should I commit the lockfile?

Yes.  The lockfile pins every repository to a concrete commit SHA, ensuring
reproducible builds.  Without it, different developers or CI runs may get
different source code.

### How do I update the lockfile?

```bash
launch-plus update                  # re-resolve all refs
launch-plus update core/my_msgs     # update a specific repo (uses lockfile key)
```

### What if a package isn't in the lockfile?

If a package is referenced in a launch file but not in the lockfile:
1. With `--rosdep`: launch-plus checks `AMENT_PREFIX_PATH` first, then runs
   `rosdep resolve` to map the key to a system package name and installs it
   via `apt-get` or `pip`
2. Without `--rosdep`: the resolver reports an error

This is by design — the lockfile is the single source of truth for your
workspace's source packages.  System packages (installed via apt/rosdep) are
handled separately.

### What if different ECUs need different versions of the same package?

Use **separate lockfiles** for each context that requires a different package
version.  A lockfile maps each package name to exactly one version — this
matches the ROS 2 runtime constraint where `AMENT_PREFIX_PATH` resolves each
package name to a single location.

```bash
# Different .repos → different lockfiles
launch-plus index -l perception.lock.repos perception.repos
launch-plus index -l planning.lock.repos planning.repos

# Each context builds against its own lockfile
launch-plus build -l perception.lock.repos perception_launch perception.launch.xml --clean
launch-plus build -l planning.lock.repos planning_launch planning.launch.xml --clean
```

That said, **sharing a single lockfile across all ECUs in a system is strongly
recommended** whenever possible.  ECUs communicate via ROS 2 topics and
services — if message type definitions diverge between ECUs, the system breaks
at runtime.  A shared lockfile guarantees that all ECUs agree on message types,
interface packages, and shared libraries.  Real-world multi-ECU deployments
(7+ ECUs) routinely share a single lockfile for this reason.

Multiple lockfiles should be reserved for cases where version divergence is
genuinely required and the teams have explicitly verified interface
compatibility.  In practice this is rare — the `.repos` files can overlap, and
launch-plus already builds only the subset each ECU needs from the shared
lockfile.
