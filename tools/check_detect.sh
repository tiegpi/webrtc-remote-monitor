#!/bin/bash
# check_detect.sh —— 人像检测开关的可证伪判据
#
# 为什么不能只看"按钮亮了"：POST 成功只证明请求到了板子；检测是不是真的在跑，
# 要看 session.frames 在涨；关掉之后是不是真的停了，要看 frames 不再涨。
# 所以这里分三段，任何一段只是"看起来对"都不算过：
#
#   1. GET /detect  → available。模型不在/缺 rknnlite 时**跳过并说明原因**，
#      不是失败 —— 检测不可用不影响视频，这也是设计的一部分。
#   2. POST on      → ok + applied；等一个观察窗，frames 必须涨、errors 必须是 0。
#      （空画面也会出帧，只是 0 个框 —— frames 证明线程在拉帧做推理。）
#   3. POST off     → ok；再等一个观察窗，frames 必须不再涨。
#
# applied=false 表示**现在没有观看端建流**：板子只记住了开关，检测线程没有
# 采集段可挂。这时只验证接口语义，推理部分不算数（脚本会明说）。
#
# 前提：板子上 device_agent.py 在跑；要验证推理，浏览器得先打开页面。
#
# 用法（在板子上跑，或任何能访问到页面地址的机器）：
#     tools/check_detect.sh
#     tools/check_detect.sh --base https://<board-ip>:8443
#     tools/check_detect.sh --watch 5        # 每个观察窗 5 秒（默认 3）
#     tools/check_detect.sh --keep           # 结束时保持开启，不恢复原状
set -uo pipefail

BASE="https://127.0.0.1:8443"
CA=""
WATCH=3
KEEP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --base) BASE="${2:-}"; shift 2 ;;
        --ca) CA="${2:-}"; shift 2 ;;
        --watch) WATCH="${2:-3}"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h|--help) grep '^#' "$0" | sed -n '2,30p' | sed 's/^# \?//'; exit 0 ;;
        *) echo "不认识的参数：$1" >&2; exit 2 ;;
    esac
done

for cmd in curl python3; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "缺 $cmd" >&2; exit 2; }
done

# 页面是自签证书（tools/gen_cert.sh 签的），默认不校验。给了 --ca 才校。
CURL=(curl -sS --max-time 20)
if [ -n "$CA" ]; then
    CURL+=(--cacert "$CA")
