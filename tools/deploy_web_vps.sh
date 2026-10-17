#!/usr/bin/env bash
# 在公网 VPS 上部署**唯一**一套观看页 + 信令服务，让外网浏览器也能打开
# 同一个 monitor.html。
#
# 用法（在开发机上跑，需要能免密 ssh $VPS_HOST）:
#     bash tools/deploy_web_vps.sh
#     VPS_HOST=ubuntu@1.2.3.4 bash tools/deploy_web_vps.sh
#
# 部署后的两条观看路径（同一个页面，两个来源）:
#
#     局域网  浏览器 → https://<板子>:8443/ → 板子的 web_server（原有路径，无鉴权）
#     外网    浏览器 → https://124.223.11.112/webrtc → nginx(Basic Auth) → VPS web_server
#
# 媒体（RTP）在两种情况下都**不经过这个 web_server**：局域网是 host candidate
# 直连板子；跨网是 TURN 中继，走 VPS 上的 coturn。这里只送页面和信令。
#
# 目录收敛（2026-10-17）:
#   /opt/webrtc-signal       唯一信令服务，signal_server.py 来自本仓库
#   /opt/webrtc-web-server   唯一观看页服务，**平铺**放 web_server.py + monitor.html
#   旧的 /opt/webrtc-signal-next、/opt/webrtc-signal-monitor、/opt/webrtc_web_server
#   都是同功能的历史副本，本脚本会把它们清掉。
#
# 端口路由:
#   nginx 443 是外网唯一入口：
#       /ws/publish              → 127.0.0.1:8443（推流端，token 鉴权，不走 Basic Auth）
#       其余观看相关路径          → Basic Auth 之后才转发
#       /webrtc                  → 127.0.0.1:8444（观看页）
#       /config.json /camera     → 127.0.0.1:8444
#       /ws/view /diag /ice.json → 127.0.0.1:8443
#       其他一切                  → 404
#   两个上游都只监听回环，外网够不到。
#   9443 只是旧 camera-pipeline 实例占着 8443 时的临时绕行；现在 8443 上
#   跑的就是统一版信令，9443 没有存在的理由，随 webrtc-signal-monitor 一起退掉。
#
# 访问控制（2026-10-17 增加）:
#   外网 IP 是公开的，仅靠"路径不好猜"不算权限。观看页、/config.json（房间号）、
#   /ice.json（TURN 凭证）、/diag、/camera、/ws/view 全部要求 HTTP Basic Auth；
#   根路径 / 直接 404，不再 302 到 /webrtc，避免把入口暴露给任何知道 IP 的人。
#   凭证首次部署时随机生成并打印，之后复用；可用 VIEW_USER / VIEW_PASS 覆盖：
#       VIEW_PASS='...' bash tools/deploy_web_vps.sh
#   只改密码别用这个脚本（它会重启信令服务，且不会自动改板子）——用
#       bash tools/set_view_password.sh '新密码'
#   它同步 VPS + 板子 .env + 重启板子进程，并自检。
#   推流端 /ws/publish 仍只认 signal_server.py 的 --token；刻意不套 Basic Auth，
#   让观看凭证和推流凭证分开，改密码不打断推流。
#   板子自己也要访问 VPS 的 /ice.json、/diag、/ws/view（局域网页面靠它中转），
#   所以板子 .env 里必须写同一组凭证：SIGNAL_AUTH=user:pass。换密码时两边都要改。
#
# 已知限制（不是漏了，是拓扑决定的）:
#   /camera 在 VPS 上找不到 device_agent 的 unix socket，会明确返回 502，
#   页面上表现为"取摄像头列表失败，不显示切换按钮"。视频/语音不受影响。
#   要支持公网切摄像头，得让板子主动开一条 SSH 反向 unix socket 隧道到 VPS，
#   属于另一件事，见 docs/architecture.md 的说明。
set -euo pipefail

VPS_HOST="${VPS_HOST:-ubuntu@124.223.11.112}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 观看端账号。密码留空 = 首次随机生成、之后复用 /root/webrtc-view.credentials。
# 用 base64 传给远端，避免密码里的引号 / $ / 空格把远端 shell 搞坏。
VIEW_USER="${VIEW_USER:-viewer}"
VIEW_PASS="${VIEW_PASS:-}"
VIEW_USER_B64="$(printf %s "$VIEW_USER" | base64 | tr -d '\n')"
VIEW_PASS_B64="$(printf %s "$VIEW_PASS" | base64 | tr -d '\n')"

