# Interface TODOs

Cases that cannot be fully expressed in the current YAML convention and need revisiting.

## Dynamic subscriptions (topic names from a parameter list)

The current format assumes a fixed set of topic names (with optional `when:` conditions).
Nodes whose subscriptions are determined at runtime from a parameter of type `list[str]`
cannot be fully represented.

### PointCloudConcatenateDataSynchronizerComponent
- **Package**: `autoware_pointcloud_preprocessor`
- **File**: `autoware_pointcloud_preprocessor/PointCloudConcatenateDataSynchronizerComponent.yaml`
- **Issue**: Input subscriptions are created in a loop over the `input_topics` parameter
  (a `list[str]` of topic names set in the launch file). The number and names of topics
  are not known statically.
- **Workaround**: Only the two output publishers are recorded; input subscriptions are absent.
- **Possible fix**: Extend the plugin format with a `dynamic_subscriptions` key that
  evaluates a Python expression returning a list of `{topic, type, ...}` dicts.

### MultiObjectTrackerNode (multi_object_tracker_node)
- **Package**: `autoware_multi_object_tracker`
- **File**: `autoware_multi_object_tracker/multi_object_tracker_node.yaml`
- **Issue**: Detection input subscriptions are created in a loop over up to 12 parameterized
  slots (`input/detection01` … `input/detection12`). Each slot's topic name is itself a
  parameter (`input/detectionNN/objects`), and a slot is only subscribed when its
  `input/detectionNN/channel` parameter is not `"none"` or empty.
  The YAML records only the conventional channel 01 entry as a representative placeholder.
- **Workaround**: Only channel 01 recorded; channels 02–12 and odometry sub omitted from static list.
- **Possible fix**: Same `dynamic_subscriptions` extension as PointCloudConcatenateDataSynchronizerComponent.

### TrafficLightMultiCameraFusionNode (traffic_light_multi_camera_fusion_node)
- **Package**: `autoware_traffic_light_multi_camera_fusion`
- **File**: `autoware_traffic_light_multi_camera_fusion/traffic_light_multi_camera_fusion_node.yaml`
- **Issue**: Per-camera subscriptions are created in a loop over the `camera_namespaces`
  parameter (a `list[str]`). For each entry N, three topics are subscribed:
  `<N>/camera_info`, `<N>/detection/rois`, `<N>/classification/traffic_signals`.
  The number of cameras is not known statically.
- **Workaround**: Only the static `~/input/vector_map` subscription and `~/output/traffic_signals`
  publisher are recorded; all per-camera subscriptions are absent.
- **Possible fix**: Same `dynamic_subscriptions` extension.

### PlanningEvaluatorNode (planning_evaluator)
- **Package**: `autoware_planning_evaluator`
- **File**: `autoware_planning_evaluator/planning_evaluator.yaml`
- **Issue**: PlanningFactor subscriptions are created in a loop over the
  `stop_decision.module_list` parameter (a `list[str]`). Each subscription topic
  is `stop_decision.topic_prefix + module_name`. The number and names of topics
  are not known statically.
- **Workaround**: Only static connections are recorded; dynamic PlanningFactorArray
  subscriptions are absent (noted as a comment in the YAML).
- **Possible fix**: Same `dynamic_subscriptions` extension as PointCloudConcatenateDataSynchronizerComponent.

### ControlEvaluatorNode (control_evaluator)
- **Package**: `autoware_control_evaluator`
- **File**: `autoware_control_evaluator/control_evaluator.yaml`
- **Issue**: PlanningFactor subscriptions are created in a loop over
  `planning_factor_metrics.stop_deviation.module_list` (a `list[str]`). Each subscription
  topic is `planning_factor_metrics.topic_prefix + module_name`. The default prefix is
  `/planning/planning_factors/` and the default modules are: blind_spot, crosswalk,
  detection_area, intersection, merge_from_private, no_drivable_lane, no_stopping_area,
  stop_line, traffic_light, virtual_traffic_light, walkway. The exact set is a runtime param.
- **Workaround**: Static entries omitted; noted as comment in the YAML.
- **Possible fix**: Same `dynamic_subscriptions` extension.

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
  backend for `ObstacleCruiseModule`.

## Dynamic publishers injected by loaded modules (topic names contain module name at runtime)

### BehaviorPathPlannerNode — per-module processing-time debug publishers
- **Package**: `autoware_behavior_path_planner`
- **File**: `autoware_behavior_path_planner/BehaviorPathPlannerNode.yaml`
- **Issue**: `PlannerManager` creates a `DebugPublisher` with namespace `~/debug` and publishes
  `~/debug/<module_name>` (Float64Stamped or similar) for each loaded module's per-iteration
  processing time. The module names are determined at runtime from the `launch_modules` parameter.
  There is also one entry per module (total_time) plus "total_time" itself. These are not recorded
  as static entries because there are too many permutations and the exact message type requires
  further verification.
- **Action needed**: Verify exact message type (`autoware_internal_debug_msgs/msg/Float64Stamped`
  or similar) and decide whether to add a `dynamic_publishers` key for these per-module topics.

## Unverified interfaces (source not in workspace)

### ublox_gps_node
- **Package**: `ublox_gps`
- **File**: `ublox_gps/ublox_gps_node.yaml`
- **Issue**: Source was not present under `src/sensor_component/` at time of writing.
  Interface was derived from known `ublox_gps` ROS 2 conventions — not verified against C++ source.
- **Action needed**: Locate source (e.g. from upstream `ublox_ros` repo) and verify or correct
  topic names and types.
