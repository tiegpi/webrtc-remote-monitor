#!/bin/bash
# 板子侧服务管理：device_agent（推流）+ web_server（局域网观看页）
#                  + ROS 模式下的 camera_node / detector_node。
#
#     tools/service.sh start   [agent|web|nodes|all]   # 默认 all
#     tools/service.sh stop    [agent|web|nodes|all]
#     tools/service.sh restart [agent|web|nodes|all]
#     tools/service.sh status
#     tools/service.sh logs    [agent|web|nodes] [-f]
#
# **all 包含哪些服务取决于运行模式**（MODE 环境变量优先，其次 .env 里的 MODE=，
# 都没有就是 monitor）：
#
#     monitor : web + agent
#     ros     : nodes + web + agent
#
# 所以 `MODE=ros tools/service.sh restart` 一条命令就能把整条链拉起来：
# camera_node 出图 → detector_node 画框 → agent 订阅并推给浏览器。
# 单独管节点用 `... restart nodes`，参数可以直接透传：
#
#     tools/service.sh restart nodes camera_source:=usb
#     tools/service.sh restart nodes width:=640 height:=480
#
# 和直接跑 run-agent.sh / run-web.sh / run-ros-nodes.sh 的区别：
#   - 进程起在独立会话里（setsid），ssh 断开不会被 SIGHUP 带走；
#   - pid 写在 run/*.pid，日志追加到 run/agent.log、run/web.log、run/nodes.log；
#   - start 会等到服务真的可用才返回（页面 healthz / 推流端连上信令 /
#     camera_node 报出第一帧），失败时直接打出日志尾部，而不是留一个
#     "看起来启动了"的死进程；
#   - stop 也会收拾手动起、没有 pid 文件的进程（按命令行找）；nodes 还会把
#     没跟着 ros2 launch 一起退的 node 进程收掉。
#
# agent 必须用登录 shell（bash -l）起：BSP 的 GST_V4L2_USE_LIBV4L2 等变量
# 只在 /etc/profile.d 里导出。run-agent.sh 内部会 exec python3，所以 pid
# 文件里的进程号在 exec 前后是同一个。
#
# 依赖 .env（ROOM / TOKEN / SIGNAL_HOST / SIGNAL_AUTH），和 run-*.sh 共用。
# nodes 不需要 .env（它只认 ROS 环境和 launch 参数）。
# 适用于当前公网部署形态。纯局域网（信令也跑在板子 8080 上）不需要它，
# 按 README 的"局域网最小形态"直接起三个进程即可。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RUN="$ROOT/run"
mkdir -p "$RUN"

AGENT_PID_FILE="$RUN/agent.pid"
AGENT_LOG="$RUN/agent.log"
AGENT_PATTERN='[s]rc/device_agent.py'
WEB_PID_FILE="$RUN/web.pid"
WEB_LOG="$RUN/web.log"
WEB_PATTERN='[s]rc/web_server.py'
WEB_URL="https://127.0.0.1:8443/healthz"
NODES_PID_FILE="$RUN/nodes.pid"
NODES_LOG="$RUN/nodes.log"
# launch 父进程：run-ros-nodes.sh 最后是 exec ros2 launch，pid 不变，
# 所以 pid 文件和这个命令行始终对得上。
NODES_PATTERN='ros2 launch webrtc_detect'
# 只给 pkill 用（扩展正则）：ros2 launch 收 SIGINT 后偶尔会留下子进程，
# 中括号是防自己匹配自己 —— pkill 自己的命令行里是 "[w]ebrtc_detect"。
NODES_CHILD_PATTERN='[w]ebrtc_detect/(camera|detector)_node'

# 运行模式：MODE 环境变量优先，其次 .env 里的 MODE=。和 run-agent.sh 保持
# 同一套优先级，免得"service.sh 以为在 ros、agent 其实起的 monitor"。
mode_name() {
    local m="${MODE:-}"
    if [ -z "$m" ] && [ -f .env ]; then
        m="$(sed -n 's/^[[:space:]]*MODE[[:space:]]*=[[:space:]]*//p' .env \
             | tail -1 | tr -d ' "\r')"
    fi
    printf '%s' "${m:-monitor}"
}
MODE_NAME="$(mode_name)"


