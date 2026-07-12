from setuptools import find_packages, setup

setup(
    name="toy_robot_controller",
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/toy_robot_controller"]),
        ("share/toy_robot_controller", ["package.xml"]),
        ("share/toy_robot_controller/interface", ["interface/controller_node.yaml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    entry_points={
        "console_scripts": [
            "controller_node = toy_robot_controller.controller_node:main",
        ],
    },
)
