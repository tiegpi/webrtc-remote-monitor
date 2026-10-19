#!/bin/bash
# 从开发机管理 VPS 上的 systemd 服务（不登服务器也能 start/stop/status）。
#
#     VPS_HOST=ubuntu@<vps> bash tools/service_vps.sh status
#     VPS_HOST=ubuntu@<vps> bash tools/service_vps.sh restart
#     VPS_HOST=ubuntu@<vps> bash tools/service_vps.sh stop web
#     VPS_HOST=ubuntu@<vps> bash tools/service_vps.sh logs signal -f
#
# 服务名（短名 → systemd unit）：
#     signal  webrtc-signal    信令服务器，127.0.0.1:8443
#     web     webrtc-web       外网观看页，127.0.0.1:8444
#     coturn  coturn           STUN / TURN
#     nginx   nginx            443 入口、Basic Auth、反向代理
#
# 目标省略时是 all（四个都管）。start/stop/restart 走 sudo，要求该账号
# 能免密 sudo（部署脚本 deploy_web_vps.sh 用的也是这个假设）。
#
# 板子上的两个进程没有 systemd，用 tools/service.sh。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f .env ]; then . ./.env; fi
: "${VPS_HOST:?没设 VPS_HOST —— VPS 的 ssh 目标，如 ubuntu@1.2.3.4}"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 "$VPS_HOST")

unit_of() {
    case "$1" in
        signal) echo webrtc-signal ;;
        web)    echo webrtc-web ;;
        coturn) echo coturn ;;
        nginx)  echo nginx ;;
        *) return 1 ;;
    esac
}

resolve_targets() {
    case "${1:-all}" in
        all)    echo "webrtc-signal webrtc-web coturn nginx" ;;
        signal|web|coturn|nginx) unit_of "$1" ;;
        *) echo "未知服务：$1（可选 signal / web / coturn / nginx / all）" >&2; return 2 ;;
    esac
}

ACTION="${1:-status}"
TARGET="${2:-all}"
UNITS="$(resolve_targets "$TARGET")" || exit 2

case "$ACTION" in
    status)
        "${SSH[@]}" "
            for u in $UNITS; do
                state=\$(systemctl is-active \$u 2>/dev/null || true)
                enabled=\$(systemctl is-enabled \$u 2>/dev/null || true)
                printf '%-16s %-10s %s\n' \"\$u\" \"\${state:-unknown}\" \"\$enabled\"
            done
            echo
            echo -n '信号 /healthz: '
            curl -sf --max-time 3 http://127.0.0.1:8443/healthz || echo '无响应'
            echo
            echo -n '观看页 /healthz: '
            curl -sf --max-time 3 http://127.0.0.1:8444/healthz || echo '无响应'
            echo
        "
        ;;
    start|stop|restart)
        # stop/start 的顺序由 systemd 自己保证（webrtc-web After=webrtc-signal）。
        # is-active 在 stop 之后本来就是非零退出码，所以单独打一行状态、不参与判断。
        "${SSH[@]}" "sudo systemctl $ACTION $UNITS; systemctl is-active $UNITS || true"
        ;;
    logs)
        if [ "${3:-}" = "-f" ]; then
            exec ssh -t -o ConnectTimeout=10 "$VPS_HOST" "sudo journalctl -u $UNITS -n 100 -f"
        fi
        "${SSH[@]}" "sudo journalctl -u $UNITS -n 100 --no-pager"
        ;;
    *)
        echo "用法：tools/service_vps.sh {start|stop|restart|status|logs} [signal|web|coturn|nginx|all] [-f]" >&2
        exit 2
        ;;
esac
