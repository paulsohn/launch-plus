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

## Unverified interfaces (source not in workspace)

### ublox_gps_node
- **Package**: `ublox_gps`
- **File**: `ublox_gps/ublox_gps_node.yaml`
- **Issue**: Source was not present under `src/sensor_component/` at time of writing.
  Interface was derived from known `ublox_gps` ROS 2 conventions — not verified against C++ source.
- **Action needed**: Locate source (e.g. from upstream `ublox_ros` repo) and verify or correct
  topic names and types.