# ------------------------------------------------------------------ 工具函数

alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

read_pidfile() {
    [ -s "$1" ] || return 1
    tr -dc '0-9' < "$1"
}

# 进程还活着，且命令行确实是目标程序（防 pid 复用后误杀）。
matches() {
    local pid="$1" pattern="$2"
    [ -r "/proc/$pid/cmdline" ] || return 1
    tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "$pattern"
}

# pid 文件优先；文件丢了或过期时按命令行兜底，这样手动 setsid 起的进程
# 也能被 status/stop 管到。
find_pid() {
    local pidfile="$1" pattern="$2" pid
    pid="$(read_pidfile "$pidfile")" || true
    if alive "$pid" && matches "$pid" "$pattern"; then
        printf '%s' "$pid"
        return 0
    fi
    pgrep -f "$pattern" | head -1
}

kind_var() {
    case "$1" in
        agent)
            PID_FILE="$AGENT_PID_FILE"; LOG_FILE="$AGENT_LOG"
            PATTERN="$AGENT_PATTERN"; LABEL="agent（推流端）"
            ;;
        web)
            PID_FILE="$WEB_PID_FILE"; LOG_FILE="$WEB_LOG"
            PATTERN="$WEB_PATTERN"; LABEL="web（局域网观看页）"
            ;;
        nodes)
            PID_FILE="$NODES_PID_FILE"; LOG_FILE="$NODES_LOG"
            PATTERN="$NODES_PATTERN"; LABEL="nodes（camera_node + detector_node）"
            ;;
        *)
            echo "未知服务：$1（可选 agent / web / nodes / all）" >&2
            return 2
            ;;
    esac
}

tail_log() {
    echo "--- $LOG_FILE（最后 20 行）---" >&2
    tail -n 20 "$LOG_FILE" >&2 2>/dev/null || echo "（日志还没有内容）" >&2
}

# ------------------------------------------------------------------ start

start_one() {
    local kind="$1"; shift || true
    kind_var "$kind" || return 2

    local pid; pid="$(find_pid "$PID_FILE" "$PATTERN")" || true
    if [ -n "$pid" ]; then
        echo "$LABEL 已在运行（pid $pid）"
        return 0
    fi

    # nodes 不吃 .env，它只认 ROS 环境和 launch 参数；agent/web 缺 .env 就没法
    # 知道房间号、token 和信令地址，必须挡住。
    if [ "$kind" = nodes ]; then
        [ -x tools/run-ros-nodes.sh ] || {
            echo "缺 tools/run-ros-nodes.sh" >&2; return 1; }
        if [ "$MODE_NAME" != ros ]; then
            echo "注意：当前模式是 $MODE_NAME，ROS 节点一般只在 ros 模式下才需要。" >&2
        fi
    else
        [ -f .env ] || { echo "缺 .env：cp .env.example .env 并填写" >&2; return 1; }
    fi

    local start_line=0
    [ -f "$LOG_FILE" ] && start_line="$(wc -l < "$LOG_FILE")"

    echo "启动 $LABEL …"
    case "$kind" in
        agent)
            # 登录 shell：BSP 的 GStreamer 环境变量只在那里导出。
            setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' \
                _ "$PID_FILE" bash -l tools/run-agent.sh \
                </dev/null >>"$LOG_FILE" 2>&1 &
            ;;
        web)
            setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' \
                _ "$PID_FILE" bash tools/run-web.sh \
                </dev/null >>"$LOG_FILE" 2>&1 &
            ;;
        nodes)
            # 多出来的参数原样透传给 launch（camera_source:=usb 这种）。
            setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' \
                _ "$PID_FILE" bash tools/run-ros-nodes.sh "$@" \
                </dev/null >>"$LOG_FILE" 2>&1 &
            ;;
    esac

    # nodes 要等 camera_node 把设备打开、跑完亮度闭环；mipi 上五六秒很正常。
    local limit=20
    [ "$kind" = nodes ] && limit=40

    local deadline=$((SECONDS + limit))
    while [ "$SECONDS" -lt "$deadline" ]; do
        pid="$(read_pidfile "$PID_FILE")" || true
        if [ -n "$pid" ] && ! alive "$pid"; then
            echo "$LABEL 启动后立刻退出了。" >&2
            rm -f "$PID_FILE"
            tail_log
            return 1
        fi
        case "$kind" in
            agent)
                # 连上信令服务器就说明 .env、证书和网络都通了；摄像头管线要等
                # 观看端接入才建，不应作为启动成功的条件。
                if [ -n "$pid" ] && tail -n +"$((start_line + 1))" "$LOG_FILE" 2>/dev/null \
                        | grep -q "已连上信令服务器"; then
                    echo "$LABEL 已启动（pid $pid）"
                    return 0
                fi
                ;;
            web)
                if curl -skf --max-time 2 "$WEB_URL" >/dev/null 2>&1; then
                    echo "$LABEL 已启动（pid ${pid:-?}，$WEB_URL 有响应）"
                    return 0
                fi
                ;;
            nodes)
                # "采集已启动" 是 camera_node 报的第一帧级别的事实，比
                # "进程活着"可靠：设备被占、分辨率不支持都会在它之前退出。
                if [ -n "$pid" ] && tail -n +"$((start_line + 1))" "$LOG_FILE" 2>/dev/null \
                        | grep -q "采集已启动"; then
                    echo "$LABEL 已启动（pid $pid，camera_node 已在出帧）"
                    return 0
                fi
                ;;
        esac
        sleep 0.5
    done

    echo "$LABEL 启动超时（$limit 秒）。" >&2
    tail_log
    return 1
}

