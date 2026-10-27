#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect.launch.py —— 起 camera_node + detector_node 两个进程。

    ros2 launch webrtc_detect detect.launch.py
    ros2 launch webrtc_detect detect.launch.py camera_source:=usb
    ros2 launch webrtc_detect detect.launch.py detect_fps:=10.0

WebRTC 那一端**不在这里**：它是 `MODE=ros tools/service.sh restart agent`
（或 tools/run-agent.sh）。两边是独立进程 —— 节点重启不影响推流会话，
推流重连也不用重启节点。

**PYTHONPATH**：两个节点都要 import 仓库 src/ 里的 sources.py / camera.py /
whitebalance.py / pipeline.py。这里把仓库的 src/ 加到子进程的 PYTHONPATH 上，
位置按下面的顺序取：

  1. 环境变量 WEBRTC_REPO_ROOT（tools/run-ros-nodes.sh 会设）；
  2. launch 文件自己的位置往上一路推（配合 colcon build --symlink-install）。

两条都拿不到时会打一行警告，节点会因为 import sources 失败而退出 —— 那种
报错比"画面一直黑"好查得多。**推荐直接用 tools/run-ros-nodes.sh**，它会把
这两件事都安排好。
"""

import os
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, SetEnvironmentVariable
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def _repo_src():
    """仓库 src/ 的绝对路径。"""
    root = os.environ.get("WEBRTC_REPO_ROOT")
    if not root:
        # launch/detect.launch.py → webrtc_detect → src → ros2_ws → 仓库根
        root = str(Path(__file__).resolve().parents[4])
    src = os.path.join(root, "src")
    if not os.path.isfile(os.path.join(src, "sources.py")):
        print("[detect.launch] 警告：%s 里没有 sources.py。节点会 import 失败 —— "
              "用 tools/run-ros-nodes.sh 启动，或设置 WEBRTC_REPO_ROOT。" % src)
    return src


def _arg(name, default, description):
    return DeclareLaunchArgument(name, default_value=str(default),
                                 description=description)


def generate_launch_description():
    src = _repo_src()
    old = os.environ.get("PYTHONPATH", "")
    pythonpath = src + ((os.pathsep + old) if old else "")

    # 子进程的 PYTHONPATH：必须在 Node 之前执行。Node 进程在启动时才读
    # env，所以 SetEnvironmentVariable 放在 LaunchDescription 的最前面就够了。
    set_pythonpath = SetEnvironmentVariable("PYTHONPATH", pythonpath)

    args = [
        _arg("camera_source", "mipi",
             "启动用哪一路摄像头（mipi/usb）。运行期用 /camera/select 切，"
             "由 camera_node 重开设备并回报"),
        _arg("width", 1280, "采集宽度。**必须和 device_agent 的 --width 一致**"),
        _arg("height", 720, "采集高度。必须和 device_agent 的 --height 一致"),
        _arg("fps", 30, "采集帧率"),
        _arg("camera_wb", "auto",
             "白平衡：auto = 自校准（灰世界，前 15 帧）；\"R,B\" 手动；空 = 不校正。"
             "只对 MIPI 这类 raw 源生效，USB 自带 AWB 会跳过"),
        _arg("camera_decoder", "sw",
             "USB(MJPG) 的解码器：sw = jpegdec 软件解码（默认）；mpp = mppjpegdec "
             "硬解。这块板子的 mppjpegdec 解 720p MJPG 会慢到 1.5 秒/帧且不报错，"
             "所以默认软件；换板子/换版本确认 MPP 正常了再用 mpp 省 CPU"),
        _arg("model", "/userdata/rknn_yolov5_demo/model/RK3588/yolov5s-640-640.rknn",
             "YOLOv5s 的 .rknn 路径（模型不入库）"),
        _arg("detect_fps", 5.0, "推理频率。视频帧率仍是 fps，中间帧复用上一次的框"),
        _arg("conf", 0.35, "person 的置信度阈值"),
        _arg("iou", 0.45, "NMS 的 IoU 阈值"),
        _arg("image_topic", "/camera/image_raw", "camera_node 发布原图的话题"),
        _arg("annotated_topic", "/detect/image_annotated",
             "detector_node 发布标注图的话题（WebRTC 订阅的是这个）"),
        _arg("persons_topic", "/detect/persons",
             "检测结果 JSON 话题（std_msgs/String）"),
        _arg("select_topic", "/camera/select", "切源请求话题"),
        _arg("source_topic", "/camera/source", "当前源回报话题（latched）"),
    ]

    camera = Node(
        package="webrtc_detect", executable="camera_node", name="camera_node",
        output="screen",
        parameters=[{
            "camera_source": ParameterValue(
                LaunchConfiguration("camera_source"), value_type=str),
            "width": ParameterValue(LaunchConfiguration("width"), value_type=int),
            "height": ParameterValue(LaunchConfiguration("height"), value_type=int),
            "fps": ParameterValue(LaunchConfiguration("fps"), value_type=int),
            "camera_wb": ParameterValue(
                LaunchConfiguration("camera_wb"), value_type=str),
            "camera_decoder": ParameterValue(
                LaunchConfiguration("camera_decoder"), value_type=str),
            "image_topic": ParameterValue(
                LaunchConfiguration("image_topic"), value_type=str),
            "select_topic": ParameterValue(
                LaunchConfiguration("select_topic"), value_type=str),
            "source_topic": ParameterValue(
                LaunchConfiguration("source_topic"), value_type=str),
        }],
    )

    detector = Node(
        package="webrtc_detect", executable="detector_node", name="detector_node",
        output="screen",
        parameters=[{
            "image_topic": ParameterValue(
                LaunchConfiguration("image_topic"), value_type=str),
            "annotated_topic": ParameterValue(
                LaunchConfiguration("annotated_topic"), value_type=str),
            "persons_topic": ParameterValue(
                LaunchConfiguration("persons_topic"), value_type=str),
            "model": ParameterValue(LaunchConfiguration("model"), value_type=str),
            "detect_fps": ParameterValue(
                LaunchConfiguration("detect_fps"), value_type=float),
            "conf": ParameterValue(LaunchConfiguration("conf"), value_type=float),
            "iou": ParameterValue(LaunchConfiguration("iou"), value_type=float),
        }],
    )

    return LaunchDescription([set_pythonpath] + args + [camera, detector])
