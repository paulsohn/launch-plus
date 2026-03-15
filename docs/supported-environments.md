# Supported Environments

## Platforms

| Platform | Status | Notes |
|---|---|---|
| Ubuntu 22.04 (x86_64) | Supported | Primary development platform |
| Ubuntu 24.04 (x86_64) | Supported | |
| Ubuntu 22.04 (aarch64) | Planned | Cross-compilation via `cross` |
| Ubuntu 24.04 (aarch64) | Planned | Cross-compilation via `cross` |
| macOS | Not supported | No `apt-get`; rosdep/colcon untested |
| Windows | Not supported | Git sparse-checkout paths untested |

## ROS 2 distributions

| Distribution | Ubuntu | Status |
|---|---|---|
| Humble Hawksbill | 22.04 | Supported |
| Jazzy Jalisco | 24.04 | Supported |
| Rolling | 24.04 | Should work (untested in CI) |

A sourced ROS 2 environment is expected.  While the resolver does not link against
any ROS 2 libraries, it relies on `AMENT_PREFIX_PATH` to locate installed packages
(including system packages like `numpy` that are only visible through the ROS 2
overlay).  Source your ROS 2 setup file (`source /opt/ros/<distro>/setup.bash`)
before running launch-plus.

## Rust toolchain

- **Minimum supported Rust version (MSRV):** 1.85
- **Edition:** 2024
- Install via [rustup.rs](https://rustup.rs)

## Python

- **Python 3.8+** required (for evaluating Python launch files)
- Must be available as `python3` in `PATH`
- No ROS 2 Python packages needed on the resolver host

## External executables

launch-plus shells out to several external tools.  Not all are required for every
command — the table below shows which tools are needed and when.

| Executable | Required for | Notes |
|---|---|---|
| `git` | All commands | Sparse-checkout, ls-remote, archive |
| `python3` | `resolve`, `build`, `check` | Evaluating Python launch files |
| `colcon` | `build`, `test` | Build orchestration; **planned to be replaceable** |
| `rosdep` | `--rosdep` flag only | System dependency resolution; **planned to be replaceable** |
| `apt-get` | `--rosdep` with `#apt` deps | Called via `sudo` |
| `pip` | `--rosdep` with `#pip` deps | Called with `--break-system-packages` |

**Planned changes:** `colcon` and `rosdep` are currently invoked as subprocesses,
but we plan to support alternative build backends and dependency resolvers in
the future, reducing the number of external dependencies.

## Launch file support

### XML launch files (`*.launch.xml`)

Fully supported.  All standard ROS 2 XML launch constructs are handled:

- `<node>`, `<composable_node>`, `<load_composable_node>`
- `<include>` with argument forwarding
- `<group>` with scoping and `<push-ros-namespace>`
- `<arg>`, `<let>`, `<set_env>`, `<unset_env>`
- `if=` / `unless=` conditional attributes
- Substitutions: `$(var)`, `$(arg)`, `$(find-pkg-share)`, `$(find-pkg-prefix)`,
  `$(env)`, `$(eval)`, `$(dirname)`

### Python launch files (`*.launch.py`)

Supported via shimmed imports.  Standard patterns work:

- `generate_launch_description()` entry point
- `Node`, `LifecycleNode`, `ComposableNodeContainer`, `LoadComposableNodes`
- `IncludeLaunchDescription` with `PythonLaunchDescriptionSource` and
  `AnyLaunchDescriptionSource`
- `DeclareLaunchArgument`, `LaunchConfiguration`
- `GroupAction`, `PushROSNamespace`
- `OpaqueFunction` (with `--apply-opaque-file-access`)
- `FindPackageShare`, `PathJoinSubstitution`
- Conditions: `IfCondition`, `UnlessCondition`, `LaunchConfigurationEquals`

### YAML launch files (`*.launch.yaml`)

Not yet supported.  XML and Python cover the vast majority of ROS 2 launch
files in practice.

### Xacro (`*.xacro`, `*.urdf.xacro`)

**Not supported.**  Xacro files are not launch files — they are XML macro
templates for URDF/SDF robot descriptions.  When a launch file references a
xacro file (e.g. via `xacro.process_file()` in an OpaqueFunction), the xacro
processing call is preserved in the resolved output but not executed by
launch-plus.  The actual xacro expansion happens at runtime when the system is
launched.

## Known limitations

### OpaqueFunction constraints

`OpaqueFunction` bodies are executed, not analyzed.  They work correctly when:
- File reads use standard patterns (`open()`, `yaml.safe_load()`)
- Package paths use `FindPackageShare` or `get_package_share_directory()`

They may fail when:
- The function performs network I/O or other side effects
- The function imports non-standard packages not available on the resolver host
- The function modifies global state that affects other launch actions

### stdout in Python launch files

Any `print()` output in Python launch files (including inside `OpaqueFunction`
bodies) is redirected to stderr.  This is because the Python resolver
communicates with Rust via JSON on stdout — any extraneous output would corrupt
the protocol.

### Conditional dependencies (REP-149)

`package.xml` condition attributes (e.g.
`<depend condition="$ROS_DISTRO == humble">pkg</depend>`) are evaluated using
the current environment.  If `ROS_DISTRO` is not set, conditional dependencies
are excluded.

### Single-machine resolution

The resolver runs on a single machine and produces output for that machine's
architecture and ROS distribution.  Cross-distribution resolution (e.g.
resolving a Humble launch file on a Jazzy host) is not supported.

## Assumptions

- **vcstool `.repos` format** — launch-plus reads standard `.repos` files.
  Other manifest formats (rosinstall, wstool) are not supported.
- **Standard package layout** — packages must have a `package.xml` at their root.
  Non-standard layouts (e.g. nested packages without a top-level `package.xml`)
  may not be detected during indexing.
- **colcon as build tool** — the `build` command invokes `colcon build`.  Other
  build tools (catkin_make, catkin_tools) are not supported.
- **Git repositories** — only git repos are supported in `.repos` files.
  Subversion, Mercurial, etc. are not handled.
