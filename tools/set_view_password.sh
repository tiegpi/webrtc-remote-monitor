#!/bin/bash
# 一条命令改观看端 Basic Auth 密码，并让板子同步 —— 不用整包重部署。
#
#     bash tools/set_view_password.sh '新密码'
#     VIEW_USER=gtie bash tools/set_view_password.sh '新密码'
#     VIEW_PASS='新密码' bash tools/set_view_password.sh     # 密码不进 shell 历史
#
# 做的事：
#   1. VPS：重写 /etc/nginx/webrtc-view.htpasswd（apr1）和
#      /root/webrtc-view.credentials（明文，部署脚本下次复用）
#   2. VPS：nginx -t + reload（不重启信令服务，推流不断）
#   3. 板子：改 .env 的 SIGNAL_AUTH=user:pass（600）
#   4. 板子：重启 device_agent / web_server
#   5. 自检：未登录 401、登录后 200、板子 /ice.json 仍能取到 TURN
#
# 为什么密码只允许 [A-Za-z0-9._-]：
#   板子 .env 是被 bash source 的，$、反引号、引号、空格会把它弄坏；
#   冒号会把 user:pass 的拆分弄乱。这个字符集对公网 Basic Auth 足够。
#
# 和 tools/deploy_web_vps.sh 的关系：那个是完整部署（会重启信令服务），
# 这个只改凭证。两者写的是同一组文件，不会互相打架。
set -euo pipefail

VPS_HOST="${VPS_HOST:-ubuntu@124.223.11.112}"
BOARD_HOST="${BOARD_HOST:-orangepi@192.168.2.187}"
BOARD_DIR="${BOARD_DIR:-/home/orangepi/webrtc-remote-monitor}"
VIEW_URL="${VIEW_URL:-https://124.223.11.112/webrtc}"
VIEW_USER="${VIEW_USER:-viewer}"

die() { echo "错误：$*" >&2; exit 1; }

# 密码来源：位置参数 > VIEW_PASS > 交互输入（只读，不回显）
PASS="${1:-${VIEW_PASS:-}}"
if [ -z "$PASS" ]; then
    if [ ! -t 0 ]; then
        die "没给密码。用法：bash tools/set_view_password.sh '新密码'"
    fi
    read -rsp "新密码: " PASS; echo
    read -rsp "再输一次: " PASS2; echo
    [ "$PASS" = "$PASS2" ] || die "两次输入不一致"
fi

[[ "$VIEW_USER" =~ ^[A-Za-z0-9._-]+$ ]] \
    || die "用户名只能用字母、数字、. _ -（当前：$VIEW_USER）"
[[ "$PASS" =~ ^[A-Za-z0-9._-]+$ ]] \
    || die "密码只能用字母、数字、. _ -（.env 会被 bash source，其他字符会弄坏它）"
[ "${#PASS}" -ge 8 ] || die "密码太短，至少 8 位"

echo "==> 检查连通性"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$VPS_HOST" true \
    || die "连不上 VPS：$VPS_HOST"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$BOARD_HOST" true \
    || die "连不上板子：$BOARD_HOST"

# 用 base64 传，避免密码里的字符被远端 shell 解释。远端自己算 apr1，
# 明文不经过命令行参数以外的任何日志。
USER_B64="$(printf %s "$VIEW_USER" | base64 | tr -d '\n')"
PASS_B64="$(printf %s "$PASS" | base64 | tr -d '\n')"

echo "==> 更新 VPS（htpasswd + 明文凭证 + nginx reload）"
ssh "$VPS_HOST" "sudo env VIEW_USER_B64='$USER_B64' VIEW_PASS_B64='$PASS_B64' bash -s" <<'REMOTE'
set -euo pipefail
U="$(printf %s "$VIEW_USER_B64" | base64 -d)"
P="$(printf %s "$VIEW_PASS_B64" | base64 -d)"
AUTH_FILE=/etc/nginx/webrtc-view.htpasswd
CRED_FILE=/root/webrtc-view.credentials
printf '%s:%s\n' "$U" "$(openssl passwd -apr1 "$P")" > "$AUTH_FILE"
printf '%s\n%s\n' "$U" "$P" > "$CRED_FILE"
chown root:www-data "$AUTH_FILE"
chmod 640 "$AUTH_FILE"
chmod 600 "$CRED_FILE"
nginx -t >/dev/null
systemctl reload nginx
echo "  htpasswd 已重写，nginx 已 reload"
REMOTE

