#!/bin/bash
# check_ros_mode.sh —— ROS 模式的整链路验收（**板子上跑**，需要真摄像头 + NPU）
#
#     tools/check_ros_mode.sh
#     tools/check_ros_mode.sh --switch-to usb
#     tools/check_ros_mode.sh --settle 8 --wait-viewer 120
#     tools/check_ros_mode.sh --base https://192.168.2.187:8443   # 走 web_server
#
# 前提：
#   1. tools/run-ros-nodes.sh 在跑（camera_node + detector_node）；
#   2. MODE=ros tools/service.sh restart agent 在跑；
#   3. **有观看端在看页面** —— 没有会话时 agent 不建管线，也就没有 ros_frames
#      可看。没有浏览器就先用 tools/selftest.py --no-teleop 挂一个无头观看端。
#
# 判据都是**设备侧**的账（agent 的 GET /stats），不是"页面上看着有画面"：
#
#   ros_frames      订阅到的帧真的被推进了 appsrc     —— 话题通、尺寸对得上
#   video_packets_out  RTP 真的发出去了                —— 编码链路没断
#   ros_errors      0                                  —— 没有逐帧被丢掉
#   ros_source      = camera_node 回报的源             —— 切源真的落到设备上
#
# 单看任何一条都能骗人：帧在涨但 RTP 不动 = 编码器/webrtcbin 有问题；RTP 在涨
# 但 ros_frames 不动 = 画面早停在那了，浏览器还挂着最后一帧。所以两条都要涨。
#
# 退出码：0 全过；1 有判据没过；2 前置条件不满足（没起服务/没观看端）。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOCK="$ROOT/run/agent.sock"
BASE=""
SWITCH_TO=""
SETTLE=5
WAIT_VIEWER=60
KEEP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --base) BASE="${2:-}"; shift 2 ;;
        --sock) SOCK="${2:-}"; shift 2 ;;
        --switch-to) SWITCH_TO="${2:-}"; shift 2 ;;
        --settle) SETTLE="${2:-5}"; shift 2 ;;
        --wait-viewer) WAIT_VIEWER="${2:-60}"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h|--help) grep '^#' "$0" | sed -n '2,26p' | sed 's/^# \?//'; exit 0 ;;
        *) echo "不认识的参数：$1" >&2; exit 2 ;;
    esac
done

for cmd in curl python3; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "缺 $cmd" >&2; exit 2; }
done

