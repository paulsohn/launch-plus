# FAQ

## General

### How is this different from just using colcon with `--packages-up-to`?

`colcon build --packages-up-to <pkg>` builds a package and its transitive
dependencies, but it requires:
1. All source code to already be cloned
2. You to know which packages to list
3. All system dependencies to already be installed

roscope automates all three: it determines the package list by analyzing the
launch file, sparse-checks out only those packages, and installs their system
dependencies.

### Is this Bazel but for ROS?

There are real similarities.  Like Bazel, roscope treats a **target** (in our
case, a launch file) as the entry point and computes the minimal set of things that
need to be built.  Both aim for reproducible, hermetic builds — Bazel through its
sandbox and content-addressable cache, roscope through its SHA-pinned lockfile.

But roscope is **not** a general-purpose build system.  It is a
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

roscope takes the opposite approach: **zero migration**.  It works with the
ROS 2 conventions your project already uses — `.repos` manifests, `package.xml`,
`colcon build`, `rosdep` — and layers the dependency-tracing and sparse-checkout
capabilities on top.  You can adopt it incrementally without changing any existing
build files.

Additionally, roscope provides **launch-graph-aware static analysis** that
Bazel does not: argument validation, namespace composition, conditional evaluation,
OpaqueFunction execution, and parameter file verification.  These are
ROS 2-specific concerns that a general-purpose build system has no reason to
address.

In short: if your team has already invested in Bazel for ROS 2, you may not need
roscope for the build subset.  But if you use the standard `colcon`/`ament`
toolchain — as most ROS 2 projects do — roscope gives you Bazel-like
targeted builds and reproducibility without requiring a build system migration.

### Do I need ROS 2 installed to use the resolver?

Yes — you should have ROS 2 installed and sourced.  Since roscope operates on
ROS 2 projects, a sourced ROS 2 environment (`AMENT_PREFIX_PATH`) is the expected
baseline.  The resolver uses `AMENT_PREFIX_PATH` to locate installed ROS packages
(e.g. buildfarm packages like `tf2_ros` or `rosbridge_server`), so packages that
are only available through the ROS 2 overlay won't be found without sourcing.

That said, the resolver itself (without `--rosdep`) does not link against any
ROS 2 libraries — it only needs Python 3, Git, and the environment variables that
`source /opt/ros/<distro>/setup.bash` provides.

### Can I use this with my existing `.repos` files?

