#!/bin/bash
# check_switch.sh —— "换摄像头"这一组的可证伪判据
#
# 为什么不能只看"没黑屏"：切换这件事有**四种失败的活法**，画面都不会报错，
#   1. 断掉了但浏览器还挂着最后一帧     → 看着有画面，其实早就没有新帧了
#   2. 换了个段但编码输出变了 caps      → 会重新协商，浏览器那边可能一直等
#   3. 段换了、画面没换（还拿着旧设备的 buffer）
#   4. 视频断了把音频也带下水
# 所以这里的判据是分层的：设备侧证明"那一段真的换了"，浏览器侧证明"解出来的是
# 新画面"和"音频没跟着断"。**只有两边同时成立才算过** —— 单看任何一边都能骗人。
#
# 用 curl 驱动，断言交给一段几十行的 python3。**不新写一套分析框架**：
# 完整的 17 条回归仍然是 tools/analyze_diag.py 的活。
#
# 前提：板子上 device_agent.py 在推流、浏览器已经打开页面并且在看。
# 这个脚本只做切换和断言，不负责把这两件事起起来。
#
# 用法（在**板子**上跑，或者任何能访问到页面地址的机器）：
#     tools/check_switch.sh                       # 默认 https://127.0.0.1:8443
#     tools/check_switch.sh --base https://<board-ip>:8443
#     tools/check_switch.sh --settle 20 --keep    # 切完不回切
set -uo pipefail

BASE="https://127.0.0.1:8443"
ROOM=""
SETTLE=12            # 每段稳态观察时长（秒）。稳态窗口要够长才能让 1Hz 的样本说话
MAX_STALL_MS=1000    # 切换造成的画面停顿上限（毫秒）。gop=10 在 20fps 下约 0.5 秒
MIN_MAD=12           # 跨切换的逐像素平均差下限（0-255 尺度）。判据还要它 ≥噪声底×3
CA=""
KEEP=0
CLEAR=1
FUSER=1

while [ $# -gt 0 ]; do
    case "$1" in
        --base) BASE="${2:-}"; shift 2 ;;
        --room) ROOM="${2:-}"; shift 2 ;;
        --settle) SETTLE="${2:-12}"; shift 2 ;;
        --max-stall-ms) MAX_STALL_MS="${2:-1000}"; shift 2 ;;
        --min-mad) MIN_MAD="${2:-12}"; shift 2 ;;
        --ca) CA="${2:-}"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        --no-clear) CLEAR=0; shift ;;
        --no-fuser) FUSER=0; shift ;;
        -h|--help) grep '^#' "$0" | sed -n '2,28p' | sed 's/^# \?//'; exit 0 ;;
        *) echo "不认识的参数：$1" >&2; exit 2 ;;
    esac
done

for cmd in curl python3; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "缺 $cmd" >&2; exit 2; }
done

# 页面是自签证书（tools/gen_cert.sh 签的），默认不校验。给了 --ca 才校。
CURL=(curl -sS --max-time 20)
if [ -n "$CA" ]; then
    CURL+=(--cacert "$CA")