# 默认打 agent 自己的控制端点（unix socket）：那是设备侧最直接的账，不经过
# web_server 的转发层。给了 --base 才走页面服务。
CURL=(curl -sS --max-time 30)
if [ -n "$BASE" ]; then
    BASE="${BASE%/}"
    [[ "$BASE" == https://* ]] && CURL+=(-k)
    STATS_URL="$BASE/stats"
    CAM_URL="$BASE/camera"
else
    [ -S "$SOCK" ] || {
        echo "找不到控制端点 $SOCK —— agent 起了吗（MODE=ros tools/service.sh start agent）？" >&2
        echo "也可以 --base https://127.0.0.1:8443 走 web_server。" >&2
        exit 2
    }
    CURL+=(--unix-socket "$SOCK")
    STATS_URL="http://localhost/stats"
    CAM_URL="http://localhost/camera"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
hr() { printf '%s\n' "------------------------------------------------------------"; }
FAILS=0
fail() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
pass() { echo "PASS  $*"; }

# /stats → 一行制表符分隔的字段。最后一个字段（错误原因）里可以有空格，
# 所以必须用 \t 分隔而不是空格。
read_stats() {
    local file="$1"
    python3 - "$file" <<'PY'
import json, sys
try:
    j = json.load(open(sys.argv[1]))
except Exception as e:
    print("读取 /stats 失败：%s" % e, file=sys.stderr)
    raise SystemExit(3)
s = j.get("session") or {}
r = j.get("ros") or {}
p = r.get("persons") or {}
def g(*vals):
    for v in vals:
        if v is not None:
            return v
    return "-"
print("\t".join(str(x) for x in (
    g(j.get("mode"), "-"),
    "1" if j.get("session") else "0",
    g(s.get("ros_frames"), "-"),
    g(s.get("ros_errors"), "-"),
    g(s.get("ros_source"), r.get("source"), "-"),
    g(s.get("ros_persons"), p.get("count"), "-"),
    g(s.get("ros_persons_ms"), p.get("inference_ms"), "-"),
    g(s.get("ros_gap_ms"), "-"),
    g(s.get("video_packets_out"), "-"),
    g(s.get("video_source"), "-"),
    g(s.get("ros_last_error"), "") or "",
)))
PY
}

get_stats() {   # $1 = 输出文件；成功返回 0
    "${CURL[@]}" "$STATS_URL" -o "$1" 2>/dev/null
}

echo "== 目标 =="
echo "  /stats   : $STATS_URL（$([ -n "$BASE" ] && echo web_server || echo "agent unix socket $SOCK")）"
echo "  切换目标 : ${SWITCH_TO:-（本轮不切）}"
echo "  观察窗口 : ${SETTLE}s"
hr

# --------------------------------------------------------------- 1. 前置条件
if ! get_stats "$TMP/s0.json"; then
    echo "取 $STATS_URL 失败。" >&2
    exit 2
fi

MODE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("mode","-"))' "$TMP/s0.json")
if [ "$MODE" != "ros" ]; then
    echo "agent 当前是 $MODE 模式，不是 ros。用 MODE=ros tools/service.sh restart agent" >&2
    exit 2
fi
echo "agent 模式 : ros"

# 节点的话题在不在。只看不判：判据是 /stats 里的帧数，但"话题压根没人发"
# 是最常见的起不来方式，这里先给人看一眼。
if command -v ros2 >/dev/null 2>&1; then
    if [ -f /opt/ros/humble/setup.bash ]; then
        # ROS 的 setup.bash 里有几处裸变量引用（AMENT_TRACE_SETUP_FILES），
        # 在 `set -u` 下会直接报 unbound variable —— source 前后临时关掉。
        set +u
        # shellcheck disable=SC1091
        . /opt/ros/humble/setup.bash
        set -u
    fi
    echo "== ROS 话题（只看不判）=="
    ros2 topic list 2>/dev/null | grep -E '^/(camera|detect)/' | sed 's/^/  /' \
        || echo "  （一个都没看到：ROS_DOMAIN_ID 和节点那边一致吗？run-ros-nodes.sh 起了吗？）"
    hr
fi

# 等观看端接入。没有会话就没有管线，ros_frames 永远是 0 —— 那不是故障，
# 但也没法验收，所以这里等，等到超时就说清楚缺什么。
if [ "$(cut -f2 <<<"$(read_stats "$TMP/s0.json")")" != "1" ]; then
    echo "还没有观看端接入。管线是观看端进来时才建的 —— 等最多 ${WAIT_VIEWER}s …"
    echo "  （没有浏览器的话：python3 tools/selftest.py --no-teleop）"
    deadline=$((SECONDS + WAIT_VIEWER))
    while [ "$SECONDS" -lt "$deadline" ]; do
        sleep 2
        get_stats "$TMP/s0.json" || continue
        [ "$(cut -f2 <<<"$(read_stats "$TMP/s0.json")")" = "1" ] && break
    done
    if [ "$(cut -f2 <<<"$(read_stats "$TMP/s0.json")")" != "1" ]; then
        echo "等不到观看端（${WAIT_VIEWER}s）。先挂一个：python3 tools/selftest.py --no-teleop" >&2
        exit 2
    fi
    echo "观看端已接入。"
fi

# --------------------------------------------------------------- 2. 基线采样
IFS=$'\t' read -r _ HS F0 E0 SRC0 P0 PM0 GAP0 V0 VSRC0 ERR0 < <(read_stats "$TMP/s0.json")
echo "基线 : ros_frames=$F0 video_packets_out=$V0 ros_errors=$E0 源=$SRC0 人=$P0"
[ "$GAP0" = "-" ] || echo "       最近一帧距今 ${GAP0}ms，板上记的视频源 $VSRC0"

sleep "$SETTLE"

if ! get_stats "$TMP/s1.json"; then
    echo "取 /stats 失败（会话在观察期间断了？）" >&2
    exit 2
fi
IFS=$'\t' read -r _ HS F1 E1 SRC1 P1 PM1 GAP1 V1 VSRC1 ERR1 < <(read_stats "$TMP/s1.json")

# --------------------------------------------------------------- 3. 判据
hr
echo "== 判据（${SETTLE}s 窗口）=="
echo "  ros_frames        $F0 → $F1"
echo "  video_packets_out $V0 → $V1"
echo "  ros_errors        $E0 → $E1"
echo "  ros_source        $SRC0 → $SRC1"

if [ "$HS" != "1" ]; then
    fail "会话在观察期间消失了"
fi

DF=$((F1 - F0))
if [ "$DF" -ge 2 ]; then
    pass "话题上的画面推进了 appsrc（+$DF 帧）"
else
    fail "ros_frames 没涨（+$DF）—— 话题没数据、尺寸不符，或 ros-spin 线程卡住了"
    [ -n "$ERR1" ] && echo "      最近一次原因：$ERR1"
fi

DV=$((V1 - V0))
if [ "$DV" -ge 10 ]; then
    pass "RTP 在发（+$DV 包）"
else
    fail "video_packets_out 没涨（+$DV）—— 编码或 webrtcbin 这一侧断了"
fi

if [ "$E1" = "0" ]; then
    pass "ros_errors = 0"
else
    fail "ros_errors = $E1（最近一次：${ERR1:-?}）"
fi

if [ "$GAP1" != "-" ] \
   && python3 -c "import sys; raise SystemExit(0 if float(sys.argv[1]) < 2000 else 1)" "$GAP1"; then
    pass "最近一帧距今 ${GAP1}ms（<2s）"
else
    fail "最近一帧距今 ${GAP1}ms —— 话题在发但很稀疏"
fi

# 检测结果：检测节点没起来时画面照常转发、只是没有框（设计如此），
# 所以这里只说，不判死。
if [ "$P1" != "-" ]; then
    echo "  检测   ：$P1 人 / ${PM1}ms（/detect/persons）"
fi

# --------------------------------------------------------------- 4. 切源
if [ -n "$SWITCH_TO" ]; then
    hr
    echo "== 切源 → $SWITCH_TO =="
    # 走 agent 的 POST /camera：它会发 /camera/select，然后等 camera_node
    # 的回报。applied 只有真等到回报才是 true。
    HTTP=$("${CURL[@]}" -o "$TMP/switch.json" -w '%{http_code}' \
        -H 'Content-Type: application/json' \
        -d "{\"name\": \"$SWITCH_TO\"}" "$CAM_URL")
    echo "  POST /camera → HTTP $HTTP"
    cat "$TMP/switch.json" | python3 -m json.tool 2>/dev/null | sed 's/^/  /' || true

    if [ "$HTTP" != "200" ]; then
        fail "切源请求被拒（HTTP $HTTP）"
    else
        if python3 - "$TMP/switch.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
raise SystemExit(0 if (j.get("ok") and j.get("applied")) else 1)
PY
        then
            pass "camera_node 回报已切到 $SWITCH_TO"
        else
            fail "请求发出去了，但没等到 camera_node 的回报（节点没跑？设备被占？）"
        fi

        # 切完还要能继续出帧 —— 只换源不重协商的前提是 caps 完全一致
        sleep 3
        get_stats "$TMP/s2.json" || true
        IFS=$'\t' read -r _ _ F2 _ SRC2 _ _ GAP2 V2 _ ERR2 < <(read_stats "$TMP/s2.json")
        echo "  切后：ros_frames=$F2 ros_source=$SRC2 最近帧 ${GAP2}ms"
        if [ "$SRC2" = "$SWITCH_TO" ]; then
            pass "ros_source 已经是 $SWITCH_TO"
        else
            fail "ros_source 还是 $SRC2，不是 $SWITCH_TO"
        fi
        if [ "$((F2 - F1))" -ge 2 ]; then
            pass "切源后画面继续推进（+$((F2 - F1)) 帧）"
        else
            fail "切源后 ros_frames 停了（+$((F2 - F1))）"
            [ -n "$ERR2" ] && echo "      最近一次原因：$ERR2"
        fi

        if [ "$KEEP" = "0" ]; then
            echo "  回切到 $SRC0 …"
            "${CURL[@]}" -o /dev/null -H 'Content-Type: application/json' \
                -d "{\"name\": \"$SRC0\"}" "$CAM_URL" || true
        fi
    fi
fi

# --------------------------------------------------------------- 5. 结论
hr
if [ "$FAILS" -eq 0 ]; then
    echo "全部通过。日志：tools/service.sh logs agent"
    exit 0
fi
echo "$FAILS 条判据没过。看日志：tools/service.sh logs agent"
echo "  ros_frames 不涨 → 查 camera_node/detector_node（ros2 topic hz /detect/image_annotated）"
echo "  RTP 不涨       → 查 agent 的 GStreamer 错误（appsrc caps / 编码器）"
exit 1
