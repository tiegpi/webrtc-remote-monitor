#!/bin/bash
# gop_sweep.sh —— 扫 gop 档位，用浏览器自己的 freezeCount 记分
#
# 为什么不用 analyze_diag.py 的"最长冻结"来比：那个指标靠"相邻样本帧增量是否为
# 0"（1Hz 采样），看不见短于约 0.95 秒的停顿，实测比浏览器自己的账**少算 2.5 倍**。
# 拿它给 gop 记分，会把"经常卡但每次都不久"整类差别抹平 —— 而缩短 gop 恰好就是
# 在削这一类。所以这里看的是「画面静止时间占比」。
#
# 每档：重启推流端（带 --gop N）→ 等浏览器新一段攒够样本 → 记分。
# 推流端一重启，PeerConnection 就断了，页面**可能**自己能重新协商上。能不能，
# 各浏览器不一样，所以脚本会先确认"新的一段是不是重启之后才开始的"；不是就
# 明确报出来要人刷新，绝不拿旧数据凑数。
#
# 用法（在本地跑，浏览器开着页面；BOARD 是 ssh 目标）：
#     BOARD=orangepi@<board-ip> tools/gop_sweep.sh 60 20 10
set -uo pipefail

BOARD="${BOARD:?没设 BOARD —— 板子的 ssh 目标，如 orangepi@192.168.2.100}"
NEED=120          # 每档至少要多少条样本（1Hz，约 2 分钟）
WAIT=300          # 等一段新数据的上限（秒）
HERE="$(cd "$(dirname "$0")/.." && pwd)"

# 和 run-agent.sh / run-web.sh 共用同一个 .env（凭据不进仓库）
if [ -f "$HERE/.env" ]; then . "$HERE/.env"; fi
: "${ROOM:?没设 ROOM —— cp .env.example .env 填上}"
: "${SIGNAL_HOST:?没设 SIGNAL_HOST —— 见 .env.example}"
DIAG="https://$SIGNAL_HOST/diag?room=$ROOM"

samples_since() {
    # 打印"起点晚于 $1 的那一段"的样本数；没有这样的段就打印 0。
    curl -sk --max-time 20 "$DIAG" -o /tmp/gop_sweep.json 2>/dev/null || { echo 0; return; }
    python3 - "$1" <<'PY' 2>/dev/null || echo 0
import json, sys
sys.path.insert(0, 'tools')
from analyze_diag import split, live_runs
t0 = float(sys.argv[1])
d = json.load(open('/tmp/gop_sweep.json'))
b, _dev = split(d['items'])
_runs, br = live_runs(b)
# 段的起点必须晚于重启时刻，否则说明页面根本没重新协商，测的还是上一档。
print(len(br) if br and br[0]['_at'] > t0 else 0)
PY
}

score() {
    curl -sk --max-time 20 "$DIAG" -o /tmp/gop_sweep.json 2>/dev/null
    python3 - <<'PY'
import json, sys
sys.path.insert(0, 'tools')
from analyze_diag import split, live_runs, steady, freeze_report
d = json.load(open('/tmp/gop_sweep.json'))
b, _dev = split(d['items'])
_runs, br = live_runs(b)
win = steady(br, 15.0)
fr = freeze_report(win)
pct = 100 * fr.total / fr.span if fr.span else 0
print('  窗口 %.0fs / %d 条' % (fr.span, len(win)))
print('  画面静止 %.1f%%（%d 次，共 %.1fs）   最长单次 %.2fs   来源 %s'
      % (pct, fr.count, fr.total, fr.longest, fr.source))
print('  采样法同一段只算出 %.1fs，漏了 %.0f%%'
      % (fr.sampled_total, 100 * (1 - fr.sampled_total / fr.total) if fr.total else 0))
PY
}

printf '%-6s %-10s %s\n' "gop" "结果" "备注"
for g in "$@"; do
    echo
    echo "================ gop=$g ================"
    t0=$(date +%s)
    timeout 60 ssh -o BatchMode=yes "$BOARD" "
        pkill -f '[d]evice_agent' || true
        cd ~/webrtc-remote-monitor
        nohup bash -l tools/run-agent.sh --gop $g > /tmp/agent.log 2>&1 &
    " 2>&1 | tail -2
    echo "  已用 --gop $g 重启推流端，等页面重新协商…"

    n=0
    for _ in $(seq 1 $((WAIT / 6))); do
        sleep 6
        n=$(samples_since "$t0")
        [ "${n:-0}" -ge "$NEED" ] 2>/dev/null && break
    done

    if [ "${n:-0}" -lt "$NEED" ] 2>/dev/null; then
        echo "  !! 推流端重启后页面没有自己恢复（重启后只拿到 ${n:-0} 条样本）。"
        echo "     去浏览器按一下 F5，然后重跑这一档。**这一档的数据不可用。**"
        continue
    fi
    score
done
