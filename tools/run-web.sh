#!/bin/bash
# 板子侧的观看页服务。浏览器开 https://<板子IP>:8443/
#
# 为什么是 https：阶段二的浏览器麦克风（getUserMedia）只在**安全上下文**里存在。
# http:// 下 navigator.mediaDevices 是 undefined，连调用机会都没有。
# 证书是自签的（tools/gen_cert.sh 生成），浏览器第一次要接受一次。
#
# 这个脚本不需要 bash -l：web_server.py 不碰 GStreamer，和 tools/run-agent.sh 不一样。
#
# ROOM 必须和 tools/run-agent.sh 里的 --room 一致。信令服务器只服务它启动时配置的
# **那一个**房间，对不上的连接会被 {"type":"error","code":"bad-room"} 直接关掉，
# 症状是"打开页面什么都没发生"，两边日志都不指向真原因。
# 页面不用手写 ?room= —— web_server 会通过 /config.json 把这个值告诉它。
#
# **ROOM / SIGNAL_HOST 从 .env 读**，和 tools/run-agent.sh 用同一个文件，
# 这样两边不可能写岔 —— 之前它们就各写各的，正是出错的地方。
set -euo pipefail

# 脚本在 tools/ 下，项目根是它的上一级。写成 dirname 而不是 ~/...，
# 这样从哪儿调起都对，本地也能直接跑。
cd "$(dirname "$0")/.."

if [ -f .env ]; then . ./.env; fi
: "${ROOM:?没设 ROOM —— cp .env.example .env 填上，或 export ROOM=...}"
: "${SIGNAL_HOST:?没设 SIGNAL_HOST —— 信令服务器主机名，见 .env.example}"

PORT=8443

if [ ! -s certs/board.crt ] || [ ! -s certs/board.key ]; then
    echo "缺证书。先跑一次 tools/gen_cert.sh" >&2
    exit 1
fi

exec python3 src/web_server.py \
    --port "$PORT" \
    --room "$ROOM" \
    --signal "https://$SIGNAL_HOST" \
    --tls-ca vps-self.crt \
    --tls-cert certs/board.crt \
    --tls-key certs/board.key \
    "$@"
