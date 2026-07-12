# toy_robot example

A minimal 3-node ROS 2 system demonstrating roscope connection metadata.

```
/cmd_vel ──► controller ──► /joint_states ──► robot_state_publisher ──► /tf
                         ├──► /odom                                   ──► /tf_static
                         └──► /tf

sensor ──► /scan
```

| Node | Package | Type | Interfaces |
|------|---------|------|------------|
| `robot_state_publisher` | `robot_state_publisher` (standard) | standalone | `interfaces/robot_state_publisher/` |
| `controller` | `toy_robot_controller` (custom) | standalone | `share/toy_robot_controller/interface/` |
| `sensor` | `toy_robot_sensor` (custom) | standalone | `share/toy_robot_sensor/interface/` |

## Inspect connections without building

Run from this directory:

```bash
roscope resolve -d toy_robot_bringup toy_robot.launch.xml \
  --plugin toy_robot_plugin.py \
  --preview
```

Two flags work together here:

- **`-d` (dirty mode)**: trust the current working tree as-is rather than
  checking it out to match a lockfile.  Since this example ships no lockfile,
  `-d` is required; without it roscope would look for `manifest.lock.repos` and
  fail.
- **`--preview`**: resolve `$(find-pkg-share ...)` against source directories in
  `src/` instead of installed paths in `install/`.  This is what makes a
  `colcon build` unnecessary — roscope reads package resources directly from the
  source tree.

The plugin reads interface definitions from two places:

- **Custom packages** (`toy_robot_controller`, `toy_robot_sensor`): definitions
  are shipped inside each package under `interface/<executable>.yaml` and
  installed to `share/<pkg>/interface/`.  In preview mode `find-pkg-share`
  resolves to `src/<pkg>/`, so the plugin finds interface YAMLs there directly.
- **Standard package** (`robot_state_publisher`): not our package, so no primary
  definition exists.  The plugin falls back to
  `interfaces/robot_state_publisher/robot_state_publisher.yaml`.

## Build and inspect connections at runtime

### 1. Build

```bash
source /opt/ros/<distro>/setup.bash          # e.g. humble or jazzy
colcon build --symlink-install
source install/setup.bash
```

### 2. Launch

```bash
ros2 launch toy_robot_bringup toy_robot.launch.xml
```

### 3. Inspect with rqt_graph

```bash
rqt_graph
```

Select **Nodes/Topics (all)** to see the full topic graph.

### 4. Inspect from the command line

```bash
# All active nodes
ros2 node list

# Publishers and subscribers of a specific node
ros2 node info /controller
ros2 node info /robot_state_publisher

# All active topics and their types
ros2 topic list -t

# Live message stream
ros2 topic echo /odom
```

### 5. Post-build roscope resolve (uses installed share paths)

After sourcing `install/setup.bash`, drop `--preview` so roscope resolves
`$(find-pkg-share ...)` against the installed packages rather than `src/`:

```bash
roscope resolve -d toy_robot_bringup toy_robot.launch.xml \
  --plugin toy_robot_plugin.py
```