Yes.  roscope reads standard [vcstool](https://github.com/dirk-thomas/vcstool)
`.repos` files.  You can point `roscope index` at your existing manifests.

### Does this replace `ros2 launch`?

Not yet.  Today roscope is a **build-time** tool: it resolves and builds the
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

### How do I use the interactive visualizer?

Pass `--visualize` to `roscope resolve`.  After resolution completes, roscope
opens an interactive graph view in your browser showing the full launch
structure — include boundaries, containers, composable nodes, topics, and
remaps.

```bash
roscope resolve -d my_pkg my_launch.xml \
  arg1:=value1 \
  --visualize
```

`--show-args` is automatically enabled in visualizer mode so launch argument
values appear in the graph.  Use `--viz-id <name>` to label the snapshot for
later reference.  Snapshots are cached in `~/.cache/roscope-viz/` and can be
reopened without re-resolving.

### Why doesn't the graph show whether a topic connection is a publisher or subscriber?

ROS 2 launch files carry no pub/sub direction information.  A `<remap>` rule
says "rename this topic name" — it does not indicate whether the node publishes
or subscribes on that name.  The resolver can therefore only record that a
node is associated with a topic, not in which direction.

This is a limitation of the launch file format as a description language.  If
`<remap>` (or a new sibling element) allowed annotating direction —
`type="pub"` / `type="sub"` — the graph could be rendered directionally
(e.g., left to right as a proper data-flow diagram) and roscope could flag
obvious mismatches such as a topic with publishers but no subscribers.  We
consider this a worthwhile addition to the ROS 2 launch API.

### Why are /tf, /tf_static, and /clock not shown in the graph?

roscope only tracks topics that appear in explicit `<remap>` rules.
Infrastructure topics like `/tf`, `/tf_static`, and `/clock` are consumed or
published by many nodes implicitly, without any remap declaration in the
launch file.

Even if a node writes `<remap from="/tf" to="/tf"/>` as an identity remap,
roscope will pick it up and track it — but this is rarely done in practice.

Tracking these topics for all nodes automatically would be noisy: a `/tf`
hub node connected to every node in the graph adds visual clutter without
much actionable information.  Opt-in tracking via explicit remaps strikes a
better balance.  Smarter handling — perhaps rendering infrastructure topics
as a distinct layer or collapsing them — is a possible future improvement.

More fundamentally, infrastructure topics are structural constants of any
ROS 2 system — they do not distinguish one configuration from another.  What
*does* change between configurations is the launch topology: which nodes run,
which parameters are set, which topics are remapped.  That is exactly what
roscope captures, making it sufficient in practice for auditing what changed
between two configurations or two versions of the same system.

### Why are service calls and actions not shown?

ROS 2 launch files have no construct for declaring service clients/servers or
action clients/servers.  That information lives in the node's source code, not
in the launch description.  Since roscope works entirely from the launch file,
it has no way to know which services or actions a node exposes.

Topics appear in the graph because `<remap>` rules give the resolver explicit,
named connections to track.  Services and actions have no equivalent in the
launch API — they are entirely implicit at launch time.

On large systems, topic-based communication represents the majority of
inter-node data flow, so the topic graph is already a useful approximation of
the system's data pipeline.  Supporting services and actions would require
either static analysis of node source code or an opt-in annotation in the
launch file — both are directions worth exploring.

## Resolution

### What does "preview" mode mean?

Preview mode (`--preview`) resolves the launch file against the **source
workspace** without requiring packages to be built.  For lockfile packages,
`FindPackageShare` resolves to source directories (fetched on demand);
packages not in the lockfile fall back to `AMENT_PREFIX_PATH`.  This is
the fastest way to see the full launch graph.

Without `--preview`, all packages are resolved via `AMENT_PREFIX_PATH`
(installed packages).  Both modes use real filesystem paths.

### What happens with OpaqueFunction?

`OpaqueFunction` is a ROS 2 launch construct that wraps an arbitrary Python
callable.  roscope **executes** these callables directly.  File reads that
access package resources work because `FindPackageShare` resolves to real
filesystem paths, and missing packages are fetched on demand.  No special flag
is needed.

### Why is the resolver redirecting my print() output?

The resolver writes resolved XML to stdout.  Any `print()` in your launch file
(including inside OpaqueFunction bodies) would corrupt the output.  roscope
redirects `sys.stdout` to `sys.stderr` before executing your launch file, so
`print()` still works — it just goes to stderr.

## Building

### Why does roscope still build `exec_depend` packages?

It shouldn't have to — `exec_depend` declares packages needed at *runtime*, not
at build time.  However, roscope currently delegates to `colcon build`, which
validates that **all** `package.xml` dependencies (including `exec_depend`) have
install artifacts before running cmake.  There is no colcon flag to disable this
check, so roscope must include `exec_depend` in the build set today.

This is a well-known pain point.  The Autoware project maintains
[`remove-exec-depend`](https://github.com/autowarefoundation/autoware-github-actions/tree/main/remove-exec-depend),
a CI action that strips `<exec_depend>` from every `package.xml` before
building, just to work around colcon's behavior.

Once colcon is replaced with direct ament invocations
([#18](https://github.com/paulsohn/roscope/issues/18)), the build closure
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

Flags that conflict with roscope-managed arguments (`--packages-select`,
`--base-paths`, `--build-base`, `--install-base`) are rejected.

### Can I build for a different ROS distro?

The resolver uses the currently-sourced ROS 2 environment (`ROS_DISTRO`,
`AMENT_PREFIX_PATH`).  To build for a different distro, source that distro's
setup file before running roscope.

### What about cross-compilation?

roscope itself is a native tool.  Cross-compilation of ROS 2 packages
depends on your colcon/CMake cross-compilation setup.  You can pass
cross-compilation flags via the colcon flagfile.

## Lockfile

### Should I commit the lockfile?

Yes.  The lockfile pins every repository to a concrete commit SHA, ensuring
reproducible builds.  Without it, different developers or CI runs may get
different source code.

### How do I update the lockfile?

```bash
roscope update                  # re-resolve all refs
roscope update core/my_msgs     # update a specific repo (uses lockfile key)
```

### What if a package isn't in the lockfile?

If a package is referenced in a launch file but not in the lockfile:
1. With `--rosdep`: roscope checks `AMENT_PREFIX_PATH` first, then runs
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
roscope index -l perception.lock.repos perception.repos
roscope index -l planning.lock.repos planning.repos

# Each context builds against its own lockfile
roscope build -l perception.lock.repos perception_launch perception.launch.xml --clean
roscope build -l planning.lock.repos planning_launch planning.launch.xml --clean
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
roscope already builds only the subset each ECU needs from the shared
lockfile.

## Design and philosophy

### Is it safe to execute OpaqueFunction code during resolution?

The same question applies to `ros2 launch` — if an OpaqueFunction is malicious,
it is equally malicious when the system is actually launched.  roscope does not
introduce new risk here.  In fact, it runs OpaqueFunction bodies *without a
real ROS 2 runtime*: there is no DDS middleware, no running nodes, and no
live services.  Side effects that require a running system fail harmlessly
instead of affecting live infrastructure — a narrower attack surface than a
live launch.

With a lockfile, roscope can go further than `ros2 launch` on safety: every
repository in the include chain is pinned to a concrete commit SHA.  Rather
than trusting a mutable branch reference, you can audit exactly which version
of each launch file will be evaluated before committing to it.  The lockfile
and resolved XML together make the full include chain explicit and reproducible.

### How reliable is resolution in dirty mode?

Consider the alternative: without roscope, discovering the topology of a dirty
workspace requires a full build followed by an actual `ros2 launch` run.  Dirty
mode gives you that same topology — the nodes, parameters, and remaps that
would be active at runtime — without the build.  The fidelity is exactly what
you would observe by running the system.

Dirty mode gives you the same guarantees as the underlying workflow it replaces:
roscope uses whatever is in `src/` as-is, the same way `ros2 launch` would
after a `vcs import`.  If `src/` has uncommitted local changes, the resolved
graph reflects those changes — the same situation arises with a symlink-install
workspace where source edits take effect immediately without a rebuild.

If you need stronger guarantees — that the resolved graph matches an exact,
known-good commit — use default or `--clean` mode instead.  Those modes verify
or enforce that every repository matches its lockfile SHA.

### How does roscope's model of launch files differ from the official `ros2 launch`?

The official `ros2 launch` is designed as an **extensible scripting system**
with deferred substitution and execution.  The `launch` and `launch_ros`
packages are intentionally separate, and each ROS 2 distribution adds new
custom `Action` and `Substitution` types.  In that model, the answer to "what
is the language of a launch file?" is simply Python — an open-ended scripting
environment.

roscope takes a different view: it treats the launch action tree as an
**abstract syntax tree (AST) of a domain-specific language** embedded in
Python, and resolution as **partial evaluation** of that AST.  The goal is
to extract the runtime system topology — nodes, parameters, remaps — without
executing the system.  Launch constructs that cannot be statically analyzed
(`OpaqueFunction`, `<include>` of externally-defined files) are treated as
**oracles**: their outputs are observed at evaluation time and taken as given,
without descending further into Python semantics.

A natural objection is that OpaqueFunction breaks the "closed language" claim
— if arbitrary Python can run, how is the language closed?  The key is that
OpaqueFunction outputs are still **constrained to be `Action` objects from the
launch API**.  The oracle can produce any combination of `Node`, `GroupAction`,
`IncludeLaunchDescription`, and so on, but it cannot produce terms outside
the launch language.  The oracle escape is bounded: it affects *completeness*
(a branch the oracle did not take may be missing from the graph) but not
*soundness* (nothing in the resolved graph represents a node or connection that
will not exist at runtime).  This soundness guarantee is what makes the output
trustworthy even when it is not exhaustive.

Closing the language is what enables **static verification** beyond simple
topology extraction: argument completeness checks, namespace collision
detection, and connectivity analysis all become possible precisely because the
language has defined terms and rules.  An open scripting system cannot support
these statically.  Formal operational semantics and completeness proofs are
ongoing work; empirical validation against large-scale systems like Autoware
confirms correctness in practice in the meantime.

### Why not contribute this directly to the official ROS 2 toolchain?

It helps to first clarify that roscope and `ros2 launch` are **complementary
tools serving different phases**: `ros2 launch` is a runtime process manager
that schedules and supervises nodes; roscope is a pre-flight static analysis
tool that extracts topology without running anything.  They share a grammar
(the launch file format) but have fundamentally different purposes — much like
a type checker and a compiler both read the same source language but are not
merged into one tool.

With that framing, several things make direct upstream integration difficult:

**Architectural mismatch.**  The official launcher is fundamentally
**event-driven and asynchronous**: all actions are scheduled through an event
loop that also manages processes, signals, and timers.  A synchronous partial
evaluator would need to either run inside that event loop (invasive, with deep
coupling to execution state) or bypass it entirely (a parallel code path that
diverges from the official implementation immediately).  Neither is a clean
integration.

**Third-party launch extensions.**  Projects like Nav2 define their own
`Action` and `Substitution` subclasses outside of the official `launch` /
`launch_ros` packages.  Even a perfectly sound resolver for the official API
would not cover these extensions — they are, by design, out of scope for any
upstream resolver.

**Incompatible runtime semantics.**  Several mechanisms in the official launcher
are built for live execution and cannot be reused for static resolution:

- `<param from="..."/>` pipes a rewritten YAML file through a temporary file
  into the node command line.  roscope always inlines parameter files fully
  into the resolved output instead.
- `LoadComposableNodes` performs a live ROS 2 service call to insert plugins
  into a running container.  roscope models this statically from the launch
  description.
- `ament_index_python`'s `get_package_share_directory()` queries the install
  tree via `AMENT_PREFIX_PATH`, which does not exist in preview mode.
  roscope intercepts it and redirects to the source directory instead.

These are not bugs to be fixed — they reflect the fact that the upstream
launcher is optimized for runtime execution, not static analysis.

The more productive direction for upstream contribution is **annotation
support** in the launch API: expressing pub/sub direction on remaps, opting
in to service/action declaration, or tagging nodes with connectivity metadata.
These annotations are useful to human readers and static analysis tools
independently of roscope — they make launch files more expressive as a system
description language.  roscope can consume such annotations immediately, and
the ecosystem benefits regardless of whether resolution is ever upstreamed.

### What does preview mode assume about package resources?

Preview mode resolves `FindPackageShare` to a package's source directory
rather than its install directory.  For this to produce correct results, two
conditions must hold:

1. **Resources are accessed via portable paths.**  Any launch file, parameter
   file, or config file referenced at resolve time must be found via
   `$(find-pkg-share <pkg>)/...` (or the Python equivalent `FindPackageShare`).
   Hard-coded absolute paths or paths relative to `__file__` will not resolve
   correctly across source and install trees.

2. **Resources are not generated during the build.**  Files produced by build
   steps — template expansion, `configure_file`, code generation — do not exist
   in the source tree and will not be found in preview mode.  Post-build
   resolution (without `--preview`) handles these correctly since it reads
   from the install tree.
