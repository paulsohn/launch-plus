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

## Unverified interfaces (source not in workspace)

### ublox_gps_node
- **Package**: `ublox_gps`
- **File**: `ublox_gps/ublox_gps_node.yaml`
- **Issue**: Source was not present under `src/sensor_component/` at time of writing.
  Interface was derived from known `ublox_gps` ROS 2 conventions — not verified against C++ source.
- **Action needed**: Locate source (e.g. from upstream `ublox_ros` repo) and verify or correct
  topic names and types.
