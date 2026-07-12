"""Differential drive controller for toy_robot.

Subscribes to /cmd_vel and publishes /joint_states, /odom, and /tf.
"""

import math

import rclpy
from geometry_msgs.msg import TransformStamped, Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from sensor_msgs.msg import JointState
from tf2_ros import TransformBroadcaster


class ControllerNode(Node):
    def __init__(self):
        super().__init__("controller")

        self.declare_parameter("wheel_radius", 0.05)
        self.declare_parameter("wheel_separation", 0.2)

        self._r = self.get_parameter("wheel_radius").get_parameter_value().double_value
        self._d = self.get_parameter("wheel_separation").get_parameter_value().double_value

        self._cmd_sub = self.create_subscription(Twist, "/cmd_vel", self._cmd_vel_cb, 10)
        self._js_pub = self.create_publisher(JointState, "/joint_states", 10)
        self._odom_pub = self.create_publisher(Odometry, "/odom", 10)
        self._tf_broadcaster = TransformBroadcaster(self)

        self._vx = 0.0
        self._wz = 0.0
        self._left_pos = 0.0
        self._right_pos = 0.0
        self._x = 0.0
        self._y = 0.0
        self._theta = 0.0
        self._last_stamp = self.get_clock().now()

        self._timer = self.create_timer(0.05, self._update)

    def _cmd_vel_cb(self, msg: Twist) -> None:
        self._vx = msg.linear.x
        self._wz = msg.angular.z

    def _update(self) -> None:
        now = self.get_clock().now()
        dt = (now - self._last_stamp).nanoseconds * 1e-9
        self._last_stamp = now

        vl = (self._vx - self._wz * self._d / 2.0) / self._r
        vr = (self._vx + self._wz * self._d / 2.0) / self._r
        self._left_pos += vl * dt
        self._right_pos += vr * dt

        self._x += self._vx * math.cos(self._theta) * dt
        self._y += self._vx * math.sin(self._theta) * dt
        self._theta += self._wz * dt

        stamp = now.to_msg()

        js = JointState()
        js.header.stamp = stamp
        js.name = ["left_wheel_joint", "right_wheel_joint"]
        js.position = [self._left_pos, self._right_pos]
        js.velocity = [vl, vr]
        self._js_pub.publish(js)

        odom = Odometry()
        odom.header.stamp = stamp
        odom.header.frame_id = "odom"
        odom.child_frame_id = "base_link"
        odom.pose.pose.position.x = self._x
        odom.pose.pose.position.y = self._y
        odom.pose.pose.orientation.z = math.sin(self._theta / 2.0)
        odom.pose.pose.orientation.w = math.cos(self._theta / 2.0)
        odom.twist.twist.linear.x = self._vx
        odom.twist.twist.angular.z = self._wz
        self._odom_pub.publish(odom)

        tf = TransformStamped()
        tf.header.stamp = stamp
        tf.header.frame_id = "odom"
        tf.child_frame_id = "base_link"
        tf.transform.translation.x = self._x
        tf.transform.translation.y = self._y
        tf.transform.rotation.z = math.sin(self._theta / 2.0)
        tf.transform.rotation.w = math.cos(self._theta / 2.0)
        self._tf_broadcaster.sendTransform(tf)


def main(args=None):
    rclpy.init(args=args)
    node = ControllerNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()
