"""Goal-driven planner for toy_robot.

Subscribes to /goal_pose, /odom, and /pose, then drives the robot toward
the goal with a simple proportional heading + speed controller.
Publishes /cmd_vel for the controller and /plan for visualization.
"""

import math

import rclpy
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, Twist
from nav_msgs.msg import Odometry, Path
from rclpy.node import Node

_GOAL_TOLERANCE = 0.1  # metres
_MAX_LINEAR = 0.3  # m/s
_MAX_ANGULAR = 1.5  # rad/s


class PlannerNode(Node):
    def __init__(self):
        super().__init__("planner")

        self._goal: PoseStamped | None = None
        self._pose: PoseWithCovarianceStamped | None = None

        self._goal_sub = self.create_subscription(PoseStamped, "/goal_pose", self._goal_cb, 10)
        self._odom_sub = self.create_subscription(Odometry, "/odom", self._odom_cb, 10)
        self._pose_sub = self.create_subscription(
            PoseWithCovarianceStamped, "/pose", self._pose_cb, 10
        )

        self._cmd_pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self._plan_pub = self.create_publisher(Path, "/plan", 10)

        self._timer = self.create_timer(0.05, self._update)

    def _goal_cb(self, msg: PoseStamped) -> None:
        self._goal = msg
        self.get_logger().info(
            "New goal: (%.2f, %.2f)",
            msg.pose.position.x,
            msg.pose.position.y,
        )

    def _odom_cb(self, msg: Odometry) -> None:
        # Odom is used as a fallback pose source when the localizer has not
        # yet published a /pose estimate.
        if self._pose is None:
            p = PoseWithCovarianceStamped()
            p.header = msg.header
            p.pose.pose = msg.pose.pose
            self._pose = p

    def _pose_cb(self, msg: PoseWithCovarianceStamped) -> None:
        self._pose = msg

    def _update(self) -> None:
        if self._goal is None or self._pose is None:
            return

        px = self._pose.pose.pose.position.x
        py = self._pose.pose.pose.position.y
        gx = self._goal.pose.position.x
        gy = self._goal.pose.position.y

        dx = gx - px
        dy = gy - py
        dist = math.hypot(dx, dy)

        cmd = Twist()
        if dist > _GOAL_TOLERANCE:
            goal_heading = math.atan2(dy, dx)
            q = self._pose.pose.pose.orientation
            yaw = math.atan2(
                2.0 * (q.w * q.z + q.x * q.y),
                1.0 - 2.0 * (q.y**2 + q.z**2),
            )
            angle_err = (goal_heading - yaw + math.pi) % (2 * math.pi) - math.pi
            cmd.linear.x = min(_MAX_LINEAR, dist * 0.5)
            cmd.angular.z = max(-_MAX_ANGULAR, min(_MAX_ANGULAR, angle_err * 2.0))

        self._cmd_pub.publish(cmd)
        self._publish_plan(px, py)

    def _publish_plan(self, px: float, py: float) -> None:
        if self._goal is None:
            return
        path = Path()
        path.header.stamp = self.get_clock().now().to_msg()
        path.header.frame_id = self._goal.header.frame_id or "odom"

        start = PoseStamped()
        start.header = path.header
        start.pose.position.x = px
        start.pose.position.y = py
        start.pose.orientation.w = 1.0

        path.poses = [start, self._goal]
        self._plan_pub.publish(path)


def main(args=None):
    rclpy.init(args=args)
    node = PlannerNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()