# ------------------------------------------------------------------ stop

stop_one() {
    kind_var "$1" || return 2

    local pid; pid="$(find_pid "$PID_FILE" "$PATTERN")" || true
    if [ -z "$pid" ]; then
        rm -f "$PID_FILE"
        if [ "$1" = nodes ] && pkill -f "$NODES_CHILD_PATTERN" 2>/dev/null; then
            echo "$LABEL 没有 launch 父进程，收掉了残留的 node 进程"
            return 0
        fi
        echo "$LABEL 没有在运行"
        return 0
    fi

    echo "停止 $LABEL（pid $pid）…"
    if [ "$1" = nodes ]; then
        # ros2 launch 只把 SIGINT 转发给子进程；SIGTERM 会把父进程带走，
        # 两个 node 留在原地继续占着摄像头。setsid 起的进程 pid 就是组长，
        # 负号一次发给整个进程组最干净。
        kill -INT -"$pid" 2>/dev/null || kill -INT "$pid" 2>/dev/null || true
    else
        kill -TERM "$pid" 2>/dev/null || true
    fi
    local deadline=$((SECONDS + 10))
    while alive "$pid" && [ "$SECONDS" -lt "$deadline" ]; do
        sleep 0.2
    done
    if alive "$pid"; then
        echo "  10 秒内没有退出，发 SIGKILL" >&2
        kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
        sleep 0.5
    fi
    rm -f "$PID_FILE"
    if [ "$1" = nodes ] && pkill -f "$NODES_CHILD_PATTERN" 2>/dev/null; then
        echo "  另收掉了没跟着退出的 node 进程" >&2
    fi
    echo "$LABEL 已停止"
}

# ------------------------------------------------------------------ status