elif [[ "$BASE" == https://* ]]; then
    CURL+=(-k)
fi
BASE="${BASE%/}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
hr() { printf '%s\n' "------------------------------------------------------------"; }

fail=0
check() {                       # check <名字> <命令...>
    local name="$1"; shift
    if "$@"; then
        echo "  [ OK ] $name"
    else
        echo "  [FAIL] $name"
        fail=1
    fi
}

get_state() { "${CURL[@]}" "$BASE/detect" -o "$1"; }

post_enabled() {                # $1 = true / false，打印 HTTP 状态码
    "${CURL[@]}" -o "$TMP/post.json" -w '%{http_code}' \
        -X POST -H 'Content-Type: application/json' \
        -d "{\"enabled\": $1}" "$BASE/detect"
}

# 从当前状态文件里取一个字段。
field() {                       # field <file> <点分路径>，缺省打印空
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    node = json.load(open(sys.argv[1]))
except Exception:
    print(""); raise SystemExit(0)
for part in sys.argv[2].split("."):
    if not isinstance(node, dict):
        node = None; break
    node = node.get(part)
if isinstance(node, bool):
    print("1" if node else "0")
elif node is None:
    print("")
else:
    print(node)
PY
}

state_line() {                  # state_line <file>
    printf '  available=%s enabled=%s frames=%s errors=%s\n' \
        "$(field "$1" available)" "$(field "$1" enabled)" \
        "$(field "$1" session.frames)" "$(field "$1" session.errors)"
}

echo "== 目标 =="
echo "  页面服务 : $BASE"
echo "  观察窗口 : ${WATCH}s"
hr

# --------------------------------------------------------------- 1. 可用性
if ! get_state "$TMP/s0.json"; then
    echo "取 $BASE/detect 失败。板子的 web_server.py 起了吗？" >&2
    exit 2
fi
echo "== 初始状态 =="
state_line "$TMP/s0.json"

AVAIL=$(field "$TMP/s0.json" available)
ENABLED0=$(field "$TMP/s0.json" enabled)
if [ "$AVAIL" != "1" ]; then
    echo
    echo "人像检测不可用：$(field "$TMP/s0.json" reason)"
    echo "这不是本脚本的失败 —— 检测不可用不影响视频链路。"
    echo "把 .rknn 放到板子上，用 device_agent 的 --detect-model 指过去再跑一次。"
    exit 0
fi
echo

# --------------------------------------------------------------- 2. 开启
echo "== 开启 =="
HTTP=$(post_enabled true)
echo "  POST /detect {\"enabled\":true} → HTTP $HTTP"
sed 's/^/    /' "$TMP/post.json"; echo
if [ "$HTTP" != "200" ]; then
    echo "  开启请求就没成功，后面不用看了。"
    echo "  409 = 板子认为模型不可用；500 = 加载/NPU 初始化失败（看 agent 日志）；"
    echo "  502 = agent 没跑；400 = 请求体不对（这个多半是脚本被改了）。"
    exit 1
fi

APPLIED=$(field "$TMP/post.json" applied)
if [ "$APPLIED" != "1" ]; then
    echo "  applied=false：现在没有观看端在拉流，检测线程没地方挂。"
    echo "  接口语义正确；推理部分跳过。在浏览器里打开页面后再跑一次。"
    if [ "$ENABLED0" != "1" ] && [ "$KEEP" != "1" ]; then
        post_enabled false >/dev/null
    fi
    exit 0
fi

get_state "$TMP/s1.json"
F0=$(field "$TMP/s1.json" session.frames)
echo "  等 ${WATCH}s，看 frames 有没有涨…"
sleep "$WATCH"
get_state "$TMP/s2.json"
state_line "$TMP/s2.json"
F1=$(field "$TMP/s2.json" session.frames)
E1=$(field "$TMP/s2.json" session.errors)

check "检测在线程里出帧（frames ${F0:-0} → ${F1:-0}）" \
    test "${F1:-0}" -gt "${F0:-0}"
check "推理没有报错（errors=${E1:-0}）" test "${E1:-0}" -eq 0
hr

# --------------------------------------------------------------- 3. 关闭
echo "== 关闭 =="
HTTP=$(post_enabled false)
echo "  POST /detect {\"enabled\":false} → HTTP $HTTP"
sed 's/^/    /' "$TMP/post.json"; echo
if [ "$HTTP" != "200" ]; then
    echo "  关闭请求失败 —— 检测可能还在跑。手动再发一次，或看 agent 日志。"
    exit 1
fi

# detector.stop() 会 join 线程（上限 2 秒），POST 返回时线程已经收工；
# 这里的 1 秒只是给最后一份结果走完 GLib 队列，不影响 frames（它在线程里加）。
sleep 1
get_state "$TMP/s3.json"
F2=$(field "$TMP/s3.json" session.frames)
echo "  等 ${WATCH}s，确认 frames 不再涨…"
sleep "$WATCH"
get_state "$TMP/s4.json"
state_line "$TMP/s4.json"
F3=$(field "$TMP/s4.json" session.frames)

check "关闭后检测线程已停（frames 停在 ${F2:-0}）" \
    test "${F3:-0}" -eq "${F2:-0}"
hr

# --------------------------------------------------------------- 4. 恢复
if [ "$ENABLED0" = "1" ] || [ "$KEEP" = "1" ]; then
    HTTP=$(post_enabled true)
    echo "已恢复为开启（初始状态 enabled=$ENABLED0，HTTP $HTTP）"
else
    echo "保持关闭（初始状态就是关的）"
fi

echo
if [ "$fail" -ne 0 ]; then
    echo "有检查项失败："
    echo "  journalctl -u webrtc-agent -n 50 --no-pager   # 或看 run-agent 的终端"
    exit 1
fi
echo "全部通过"
