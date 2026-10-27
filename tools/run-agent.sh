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
#   detect   轻量化直连：采集后先 OpenCV 预处理、RKNN 推理，把人框**烧进画面**，
#            再硬件编码推流。适合简单远程监控，不依赖机器人框架。
#            依赖 numpy + OpenCV + rknn-toolkit-lite2（见 README「detect 模式」）。
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
#                      （当前这间屋子实测 G/R=1.71、G/B=1.55）。monitor 模式
#                      架一条 appsink→numpy→appsrc 的桥，拿前 15 帧做灰世界
#                      校准；detect 模式在 BGR 帧上做同一套校正（只对 MIPI
#                      生效，USB 自带 AWB 会跳过）。启动时要让镜头对着白墙/
#                      灰一点的地方，灰世界会把画面平均色当成"本该是灰的"。
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

# 运行模式与 detect 参数。给默认值而不是强制要求：只想跑 monitor 的人
# 不该为了一个不用的开关去改 .env。
MODE="${MODE:-monitor}"
CAMERA_SOURCE="${CAMERA_SOURCE:-}"
DETECT_MODEL="${DETECT_MODEL:-}"
DETECT_FPS="${DETECT_FPS:-}"
DETECT_CONF="${DETECT_CONF:-}"
DETECT_IOU="${DETECT_IOU:-}"

case "$MODE" in
  monitor|detect) ;;
  *) echo "MODE 只能是 monitor 或 detect，收到：$MODE" >&2; exit 2 ;;
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
if [ -n "$DETECT_MODEL" ]; then ARGS+=(--detect-model "$DETECT_MODEL"); fi
if [ -n "$DETECT_FPS" ]; then ARGS+=(--detect-fps "$DETECT_FPS"); fi
if [ -n "$DETECT_CONF" ]; then ARGS+=(--detect-conf "$DETECT_CONF"); fi
if [ -n "$DETECT_IOU" ]; then ARGS+=(--detect-iou "$DETECT_IOU"); fi

# **detect 模式必须用 python3 -s。** 板子的 /usr/lib/python3/dist-packages 里
# 是 numpy 1.21.5，而 python3-opencv（4.5.4）正是按那个 ABI 编的；用户目录
# (~/.local/lib/python3.11/site-packages) 里还装着一个 numpy 2.2.6，默认会
# 盖住系统版本，于是 import cv2 直接抛
#   "A module that was compiled using NumPy 1.x cannot be run in NumPy 2.x"。
# -s（或 PYTHONNOUSERSITE=1）不加载用户目录，两个版本就都对上了。monitor
# 模式不加 -s，保持和以前完全一样，免得改变现有部署的行为。
PY=(python3)
if [ "$MODE" = "detect" ]; then
  PY=(python3 -s)
fi

exec "${PY[@]}" src/device_agent.py "${ARGS[@]}" "$@"