show_status() {
    local agent_pid web_pid nodes_pid health last
    agent_pid="$(find_pid "$AGENT_PID_FILE" "$AGENT_PATTERN")" || true
    web_pid="$(find_pid "$WEB_PID_FILE" "$WEB_PATTERN")" || true
    nodes_pid="$(find_pid "$NODES_PID_FILE" "$NODES_PATTERN")" || true

    printf '模式    %s（all = %s）\n' "$MODE_NAME" "$(all_targets | tr '\n' ' ')"

    if [ -n "$nodes_pid" ]; then
        last="$(tail -n 1 "$NODES_LOG" 2>/dev/null)"
        printf 'nodes   运行中  pid %-7s %s\n' "$nodes_pid" "${last:0:80}"
    elif [ "$MODE_NAME" = ros ]; then
        printf 'nodes   未运行  （ros 模式下没有它就没有画面）\n'
    fi

    if [ -n "$agent_pid" ]; then
        last="$(tail -n 1 "$AGENT_LOG" 2>/dev/null)"
        printf 'agent   运行中  pid %-7s %s\n' "$agent_pid" "${last:0:80}"
    else
        printf 'agent   未运行\n'
    fi

    if [ -n "$web_pid" ]; then
        health="$(curl -skf --max-time 2 "$WEB_URL" 2>/dev/null || echo 'healthz 无响应')"
        printf 'web     运行中  pid %-7s %s\n' "$web_pid" "$health"
    else
        printf 'web     未运行\n'
    fi

    if [ -z "$agent_pid" ] && [ -z "$web_pid" ] && [ -z "$nodes_pid" ]; then
        return 1
    fi
    return 0
}

# ------------------------------------------------------------------ logs

show_logs() {
    case "$1" in
        agent) kind_var agent || return 2 ;;
        web)   kind_var web   || return 2 ;;
        nodes) kind_var nodes || return 2 ;;
        *)
            echo "用法：tools/service.sh logs [agent|web|nodes] [-f]" >&2
            return 2
            ;;
    esac
    if [ ! -f "$LOG_FILE" ]; then
        echo "还没有日志：$LOG_FILE（服务还没用本脚本启动过？）" >&2
        return 1
    fi
    if [ "${2:-}" = "-f" ]; then
        exec tail -n 100 -f "$LOG_FILE"
    fi
    tail -n 100 "$LOG_FILE"
}

# ------------------------------------------------------------------ 入口

# all 包含哪些服务由模式决定。起的时候"生产者在前"，停的时候反过来：先把
# 观看页和推流端停掉，最后才关摄像头，免得中间几步在日志里刷一串断流。
all_targets() {
    if [ "$MODE_NAME" = ros ]; then
        printf '%s\n' nodes web agent
    else
        printf '%s\n' web agent
    fi
}

all_targets_rev() {
    all_targets | tac
}

ACTION="${1:-status}"
TARGET="${2:-all}"
if [ $# -ge 2 ]; then shift 2; else shift $#; fi
EXTRA=("$@")

if [ "$ACTION" = logs ]; then
    [ "$TARGET" = all ] && TARGET=agent
    show_logs "$TARGET" "${EXTRA[0]:-}"
    exit $?
fi

case "$TARGET" in
    agent|web|nodes|all) ;;
    *) echo "未知服务：$TARGET（可选 agent / web / nodes / all）" >&2; exit 2 ;;
esac

rc=0
case "$ACTION" in
    start)
        if [ "$TARGET" = all ]; then
            while read -r t; do start_one "$t" ${EXTRA[@]+"${EXTRA[@]}"} || rc=1; done \
                < <(all_targets)
        else
            start_one "$TARGET" ${EXTRA[@]+"${EXTRA[@]}"} || rc=1
        fi
        ;;
    stop)
        if [ "$TARGET" = all ]; then
            while read -r t; do stop_one "$t"; done < <(all_targets_rev)
        else
            stop_one "$TARGET"
        fi
        ;;
    restart)
        # 先停再起，避免新旧进程抢同一个端口 / 同一个摄像头。
        if [ "$TARGET" = all ]; then
            while read -r t; do stop_one "$t"; done < <(all_targets_rev)
            while read -r t; do start_one "$t" ${EXTRA[@]+"${EXTRA[@]}"} || rc=1; done \
                < <(all_targets)
        else
            stop_one "$TARGET"
            start_one "$TARGET" ${EXTRA[@]+"${EXTRA[@]}"} || rc=1
        fi
        ;;
    status)
        show_status || rc=1
        ;;
    *)
        echo "用法：tools/service.sh {start|stop|restart|status|logs} [agent|web|nodes|all]" >&2
        exit 2
        ;;
esac

exit "$rc"
