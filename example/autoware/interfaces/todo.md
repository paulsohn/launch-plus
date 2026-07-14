# Interface TODOs

Cases that cannot be fully expressed in the current YAML convention and need revisiting.

## Launch file remaps that are stale or conditional

See [legacy.md](legacy.md) for remaps that appear in launch files but have no matching
source connection in the current codebase.

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

## eagleye_rt — argument-driven topic selection

Many eagleye_rt executables select topic names via argv[1] ("1st"/"2nd"/"3rd") or parameters:
- `heading`, `heading_interpolate`, `yaw_rate_offset`: argv[1] selects which order's pub/sub topics — SKIP
- `rtk_heading`, `rtk_dead_reckoning`, `height`, `smoothing`, `velocity_estimator`, `slip_coefficient`,
  `velocity_scale_factor`: topic names are C++ local variables loaded from a YAML config file, not
  declared as ROS parameters — cannot be expressed via `name_expr:`. YAML files retain static defaults.
