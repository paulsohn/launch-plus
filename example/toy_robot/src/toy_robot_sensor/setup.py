from setuptools import find_packages, setup

setup(
    name="toy_robot_sensor",
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/toy_robot_sensor"]),
        ("share/toy_robot_sensor", ["package.xml"]),
        ("share/toy_robot_sensor/interface", ["interface/sensor_node.yaml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    entry_points={
        "console_scripts": [
            "sensor_node = toy_robot_sensor.sensor_node:main",
        ],
    },
)
