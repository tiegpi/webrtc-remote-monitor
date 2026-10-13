#!/bin/bash
# gen_cert.sh —— 给板子的观看页签一张自签证书（带 IP SAN）
#
# 为什么需要：getUserMedia（阶段二的浏览器麦克风）属于**安全上下文** API。
# http:// 下 navigator.mediaDevices 直接是 undefined —— 连调用机会都没有，
# 症状是"视频好好的，麦克风永远拿不到"，而不是一个明确的报错。
#
# 幂等：证书已存在且 SAN 里已经包含当前 IP 就直接跳过。板子换了 IP（DHCP）
# 会自动重签 —— SAN 对不上时浏览器报的是 SEC_ERROR_UNSUPPORTED_NAME /
# ERR_CERT_COMMON_NAME_INVALID，而这些只在证书详情里看得见，很容易被
# 当成"证书没生效"然后去改一堆无关的东西。
#
# 用法：
#     tools/gen_cert.sh            # 自动取本机 IP
#     tools/gen_cert.sh 10.0.0.5   # 指定 IP
set -euo pipefail

cd "$(dirname "$0")/.."

IP="${1:-}"
if [ -z "$IP" ]; then
    # 连 1.1.1.1 但不会真的发包：UDP connect 只让内核选一条路由，
    # 然后把选中的源地址告诉我们。
    IP="$(ip -4 route get 1.1.1.1 2>/dev/null \
          | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
fi
if [ -z "$IP" ]; then
    echo "取不到本机 IP。用法: $0 <IP>" >&2
    exit 1
fi

CERT=certs/board.crt
KEY=certs/board.key
mkdir -p certs

# 已经覆盖这个 IP 就不重签。\b 是必要的：不加的话 192.168.2.1 会误判成
# 已经覆盖 192.168.2.187（前缀相同）。
if [ -s "$CERT" ] && [ -s "$KEY" ]; then
    if openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null \
       | grep -q "IP Address:$IP\b"; then
        echo "证书已存在且覆盖 $IP，跳过。"
        openssl x509 -in "$CERT" -noout -subject -enddate | sed 's/^/  /'
        exit 0
    fi
    echo "已存在的证书不含 $IP，重新签发。"
fi

# -days 397：压在 Chrome 的证书有效期上限（398 天）以下，否则直接被拒。
# SAN 里三个名字都要有：浏览器按 IP 访问，本机调试可能走 127.0.0.1 或 localhost。
openssl req -x509 -newkey rsa:2048 -nodes -days 397 \
    -keyout "$KEY" -out "$CERT" \
    -subj "/CN=$IP" \
    -addext "subjectAltName=IP:$IP,IP:127.0.0.1,DNS:localhost" \
    -addext "extendedKeyUsage=serverAuth" \
    2>/dev/null

chmod 600 "$KEY"
chmod 644 "$CERT"

echo "已签发：$CERT / $KEY"
openssl x509 -in "$CERT" -noout -subject -dates -ext subjectAltName | sed 's/^/  /'
echo
echo "用 tools/run-web.sh 启动就会带上它。浏览器第一次打开会拦一下，"
echo "点「继续前往 / 接受风险」即可 —— 点过之后就是合法的安全上下文。"