echo "==> 更新板子 .env"
# 板子 .env 的 SIGNAL_AUTH 必须和 VPS 是同一组，否则板子取 /ice.json、
# POST /diag、代理 /ws/view 全部 401（页面能开，但没有 ICE 配置和统计）。
SIGNAL_AUTH_B64="$(printf '%s:%s' "$VIEW_USER" "$PASS" | base64 | tr -d '\n')"
ssh "$BOARD_HOST" "SIGNAL_AUTH_B64='$SIGNAL_AUTH_B64' BOARD_DIR='$BOARD_DIR' bash -s" <<'REMOTE'
set -euo pipefail
SA="$(printf %s "$SIGNAL_AUTH_B64" | base64 -d)"
ENV_FILE="$BOARD_DIR/.env"
[ -s "$ENV_FILE" ] || { echo "板子缺 $ENV_FILE" >&2; exit 1; }
python3 - "$ENV_FILE" "$SA" <<'PY'
import os
import sys

path, value = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()
out, seen = [], False
for line in lines:
    if line.startswith("SIGNAL_AUTH="):
        out.append("SIGNAL_AUTH=" + value)
        seen = True
    else:
        out.append(line)
if not seen:
    out.append("SIGNAL_AUTH=" + value)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
echo "  已写入 SIGNAL_AUTH（600）"
REMOTE

echo "==> 重启板子进程"
ssh "$BOARD_HOST" "BOARD_DIR='$BOARD_DIR' bash -s" <<'REMOTE'
set -euo pipefail
cd "$BOARD_DIR"
pkill -f 'src/device_agent.py' || true
pkill -f 'src/web_server.py' || true
# web_server 优雅停机可能要十几秒（/ws/view 是长连接），但监听套接字会先关。
# 判据是 8443 有没有人 LISTEN，不是进程还在不在。
for _ in $(seq 1 40); do
    ss -ltn 2>/dev/null | grep -q ':8443 ' || break
    sleep 0.5
done
setsid nohup bash -l tools/run-agent.sh </dev/null >/tmp/run-agent.log 2>&1 &
setsid nohup bash tools/run-web.sh </dev/null >/tmp/run-web.log 2>&1 &
sleep 2
echo "  新进程："
pgrep -af 'src/device_agent.py|src/web_server.py' \
    | sed -e 's/--token [^ ]*/--token <已设置>/' \
          -e 's/--signal-auth [^ ]*/--signal-auth <已设置>/'
REMOTE

echo "==> 自检"
code() { curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "$1"; }
UNAUTH="$(code "$VIEW_URL")"
[ "$UNAUTH" = 401 ] || die "未登录访问 $VIEW_URL 应得 401，实际 $UNAUTH"
AUTH="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
        -u "$VIEW_USER:$PASS" "$VIEW_URL")"
[ "$AUTH" = 200 ] || die "登录后访问 $VIEW_URL 应得 200，实际 $AUTH"
echo "  VPS：未登录 401，登录后 200"

# 板子的 /ice.json 是它替局域网浏览器去 VPS 取的，走的就是 .env 里的凭证。
# 板子进程刚起，给它几秒。
ICE=""
for _ in $(seq 1 20); do
    ICE="$(curl -sk --max-time 5 "https://192.168.2.187:8443/ice.json" 2>/dev/null || true)"
    printf '%s' "$ICE" | grep -q . && break
    sleep 1
done
printf '%s' "$ICE" | grep -qi 'turn' \
    || die "板子 /ice.json 没取到 TURN —— 看 /tmp/run-web.log 和 /tmp/run-agent.log"

# agent 自己的启动日志是另一条路径（它自己取 /ice.json 建会话）。
# 它会先做亮度闭环再连信令，通常要 5~10 秒，所以轮询而不是固定 sleep。
OK=0
for _ in $(seq 1 30); do
    if ssh "$BOARD_HOST" 'grep -q "ICE 配置" /tmp/run-agent.log' 2>/dev/null; then
        OK=1
        break
    fi
    sleep 1
done
[ "$OK" = 1 ] || die "板子 agent 没拿到 ICE 配置 —— tail /tmp/run-agent.log"
echo "  板子：/ice.json 有 TURN，agent 日志确认拿到 ICE 配置"

echo
echo "完成。"
echo "  账号： $VIEW_USER"
echo "  密码： 已更新（不回显）；明文在 VPS /root/webrtc-view.credentials"
echo "  入口： $VIEW_URL"
echo "  注意： 浏览器缓存的旧密码会立即失效，页面会重新弹登录框。"
