#!/bin/bash
# check_fd.sh —— 一轮会话结束之后，摄像头设备的 fd 有没有还回去
#
# ## 这条判据防的是什么
#
# 一个**看不见的**泄漏：每建一次会话（浏览器连上来看一次），`/dev/video11`
# 上就少还一个 fd。它不报错、不掉帧、不影响观感 —— 只有跑上几十轮、fd 表满了、
# 或者 MIPI 传感器被一直占着不放的时候才发作。所以它必须有个自动判据，
# 靠人肉看日志永远看不出来。
#
# 根因在 src/pipeline.py 的 `VideoSwitch.dispose()` 里写全了，一句话版本：
# 采集段里的 appsink 被拉过 sample 之后会一直攥着一个 v4l2 buffer，那个 buffer
# 不让 v4l2 的 buffer pool 析构，而 pool 握着设备 fd；appsink 要析构得先让它的
# 父 bin 析构，父 bin 又挂在 pipeline 上，而 pipeline 因为一条**跨 C/Python 的
# 闭包环**永远到不了 refcount 0。所以拆会话时必须把采集段从管道里摘掉。
#
# ## 为什么必须连跑好几轮、而且必须量"会话进行中"
#
#   连跑几轮 —— 单轮"关掉之后 0 个"可能只是那一轮刚好没漏。每轮稳定 +1 这种
#               慢泄漏，只有连着跑才和"不漏"分得开。
#
#   进行中量 —— 这一条是**防假通过**的，比上面那条更重要。如果 WS 根本没连上、
#               会话压根没建起来，那么"关掉之后 0 个 fd"是白送的 —— 判据会以
#               最漂亮的方式骗过你（这是本项目反复踩的坑：判据全过和根本没测
#               长得一模一样）。会话真的在推的时候，这个设备上**必须**能看到
#               fd。所以每一轮都先证明"fd 真的开过"，再证明"真的还了"。
#
# ## 用法（**在板子上跑** —— 它要读本机 /proc 里那个 agent 进程）
#
#     tools/check_fd.sh                      # 默认 3 轮
#     tools/check_fd.sh --cycles 10          # 泄漏是每轮 +1，轮数越多越灵敏
#     tools/check_fd.sh --hold 10            # 每轮让会话挂多久
#     tools/check_fd.sh --base https://127.0.0.1:8443 --room <房间>
#
# 房间号默认从 agent 自己的命令行里读（`--room X`），不用在这儿再抄一遍 ——
# 抄一遍就有抄错、以及"改了一处忘了另一处"的机会。
set -uo pipefail

BASE="https://127.0.0.1:8443"
ROOM=""
CYCLES=3
HOLD=8              # 每轮会话挂多久（秒）。太短会截在 pipeline 还没进 PLAYING 的时候
CA=""

while [ $# -gt 0 ]; do
    case "$1" in
        --base) BASE="${2:-}"; shift 2 ;;
        --room) ROOM="${2:-}"; shift 2 ;;
        --cycles) CYCLES="${2:-}"; shift 2 ;;
        --hold) HOLD="${2:-}"; shift 2 ;;
        --ca) CA="${2:-}"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed -n '2,45p' | sed 's/^# \?//'; exit 0 ;;
        *) echo "不认识的参数：$1" >&2; exit 2 ;;
    esac
done

for cmd in python3 curl; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "缺 $cmd" >&2; exit 2; }
done

# --------------------------------------------------------------- 找到 agent
# 方括号那一下不是洁癖：`pgrep -f "device_agent.py"` 会连**这个脚本自己**的
# 命令行一起匹配上（那条命令行里就有这个字符串），于是 pgrep 把自己也算进去，
# 后面所有针对 PID 的操作都作用在一个正在读它自己的进程上。
PID=$(pgrep -f "device_agent[.]py" | head -1)
if [ -z "$PID" ]; then
    echo "板子上没有在跑的 device_agent.py。先起它：tools/run-agent.sh" >&2
    exit 2
fi

