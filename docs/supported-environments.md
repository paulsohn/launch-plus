# Supported Environments

## Platforms

| Platform | Status | Notes |
|---|---|---|
| Ubuntu 22.04 (x86_64) | Supported | Primary development platform |
| Ubuntu 24.04 (x86_64) | Supported | |
| Ubuntu 22.04 (aarch64) | Planned | |
| Ubuntu 24.04 (aarch64) | Planned | |
| macOS | Not supported | No `apt-get`; rosdep/colcon untested |
| Windows | Not supported | Git sparse-checkout paths untested |

## ROS 2 distributions

| Distribution | Ubuntu | Status |
|---|---|---|
| Humble Hawksbill | 22.04 | Supported |
| Jazzy Jalisco | 24.04 | Supported |
| Rolling | 24.04 | Should work (untested in CI) |

A sourced ROS 2 environment is expected.  While the resolver does not link against
any ROS 2 libraries, it relies on `AMENT_PREFIX_PATH` to locate installed ROS
packages (e.g. buildfarm packages like `rosbridge_server` or `tf2_ros`).  Source
your ROS 2 setup file (`source /opt/ros/<distro>/setup.bash`) before running
launch-plus.

## Python

- **Python 3.10+** required (for evaluating Python launch files and `$(eval ...)`
  substitutions in XML launch files)
- Must be available as `python3` in `PATH`
- **PyYAML** (`python3-yaml`) must be installed — the Python resolver imports it
  to parse parameter files in OpaqueFunction bodies
- No ROS 2 Python packages needed on the resolver host

## External executables

launch-plus shells out to several external tools.  Not all are required for every
command — the table below shows which tools are needed and when.

| Executable | Required for | Notes |
|---|---|---|
| `git` | All commands | Sparse-checkout, ls-remote, archive |
| `python3` | `resolve`, `build`, `check`, `test` | Evaluating Python launch files and `$(eval ...)` |
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
- `OpaqueFunction`
- `FindPackageShare`, `PathJoinSubstitution`
- Conditions: `IfCondition`, `UnlessCondition`, `LaunchConfigurationEquals`
- Event handlers: `OnProcessExit`, `OnProcessStart`, etc.
- `EmitEvent` and other built-in event actions

**Lifecycle and event handling caveats:**  `LifecycleNode` and event-related
constructs (`RegisterEventHandler`, `OnProcessExit`, `OnProcessStart`, etc.)
are captured by the shims and appear as static elements in the resolved XML
(e.g. `<lifecycle_node>`, `<on_process_exit>`).  However:

- There is currently **no executor that recognizes these extended XML elements**.
  The resolved output preserves the structure for inspection, but `ros2 launch`
  does not understand `<lifecycle_node>` or `<on_process_exit>` tags in XML.
- Event handler **callbacks have very limited support**: built-in actions like
  `EmitEvent` are supported, but arbitrary Python function callbacks are not —
  they cannot be serialized to XML.

### YAML launch files (`*.launch.yaml`)

Supported via the YAML parser.

### Xacro (`*.xacro`, `*.urdf.xacro`)

**Not supported.**  Xacro files are not launch files — they are XML macro
templates for URDF/SDF robot descriptions.  When an XML launch file references
xacro (e.g. via a `$(xacro ...)` substitution), the xacro call is preserved
in the resolved output but not executed by launch-plus — the actual xacro
expansion happens at runtime when the system is launched.

Python-side xacro calls (e.g. `xacro.process_file()` in an OpaqueFunction)
are not supported and will fail, since the `xacro` package is not available
through the resolver's shimmed imports.

## Known limitations

### `get_package_share_directory()` in preview mode

`get_package_share_directory()` (from `ament_index_python`) looks up installed
packages via `AMENT_PREFIX_PATH`.  In **preview mode** (`--preview`), source
packages have not been built or installed yet, so the call will fail with a
`PackageNotFoundError` for any package that exists only in the source tree.

**Recommended replacement:** use the `FindPackageShare` substitution instead.
`FindPackageShare` is resolved by launch-plus itself: in preview mode it points
to the package's source directory (when the package is present in the lockfile
source tree); after a build it points to the install directory.

```python
# Instead of:
from ament_index_python.packages import get_package_share_directory
pkg_share = get_package_share_directory("my_pkg")

# Use:
from launch.substitutions import FindPackageShare, PathJoinSubstitution
pkg_share = FindPackageShare("my_pkg")
config = PathJoinSubstitution([pkg_share, "config", "params.yaml"])
```

If a string path is required (e.g. to pass into a Python function that does not
accept substitutions), wrap the lookup in an `OpaqueFunction` and call
`get_package_share_directory()` there — `OpaqueFunction` bodies run after the
environment is resolved, so installed packages are available.

**Source/install path assumption:** launch-plus assumes that any resource file
referenced by path in a launch file (launcher files, parameter files, etc.) is
present at the **same relative path within the package share directory** in both
the source tree and the install tree, and that the file contents are identical.
This is the standard ROS 2 convention (resources are installed via CMake
`install(DIRECTORY ...)` rules).  Resources that are generated or transformed
during the build (e.g. files processed by `configure_file`) may not satisfy
this assumption.

### OpaqueFunction constraints

`OpaqueFunction` bodies are executed directly, not analyzed.  They may fail when:
- The function performs network I/O or other side effects
- The function imports non-standard packages not available on the resolver host
- The function modifies global state that affects other launch actions
- The function calls `get_package_share_directory()` in preview mode (see above)

### stdout in Python launch files

Any `print()` output in Python launch files (including inside `OpaqueFunction`
bodies) is redirected to stderr.  This is because the resolver writes resolved
XML to stdout — any extraneous output would corrupt the result.

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
