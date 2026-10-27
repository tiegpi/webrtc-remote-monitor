#!/bin/bash
# 编 ROS 2 工作空间（ros2_ws/src/webrtc_detect）。
#
#     tools/build-ros-ws.sh                 # colcon build --symlink-install
#     tools/build-ros-ws.sh --packages-select webrtc_detect
#
# **--symlink-install 不是可选项**：这是 ament_python 包，源码改了要立刻生效，
# 否则每次改 camera_node 都要重编一遍。symlink 之后 install/ 里只是指向
# ros2_ws/src/ 的链接，launch 文件改完直接 ros2 launch 就是新的。
#
# 只编 Python 节点，不涉及 C++；板子上第一次跑大约十几秒。
#
# 编完会在 ros2_ws/install/setup.bash 里放环境脚本 —— 用 tools/run-ros-nodes.sh
# 启动会自动 source 它，不用手动。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WS="$ROOT/ros2_ws"

if [ ! -f /opt/ros/humble/setup.bash ]; then
    echo "找不到 /opt/ros/humble/setup.bash —— ROS 2 Humble 没装？" >&2
    exit 2
fi
# ROS 的 setup.bash 里有裸变量引用，在 `set -u` 下 source 会直接退出。
set +u
# shellcheck disable=SC1091
. /opt/ros/humble/setup.bash
set -u

if ! command -v colcon >/dev/null 2>&1; then
    echo "找不到 colcon：apt install python3-colcon-common-extensions" >&2
    exit 2
fi

cd "$WS"
# --symlink-install + ament_python 的已知组合：colcon 会为每个 Python 文件建链接，
# 日志噪音不少，但不影响使用。
colcon build --symlink-install "$@"

echo
echo "完成。接下来："
echo "  tools/run-ros-nodes.sh                    # 起 camera_node + detector_node"
echo "  MODE=ros tools/service.sh restart agent   # 起 WebRTC 推流端（订阅标注图）"
