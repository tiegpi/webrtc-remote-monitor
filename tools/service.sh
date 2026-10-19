#!/bin/bash
# 板子侧服务管理：device_agent（推流）+ web_server（局域网观看页）。
#
#     tools/service.sh start   [agent|web|all]   # 默认 all
#     tools/service.sh stop    [agent|web|all]
#     tools/service.sh restart [agent|web|all]
#     tools/service.sh status
#     tools/service.sh logs    [agent|web] [-f]
#
# 和直接跑 run-agent.sh / run-web.sh 的区别：
#   - 进程起在独立会话里（setsid），ssh 断开不会被 SIGHUP 带走；
#   - pid 写在 run/*.pid，日志追加到 run/agent.log / run/web.log；
#   - start 会等到服务真的可用才返回（页面 healthz / 推流端连上信令），
#     失败时直接打出日志尾部，而不是留一个"看起来启动了"的死进程；
#   - stop 也会收拾手动起、没有 pid 文件的进程（按命令行找）。
#
# agent 必须用登录 shell（bash -l）起：BSP 的 GST_V4L2_USE_LIBV4L2 等变量
# 只在 /etc/profile.d 里导出。run-agent.sh 内部会 exec python3，所以 pid
# 文件里的进程号在 exec 前后是同一个。
#
# 依赖 .env（ROOM / TOKEN / SIGNAL_HOST / SIGNAL_AUTH），和 run-*.sh 共用。
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
        *)
            echo "未知服务：$1（可选 agent / web / all）" >&2
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
    kind_var "$1" || return 2

    local pid; pid="$(find_pid "$PID_FILE" "$PATTERN")" || true
    if [ -n "$pid" ]; then
        echo "$LABEL 已在运行（pid $pid）"
        return 0
    fi

    [ -f .env ] || { echo "缺 .env：cp .env.example .env 并填写" >&2; return 1; }

    local start_line=0
    [ -f "$LOG_FILE" ] && start_line="$(wc -l < "$LOG_FILE")"

    echo "启动 $LABEL …"
    if [ "$1" = agent ]; then
        # 登录 shell：BSP 的 GStreamer 环境变量只在那里导出。
        setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' \
            _ "$PID_FILE" bash -l tools/run-agent.sh \
            </dev/null >>"$LOG_FILE" 2>&1 &
    else
        setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' \
            _ "$PID_FILE" bash tools/run-web.sh \
            </dev/null >>"$LOG_FILE" 2>&1 &
    fi

    local deadline=$((SECONDS + 20))
    while [ "$SECONDS" -lt "$deadline" ]; do
        pid="$(read_pidfile "$PID_FILE")" || true
        if [ -n "$pid" ] && ! alive "$pid"; then
            echo "$LABEL 启动后立刻退出了。" >&2
            rm -f "$PID_FILE"
            tail_log
            return 1
        fi
        if [ "$1" = agent ]; then
            # 连上信令服务器就说明 .env、证书和网络都通了；摄像头管线要等
            # 观看端接入才建，不应作为启动成功的条件。
            if [ -n "$pid" ] && tail -n +"$((start_line + 1))" "$LOG_FILE" 2>/dev/null \
                    | grep -q "已连上信令服务器"; then
                echo "$LABEL 已启动（pid $pid）"
                return 0
            fi
        else
            if curl -skf --max-time 2 "$WEB_URL" >/dev/null 2>&1; then
                echo "$LABEL 已启动（pid ${pid:-?}，$WEB_URL 有响应）"
                return 0
            fi
        fi
        sleep 0.5
    done

    echo "$LABEL 启动超时（20 秒）。" >&2
    tail_log
    return 1
}

# ------------------------------------------------------------------ stop

stop_one() {
    kind_var "$1" || return 2

    local pid; pid="$(find_pid "$PID_FILE" "$PATTERN")" || true
    if [ -z "$pid" ]; then
        rm -f "$PID_FILE"
        echo "$LABEL 没有在运行"
        return 0
    fi

    echo "停止 $LABEL（pid $pid）…"
    kill -TERM "$pid" 2>/dev/null || true
    local deadline=$((SECONDS + 10))
    while alive "$pid" && [ "$SECONDS" -lt "$deadline" ]; do
        sleep 0.2
    done
    if alive "$pid"; then
        echo "  10 秒内没有退出，发 SIGKILL" >&2
        kill -KILL "$pid" 2>/dev/null || true
        sleep 0.5
    fi
    rm -f "$PID_FILE"
    echo "$LABEL 已停止"
}

# ------------------------------------------------------------------ status

show_status() {
    local agent_pid web_pid health last
    agent_pid="$(find_pid "$AGENT_PID_FILE" "$AGENT_PATTERN")" || true
    web_pid="$(find_pid "$WEB_PID_FILE" "$WEB_PATTERN")" || true

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

    if [ -z "$agent_pid" ] && [ -z "$web_pid" ]; then
        return 1
    fi
    return 0
}

# ------------------------------------------------------------------ logs

show_logs() {
    case "$1" in
        agent) kind_var agent || return 2 ;;
        web)   kind_var web   || return 2 ;;
        *)
            echo "用法：tools/service.sh logs [agent|web] [-f]" >&2
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

ACTION="${1:-status}"
TARGET="${2:-all}"

if [ "$ACTION" = logs ]; then
    show_logs "${2:-agent}" "${3:-}"
    exit $?
fi

if [ "$TARGET" != all ] && [ "$TARGET" != agent ] && [ "$TARGET" != web ]; then
    echo "未知服务：$TARGET（可选 agent / web / all）" >&2
    exit 2
fi

rc=0
case "$ACTION" in
    start)
        if [ "$TARGET" = all ]; then
            start_one web || rc=1
            start_one agent || rc=1
        else
            start_one "$TARGET" || rc=1
        fi
        ;;
    stop)
        # agent 先停：web 的 /camera 代理不该打到一个正在拆的会话上。
        if [ "$TARGET" = all ]; then
            stop_one agent; stop_one web
        else
            stop_one "$TARGET"
        fi
        ;;
    restart)
        # 先停再起，避免新旧进程抢同一个端口 / 同一个摄像头。
        if [ "$TARGET" = all ]; then
            stop_one agent; stop_one web
            start_one web || rc=1
            start_one agent || rc=1
        else
            stop_one "$TARGET"
            start_one "$TARGET" || rc=1
        fi
        ;;
    status)
        show_status || rc=1
        ;;
    *)
        echo "用法：tools/service.sh {start|stop|restart|status|logs} [agent|web|all]" >&2
        exit 2
        ;;
esac

exit "$rc"