elif [[ "$BASE" == https://* ]]; then
    CURL+=(-k)
fi
BASE="${BASE%/}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
hr() { printf '%s\n' "------------------------------------------------------------"; }

# /diag 的查询参数：room 决定看哪个房间的样本，role 决定看浏览器还是板子发的。
# 不给 room 会把所有房间混在一起 —— 那正是"判据通过但实际没通"的经典来源。
diag_q() {
    local role="$1"
    if [ -n "$ROOM" ]; then printf '?room=%s&role=%s' "$ROOM" "$role"
    else printf '?role=%s' "$role"; fi
}

echo "== 目标 =="
echo "  页面服务 : $BASE"
echo "  房间     : ${ROOM:-（不筛，全部）}"
echo "  稳态窗口 : ${SETTLE}s"

# --------------------------------------------------------------- 1. 摄像头清单
if ! "${CURL[@]}" "$BASE/camera" -o "$TMP/cam.json"; then
    echo "取 $BASE/camera 失败。板子的 web_server.py 起了吗？" >&2
    exit 2
fi

if ! read -r CUR TGT N < <(python3 - "$TMP/cam.json" <<'PY'
import json, sys
try:
    j = json.load(open(sys.argv[1]))
except Exception as e:
    print("读取 /camera 响应失败：%s" % e, file=sys.stderr); raise SystemExit(3)
avail = j.get("available") or []
cur = j.get("current")
if len(avail) < 2:
    print("板子上只认出一个摄像头，切换无从谈起", file=sys.stderr); raise SystemExit(3)
missing = [c["name"] for c in avail if not c.get("present")]
if missing:
    print("这些摄像头没插好（设备节点不存在）：%s" % ", ".join(missing), file=sys.stderr)
    raise SystemExit(3)
tgt = next((c["name"] for c in avail if c["name"] != cur), None)
if tgt is None:
    print("当前源 %r 不在清单里，状态对不上" % cur, file=sys.stderr); raise SystemExit(3)
print(cur, tgt, len(avail))
PY
); then
    echo "== 前置条件不满足，退出 ==" >&2
    exit 2
fi

echo "  摄像头   : $N 路（当前 $CUR，本轮切到 $TGT）"
hr

# 设备节点持有情况。**只看不判** —— fuser 的输出格式随版本变，而且"没人持有"
# 就是空输出，拿它做断言很容易变成一条永远通过的判据。切换前后各打一次，
# 给人对着看：切完 /dev/video11 应该空出来、/dev/video20 被 agent 拿着。
show_fuser() {
    [ "$FUSER" = "1" ] || return 0
    command -v fuser >/dev/null 2>&1 || return 0
    local nodes
    nodes=$(python3 - "$TMP/cam.json" <<'PY'
import json, sys
print(" ".join(c["device"] for c in json.load(open(sys.argv[1])).get("available", [])))
PY
)
    echo "  fuser $nodes"
    # shellcheck disable=SC2086
    fuser -v $nodes 2>&1 | sed 's/^/    /' || true
}

if [ "$CLEAR" = "1" ]; then
    "${CURL[@]}" -X POST "$BASE/diag/clear" -o /dev/null \
        && echo "已清空 /diag 样本（本轮从零开始记）" \
        || echo "清空 /diag 失败（服务器拒绝？）—— 继续，但结论里可能混进旧样本"
fi

echo "== 切换前（$CUR）=="
show_fuser
echo "  等 ${SETTLE}s 稳态…"
sleep "$SETTLE"
"${CURL[@]}" "$BASE/diag$(diag_q device)"  -o "$TMP/dev_a.json"
"${CURL[@]}" "$BASE/diag$(diag_q browser)" -o "$TMP/bro_a.json"
hr

# ------------------------------------------------------------------ 2. 切！
# 时间戳在 POST **之前**取：服务器盖的 _at 是收到时刻，用它分窗口时
# 必须保证"切换请求"这个分界点只可能早于新段的第一份样本。
SWITCH_AT=$(python3 -c 'import time; print("%.3f" % time.time())')
echo "== 切换 → $TGT =="
HTTP=$(curl -sS --max-time 20 -k -o "$TMP/post.json" -w '%{http_code}' \
       -X POST -H 'Content-Type: application/json' \
       -d "{\"name\": \"$TGT\"}" "$BASE/camera")
echo "  POST /camera → HTTP $HTTP"
sed 's/^/    /' "$TMP/post.json"; echo

if [ "$HTTP" != "200" ]; then
    echo "  切换请求本身就没成功，后面不用看了。"
    echo "  409 = 设备节点不存在（摄像头没插好）；400 = 名字不认识；"
    echo "  500 = 换段抛异常（看 device_agent 的日志）；502 = agent 没跑。"
    exit 1
fi

echo "  等 ${SETTLE}s 稳态…"
sleep "$SETTLE"
"${CURL[@]}" "$BASE/diag$(diag_q device)"  -o "$TMP/dev_b.json"
"${CURL[@]}" "$BASE/diag$(diag_q browser)" -o "$TMP/bro_b.json"
echo "== 切换后（$TGT）=="
show_fuser
hr

# ------------------------------------------------------------------ 3. 断言
export SWITCH_AT CUR TGT MAX_STALL_MS MIN_MAD BASE
python3 - "$TMP/dev_a.json" "$TMP/bro_a.json" "$TMP/dev_b.json" "$TMP/bro_b.json" <<'PY'
import json, os, sys

CUR = os.environ["CUR"]; TGT = os.environ["TGT"]
SWITCH_AT = float(os.environ["SWITCH_AT"])
MAX_STALL = float(os.environ["MAX_STALL_MS"])
MIN_MAD = float(os.environ["MIN_MAD"])

def load(path):
    try:
        items = json.load(open(path)).get("items") or []
    except Exception:
        items = []
    return sorted(items, key=lambda s: s.get("_at") or 0)

def merge(*groups):
    """按 _at 去重合并。

    两次抓取之间**没有清空**，所以后一次拿到的样本**包含**前一次的。
    直接相加会把重叠那段算两遍：连续性检查看到的是 50,100,...,50,100，
    于是报"计数回退了 1 处" —— 一个纯粹由脚本自己造出来的失败。
    """
    by = {}
    for g in groups:
        for s in g:
            by[s.get("_at")] = s
    return sorted(by.values(), key=lambda s: s.get("_at") or 0)

dev_a, bro_a, dev_b, bro_b = (load(p) for p in sys.argv[1:5])
dev = merge(dev_a, dev_b)
bro = merge(bro_a, bro_b)

checks = []
def add(name, ok, detail, skipped=False):
    checks.append((name, bool(ok), detail, skipped))

def last(items, key, default=None):
    for s in reversed(items):
        v = s.get(key)
        if v is not None:
            return v
    return default

def cam(items):
    return last(items, "camera") or {}

# 没有浏览器样本时，一半的判据无从谈起。**明确说出来**，而不是让它们静默通过 ——
# "判据全过"和"根本没测"长得一模一样是这个项目反复踩的坑。
if len(bro_b) < 3:
    print("  浏览器侧样本只有 %d 份。页面开着并且在看吗？" % len(bro_b),
          file=sys.stderr)
    print("  （先把页面跑起来：Chrome 打开 %s）" % os.environ.get("BASE", ""),
          file=sys.stderr)
    raise SystemExit(2)

# ---- 设备侧：那一段真的换了 ----------------------------------------------
a_src = last(dev_a, "video_source")
n_a = last(dev_a, "video_switches", 0)
n_b = last(dev_b, "video_switches", 0)
b_src = last(dev_b, "video_source")

add("切换前设备在 %s 上" % CUR, a_src == CUR, "实测 video_source=%r" % a_src)
add("切换后设备在 %s 上" % TGT, b_src == TGT, "实测 video_source=%r" % b_src)
add("切换计数 +1", (n_b or 0) == (n_a or 0) + 1, "%s → %s" % (n_a, n_b))

# 样本得真的**跨过**切换时刻，否则下面那些"连续性"判据是空的：
# 一个只有切换后样本的窗口当然"没有缺口"。这条是为了防仪器自己被静默通过。
pre = [s for s in bro if (s.get("_at") or 0) < SWITCH_AT]
post = [s for s in bro if (s.get("_at") or 0) > SWITCH_AT]
add("样本覆盖切换前后", len(pre) >= 2 and len(post) >= 2,
    "切换前 %d 份 / 切换后 %d 份（各要 ≥2，不够就调大 --settle）"
    % (len(pre), len(post)))

# ---- 浏览器侧：解出来的是新画面 -------------------------------------------
# camera.current 是**板子**在遥测里报的那个值，页面只是转述它 —— 所以这一条
# 不能拿来证明"页面知道换了"，它证明的是"两边说的是同一件事"。
add("浏览器页面也认为当前是 %s" % TGT, cam(bro_b).get("current") == TGT,
    "camera.current=%r" % cam(bro_b).get("current"))

def meanY(items):
    for s in reversed(items):
        v = (s.get("camera") or {}).get("meanY")
        if isinstance(v, (int, float)):
            return v
    return None

ya, yb = meanY(bro_a), meanY(bro_b)
dy = abs(yb - ya) if (ya is not None and yb is not None) else None

# ---- 画面真的换了（最强的一条，最接近人眼）-------------------------------
#
# 起初这里判的是"亮度均值变了 ≥40"，那是照着"MIPI 增益顶格 Y≈78、USB 自带
# AE 到 Y≈226，差三倍"定的。**实测下来这个前提不成立**：板上的 MIPI 跑了
# 亮度闭环 + 白平衡，页面量到的是 ~161 而不是 78；USB 那路对着的方向偏暗，
# 量到 ~124。真实差值是 37，卡在 40 下面 —— 一条按错误前提定的门槛，
# 把一次**真的换了画面**的切换判成了失败。
#
# 改门槛是最糟的解法（下次换个场景又不对）。均值本身也不该当主判据：
# 两个不同的摄像头"恰好一样亮"完全可能，而"是不是同一路画面"本质上是
# **逐像素**的问题。所以页面现在多报一张 32×18 的亮度缩略图，这里改成：
#
#   1. 先量一个**噪声底** —— 同一路里相邻两帧的逐像素平均差。
#      画面自己也在动（噪声、曝光微调、场景变化），跨切换的差值必须
#      **明显大于**这个底，否则"换了"和"没换"根本分不开。
#   2. 再量跨切换的差值，要求它 ≥ 绝对下限，且 ≥ 噪声底的 3 倍。
#
# 这样判据自己给自己定标，不依赖"两个摄像头亮度差多少"这种会随场景变的假设。
# 板子要是撒谎说切了、实际还推原来那路，跨切换差值就只剩噪声底那么大，
# 第 2 条当场就红。
def thumb(sample):
    import base64
    t = (sample.get("camera") or {}).get("thumb")
    if not t:
        return None
    try:
        return base64.b64decode(t)
    except Exception:
        return None

def mad(x, y):
    """逐像素平均绝对差。两张图尺寸不一致就返回 None。"""
    if x is None or y is None or len(x) != len(y) or not x:
        return None
    return sum(abs(p - q) for p, q in zip(x, y)) / len(x)

post_thumbs = [t for t in (thumb(s) for s in bro_b) if t]
pre_thumbs = [t for t in (thumb(s) for s in bro_a) if t]
all_thumbs = [t for t in (thumb(s) for s in bro) if t]

add("页面报了画面缩略图", bool(all_thumbs),
    "有缩略图的样本 %d 份（32×18 亮度）。没有的话下面两条判据没得算"
    % len(all_thumbs))

if len(all_thumbs) >= 4:
    # 噪声底：相邻两帧的差。取中位数而不是平均，免得被切换那一跳带偏。
    diffs = sorted(d for d in (mad(all_thumbs[i], all_thumbs[i + 1])
                               for i in range(len(all_thumbs) - 1)) if d is not None)
    noise = diffs[len(diffs) // 2] if diffs else 0.0
    # 跨切换：切换前最后一张 vs 切换后最后一张（都取稳态里的，避开过渡帧）
    cross = mad(pre_thumbs[-1], post_thumbs[-1]) if (pre_thumbs and post_thumbs) else None
    add("画面内容真的换了（逐像素）",
        cross is not None and cross >= MIN_MAD and cross >= 3 * noise,
        "跨切换逐像素平均差 %.1f，同一路相邻帧的噪声底 %.1f（中位数）。"
        "门槛：≥%.0f 且 ≥噪声底 3 倍。均值 %s → %s（差 %s，仅供参考 —— "
        "均值会被「两路恰好一样亮」骗过，所以不当判据）"
        % (cross if cross is not None else -1, noise, MIN_MAD,
           ya, yb, "%.1f" % dy if dy is not None else "?"))

    # 顺手把两张缩略图打成 ASCII。数字说"变了"，这张图让人**看出来**变成了什么。
    def ascii_art(t):
        CH = " .:-=+*#%@"
        lines = []
        for r in range(18):
            lines.append("".join(
                CH[min(9, t[r * 32 + c] * 10 // 256)] for c in range(32)))
        return lines
    if pre_thumbs and post_thumbs:
        a_art, b_art = ascii_art(pre_thumbs[-1]), ascii_art(post_thumbs[-1])
        print("  画面缩略图（左 = 切换前 %s，右 = 切换后 %s）:" % (CUR, TGT))
        for la, lb in zip(a_art, b_art):
            print("    |%s|  |%s|" % (la, lb))
else:
    add("画面内容真的换了（逐像素）", False,
        "缩略图样本不足（%d 份），算不出噪声底和跨切换差值" % len(all_thumbs))

# 切换停顿。gapSinceSwitchMs 由 requestVideoFrameCallback 逐帧记账，
# **不受 1Hz 采样地板限制** —— 这个项目在冻结指标上吃过一次采样法的亏。
#
# 但这个数是**页面报的**，页面得先知道"源换了"。这个脚本是用 curl 发起的 POST，
# 页面不是发起方，只能靠它自己每 2 秒轮询 /camera 发现。页面的实现是发现之后
# **回看**最近 4 秒取最大帧间隔，所以晚发现也量得到（多算了 0～2 秒窗口，
# 是上界）。要是页面根本没在记，这个字段就是 None —— 那和"停顿 0 毫秒"必须
# 分得开：前者说明这条判据没测，后者才叫通过。
seen = max(((s.get("camera") or {}).get("framesSeen") or 0) for s in bro_b)
add("页面逐帧记账在工作（停顿那条的前提）", seen > 0,
    "framesSeen=%d。为 0 说明 requestVideoFrameCallback 没触发，"
    "下面那条停顿判据**不算数**" % seen)

stalls = [((s.get("camera") or {}).get("gapSinceSwitchMs")) for s in bro_b]
stalls = [v for v in stalls if isinstance(v, (int, float))]
add("停顿被量到了（不是 None）", bool(stalls),
    "gapSinceSwitchMs 出现过 %d 次数值。一直是 None 说明页面没发现源变过 —— "
    "它每 2 秒轮询 /camera，超过一个轮询周期还没发现就是页面那边的问题"
    % len(stalls))

stall = max(stalls) if stalls else 0
add("切换停顿在容忍范围内", bool(stalls) and seen > 0 and stall <= MAX_STALL,
    "最长 %d ms（上限 %.0f）。含页面发现延迟，所以是偏保守的上界"
    % (stall, MAX_STALL))

# 切换后必须还在出帧，否则上面那条"停顿"只是因为断面之后再没来过帧。
fd = [s.get("video", {}).get("framesDecoded") for s in bro_b]
fd = [v for v in fd if isinstance(v, int)]
add("切换后解码帧继续增长", len(fd) >= 2 and fd[-1] > fd[0],
    "framesDecoded %s → %s（%d 份样本）"
    % (fd[0] if fd else "?", fd[-1] if fd else "?", len(fd)))

# ---- 音频：不能因为换摄像头就把对讲带下水 --------------------------------
# 硬约束的可证伪形式。**两路都要看**：设备侧发包连续只能证明板子还在推，
# 浏览器侧收包连续才证明这条链真的还通着。
# 没跑音频时**报 SKIP 而不是 PASS** —— 一条没测的判据混在"全过"里，
# 比它不存在更糟。
def audio_series(items, pick):
    out = []
    for s in items:
        v = pick(s)
        if isinstance(v, int):
            out.append((s.get("_at") or 0, v))
    return out

def continuity(series):
    """返回 (缺口数, 计数回退数, 增量)。"""
    gaps = sum(1 for (t1, _), (t2, _) in zip(series, series[1:]) if t2 - t1 > 3.0)
    back = sum(1 for (_, v1), (_, v2) in zip(series, series[1:]) if v2 < v1)
    return gaps, back, series[-1][1] - series[0][1]

d_ap = audio_series(dev, lambda s: s.get("audio_packets_out"))
if d_ap and max(v for _, v in d_ap) > 0:
    g, b, grew = continuity(d_ap)
    add("音频没断（设备侧发包连续）", not g and not b and grew > 0,
        "共 +%d 包，无缺口、无回退（%d 份样本）" % (grew, len(d_ap))
        if not (g or b) else "缺口 %d 处、计数回退 %d 处、增量 %d" % (g, b, grew))
else:
    add("音频没断（设备侧发包连续）", True,
        "没有音频样本，**这条没测**（--no-audio 跑的？）", skipped=True)

b_ai = audio_series(bro, lambda s: (s.get("audio") or {}).get("inPacketsReceived"))
if b_ai and max(v for _, v in b_ai) > 0:
    g, b, grew = continuity(b_ai)
    add("音频没断（浏览器侧收包连续）", not g and not b and grew > 0,
        "共 +%d 包，无缺口、无回退（%d 份样本）" % (grew, len(b_ai))
        if not (g or b) else "缺口 %d 处、计数回退 %d 处、增量 %d" % (g, b, grew))
else:
    add("音频没断（浏览器侧收包连续）", True,
        "浏览器没有音频样本，**这条没测**（没点「开始对讲」？）", skipped=True)

width = max(len(n) for n, _, _, _ in checks)
failed = skipped = 0
print()
for name, ok, detail, skip in checks:
    if skip:
        skipped += 1
    elif not ok:
        failed += 1
    mark = "SKIP" if skip else ("PASS" if ok else "FAIL")
    print("  [%s] %-*s  %s" % (mark, width, name, detail))
print()
if failed:
    print("  %d / %d 条判据未通过" % (failed, len(checks) - skipped))
    raise SystemExit(1)
print("  全部 %d 条判据通过%s"
      % (len(checks) - skipped, "（另有 %d 条跳过，见上）" % skipped if skipped else ""))
PY
RC=$?
hr

# ------------------------------------------------------------------ 4. 回切
# 默认切回去：演示的默认视角是 MIPI，跑完验证把它留在 USB 上，
# 下次打开页面的人会以为"摄像头装反了"。
if [ "$KEEP" = "0" ] && [ "$RC" = "0" ]; then
    echo "== 切回 $CUR =="
    curl -sS --max-time 30 -k -X POST -H 'Content-Type: application/json' \
         -d "{\"name\": \"$CUR\"}" "$BASE/camera"
    echo
    echo "  （切回 MIPI 有几秒画面在爬：曝光闭环 + 白平衡前 15 帧要重跑）"
elif [ "$KEEP" = "1" ]; then
    echo "（--keep：留在 $TGT 上，没切回去）"
fi

exit "$RC"
