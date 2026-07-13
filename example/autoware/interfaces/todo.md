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

## Runtime-configurable topic monitors

### TopicStateMonitorNode
- **Package**: `autoware_topic_state_monitor`
- **File**: `autoware_topic_state_monitor/TopicStateMonitorNode.yaml`
- **Resolved**: Subscription topic name is expressed via `name_expr: "params['topic']"`. The
  message type (`topic_type` param) is still dynamic and cannot be expressed in the current
  `msg_type` field; the entry omits `msg_type`.

### processing_time_checker_node
- **Package**: `autoware_processing_time_checker`
- **File**: `autoware_processing_time_checker/processing_time_checker_node.yaml`
- **Issue**: Subscriptions are created in a loop over the `processing_time_topic_name_list`
  parameter (a `list[str]`). Each entry becomes a subscription to
  `autoware_internal_debug_msgs/msg/Float64Stamped`.
- **Workaround**: Only the static `~/metrics` publisher is recorded.
- **Possible fix**: Same `dynamic_subscriptions` extension.

### autoware_pipeline_latency_monitor_node
- **Package**: `autoware_pipeline_latency_monitor`
- **File**: `autoware_pipeline_latency_monitor/autoware_pipeline_latency_monitor_node.yaml`
- **Issue**: Subscriptions are created from the `processing_steps.sequence` parameter (a
  `list[str]`). For each step, the topic name and type are declared in sub-parameters
  `processing_steps.<step>.topic` and `processing_steps.<step>.topic_type`.
  Supported types are `autoware_internal_debug_msgs/msg/Float64Stamped` and
  `autoware_planning_validator/msg/PlanningValidatorStatus`.
- **Workaround**: Only the static `~/output/total_latency_ms` publisher and `/diagnostics` are recorded.
- **Possible fix**: Same `dynamic_subscriptions` extension.

## Fully dynamic topic nodes (topic from CLI args, not params)

### topic_tools/relay
- **Package**: `topic_tools`
- **File**: `topic_tools/relay.yaml`
- **Issue**: Input and output topic names are positional CLI `args`, not ROS parameters.
  They cannot be determined from params at all. Only a stub is recorded.
- **Workaround**: Empty connections list; relay instances are identified via node name in the launch file.
- **Possible fix**: Provide instance-level override in the plugin or parse `args` from the launch XML.

## Skipped (visualization tools with no static roscope connections)

- `rviz2` — subscribes to a user-configurable set of display topics; not covered.

## Dynamic topics from parameter lists (extension packages)

### BEVFusionNode
- **Package**: `autoware_bevfusion`
- **File**: `autoware_bevfusion/BEVFusionNode.yaml`
- **Issue**: `~/input/image{i}` and `~/input/camera_info{i}` (i = 0..num_cameras-1); count from `num_cameras` TensorRT config. Only index 0 recorded.

### StreamPetrNode
- **Package**: `autoware_camera_streampetr`
- **File**: `autoware_camera_streampetr/StreamPetrNode.yaml`
- **Issue**: `~/input/camera{i}/camera_info` and `~/input/camera{i}/image` (i = 0..rois_number-1); count from `rois_number` param. Only index 0 recorded.

### BboxObjectLocatorNode
- **Package**: `autoware_image_object_locator`
- **File**: `autoware_image_object_locator/BboxObjectLocatorNode.yaml`
- **Issue**: Per-ROI camera_info, ROI subscribers, and output publishers created for each entry in `rois_ids` param. Topic names configurable per camera. Only index 0 recorded.

### Image projection fusion nodes (all)
- **Package**: `autoware_image_projection_based_fusion`
- **Files**: `RoiClusterFusionNode.yaml`, `RoiDetectedObjectFusionNode.yaml`, `RoiPointCloudFusionNode.yaml`, `PointPaintingFusionNode.yaml`, `SegmentationPointCloudFusionNode.yaml`
- **Issue**: `input/rois{i}` and `input/camera_info{i}` (i = 0..rois_number-1); count from `rois_number`. Only index 0 recorded.

### SimpleDetectedObjectMergerNode / SimpleTrackedObjectMergerNode
- **Package**: `autoware_simple_object_merger`
- **Files**: `SimpleDetectedObjectMergerNode.yaml`, `SimpleTrackedObjectMergerNode.yaml`
- **Issue**: Input topics entirely from `input_topics` string array param. No static topic names; YAML has placeholder comment only.

