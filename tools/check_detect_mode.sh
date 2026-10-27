#!/bin/bash
# check_detect_mode.sh —— detect 模式的端到端判据（板子在推流 + 有观看端在看）
#
# 和 tools/check_detect_mode.py 的分工：
#   .py  纯函数/模型：letterbox、解码、NMS、白平衡，不需要管线；
#   .sh  真管线：/stats 里的检测计数到底有没有在涨，换摄像头之后还涨不涨。
#
# 为什么不能只看画面里有没有框：框是**烧进** H.264 的，浏览器缓存、旧 IDR、
# 甚至一直挂着最后一帧，都能让"画面里有框"成立。所以这里看板子自己的账
# （GET /stats，和 1Hz 遥测同一份）：
#
#   detect_ready        RKNN 已在检测线程里加载
#   detect_frames       推给编码器的帧数（应该每帧都涨）
#   detect_inferences   推理次数（按 --detect-fps 涨，比帧数慢）
#   detect_boxes        累计检出的人框数
#   detect_errors       桥里的异常计数（必须不涨）
#
# 计数是跨摄像头切换累计的（桥每段一个，DetectSwitch 会把旧桥的账结掉），
# 所以"切换后还涨"可以用同一对数证明。
#
# 前提：板子上 device_agent.py 以 **detect 模式**在推流，并且浏览器/观看端
# 已经连上看得到画面（没有观看端时 /stats 的 session 是 null，本脚本会等）。
#
# 用法（板子上，或者任何能访问到 web_server 的机器）：
#     tools/check_detect_mode.sh
#     tools/check_detect_mode.sh --base https://192.168.2.187:8443
#     tools/check_detect_mode.sh --expect-boxes            # 镜头前得有人
#     tools/check_detect_mode.sh --switch-to usb --settle 4
#
# 退出码：0 全过；1 有断言失败；2 前置条件不满足（模式不对/没观看端）。
set -uo pipefail

BASE="https://127.0.0.1:8443"
CA=""
WAIT=8               # 两次采样之间的间隔（秒）。detect_fps 默认 5，够看涨
VIEWER_TIMEOUT=60    # 等观看端接入的上限（秒）
SETTLE=3             # 切摄像头后等新桥出第一帧的时间（秒）
SWITCH_TO=""
EXPECT_BOXES=0

while [ $# -gt 0 ]; do
    case "$1" in
        --base) BASE="${2:-}"; shift 2 ;;
        --ca) CA="${2:-}"; shift 2 ;;
        --wait) WAIT="${2:-}"; shift 2 ;;
        --viewer-timeout) VIEWER_TIMEOUT="${2:-}"; shift 2 ;;
        --settle) SETTLE="${2:-}"; shift 2 ;;
        --switch-to) SWITCH_TO="${2:-}"; shift 2 ;;
        --expect-boxes) EXPECT_BOXES=1; shift ;;
        -h|--help) grep '^#' "$0" | sed -n '2,34p' | sed 's/^# \?//'; exit 0 ;;
        *) echo "不认识的参数：$1" >&2; exit 2 ;;
    esac
done

for cmd in curl python3; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "缺 $cmd" >&2; exit 2; }
done

CURL=(curl -sS --max-time 20)
if [ -n "$CA" ]; then
    CURL+=(--cacert "$CA")
