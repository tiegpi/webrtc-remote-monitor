#!/bin/bash
# 起 camera_node + detector_node（ROS 模式的"摄像头 + 检测"这一半）。
#
#     tools/run-ros-nodes.sh
#     tools/run-ros-nodes.sh camera_source:=usb
#     tools/run-ros-nodes.sh detect_fps:=10.0 width:=640 height:=480
#     CAMERA_SOURCE=mipi tools/run-ros-nodes.sh
#
# 另一半是 WebRTC 推流端，单独起：
#
#     MODE=ros tools/service.sh restart agent
#     # 或前台：MODE=ros tools/run-agent.sh
#
# 两边是**独立进程**，先后顺序不重要（推流端订阅 /detect/image_annotated，
# 节点还没起时只是没画面，节点起来就自动有了）。
#
# 参数透传给 launch，认这些：
#   camera_source  width  height  fps  camera_wb  camera_decoder  model
#   detect_fps  conf  iou
#   image_topic  annotated_topic  persons_topic  select_topic  source_topic
#   ros2 launch webrtc_detect detect.launch.py --show-args  看完整列表
#
# **width/height 必须和 device_agent 的一致**。推流端按 device_agent 的
# --width/--height 建 appsrc 的 caps，话题上的图尺寸对不上会被丢掉（日志里
# 是"尺寸不符"，不是黑屏）。agent 默认 1280x720，所以这里默认也是。
#
# 这个脚本会做三件环境上的事，缺一件都跑不起来：
#   1. source /opt/ros/humble/setup.bash —— rclpy / sensor_msgs；
#   2. source ros2_ws/install/setup.bash —— 编好的 webrtc_detect 包；
#   3. 把仓库 src/ 放进 PYTHONPATH —— 节点要 import sources/camera/whitebalance，
#      这套摄像头配置在仓库里，不在 ROS 包里（不重复维护第二份）。launch 文件
#      也会做这件事，这里显式设一遍是为了 ros2 run 时也一样。
#
# 没编过会直接提示去跑 tools/build-ros-ws.sh，而不是抛一个 ament 的堆栈。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -f /opt/ros/humble/setup.bash ]; then
    echo "找不到 /opt/ros/humble/setup.bash —— ROS 模式需要 ROS 2 Humble" >&2
    exit 2
fi
# ROS 的 setup.bash 里有裸变量引用（AMENT_TRACE_SETUP_FILES 之类），在
# `set -u` 下 source 会直接报 unbound variable 退出。前后临时关掉。
set +u
# shellcheck disable=SC1091
. /opt/ros/humble/setup.bash
set -u

if [ ! -f "$ROOT/ros2_ws/install/setup.bash" ]; then
    echo "ros2_ws 还没编过。先跑一次：" >&2
    echo "  tools/build-ros-ws.sh" >&2
    exit 2
fi
set +u
# shellcheck disable=SC1091
. "$ROOT/ros2_ws/install/setup.bash"
set -u

export WEBRTC_REPO_ROOT="$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# **必须的，不是保险**：板子的用户目录里有一个 numpy 2.x，会盖住系统的 1.21.5，
# 而 python3-opencv 是按 1.x 编的 —— 一旦被盖住，detector_node / camera_node
# 里 `import cv2` 直接抛 "numpy.core.multiarray failed to import"。
# `ros2 launch` 起的子进程用的是入口脚本的 shebang，**没法给它加 `-s`**，
# 所以这里用环境变量达到同样效果（只在本次启动的子进程里生效）。
export PYTHONNOUSERSITE=1

# 默认源：和 MONITOR 模式的默认一致（mipi），但可以用环境变量改，
# 省得每次都多敲一个参数。
if [ -n "${CAMERA_SOURCE:-}" ]; then
    set -- "camera_source:=$CAMERA_SOURCE" "$@"
fi

exec ros2 launch webrtc_detect detect.launch.py "$@"
