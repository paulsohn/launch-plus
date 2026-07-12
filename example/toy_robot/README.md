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

roscope's dirty mode (`-d`) scans `src/` for `package.xml` files directly,
so no `colcon build` is needed. Run from this directory:

```bash
roscope resolve -d toy_robot_bringup toy_robot.launch.xml \
  --apply-launch-arg-defaults \
  --plugin toy_robot_plugin.py \
  --preview
```

`--preview` prints the resolved launch XML to stdout instead of writing it to a
file, which is useful for a quick sanity check.  Drop it to write
`resolved.launch.xml` to the current directory.

The plugin reads interface definitions from two places:

- **Custom packages** (`toy_robot_controller`, `toy_robot_sensor`): definitions
  are shipped inside each package under `interface/<executable>.yaml` and
  installed to `share/<pkg>/interface/`.  In dirty mode roscope cannot resolve
  `find-pkg-share`, so the plugin falls back to the local `interfaces/` registry
  automatically.
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

After sourcing `install/setup.bash`, roscope can resolve `find-pkg-share`
correctly, so the plugin will find interface YAMLs from the installed packages
directly:

```bash
roscope resolve toy_robot_bringup toy_robot.launch.xml \
  --apply-launch-arg-defaults \
  --plugin toy_robot_plugin.py \
  --preview
```
