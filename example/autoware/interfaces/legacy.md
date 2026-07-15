# Legacy remaps in launch files with no matching source connection

These remaps appear in launch files but do not correspond to any subscription or publisher in the
current source code. They are recorded as comments in the respective YAML files and listed here
for tracking.

## BehaviorVelocityPlannerNode — unused pointcloud and stop_reasons remaps
- **Package**: `autoware_behavior_velocity_planner`
- **File**: `autoware_behavior_velocity_planner/BehaviorVelocityPlannerNode.yaml`
- **Topics**: `~/input/compare_map_filtered_pointcloud`, `~/input/vector_map_inside_area_filtered_pointcloud`, `~/output/stop_reasons`
- **Note**: README documents these (compare_map is run_out-specific, stop_reasons is tier4_planning_msgs)
  but they are absent from the current node.cpp/node.hpp. May be legacy from removed modules.

## BehaviorPathPlannerNode — unused stop_reasons remap
- **Package**: `autoware_behavior_path_planner`
- **File**: `autoware_behavior_path_planner/BehaviorPathPlannerNode.yaml`
- **Topic**: `~/output/stop_reasons`
- **Note**: Appears in launch file but not in BPP source code. May be legacy.

## MotionVelocityPlannerNode — unused virtual_traffic_light, stop_reasons, velocity_factors
- **Package**: `autoware_motion_velocity_planner`
- **File**: `autoware_motion_velocity_planner/MotionVelocityPlannerNode.yaml`
- **Topics**: `~/input/virtual_traffic_light_states`, `~/output/stop_reasons`, `~/output/velocity_factors`
- **Note**: Added by `autoware_core_planning.launch.xml` but absent from MVP node.cpp/node.hpp.
  May be legacy from an older universe-based MVP interface.

## PlanningValidatorNode — unused objects remap
- **Package**: `autoware_planning_validator`
- **File**: `autoware_planning_validator/PlanningValidatorNode.yaml`
- **Topic**: `~/input/objects`
- **Note**: Appears in launch file, absent from node.cpp (only `~/input/trajectory` is subscribed).
  May be legacy from an older validator module (IntersectionCollisionChecker uses objects internally
  but via a different mechanism than a direct node subscription).

## OperationModeTransitionManager — unused steering and is_autonomous_available
- **Package**: `autoware_operation_mode_transition_manager`
- **File**: `autoware_operation_mode_transition_manager/OperationModeTransitionManager.yaml`
- **Topics**: `steering` (subscription), `is_autonomous_available` (publisher)
- **Note**: In launch file but absent from node.hpp/node.cpp. Source subscribes to `kinematics`,
  `trajectory`, `control_cmd` etc., but not `steering`. `is_autonomous_available` publisher is not
  in source either. May be legacy from an older interface.

## Controller (trajectory_follower_node) — unused stop_reason remap
- **Package**: `autoware_trajectory_follower_node`
- **File**: `autoware_trajectory_follower_node/Controller.yaml`
- **Topic**: `~/output/stop_reason`
- **Note**: In launch file but absent from controller_node.cpp and PID/MPC implementations.
  May be legacy from an older longitudinal controller interface.
