import os
from glob import glob

from setuptools import setup

package_name = "webrtc_detect"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages",
         ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        # launch 文件也装进去：ros2 launch webrtc_detect detect.launch.py
        (os.path.join("share", package_name, "launch"),
         glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Ge Tie",
    maintainer_email="tieg77@163.com",
    description="camera_node + detector_node：图像走 ROS 话题，WebRTC 端订阅标注图",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "camera_node = webrtc_detect.camera_node:main",
            "detector_node = webrtc_detect.detector_node:main",
        ],
    },
)
