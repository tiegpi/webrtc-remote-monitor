#!/usr/bin/env bash
# 在公网 VPS 上部署**信令服务器 + coturn(STUN/TURN)**。
#
# 用法（在 VPS 上，先把本目录传上去）:
#     sudo bash deploy_vps.sh
#
# 它会:
#   1. 探测内网/公网 IP
#   2. 装 coturn + python3-aiohttp
#   3. 生成 TURN 密钥 / 房间名 / token（已存在则复用，不覆盖）
#   4. 写 /etc/turnserver.conf、/etc/webrtc-signal.env、webrtc-signal.service
#   5. 起服务并自检
#   6. 打印安全组要放行的端口
#
# 目录/端口（2026-10-17 统一后）:
#   /opt/webrtc-signal/signal_server.py   唯一信令源码（本仓库 src/signal_server.py）
#   127.0.0.1:8443                        只监听回环；外网入口是 nginx 443
#   观看页和 nginx 路由不在这里装 —— 在开发机上跑 tools/deploy_web_vps.sh。
#
# turnserver.conf 由本脚本生成而不是单独放一份模板文件：公网 IP、内网 IP 都靠探测，
# 单独放模板会变成两份会互相漂移的"真相"。
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "请用 root 跑：sudo bash deploy_vps.sh" >&2; exit 1; }

# 本脚本在 tools/ 下，所以仓库根是它的上一级。
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_SRC="$SRC/src"
DEST=/opt/webrtc-signal
ENV_FILE=/etc/webrtc-signal.env
# 统一端口。nginx 的 snippet 也按 8443 生成；改这里要同步改 deploy_web_vps.sh。
SIGNAL_PORT=8443

# ---- 凭证：已存在就复用。
# 每次重跑都重新生成的话，会把已经连着的推流端和正在用的 TURN 凭证一起作废，
# 而部署脚本多半是"改了点东西再跑一遍"，不该有这种副作用。
if [ -f "$ENV_FILE" ]; then
    echo "==> 复用已有的 $ENV_FILE"
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi
PUBLIC_IP="${PUBLIC_IP:-}"
TURN_SECRET="${TURN_SECRET:-}"
ROOM="${ROOM:-}"
TOKEN="${TOKEN:-}"
# 统一端口；不要让 env 里的历史 SIGNAL_PORT 覆盖它 ——
# nginx 的 snippet 也按 8443 生成，两边必须一致。
SIGNAL_PORT=8443

# 先装依赖再探测 IP：探测公网 IP 要用 curl，而 curl 不保证预装。
# 反过来的话，干净镜像上会以"探测不到公网 IP"这种极具误导性的方式失败。
echo "==> 安装依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq coturn python3-aiohttp curl

echo "==> 探测 IP"
# 腾讯云有实例元数据接口，拿公网 IP 最准
if [ -z "$PUBLIC_IP" ]; then
    PUBLIC_IP="$(curl -s --max-time 3 http://metadata.tencentyun.com/latest/meta-data/public-ipv4 || true)"