echo "==> 目标 $VPS_HOST"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$VPS_HOST" true

echo "==> 上传文件"
ssh "$VPS_HOST" 'sudo mkdir -p /opt/webrtc-signal /opt/webrtc-web-server'
scp -q "$SRC/src/web_server.py"    "$VPS_HOST:/tmp/webrtc-web_server.py"
scp -q "$SRC/src/signal_server.py" "$VPS_HOST:/tmp/webrtc-signal_server.py"
scp -q "$SRC/web/monitor.html"     "$VPS_HOST:/tmp/webrtc-monitor.html"

echo "==> 在 VPS 上安装并配置"
ssh "$VPS_HOST" "sudo env VIEW_USER_B64='$VIEW_USER_B64' VIEW_PASS_B64='$VIEW_PASS_B64' bash -s" <<'REMOTE'
set -euo pipefail

if [ ! -f /etc/webrtc-signal.env ]; then
    echo "缺少 /etc/webrtc-signal.env（PUBLIC_IP / TURN_SECRET / ROOM / TOKEN），拒绝继续"
    exit 1
fi

# ---------------------------------------------------------------- 观看端账号
# htpasswd 只存 apr1 哈希；/root/webrtc-view.credentials 存一份明文，
# 一是重跑时能复用同一组账号密码（不然每次部署都换密码，所有设备要重输），
# 二是自检需要它去验证"登录后真的能拿到页面"。两个文件都只有 root 可读
# （htpasswd 额外给 www-data 读，nginx worker 要它）。
VIEW_USER="$(printf %s "${VIEW_USER_B64:-}" | base64 -d 2>/dev/null || true)"
VIEW_PASS="$(printf %s "${VIEW_PASS_B64:-}" | base64 -d 2>/dev/null || true)"
VIEW_USER="${VIEW_USER:-viewer}"
AUTH_FILE=/etc/nginx/webrtc-view.htpasswd
CRED_FILE=/root/webrtc-view.credentials
CREDS_KNOWN=0

if [ -z "$VIEW_PASS" ] && [ -s "$CRED_FILE" ]; then
    { IFS= read -r VIEW_USER; IFS= read -r VIEW_PASS; } < "$CRED_FILE" || true
fi
if [ -n "$VIEW_PASS" ]; then
    CREDS_KNOWN=1
elif [ -s "$AUTH_FILE" ]; then
    # 有哈希但找不到明文（比如旧部署只留了 htpasswd）。不覆盖已有密码，
    # 只是这次没法做"登录成功"的自检。
    echo "  !! 复用已有 $AUTH_FILE，但找不到明文凭证；跳过登录后自检"
    echo "     要重置密码：VIEW_PASS='新密码' bash tools/deploy_web_vps.sh"
else
    VIEW_PASS="$(openssl rand -base64 24 | tr -d '/+=' | head -c 20)"
    CREDS_KNOWN=1
fi

if [ "$CREDS_KNOWN" = 1 ]; then
    printf '%s:%s\n' "$VIEW_USER" "$(openssl passwd -apr1 "$VIEW_PASS")" > "$AUTH_FILE"
    printf '%s\n%s\n' "$VIEW_USER" "$VIEW_PASS" > "$CRED_FILE"
    chmod 600 "$CRED_FILE"
    echo "  观看端账号已写入 $AUTH_FILE（用户 $VIEW_USER）"
fi
# nginx worker 以 www-data 运行；640 root:www-data 足够它读，其他人读不到。
chown root:www-data "$AUTH_FILE"
chmod 640 "$AUTH_FILE"

# ---------------------------------------------------------------- 收敛
# 只留两个目录。先把引用旧目录的 unit 停掉，避免删文件时还在跑。
systemctl stop webrtc-signal-monitor 2>/dev/null || true
systemctl stop webrtc-signal         2>/dev/null || true
systemctl stop webrtc-web            2>/dev/null || true
systemctl disable webrtc-signal-monitor 2>/dev/null || true

# 旧 unit / 旧副本目录 / 旧 web 目录。备份一下旧 8443 的 unit 和源码，
# 万一还要回头看 camera-pipeline 那版，至少不用去翻服务器镜像。
BACKUP="/root/webrtc-consolidation-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
cp -a /etc/systemd/system/webrtc-signal.service         "$BACKUP/" 2>/dev/null || true
cp -a /etc/systemd/system/webrtc-signal-monitor.service "$BACKUP/" 2>/dev/null || true
cp -a /opt/webrtc-signal/signal_server.py               "$BACKUP/old-signal_server-8443.py" 2>/dev/null || true
cp -a /opt/webrtc-signal/call.html                      "$BACKUP/" 2>/dev/null || true
cp -a /opt/webrtc-signal/viewer.html                    "$BACKUP/" 2>/dev/null || true
echo "  旧文件备份在 $BACKUP"

