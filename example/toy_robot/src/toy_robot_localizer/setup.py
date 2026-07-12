from setuptools import find_packages, setup

setup(
    name="toy_robot_localizer",
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/toy_robot_localizer"]),
        ("share/toy_robot_localizer", ["package.xml"]),
        ("share/toy_robot_localizer/interface", ["interface/localizer_node.yaml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    entry_points={
        "console_scripts": [
            "localizer_node = toy_robot_localizer.localizer_node:main",
        ],
    },
)