### CudaPointCloudConcatenateDataSynchronizerComponent
- **Package**: `autoware_cuda_pointcloud_preprocessor`
- **File**: `autoware_cuda_pointcloud_preprocessor/CudaPointCloudConcatenateDataSynchronizerComponent.yaml`
- **Issue**: Each entry in `input_topics` (vector<string>) becomes a CudaPointCloud2 subscription. Only fixed `output` and `output_info` publishers recorded.

### CalibrationStatusClassifierNode
- **Package**: `autoware_calibration_status_classifier`
- **File**: `autoware_calibration_status_classifier/CalibrationStatusClassifierNode.yaml`
- **Issue**: `input.cloud_topics` (vector<string>) → PointCloud2 subs; `input.image_topics` (vector<string>) → Image subs; msg types for velocity/angular velocity/objects subs vary by source parameter. Array subs omitted.

### VadNode
- **Package**: `autoware_tensorrt_vad`
- **File**: `autoware_tensorrt_vad/VadNode.yaml`
- **Issue**: `~/input/image<i>` and `~/input/camera_info<i>` (i = 0..num_cameras-1) from `node_params.num_cameras`. Only static entries recorded.

## Dynamic topics from runtime config or CLI args (extension packages)

### autoware_carla_interface (main node)
- **Package**: `autoware_carla_interface`
- **File**: `autoware_carla_interface/autoware_carla_interface.yaml`
- **Issue**: Sensor publishers (Image, CameraInfo, PointCloud2, Imu, etc.) created from a runtime sensor kit config file. Only vehicle interface topics recorded.

### multi_camera_combiner
- **Package**: `autoware_carla_interface`
- **File**: `autoware_carla_interface/multi_camera_combiner.yaml`
- **Issue**: Camera subscriptions from a runtime list parameter. Only combined output publisher recorded.

### TopicRelayController (generic mode)
- **Package**: `autoware_topic_relay_controller`
- **File**: `autoware_topic_relay_controller/TopicRelayController.yaml`
- **Partially resolved**: Generic-mode topic and remap_topic names are now expressed via
  `name_expr:` with `when:` guards. The message type (`topic_type` param) cannot be expressed
  in the current `msg_type` field; generic-mode entries omit `msg_type`.

### ConverterNode
- **Package**: `autoware_scenario_simulator_v2_adapter`
- **File**: `autoware_scenario_simulator_v2_adapter/ConverterNode.yaml`
- **Issue**: Metric topic subscriptions from a runtime parameter list; `UserDefinedValue` publishers with runtime-derived names from metric fields. Only `/diagnostics` subscription recorded.

### ControlCmdGate
- **Package**: `autoware_control_command_gate`
- **File**: `autoware_control_command_gate/ControlCmdGate.yaml`
- **Issue**: Per-source input topics `~/inputs/<name>/control`, `~/inputs/<name>/gear`, etc., where names come from `inputs_names.<id>` parameters. Only static outputs and vehicle status subscriptions recorded.

### PathToTrajectory
- **Package**: `autoware_planning_topic_converter`
- **File**: `autoware_planning_topic_converter/PathToTrajectory.yaml`
- **Resolved**: Both `input_topic` and `output_topic` are now expressed via `name_expr:`.

## eagleye_rt — argument-driven topic selection

Many eagleye_rt executables select topic names via argv[1] ("1st"/"2nd"/"3rd") or parameters:
- `heading`, `heading_interpolate`, `yaw_rate_offset`: argv[1] selects which order's pub/sub topics — SKIP
- `rtk_heading`, `rtk_dead_reckoning`, `height`, `smoothing`, `velocity_estimator`, `slip_coefficient`,
  `velocity_scale_factor`: topic names are C++ local variables loaded from a YAML config file, not
  declared as ROS parameters — cannot be expressed via `name_expr:`. YAML files retain static defaults.
- `tf_converted_imu`: `imu_topic` and `publish_imu_topic` params expressed via `name_expr:` — **resolved**
- `twist_relay`: `twist.twist_topic` param expressed via `name_expr:` for subscription;
  publisher `vehicle/twist` is hardcoded — **partially resolved**
- `monitor`: `rtklib_nav_topic`, `gga_topic`, `monitor.comparison_twist_topic` params expressed
  via `name_expr:` — **resolved**
- `slip_coefficient`: no ROS publishers (results written to file)

## Unverified interfaces (source not in workspace)

### ublox_gps_node
- **Package**: `ublox_gps`
- **File**: `ublox_gps/ublox_gps_node.yaml`
- **Issue**: Source was not present under `src/sensor_component/` at time of writing.
  Interface was derived from known `ublox_gps` ROS 2 conventions — not verified against C++ source.
- **Action needed**: Locate source (e.g. from upstream `ublox_ros` repo) and verify or correct
  topic names and types.
