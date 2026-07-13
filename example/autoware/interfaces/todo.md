# Interface TODOs

Cases that cannot be fully expressed in the current YAML convention and need revisiting.

## Absolute topic subscriptions injected by loaded modules

### MotionVelocityPlannerNode — ObstacleCruiseModule (optimization_based_planner)
- **Package**: `autoware_motion_velocity_planner`
- **File**: `autoware_motion_velocity_planner/MotionVelocityPlannerNode.yaml`
- **Issue**: When `ObstacleCruiseModule` is loaded, its `OptimizationBasedPlanner` subscribes to
  the hardcoded absolute topic `/planning/trajectory` (not a node-relative `~/` topic). This topic
  name is fixed in source and is not a parameter. It is recorded in the YAML with a `when:` guard,
  but the absolute topic path cannot be remapped via the node's topic remapping and may conflict
  with other nodes publishing on that topic.
- **Action needed**: Verify whether `/planning/trajectory` is intentional (debug-only) or should
  be remapped to a node-private topic; check if the optimization-based planner is still the active
  backend for `ObstacleCruiseModule`. (Source not in workspace — cannot verify locally.)

## Dynamic publishers injected by loaded modules (topic names contain module name at runtime)

### BehaviorPathPlannerNode — per-module processing-time debug publishers
- **Package**: `autoware_behavior_path_planner`
- **File**: `autoware_behavior_path_planner/BehaviorPathPlannerNode.yaml`
- **Issue**: `PlannerManager` publishes `~/debug/<short_name>/processing_time_ms`
  (`autoware_internal_debug_msgs/msg/Float64Stamped`, confirmed) and
  `~/debug/total_time/processing_time_ms` for each loaded module.
  The short names come from `SceneModuleManagerInterface::name_` (e.g. `"avoidance"`,
  `"goal_planner"`), which are set in each manager's constructor — not derivable from the
  `launch_modules` class-name strings. `loop:` over `launch_modules` would yield class names,
  not topic short names. These entries are not recorded in the YAML.
- **Possible fix**: Add static entries per known module (analogous to the existing per-module
  MarkerArray publishers), or introduce a class-name → short-name mapping mechanism.

## Legacy remaps in launch files with no matching source connection

These remaps appear in launch files but do not correspond to any subscription or publisher in the
current source code. They are recorded as notes in the respective YAML files and listed here for
tracking.

### BehaviorVelocityPlannerNode — unused pointcloud and stop_reasons remaps
- **Package**: `autoware_behavior_velocity_planner`
- **File**: `autoware_behavior_velocity_planner/BehaviorVelocityPlannerNode.yaml`
- **Topics**: `~/input/compare_map_filtered_pointcloud`, `~/input/vector_map_inside_area_filtered_pointcloud`, `~/output/stop_reasons`
- **Note**: README documents these (compare_map is run_out-specific, stop_reasons is tier4_planning_msgs)
  but they are absent from the current node.cpp/node.hpp. May be legacy from removed modules.

### BehaviorPathPlannerNode — unused stop_reasons remap
- **Package**: `autoware_behavior_path_planner`
- **File**: `autoware_behavior_path_planner/BehaviorPathPlannerNode.yaml`
- **Topic**: `~/output/stop_reasons`
- **Note**: Appears in launch file but not in BPP source code. May be legacy.

### MotionVelocityPlannerNode — unused virtual_traffic_light, stop_reasons, velocity_factors
- **Package**: `autoware_motion_velocity_planner`
- **File**: `autoware_motion_velocity_planner/MotionVelocityPlannerNode.yaml`
- **Topics**: `~/input/virtual_traffic_light_states`, `~/output/stop_reasons`, `~/output/velocity_factors`
- **Note**: Added by `autoware_core_planning.launch.xml` but absent from MVP node.cpp/node.hpp.
  May be legacy from an older universe-based MVP interface.

### PlanningValidatorNode — unused objects remap
- **Package**: `autoware_planning_validator`
- **File**: `autoware_planning_validator/PlanningValidatorNode.yaml`
- **Topic**: `~/input/objects`
- **Note**: Appears in launch file, absent from node.cpp (only `~/input/trajectory` is subscribed).
  May be legacy from an older validator module (IntersectionCollisionChecker uses objects internally
  but via a different mechanism than a direct node subscription).

### OperationModeTransitionManager — unused steering and is_autonomous_available
- **Package**: `autoware_operation_mode_transition_manager`
- **File**: `autoware_operation_mode_transition_manager/OperationModeTransitionManager.yaml`
- **Topics**: `steering` (subscription), `is_autonomous_available` (publisher)
- **Note**: In launch file but absent from node.hpp/node.cpp. Source subscribes to `kinematics`,
  `trajectory`, `control_cmd` etc., but not `steering`. `is_autonomous_available` publisher is not
  in source either. May be legacy from an older interface.

### Controller (trajectory_follower_node) — unused stop_reason remap
- **Package**: `autoware_trajectory_follower_node`
- **File**: `autoware_trajectory_follower_node/Controller.yaml`
- **Topic**: `~/output/stop_reason`
- **Note**: In launch file but absent from controller_node.cpp and PID/MPC implementations.
  May be legacy from an older longitudinal controller interface.

## Skipped (visualization tools with no static roscope connections)

- `rviz2` — subscribes to a user-configurable set of display topics; not covered.

## Dynamic topics from runtime config or CLI args (extension packages)

### autoware_carla_interface (main node)
- **Package**: `autoware_carla_interface`
- **File**: `autoware_carla_interface/autoware_carla_interface.yaml`
- **Issue**: Sensor publishers (Image, CameraInfo, PointCloud2, Imu, etc.) created from a runtime sensor kit config file. Only vehicle interface topics recorded.

### TopicRelayController (generic mode)
- **Package**: `autoware_topic_relay_controller`
- **File**: `autoware_topic_relay_controller/TopicRelayController.yaml`
- **Partially resolved**: Generic-mode topic and remap_topic names are now expressed via
  `name_expr:` with `when:` guards. The message type (`topic_type` param) cannot be expressed
  in the current `msg_type` field; generic-mode entries omit `msg_type`.

### ConverterNode — Dynamic UserDefinedValue publishers
- **Package**: `autoware_scenario_simulator_v2_adapter`
- **File**: `autoware_scenario_simulator_v2_adapter/ConverterNode.yaml`
- **Issue**: Per-metric-field topic names (UserDefinedValue publishers) are not expressible.
  The `loop:` entry covers `metric_topic_list` subscriptions only.

### ControlCmdGate — **Not expressible** (two-level param indirection)
- **Package**: `autoware_control_command_gate`
- **File**: `autoware_control_command_gate/ControlCmdGate.yaml`
- **Issue**: Per-source input topics `~/inputs/<name>/control`, `~/inputs/<name>/gear`, etc.
  The source names come from `inputs_names.<id>` parameters where `<id>` is an integer from the
  `inputs` list. This requires resolving an integer ID to a string name, then using that name in
  the topic path — a two-level indirection not expressible with the current `loop:` format.

## eagleye_rt — argument-driven topic selection

Many eagleye_rt executables select topic names via argv[1] ("1st"/"2nd"/"3rd") or parameters:
- `heading`, `heading_interpolate`, `yaw_rate_offset`: argv[1] selects which order's pub/sub topics — SKIP
- `rtk_heading`, `rtk_dead_reckoning`, `height`, `smoothing`, `velocity_estimator`, `slip_coefficient`,
  `velocity_scale_factor`: topic names are C++ local variables loaded from a YAML config file, not
  declared as ROS parameters — cannot be expressed via `name_expr:`. YAML files retain static defaults.