rm -f /etc/systemd/system/webrtc-signal-monitor.service
rm -rf /opt/webrtc-signal-next /opt/webrtc-signal-monitor /opt/webrtc_web_server

# 旧 /opt/webrtc-signal 里是 camera-pipeline 版的通话页和测试视频；
# 信令文件马上被覆盖，这些页面/视频没有路由再会用到，清掉。
rm -f /opt/webrtc-signal/call.html /opt/webrtc-signal/viewer.html
rm -f /opt/webrtc-signal/*.mp4

install -o root -g root -m 644 /tmp/webrtc-signal_server.py /opt/webrtc-signal/signal_server.py
# 平铺：web_server.py 和 monitor.html 直接放在 /opt/webrtc-web-server 下。
install -o root -g root -m 644 /tmp/webrtc-web_server.py    /opt/webrtc-web-server/web_server.py
install -o root -g root -m 644 /tmp/webrtc-monitor.html     /opt/webrtc-web-server/monitor.html
rm -f /tmp/webrtc-web_server.py /tmp/webrtc-monitor.html /tmp/webrtc-signal_server.py

# ---------------------------------------------------------------- 信令
# 只监听 127.0.0.1：外网入口只有 nginx 的 443。
# 源码统一用仓库的 src/signal_server.py；--ice-mode turn 必须显式给，
# 这个版本的默认是 none（局域网形态），不写的话 /ice.json 里没有 TURN。
cat > /etc/systemd/system/webrtc-signal.service <<'UNIT'
[Unit]
Description=WebRTC signaling server (remote-monitor)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/webrtc-signal.env
WorkingDirectory=/opt/webrtc-signal
ExecStart=/usr/bin/python3 /opt/webrtc-signal/signal_server.py \
    --host 127.0.0.1 --port 8443 \
    --public-ip ${PUBLIC_IP} --turn-secret ${TURN_SECRET} \
    --ice-mode turn --room ${ROOM} --token ${TOKEN}
Restart=always
RestartSec=3
User=webrtc

[Install]
WantedBy=multi-user.target
UNIT

# ---------------------------------------------------------------- 观看页
# RuntimeDirectory 会建 /run/webrtc-web（属主 webrtc）。现在里面没有
# agent.sock，所以 /camera 会 502；将来若给板子开反向隧道，socket 放这里。
# --web-dir 显式指到平铺目录，不依赖相对路径的默认解析。
cat > /etc/systemd/system/webrtc-web.service <<'UNIT'
[Unit]
Description=WebRTC remote monitor web page (VPS)
After=network-online.target webrtc-signal.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/webrtc-signal.env
WorkingDirectory=/opt/webrtc-web-server
RuntimeDirectory=webrtc-web
ExecStart=/usr/bin/python3 /opt/webrtc-web-server/web_server.py \
    --host 127.0.0.1 --port 8444 \
    --web-dir /opt/webrtc-web-server \
    --signal http://127.0.0.1:8443 \
    --room ${ROOM} \
    --agent-socket /run/webrtc-web/agent.sock
Restart=always
RestartSec=3
User=webrtc

[Install]
WantedBy=multi-user.target
UNIT

# ---------------------------------------------------------------- nginx
# 443 上的分派：
#   /ws/publish                → 8443（推流端，token 鉴权，不套 Basic Auth）
#   /ws/view /diag /ice.json   → 8443（观看端，Basic Auth）
#   /webrtc /monitor.html      → 8444（观看页，Basic Auth）
#   /config.json /camera       → 8444（Basic Auth）
#   /                          → 404（不再重定向，不向陌生人暴露入口）
#   其他一切                    → 404
#
# 为什么入口用**不带结尾斜杠**的 /webrtc：
#   页面里的 fetch("./config.json")、"./camera"、"./ice.json"、"./diag" 都是相对 URL。
#   浏览器对 /webrtc 的基准目录是 /，这些请求会落到 /config.json 等根路径；
#   如果入口是 /webrtc/，它们会变成 /webrtc/config.json，那些路径没有对应路由。
#   所以 /webrtc 是规范形式，/webrtc/ 统一 301 回来。
#
# 关于 auth_basic 与 WebSocket：
#   页面本身用 Basic Auth 登录后，浏览器会把凭证缓存到该 origin；同源的
#   fetch 和 WebSocket 握手都会自动带上 Authorization。不需要页面改代码。
#   （/ws/publish 例外，见上。）
cat > /etc/nginx/snippets/webrtc-monitor.conf <<'NGINX'
# 由 tools/deploy_web_vps.sh 生成。
#
# 观看相关路径全部要求 HTTP Basic Auth。`return` 指令在 rewrite 阶段执行，
# 早于 auth_basic 的 access 阶段，所以 / 的 404 和 /webrtc/ 的 301 不会先弹登录框；
# 它们只回状态码，不吐任何内容，而真正的入口 /webrtc 仍然要登录。

# 推流端。精确匹配，且**不能**加 auth_basic —— GStreamer 的 websocket
# 客户端不会处理 401 挑战，加了会直接把推流打死。它自己的 token 校验
# 在 signal_server.py 里，已经足够。
location = /ws/publish {
    # query string 里有推流 token，默认 access log 会原样记下来。
    access_log off;
    proxy_pass http://127.0.0.1:8443;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    # 信令是长连接。nginx 默认 60s 读超时会把空闲的 WebSocket 掐断，
    # 表现成"看着看着信令就断了"。
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}

# 观看端 WebSocket。浏览器登录过页面后会自动把同一组凭证带到这里。
location = /ws/view {
    auth_basic "webrtc monitor";
    auth_basic_user_file /etc/nginx/webrtc-view.htpasswd;
    proxy_pass http://127.0.0.1:8443;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}

# 遥测上报（板子 1Hz 计数、浏览器 getStats 摘要）。analyze_diag.py 读它。
location = /diag {
    auth_basic "webrtc monitor";
    auth_basic_user_file /etc/nginx/webrtc-view.htpasswd;
    proxy_pass http://127.0.0.1:8443;
    proxy_set_header Host $host;
}

location = /diag/clear {
    auth_basic "webrtc monitor";
    auth_basic_user_file /etc/nginx/webrtc-view.htpasswd;
    proxy_pass http://127.0.0.1:8443;
    proxy_set_header Host $host;
}

# TURN 凭证。必须保护：拿到 username/credential 的人可以白嫖这台中继
# （在凭证有效期内随便用 coturn 的带宽和出口）。
location = /ice.json {
    auth_basic "webrtc monitor";
    auth_basic_user_file /etc/nginx/webrtc-view.htpasswd;
    proxy_pass http://127.0.0.1:8443;
    proxy_set_header Host $host;
}

# 观看页入口。proxy_pass 末尾的 / 会把上游 URI 归一成 /，
# 等价于访问 web_server 的根路径；查询串（?room=...）原样带过去。
location = /webrtc {
    auth_basic "webrtc monitor";
    auth_basic_user_file /etc/nginx/webrtc-view.htpasswd;
    proxy_pass http://127.0.0.1:8444/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# 备用入口，同样保护。web_server 自己也认 /monitor.html。
location = /monitor.html {
    auth_basic "webrtc monitor";
    auth_basic_user_file /etc/nginx/webrtc-view.htpasswd;
    proxy_pass http://127.0.0.1:8444;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# 带斜杠的写法归一化。用 $is_args$args 保住 ?room=...。
# 这个 301 在鉴权之前发生，但它只回一个 Location，目标仍然要登录。
location = /webrtc/ {
    return 301 /webrtc$is_args$args;
}

# 根路径不再跳转。知道 IP 不等于有观看权，没必要主动告诉对方入口在哪。
location = / {
    return 404;
}

# 页面里的相对路径（./config.json、./camera）落到这里。
location = /config.json {
    auth_basic "webrtc monitor";
    auth_basic_user_file /etc/nginx/webrtc-view.htpasswd;
    proxy_pass http://127.0.0.1:8444;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location = /camera {
    auth_basic "webrtc monitor";
    auth_basic_user_file /etc/nginx/webrtc-view.htpasswd;
    proxy_pass http://127.0.0.1:8444;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# 其他一切都不对外暴露：/healthz、/web_server.py、favicon、目录探测……
location / {
    return 404;
}
NGINX

# 两处 nginx 站点改动：
#
#   80  → 统一 301 到 https。否则用户手敲 http://124.223.11.112/ 会看到
#         nginx 的默认欢迎页，像"没部署成功"；而且浏览器麦克风只在 https 的
#         安全上下文里存在。
#   443 → 原本有一个 location /（try_files 静态文件），会和 snippet 里的
#         location / 重复，nginx 直接拒绝启动。把它删掉，交给 snippet。
#
# 两处都带 marker，重复执行不会重复改。
python3 - <<'PY'
import pathlib
p = pathlib.Path('/etc/nginx/sites-available/default')
s = p.read_text()

# ---- 80: /whoami 之外也跳 https
if '# WEB_MONITOR_HTTP_REDIRECT' in s:
    print('  nginx 80 的跳转已经改过，跳过')
else:
    anchor80 = s.find('listen 80 default_server;')
    idx80 = s.find('location / {', anchor80) if anchor80 >= 0 else -1
    end80 = s.find('\n\t}', idx80) if idx80 >= 0 else -1
    if idx80 < 0 or end80 < 0:
        raise SystemExit('找不到 80 server 块里的 location /，拒绝改 nginx')
    s = (s[:idx80]
         + '# WEB_MONITOR_HTTP_REDIRECT\n'
         + '\tlocation / {\n\t\treturn 301 https://$host$request_uri;\n\t}'
         + s[end80 + 3:])
    print('  nginx 80 已改为统一跳 https')

# ---- 443: 删掉原来的 location /，交给 snippet
if '# WEB_MONITOR_LOCATION_ROOT' in s:
    print('  nginx 443 的 location / 已经改过，跳过')
else:
    anchor = s.find('listen 443 ssl http2 default_server;')
    if anchor < 0:
        raise SystemExit('找不到 443 server 块，拒绝继续改 nginx')
    idx = s.find('location / {', anchor)
    if idx < 0:
        raise SystemExit('找不到 443 server 块里的 location /')
    end = s.find('\n\t}', idx)
    if end < 0:
        raise SystemExit('location / 块没有找到闭合大括号')
    s = (s[:idx]
         + '# WEB_MONITOR_LOCATION_ROOT: location / 由 snippets/webrtc-monitor.conf 提供\n'
         + s[end + 3:])
    print('  已把 443 的 location / 交给 snippets/webrtc-monitor.conf')

p.write_text(s)
PY

nginx -t
systemctl daemon-reload
systemctl enable webrtc-signal webrtc-web >/dev/null
if ! systemctl restart webrtc-signal webrtc-web; then
    echo "服务启动失败："
    journalctl -u webrtc-signal -u webrtc-web -n 40 --no-pager
    exit 1
fi
systemctl reload nginx

sleep 2

# ---------------------------------------------------------------- 自检
fail=0
check() {
    if eval "$2" >/dev/null 2>&1; then echo "  [OK]   $1"; else echo "  [FAIL] $1"; fail=1; fi
}
check "信令(8443) 在监听"          "ss -ltn | grep -q '127.0.0.1:8443'"
check "观看页(8444) 在监听"        "ss -ltn | grep -q '127.0.0.1:8444'"
check "旧 9443 已退掉"             "! ss -ltn | grep -q ':9443 '"
check "信令服务 active"            "systemctl is-active --quiet webrtc-signal"
check "观看页服务 active"          "systemctl is-active --quiet webrtc-web"
check "旧 webrtc-signal-monitor 单元已删除" \
    "! systemctl list-unit-files | grep -q webrtc-signal-monitor"
check "旧副本目录已清掉"           \
    "[ ! -e /opt/webrtc-signal-next ] && [ ! -e /opt/webrtc-signal-monitor ] && [ ! -e /opt/webrtc_web_server ]"
check "web 目录已平铺"             \
    "[ -f /opt/webrtc-web-server/web_server.py ] && [ -f /opt/webrtc-web-server/monitor.html ] && [ ! -d /opt/webrtc-web-server/src ] && [ ! -d /opt/webrtc-web-server/web ]"
check "信令 /healthz"              "curl -sf --max-time 3 http://127.0.0.1:8443/healthz"
check "观看页 /healthz"            "curl -sf --max-time 3 http://127.0.0.1:8444/healthz"
check "观看页认到平铺目录"          "curl -sf --max-time 3 http://127.0.0.1:8444/healthz | grep -q 'webrtc-web-server'"
check "观看页 /config.json 有房间号" "curl -sf --max-time 3 http://127.0.0.1:8444/config.json | grep -q room"
# 未登录时应看到的：/ 404，观看路径 401。
code() { curl -sk -o /dev/null -w '%{http_code}' --max-time 3 "$1"; }
auth_ok() { curl -skf --max-time 3 -u "$VIEW_USER:$VIEW_PASS" "$1"; }
auth_code_is() {
    [ "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 3 \
         -u "$VIEW_USER:$VIEW_PASS" "$1")" = "$2" ]
}

check "htpasswd 已生成、权限 640"   "[ -s $AUTH_FILE ] && [ \"\$(stat -c %a $AUTH_FILE)\" = 640 ]"
check "443 / 返回 404（不再跳转）" "[ \"\$(code https://127.0.0.1/)\" = 404 ]"
check "443 /webrtc 未登录 401"     "[ \"\$(code https://127.0.0.1/webrtc)\" = 401 ]"
check "443 /monitor.html 未登录 401" "[ \"\$(code https://127.0.0.1/monitor.html)\" = 401 ]"
check "443 /config.json 未登录 401" "[ \"\$(code https://127.0.0.1/config.json)\" = 401 ]"
check "443 /ice.json 未登录 401"   "[ \"\$(code https://127.0.0.1/ice.json)\" = 401 ]"
check "443 /diag 未登录 401"       "[ \"\$(code https://127.0.0.1/diag)\" = 401 ]"
check "443 /ws/view 未登录 401"    "[ \"\$(code https://127.0.0.1/ws/view)\" = 401 ]"
# 推流端必须不受 Basic Auth 影响（没有 Upgrade 头时信令会回 400，但不是 401）。
check "443 /ws/publish 不走 Basic Auth" \
    "[ \"\$(code 'https://127.0.0.1/ws/publish?room=x&token=y')\" != 401 ]"
check "443 /healthz 不对外暴露"    "[ \"\$(code https://127.0.0.1/healthz)\" = 404 ]"
# 平铺部署把 web_server.py 放进了页面目录；catch-all 必须挡住源码下载。
check "443 /web_server.py 不可下载" "[ \"\$(code https://127.0.0.1/web_server.py)\" = 404 ]"
check "443 /webrtc/ 跳到 /webrtc"  "[ \"\$(code https://127.0.0.1/webrtc/)\" = 301 ]"
check "80 统一跳转到 https"        \
    "[ \"\$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1/)\" = 301 ]"

# 登录后的正向验证。只有在知道明文密码时才做（首次部署/显式传 VIEW_PASS）。
if [ "$CREDS_KNOWN" = 1 ]; then
    check "443 /webrtc 登录后返回 monitor.html" \
        "auth_ok https://127.0.0.1/webrtc | grep -q '远程监视'"
    check "443 /config.json 登录后有房间号" \
        "auth_ok https://127.0.0.1/config.json | grep -q room"
    check "443 /ice.json 登录后有 TURN 凭证" \
        "auth_ok https://127.0.0.1/ice.json | grep -q turn"
    # /camera 在 VPS 上没有 agent socket，必须是一个**明确的** 502。
    check "443 /camera 登录后 502（预期）" \
        "auth_code_is https://127.0.0.1/camera 502"
else
    echo "  [SKIP] 明文密码未知，跳过登录后的正向验证"
fi

echo
if [ "$fail" -ne 0 ]; then
    echo "有检查项失败："
    echo "  journalctl -u webrtc-signal -n 50 --no-pager"
    echo "  journalctl -u webrtc-web -n 50 --no-pager"
    echo "  tail -50 /var/log/nginx/error.log"
    exit 1
fi

echo "部署完成。"
echo "  外网观看页： https://124.223.11.112/webrtc  （需要登录）"
echo "  根路径：     https://124.223.11.112/  → 404，不再跳转"
echo "  局域网页面： https://<板子IP>:8443/  （不变，无鉴权）"
echo
echo "  观看端账号： $VIEW_USER"
if [ "$CREDS_KNOWN" = 1 ]; then
    echo "  观看端密码： $VIEW_PASS"
else
    echo "  观看端密码： 未知（见 $AUTH_FILE；重置用 VIEW_PASS='新密码' 重跑）"
fi
echo "  明文凭证：   $CRED_FILE（600 root）"
echo "  htpasswd：   $AUTH_FILE（640 root:www-data）"
echo "  板子 .env：  SIGNAL_AUTH=$VIEW_USER:$VIEW_PASS"
echo "  改密码：     VIEW_PASS='新密码' bash tools/deploy_web_vps.sh（板子同步改 SIGNAL_AUTH）"
echo
echo "  旧文件备份： $BACKUP"
REMOTE

echo "==> VPS 侧自检完成"