if [ -z "$ROOM" ]; then
    # cmdline 是 NUL 分隔的，先拆开再找 --room（两种写法都认）。
    # 用 python3 而不是 sed：这一段要处理"下一个参数"的语义，sed 写出来
    # 又长又容易错（第一版就写错了，报的是 "unknown option to `s'"）。
    ROOM=$(python3 -c '
import sys
a = open("/proc/%s/cmdline" % sys.argv[1]).read().split("\0")
for i, x in enumerate(a):
    if x == "--room" and i + 1 < len(a):
        print(a[i + 1]); break
    if x.startswith("--room="):
        print(x.split("=", 1)[1]); break
' "$PID")
fi
if [ -z "$ROOM" ]; then
    echo "agent 的命令行里读不到 --room，请用 --room 指定" >&2
    exit 2
fi

# 设备节点：从 agent 的源表里问，别在这儿硬编码
NODES=$(cd "$(dirname "$0")/.." && python3 -c '
import sys; sys.path.insert(0, "src")
import sources
print(" ".join(s.device for s in sources.SOURCES))
' 2>/dev/null)
[ -n "$NODES" ] || NODES="/dev/video11 /dev/video20"

# --------------------------------------------------------------- 数 fd
count_video_fds() {
    local n=0 f
    for f in "/proc/$PID/fd"/*; do
        case "$(readlink "$f" 2>/dev/null)" in
            /dev/video*) n=$((n + 1)) ;;
        esac
    done
    echo "$n"
}

# 哪些设备、各几个、flags 是什么。flags 一起看是因为它是 open(2) 那一刻的实参，
# 能回答"这个 fd 是谁开的"—— v4l2src 开设备有它自己的一套。
dump_video_fds() {
    local f num tgt fl
    for f in "/proc/$PID/fd"/*; do
        tgt=$(readlink "$f" 2>/dev/null) || continue
        case "$tgt" in
            /dev/video*)
                num=${f##*/}
                fl=$(sed -n 's/^flags:\s*//p' "/proc/$PID/fdinfo/$num" 2>/dev/null)
                echo "        fd $num -> $tgt   flags=$fl"
                ;;
        esac
    done
}

# 一轮：连上、挂着、断开。和页面走同一条路（同一个 URL 形状、同一个 web_server
# 代理、同一段 agent 逻辑），区别只是没人发 offer —— 会话是在"观看端接入"那一刻
# 建的，不需要协商，更不需要解码。
ws_cycle() {
    python3 - "$BASE" "$ROOM" "$HOLD" "$CA" <<'PY'
import asyncio, ssl, sys, urllib.parse
import aiohttp

base, room, hold, ca = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
u = urllib.parse.urlsplit(base)
ctx = ssl.create_default_context(cafile=ca or None)
if not ca:
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
scheme = "wss" if u.scheme == "https" else "ws"
url = "%s://%s/ws/view?room=%s" % (scheme, u.netloc, urllib.parse.quote(room))

async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url, ssl=ctx, heartbeat=None, origin=None) as ws:
            await asyncio.sleep(hold)
            await ws.close()

asyncio.run(main())
PY
}

# --------------------------------------------------------------- 开跑
echo "== 目标 =="
echo "  agent 进程 : $PID"
echo "  页面服务   : $BASE"
echo "  房间       : $ROOM"
echo "  设备节点   : $NODES"
echo "  轮数       : $CYCLES（每轮挂 ${HOLD}s）"
echo "------------------------------------------------------------"

BASE_FD=$(count_video_fds)
echo "基线：agent 手里有 $BASE_FD 个 video fd"
if [ "$BASE_FD" != "0" ]; then
    echo
    echo "  **基线不是 0** —— 说明现在就有别人在看（页面开着？），或者上一次会话" >&2
    echo "  的 fd 已经漏了。两种情况下这一轮的数字都不干净，先排除掉再跑。" >&2
    dump_video_fds >&2
    exit 2
fi

FAIL=0
for i in $(seq 1 "$CYCLES"); do
    echo
    echo "== 第 $i / $CYCLES 轮 =="

    ws_cycle >/tmp/check_fd_cycle.log 2>&1 &
    CYCLE=$!
    sleep $((HOLD / 2))

    DURING=$(count_video_fds)
    if [ "$DURING" -ge 1 ]; then
        echo "  会话进行中：$DURING 个 video fd  ✔ 会话真的建起来了"
        dump_video_fds | sed 's/^/  /'
    else
        # 这一条是防假通过的：没有这一段，"关掉之后 0 个"什么都证明不了。
        echo "  会话进行中：0 个 video fd  ✘ 这一轮压根没推流，后面那个 0 不算数"
        echo "     WS 探针的输出："
        sed 's/^/       /' /tmp/check_fd_cycle.log
        FAIL=1
    fi

    wait $CYCLE 2>/dev/null
    sleep 2                      # 拆会话是异步的（set_state(NULL) 立刻返回），给它落地的时间

    AFTER=$(count_video_fds)
    if [ "$AFTER" = "$BASE_FD" ]; then
        echo "  断开之后：$AFTER 个  ✔ 还回去了"
    else
        echo "  断开之后：$AFTER 个  ✘ 比基线多了 $((AFTER - BASE_FD)) 个（每轮 +1 = 每轮漏一个）"
        dump_video_fds | sed 's/^/  /'
        FAIL=1
    fi
done

echo "------------------------------------------------------------"
if [ "$FAIL" = "0" ]; then
    echo "全部 $CYCLES 轮通过：每轮都真的推了流，且每轮都把 fd 还干净了。"
    exit 0
fi

echo "有判据没过。fd 泄漏的根因和修法见 src/pipeline.py 的 VideoSwitch.dispose()。" >&2
exit 1
