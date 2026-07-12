"""Scan-based localizer for toy_robot.

Subscribes to /scan and uses the TF tree to look up the current
odom→base_link transform, then publishes it as a /pose estimate.
In a real system this would run a particle filter or ICP against a map.
"""

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from rclpy.node import Node
from sensor_msgs.msg import LaserScan
from tf2_ros import Buffer, TransformListener


class LocalizerNode(Node):
    def __init__(self):
        super().__init__("localizer")

        self._tf_buffer = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self)

        self._scan_sub = self.create_subscription(LaserScan, "/scan", self._scan_cb, 10)
        self._pose_pub = self.create_publisher(PoseWithCovarianceStamped, "/pose", 10)

    def _scan_cb(self, msg: LaserScan) -> None:
        try:
            tf = self._tf_buffer.lookup_transform("odom", "base_link", rclpy.time.Time())
        except Exception:
            return

        pose = PoseWithCovarianceStamped()
        pose.header.stamp = self.get_clock().now().to_msg()
        pose.header.frame_id = "odom"
        pose.pose.pose.position.x = tf.transform.translation.x
        pose.pose.pose.position.y = tf.transform.translation.y
        pose.pose.pose.orientation = tf.transform.rotation
        self._pose_pub.publish(pose)


def main(args=None):
    rclpy.init(args=args)
    node = LocalizerNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()
