"""Simulated 2-D laser scanner for toy_robot.

Uses ~/scan as the output topic so the node can be reused without
namespace collision.  The bringup launch file remaps ~/scan to /scan.
"""

import math

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import LaserScan


class SensorNode(Node):
    _NUM_READINGS = 360

    def __init__(self):
        super().__init__("sensor")

        self.declare_parameter("frame_id", "laser_link")
        self.declare_parameter("range_max", 10.0)

        self._frame_id = self.get_parameter("frame_id").get_parameter_value().string_value
        self._range_max = self.get_parameter("range_max").get_parameter_value().double_value

        self._pub = self.create_publisher(LaserScan, "~/scan", 10)
        self._timer = self.create_timer(0.1, self._publish_scan)

    def _publish_scan(self) -> None:
        msg = LaserScan()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self._frame_id
        msg.angle_min = -math.pi
        msg.angle_max = math.pi
        msg.angle_increment = 2.0 * math.pi / self._NUM_READINGS
        msg.scan_time = 0.1
        msg.range_min = 0.1
        msg.range_max = self._range_max
        msg.ranges = [1.0] * self._NUM_READINGS
        self._pub.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = SensorNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()