elif [[ "$BASE" == https://* ]]; then
    CURL+=(-k)          # 自签证书，和 check_switch.sh 一致
fi
BASE="${BASE%/}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
hr() { printf '%s\n' "------------------------------------------------------------"; }

# 取一份 /stats。失败时打印的是 curl 自己的话（502 = web_server 连不上 agent）。
get_stats() {
    "${CURL[@]}" "$BASE/stats" -o "$1" || return 1
}

# jget <file> <a.b.c>：打印字段；null/缺失都打印 null，不猜。
jget() {
    python3 -s - "$1" "$2" <<'PY'
import json, sys
try:
    cur = json.load(open(sys.argv[1]))
except Exception as e:
    print("bad-json:%s" % e); raise SystemExit(0)
for k in sys.argv[2].split("."):
    cur = cur.get(k) if isinstance(cur, dict) else None
if cur is None:
    print("null")
elif isinstance(cur, bool):
    print("true" if cur else "false")
else:
    print(cur)
PY
}

echo "== 目标 =="
echo "  页面服务 : $BASE"
echo "  采样间隔 : ${WAIT}s"

if ! get_stats "$TMP/a.json"; then
    echo "取 $BASE/stats 失败。板子的 web_server.py 起了吗？" >&2
    exit 2
fi
MODE=$(jget "$TMP/a.json" mode)
if [ "$MODE" != "detect" ]; then
    echo "板子当前是 $MODE 模式，不是 detect —— 用 MODE=detect tools/run-agent.sh 重启" >&2
    exit 2
fi

# 没有观看端就没有会话，检测桥也不会起。等，而不是判失败：这通常是"页面还没刷"。
if [ "$(jget "$TMP/a.json" session)" = "null" ]; then
    echo "  等观看端接入（最多 ${VIEWER_TIMEOUT}s）…"
    deadline=$((SECONDS + VIEWER_TIMEOUT))
    while [ "$(jget "$TMP/a.json" session)" = "null" ]; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "等不到观看端：/stats 的 session 一直是 null。" >&2
            echo "先打开观看页（或让无头观看端连上），检测桥才会启动。" >&2
            exit 2
        fi
        sleep 2
        get_stats "$TMP/a.json" || true
    done
fi

echo "  当前源   : $(jget "$TMP/a.json" source)"
hr

# 等 RKNN 真正加载完。加载失败时 detect_ready 会一直是 false，errors 里也有话。
echo "== 等检测桥就绪 =="
deadline=$((SECONDS + 30))
while [ "$(jget "$TMP/a.json" session.detect_ready)" != "true" ]; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "30s 内 detect_ready 没变 true。errors=$(jget "$TMP/a.json" session.detect_errors)" >&2
        echo "last_error: $(jget "$TMP/a.json" session.detect_last_error)" >&2
        echo "模型加载失败？看 device_agent 日志里的 'detect' 行。" >&2
        exit 1
    fi
    sleep 1
    get_stats "$TMP/a.json" || true
done
echo "  就绪（last_ms=$(jget "$TMP/a.json" session.detect_last_ms)）"

# 快照 A → 等待 → 快照 B。两段之间只允许计数前进。
cp "$TMP/a.json" "$TMP/before.json"
echo "  观察 ${WAIT}s…"
sleep "$WAIT"
get_stats "$TMP/after.json" || { echo "第二次取 /stats 失败" >&2; exit 1; }

echo
echo "== 判据（检测是否在跑）=="
python3 -s - "$TMP/before.json" "$TMP/after.json" "$EXPECT_BOXES" <<'PY'
import json, sys

def load(p):
    d = json.load(open(p))
    return d, (d.get("session") or {})

a, sa = load(sys.argv[1])
b, sb = load(sys.argv[2])
expect_boxes = sys.argv[3] == "1"
fails = []

def show(name, ok, detail):
    print("  %-4s %-26s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append(name)

show("模式仍是 detect", b.get("mode") == "detect", b.get("mode"))
show("会话还在", b.get("session") is not None, b.get("source"))
show("RKNN 已加载", sb.get("detect_ready") is True, sb.get("detect_ready"))

df = int(sb.get("detect_frames", 0)) - int(sa.get("detect_frames", 0))
di = int(sb.get("detect_inferences", 0)) - int(sa.get("detect_inferences", 0))
db = int(sb.get("detect_boxes", 0)) - int(sa.get("detect_boxes", 0))
de = int(sb.get("detect_errors", 0)) - int(sa.get("detect_errors", 0))

show("帧数在涨", df > 0, "+%d 帧（%d → %d）"
     % (df, sa.get("detect_frames", 0), sb.get("detect_frames", 0)))
show("推理在跑", di > 0, "+%d 次（%d → %d，配置 %s fps）"
     % (di, sa.get("detect_inferences", 0), sb.get("detect_inferences", 0),
        sb.get("detect_fps")))
show("推理耗时可读", float(sb.get("detect_last_ms") or 0) > 0,
     "%s ms" % sb.get("detect_last_ms"))
show("桥没有新错误", de == 0,
     "+%d 次（last_error=%r）" % (de, sb.get("detect_last_error") or ""))
if expect_boxes:
    show("镜头前有人", db > 0, "+%d 个框（累计 %d）"
         % (db, sb.get("detect_boxes", 0)))
else:
    print("  INFO 人框累计 +%d（要断言有人请加 --expect-boxes）" % db)

print()
if fails:
    print("失败 %d 项：%s" % (len(fails), "、".join(fails)))
    raise SystemExit(1)
print("检测链路正常。")
PY
RC=$?
if [ "$RC" != "0" ]; then
    exit "$RC"
fi

# --------------------------------------------------------------- 换摄像头
if [ -n "$SWITCH_TO" ]; then
    hr
    echo "== 切摄像头 → $SWITCH_TO =="
    if ! "${CURL[@]}" -X POST "$BASE/camera" \
            -H 'Content-Type: application/json' \
            -d "{\"name\":\"$SWITCH_TO\"}" -o "$TMP/switch.json"; then
        echo "POST /camera 失败" >&2
        exit 1
    fi
    if [ "$(jget "$TMP/switch.json" ok)" != "true" ]; then
        echo "切换被拒绝：$(jget "$TMP/switch.json" error)" >&2
        exit 1
    fi
    echo "  板子回：current=$(jget "$TMP/switch.json" current) applied=$(jget "$TMP/switch.json" applied)"

    # 新段+新桥要一点时间才出第一帧；先等，再取基线。**不能**直接拿切换前
    # 的快照做差：那样会把"新桥起步慢"误判成"切换把检测弄坏了"。
    sleep "$SETTLE"
    get_stats "$TMP/sw1.json" || { echo "切换后取 /stats 失败" >&2; exit 1; }
    echo "  观察 ${WAIT}s…"
    sleep "$WAIT"
    get_stats "$TMP/sw2.json" || { echo "切换后第二次取 /stats 失败" >&2; exit 1; }

    echo
    echo "== 判据（切换后）=="
    python3 -s - "$TMP/sw1.json" "$TMP/sw2.json" "$SWITCH_TO" <<'PY'
import json, sys

def load(p):
    d = json.load(open(p))
    return d, (d.get("session") or {})

a, sa = load(sys.argv[1])
b, sb = load(sys.argv[2])
target = sys.argv[3]
fails = []

def show(name, ok, detail):
    print("  %-4s %-26s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append(name)

show("源已切到目标", b.get("source") == target, b.get("source"))
show("会话还在", b.get("session") is not None, b.get("source"))
df = int(sb.get("detect_frames", 0)) - int(sa.get("detect_frames", 0))
di = int(sb.get("detect_inferences", 0)) - int(sa.get("detect_inferences", 0))
de = int(sb.get("detect_errors", 0)) - int(sa.get("detect_errors", 0))
show("帧数继续涨", df > 0, "+%d 帧" % df)
show("推理继续跑", di > 0, "+%d 次" % di)
show("桥没有新错误", de == 0,
     "+%d 次（last_error=%r）" % (de, sb.get("detect_last_error") or ""))
show("计数没有倒退", int(sb.get("detect_frames", 0)) >= int(sa.get("detect_frames", 0)),
     "%s → %s" % (sa.get("detect_frames"), sb.get("detect_frames")))

print()
if fails:
    print("失败 %d 项：%s" % (len(fails), "、".join(fails)))
    raise SystemExit(1)
print("切换后检测依然正常。")
PY
    exit $?
fi
