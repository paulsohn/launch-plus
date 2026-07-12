from setuptools import find_packages, setup

setup(
    name="toy_robot_planner",
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/toy_robot_planner"]),
        ("share/toy_robot_planner", ["package.xml"]),
        ("share/toy_robot_planner/interface", ["interface/planner_node.yaml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    entry_points={
        "console_scripts": [
            "planner_node = toy_robot_planner.planner_node:main",
        ],
    },
)