fi
if ! [[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    PUBLIC_IP="$(curl -s --max-time 5 https://ifconfig.me || true)"
fi
if ! [[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "  !! 探测不到公网 IP，请手动指定：sudo PUBLIC_IP=1.2.3.4 bash deploy_vps.sh" >&2
    exit 1
fi

# 内网 IP 每次都重新探测（换机器/重建实例后网卡会变，缓存下来只会更难查）。
# 必须是默认路由对应的具体网卡地址，不能靠 0.0.0.0 自动枚举 ——
# 镜像上常有 docker0 之类的虚拟网卡，枚举出多余地址后单条 external-ip 就会映射错位。
PRIVATE_IP="$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1)"
[ -n "$PRIVATE_IP" ] || { echo "  !! 探测不到内网 IP" >&2; exit 1; }
echo "  公网 $PUBLIC_IP   内网 $PRIVATE_IP"

if [ "$PUBLIC_IP" = "$PRIVATE_IP" ]; then
    echo "  公网 IP 直接绑在网卡上，coturn 不需要 external-ip"
    EXTERNAL_LINE=""
else
    # 腾讯云 CVM 的网卡上是内网 IP，公网是 NAT 映射的。不写这行的话
    # TURN candidate 里会是 10.x，外网必然连不上。
    echo "  网卡上是内网 IP（NAT），coturn 需要 external-ip"
    EXTERNAL_LINE="external-ip=$PUBLIC_IP/$PRIVATE_IP"
fi

if [ -z "$TURN_SECRET" ] || [ -z "$ROOM" ] || [ -z "$TOKEN" ]; then
    echo "==> 生成凭证"
    TURN_SECRET="${TURN_SECRET:-$(openssl rand -hex 32)}"
    ROOM="${ROOM:-$(openssl rand -hex 4)}"
    TOKEN="${TOKEN:-$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)}"
    cat > "$ENV_FILE" <<EOF
PUBLIC_IP=$PUBLIC_IP
SIGNAL_PORT=$SIGNAL_PORT
TURN_SECRET=$TURN_SECRET
ROOM=$ROOM
TOKEN=$TOKEN
EOF
    chmod 600 "$ENV_FILE"
fi

echo "==> 写 /etc/turnserver.conf"
cat > /etc/turnserver.conf <<EOF
# 由 deploy_vps.sh 生成。改完要 systemctl restart coturn。
listening-port=3478
listening-ip=$PRIVATE_IP
relay-ip=$PRIVATE_IP
$EXTERNAL_LINE

# relay 端口收窄，方便安全组放行。每路观看者占一个端口，40 个够测试台用
min-port=49160
max-port=49200

fingerprint
# use-auth-secret 内部会自动启用 lt-cred-mech，两个都写 coturn 会报错
use-auth-secret
static-auth-secret=$TURN_SECRET
realm=webrtc-test
stale-nonce=600

no-cli
no-multicast-peers
# 不要照抄网上老教程里的 no-loopback-peers：新版 coturn 已经移除了这个选项，
# 写了会直接拒绝启动。要确认当前版本支持哪些：
#     turnserver --help | grep loopback

# 别让这台机器被拿去当中继去打 VPC 内网或做扫描（会被云厂商封号）
user-quota=12
total-quota=100
max-bps=3000000
denied-peer-ip=10.0.0.0-10.255.255.255
denied-peer-ip=127.0.0.0-127.255.255.255
denied-peer-ip=169.254.0.0-169.254.255.255
denied-peer-ip=172.16.0.0-172.31.255.255
denied-peer-ip=192.168.0.0-192.168.255.255
denied-peer-ip=100.64.0.0-100.127.255.255

no-tls
no-dtls
# log-file=stdout：让 coturn 把日志写到标准输出，systemd 会收进 journal，
# journalctl -u coturn 就能看全（不写这行的话 coturn 默认走 syslog，不进 journal）。
# **不要**写成文件路径：实测 /var/log/turnserver.log 属于 root，而 coturn 以
# turnserver 用户运行，启动直接报 "Cannot open log file for writing" 并且日志全丢，
# 排查时最需要的那几行（external-ip 到底生效没有）恰好就看不到。
log-file=stdout
simple-log
EOF

# Debian/Ubuntu 的 coturn 包默认不自启，要显式打开
if [ -f /etc/default/coturn ]; then
    sed -i 's/^#*TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn
    grep -q '^TURNSERVER_ENABLED=1' /etc/default/coturn \
        || echo 'TURNSERVER_ENABLED=1' >> /etc/default/coturn
fi

echo "==> 部署信令服务器到 $DEST"
mkdir -p "$DEST"
install -m 644 "$PY_SRC/signal_server.py" "$DEST/signal_server.py"

id -u webrtc >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin webrtc

# 只监听回环：外网唯一入口是 nginx 443（由 deploy_web_vps.sh 配置）。
# --ice-mode turn 必须显式给：这个版本的默认是 none（局域网形态），
# 不写的话 /ice.json 里没有 TURN，跨网观看必然连不上。
cat > /etc/systemd/system/webrtc-signal.service <<EOF
[Unit]
Description=WebRTC signaling server (remote-monitor)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
WorkingDirectory=$DEST
ExecStart=/usr/bin/python3 $DEST/signal_server.py \\
    --host 127.0.0.1 --port $SIGNAL_PORT \\
    --public-ip \${PUBLIC_IP} --turn-secret \${TURN_SECRET} \\
    --ice-mode turn --room \${ROOM} --token \${TOKEN}
Restart=always
RestartSec=3
User=webrtc

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable coturn webrtc-signal
# 必须 restart，不能写 enable --now：
# apt 装 coturn 时 systemd 已经用**发行版默认配置**把它拉起来了，此时 enable --now
# 看到服务已在运行就什么都不做，coturn 会继续用旧配置跑下去。症状极具迷惑性 ——
# external-ip / use-auth-secret / min-port 全部不生效（中继地址是内网 IP、
# 匿名也能 Allocate、中继端口跑到 49160-49200 之外），但 journalctl 里一条报错都没有，
# 只有一行容易忽略的 "NO EXPLICIT RELAY ADDRESS(ES) ARE CONFIGURED"。
# 实测踩过这一次，下面的 cfg_age_check 就是专门用来抓它的。
systemctl restart coturn
systemctl restart webrtc-signal

echo "==> 等待服务起来"
sleep 3

echo
echo "==> 自检"
fail=0
check() {
    if eval "$2" >/dev/null 2>&1; then echo "  [OK]   $1"; else echo "  [FAIL] $1"; fail=1; fi
}

# 服务必须是在配置文件最后一次修改**之后**启动的，否则就是"改了配置但没重启"。
# 这种情况 coturn 会静默地继续用旧配置，不是崩溃而是**不生效**，最难查。
cfg_age_check() {
    local unit="$1" conf="$2" start mtime
    start="$(date -d "$(systemctl show -p ActiveEnterTimestamp --value "$unit")" +%s 2>/dev/null || echo 0)"
    mtime="$(stat -c %Y "$conf" 2>/dev/null || echo 0)"
    [ "$start" -ge "$mtime" ]
}

check "coturn 正在运行"                 "systemctl is-active --quiet coturn"
check "coturn 加载的是最新配置"         "cfg_age_check coturn /etc/turnserver.conf"
check "信令服务加载的是最新配置"        "cfg_age_check webrtc-signal /etc/systemd/system/webrtc-signal.service"
check "信令服务器正在运行"               "systemctl is-active --quiet webrtc-signal"
check "UDP 3478 在监听"                 "ss -lun | grep -q ':3478'"
check "信令 $SIGNAL_PORT 只监听回环"     "ss -ltn | grep -q '127.0.0.1:$SIGNAL_PORT'"
check "信令没有暴露到 0.0.0.0"           "! ss -ltn | grep -q '0.0.0.0:$SIGNAL_PORT'"
check "/healthz 有响应"                 "curl -sf --max-time 3 http://127.0.0.1:$SIGNAL_PORT/healthz"
check "/ice.json 有响应"                "curl -sf --max-time 3 http://127.0.0.1:$SIGNAL_PORT/ice.json"
check "/ice.json 带 TURN 凭证"           "curl -sf --max-time 3 http://127.0.0.1:$SIGNAL_PORT/ice.json | grep -q turn"
check "turnserver.conf 没写已废弃选项"  "! grep -q '^no-loopback-peers' /etc/turnserver.conf"

echo
if [ "$fail" -ne 0 ]; then
    echo "有检查项失败，看日志："
    echo "  journalctl -u webrtc-signal -n 50 --no-pager"
    echo "  journalctl -u coturn        -n 50 --no-pager"
    exit 1
fi

cat <<EOF
------------------------------------------------------------
  腾讯云安全组需要放行（和主机防火墙两层都要放）:
    TCP  443                 nginx（外网唯一入口，信令+页面都走这里）
    UDP  3478                STUN / TURN
    TCP  3478                TURN over TCP（兜底）
    UDP  49160-49200         relay 中继端口（与 min/max-port 一致）
    TCP  22                  SSH

  注意：$SIGNAL_PORT 现在只监听 127.0.0.1，不需要对外放行。

  下一步（在开发机上）:
    bash tools/deploy_web_vps.sh
    # 装观看页 + nginx 443 路由；之后：
    #   外网观看   https://$PUBLIC_IP/webrtc
    #   推流端     wss://$PUBLIC_IP/ws/publish（房间 $ROOM）

  验证 TURN 真的生效（**必须在另一台机器上跑**，在 VPS 本机测不出 NAT 问题）:
    read -r U W < <(curl -s http://127.0.0.1:$SIGNAL_PORT/ice.json | python3 -c \\
      "import json,sys; t=[s for s in json.load(sys.stdin)['iceServers'] if 'username' in s][0]; print(t['username'], t['credential'])")
    python3 tools/turn_check.py $PUBLIC_IP "\$U" "\$W" 3478
    - 中继地址必须是 $PUBLIC_IP，是 10.x 就说明 external-ip 没生效
    - 中继端口必须落在 49160-49200，不在就说明 min-port/max-port 没加载
    - 不带凭证的 Allocate 必须被拒（0x0113），否则这台机器是开放中继

  决定性判据（媒体正在流的时候）:
    systemctl stop coturn      # 画面必须在 10-30 秒内断掉
    停了还在动 = 走的是 host/srflx 直连，不是 TURN
------------------------------------------------------------
EOF
