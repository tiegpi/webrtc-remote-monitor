#!/bin/bash
# 板子侧发布端。信令服务器在 VPS 上，经 nginx 443 反代（wss）。
#
#     tools/run-agent.sh            正常启动（monitor 模式）
#     tools/run-agent.sh --gop 30   追加/覆盖参数（后面的同名参数生效）
#
# 两种运行模式（--mode，也可以用环境变量 MODE 选）：
#
#   monitor  采集后直接交给硬件 H.264 编码：推流 + 双向语音 + 遥操作命令台。
#            这是阶段一/二/三的行为，默认值。
#
#   ros      图像走 ROS 2 话题总线：camera_node 采集、detector_node 推理，本进程
#            只订阅 /detect/image_annotated 并推给浏览器。切源、检测统计都走
#            ROS 话题（见 ros2_ws/ 和 README「ROS 模式」）。
#            本模式要 source /opt/ros/humble/setup.bash（下面会替你 source）。
#
# **--gop 不用在这儿写**：device_agent.py 的默认值就是 10，而整条验收线都压在
# 那个值上（中途加入的浏览器、切换摄像头都要等下一个 IDR；60 会让
# analyze_diag.py 的"画面静止时间占比"超上限）。要改就显式传，别改默认值。
#
# **用 bash -l 起**（`nohup bash -l tools/run-agent.sh`）：板子 BSP 的
# GStreamer/MPP 环境变量只在登录 shell 里导出。其中 GST_V4L2_USE_LIBV4L2=1
# 会让 rkisp 分配缓冲失败，pipeline.py 在导入时会把它清掉（必须早于
# Gst.init()），所以这里照常用登录 shell 没问题。
#
# 画质那两件事都不在 pipeline 串里，参数在这儿：
#
#   --camera-wb auto   板子上没有 rkaiq 3A，ISP 不跑 AWB，画面整体偏绿
#                      （当前这间屋子实测 G/R=1.71、G/B=1.55）。这个开关架一条
#                      appsink→numpy→appsrc 的桥，拿前 15 帧做灰世界校准，
#                      之后增益冻结。启动时要让镜头对着白墙/灰一点的地方，
#                      灰世界会把画面平均色当成"本该是灰的"。
#
#   亮度**不用写参数**：device_agent 启动时会自己闭环（板子没 AE），
#   量一帧、按目标亮度算增益、写回 sensor subdev。这颗 sensor 在当前这间
#   屋子里最多只能到 Y≈78（增益和曝光都顶到上限了），到顶会打告警。
#
# ROOM 必须和 tools/run-web.sh 的 ROOM 一致，见那个文件的说明。
#
# **凭据从 .env 读，不写在本文件里。** 房间号本身就是访问控制（信令服务器只认
# 它那一个房间），加上 token，两者一起进仓库等于把摄像头公开。.env 已 gitignore，
# 模板见 .env.example。也可以直接 export 覆盖，适合临时换房间调试。
set -euo pipefail

# 脚本在 tools/ 下，项目根是它的上一级。写成 dirname 而不是 ~/...，
# 这样从哪儿调起都对，本地也能直接跑。
cd "$(dirname "$0")/.."

if [ -f .env ]; then . ./.env; fi
: "${ROOM:?没设 ROOM —— cp .env.example .env 填上，或 export ROOM=...}"
: "${TOKEN:?没设 TOKEN —— cp .env.example .env 填上，或 export TOKEN=...}"
: "${SIGNAL_HOST:?没设 SIGNAL_HOST —— 信令服务器主机名，见 .env.example}"
# 取 /ice.json（TURN 配置）和 POST /diag（字节守恒遥测）时用；推流那条
# WebSocket 仍然只认 TOKEN，不要混用。
: "${SIGNAL_AUTH:?没设 SIGNAL_AUTH（user:pass）—— 见 .env.example}"

# 运行模式。给默认值而不是强制要求：只想跑 monitor 的人不该为了一个不用的
# 开关去改 .env。
MODE="${MODE:-monitor}"
CAMERA_SOURCE="${CAMERA_SOURCE:-}"
ROS_IMAGE_TOPIC="${ROS_IMAGE_TOPIC:-}"
ROS_PERSONS_TOPIC="${ROS_PERSONS_TOPIC:-}"
ROS_SELECT_TOPIC="${ROS_SELECT_TOPIC:-}"
ROS_SOURCE_TOPIC="${ROS_SOURCE_TOPIC:-}"

case "$MODE" in
  monitor|ros) ;;
  *) echo "MODE 只能是 monitor 或 ros，收到：$MODE" >&2; exit 2 ;;
esac

ARGS=(
  --signal "wss://$SIGNAL_HOST/ws/publish"
  --room "$ROOM"
  --token "$TOKEN"
  --signal-auth "$SIGNAL_AUTH"
  --tls-ca certs/vps-self.crt
  --camera-wb auto
  --mode "$MODE"
)
if [ -n "$CAMERA_SOURCE" ]; then ARGS+=(--camera-source "$CAMERA_SOURCE"); fi
if [ -n "$ROS_IMAGE_TOPIC" ]; then ARGS+=(--ros-image-topic "$ROS_IMAGE_TOPIC"); fi
if [ -n "$ROS_PERSONS_TOPIC" ]; then ARGS+=(--ros-persons-topic "$ROS_PERSONS_TOPIC"); fi
if [ -n "$ROS_SELECT_TOPIC" ]; then ARGS+=(--ros-select-topic "$ROS_SELECT_TOPIC"); fi
if [ -n "$ROS_SOURCE_TOPIC" ]; then ARGS+=(--ros-source-topic "$ROS_SOURCE_TOPIC"); fi

# **ros 模式必须 source ROS 环境。** rclpy / sensor_msgs 在
# /opt/ros/humble/lib/python3.x/site-packages 里，不 source 就是 ImportError，
# 而那条报错看起来像"没装 ROS"。这里替调用者 source，和服务脚本无关。
#
# **也必须用 python3 -s。** 板子的系统 numpy 是 1.21.5，用户目录里还装着一个
# numpy 2.x，默认会盖住系统版本；ROS 模式的 camera_node 要 import cv2
# （python3-opencv 按 1.x 编的），numpy 2.x 会让它直接抛 ABI 报错。
# -s（或 PYTHONNOUSERSITE=1）不加载用户目录。monitor 模式不加 -s，
# 保持和以前完全一样。
PY=(python3)
if [ "$MODE" = "ros" ]; then
  if [ -f /opt/ros/humble/setup.bash ]; then
    # ROS 的 setup.bash 里有裸变量引用（AMENT_TRACE_SETUP_FILES），在
    # `set -u` 下 source 会报 unbound variable 直接退出 —— 前后临时关掉。
    set +u
    # shellcheck disable=SC1091
    . /opt/ros/humble/setup.bash
    set -u
  else
    echo "找不到 /opt/ros/humble/setup.bash —— ROS 模式需要 ROS 2 Humble" >&2
    exit 2
  fi
  PY=(python3 -s)
fi

exec "${PY[@]}" src/device_agent.py "${ARGS[@]}" "$@"
