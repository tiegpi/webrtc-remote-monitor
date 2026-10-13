#!/bin/bash
# check_audio.sh —— 阶段二第一步：先把硬件验掉，再决定用哪个麦克风
#
# 为什么这件事必须排在写代码前面：板子的音频有两类**静默失败**，一个错都不报 ——
#
#   1. 3.5mm 口里插的是三极耳机（没有麦克风那一极），采样到的只是本底噪声。
#      `arecord` 照样跑通、波形照样有、rms 也不是 0，看着完全正常。
#   2. `hp switch` / `spk switch` 是播放功放的门控，关着就是没声音出来 ——
#      而这两个开关**回读不可信**（见下面的说明），没法用脚本判断。
#
# 这两条不排掉，后面就是在调一个根本不存在的 WebRTC bug。
#
# ## 路由和增益要按厂商的 profile 设，不能想当然
#
# ES8388 的采集通路上有一串 PGA mux / 增益，默认值下测出来的电平没有意义。
# 板子自带的 `/usr/local/bin/test_record.sh` 就是干这个的，本脚本照它的做法来
# （`--profile headset|main`，默认 headset，走 3.5mm 那一路）：
#
#     headset: PGA Mux = Line 1L/1R, Differential Mux = Line 1, ALC Max/Min PGA = 2/1
#     main   : PGA Mux = Line 2L/2R, Differential Mux = Line 2, ALC Max/Min PGA = 4/2
#
# 两边共同的：Capture Digital Volume = 192（满）, Left/Right Channel Capture Volume = 4。
# **路由不对，麦克风接得再对也是本底噪声**，所以第一步先核对这一串。
#
# ## `hp switch` 为什么不能用脚本判定
#
# `amixer -c 2 sset 'hp switch' on` 会报成功（打印 `[on]`），但紧接着
# `sget` / `cget numid=40` 读回来还是 `off`，而且没有任何守护进程在改它
# （板子上没有 pulseaudio/pipewire/wireplumber）。也就是说这个值**读回来不能代表
# 硬件状态**。声学回环也测不出来 —— 耳机插在孔里，声音进的是耳塞，房间里的
# USB 麦克风听不到。所以：**这一条只能用耳朵**，用 `--tone`。
#
# 用法（在**板子**上跑）：
#     tools/check_audio.sh                 体检 + 给结论
#     tools/check_audio.sh --live 6        开 6 秒电平条，让人对着麦克风说话确认
#     tools/check_audio.sh --tone          放 1 秒 440Hz，用耳朵确认播放通不通
#     tools/check_audio.sh --apply-profile headset   把厂商那套采集设置写进去
set -uo pipefail

# 判定"这根线后面真的接着麦克风"的门槛（rms 占满量程的百分比）。
# 依据是实测两类样本差了近 100 倍，门槛放哪儿都不敏感：
#     浮空的 3.5mm 输入   0.04% ~ 0.25%
#     USB 摄像头麦克风    12.6%
MIC_MIN_PCT=1.0
PROFILE=headset
LIVE=0
TONE=0
APPLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --profile) PROFILE="${2:-}"; shift 2
                   case "$PROFILE" in headset|main) ;; *) echo "--profile 只能是 headset 或 main" >&2; exit 2 ;; esac ;;
        --apply-profile) APPLY=1; PROFILE="${2:-headset}"; shift 2
                   case "$PROFILE" in headset|main) ;; *) echo "--apply-profile 只能是 headset 或 main" >&2; exit 2 ;; esac ;;
        --live) LIVE="${2:-6}"; shift 2 ;;
        --tone) TONE=1; shift ;;
        -h|--help) grep '^#' "$0" | sed -n '2,49p' | sed 's/^# \?//'; exit 0 ;;
        *) echo "不认识的参数：$1" >&2; exit 2 ;;
    esac
done

hr() { printf '%s\n' "------------------------------------------------------------"; }

# ------------------------------------------------------------------ 声卡编号
# 不写死 2：换个内核/插拔设备，编号会变。厂商脚本也是这么找的。
CARD="$(aplay -l 2>/dev/null | sed -n 's/^card \([0-9]*\): rockchipes8388.*/\1/p' | head -1)"
if [ -z "$CARD" ]; then
    echo "找不到 es8388 声卡（3.5mm 那一路）。aplay -l 的输出：" >&2
    aplay -l >&2
    exit 1
fi

sget_val() {  # $1 = 控件名，回数据行的值
    # 必须锚定到 `  : values=` 那一行。cget 的输出里上面还有一行
    # `; type=INTEGER,access=...,values=2,min=0,max=192,step=0`，
    # 用 `.*values=` 去匹配会贪心到那一行，把 "2,min=0,max=192,step=0" 当成值。
    amixer -c "$CARD" cget name="$1" 2>/dev/null | sed -n 's/^  : values=//p' | head -1
}

echo "== 声卡 =="
echo "  es8388（3.5mm）= card $CARD"
echo "--- 采集 ---"; arecord -l 2>/dev/null | sed 's/^/  /'
echo "--- 播放 ---"; aplay -l 2>/dev/null | sed 's/^/  /'
hr

# ------------------------------------------------- 1. 采集路由与增益（厂商 profile）
if [ "$PROFILE" = "headset" ]; then
    MUX_L='Line 1L'; MUX_R='Line 1R'; DIFF='Line 1'; MAXPGA=2; MINPGA=1
else
    MUX_L='Line 2L'; MUX_R='Line 2R'; DIFF='Line 2'; MAXPGA=4; MINPGA=2
fi

if [ "$APPLY" = "1" ]; then
    echo "== 按厂商 profile 写入采集设置（$PROFILE）=="
    for kv in "ALC Capture Max PGA:$MAXPGA" "ALC Capture Min PGA:$MINPGA" \
              "Capture Digital Volume:192" \
              "Left Channel Capture Volume:4" "Right Channel Capture Volume:4" \
              "Left PGA Mux:$MUX_L" "Right PGA Mux:$MUX_R" "Differential Mux:$DIFF"; do
        amixer -c "$CARD" cset name="${kv%%:*}" "${kv#*:}" >/dev/null 2>&1 \
            && printf '  %-32s = %s\n' "${kv%%:*}" "${kv#*:}" \
            || printf '  %-32s 写入失败\n' "${kv%%:*}"
    done
    hr
fi

echo "== 采集路由/增益（对照厂商 $PROFILE profile）=="
check_int() {  # $1=控件名 $2=期望值
    local got; got="$(sget_val "$1")"
    if [ "$got" = "$2" ]; then printf '  %-32s = %-6s\n' "$1" "$got"
    else printf '  %-32s = %-6s  ← 期望 %s\n' "$1" "$got" "$2"; BAD_ROUTE=1; fi
}
check_enum() { # $1=控件名 $2=期望的 Item 下标 $3=期望文本
    local got; got="$(sget_val "$1")"
    if [ "$got" = "$2" ]; then printf '  %-32s = %s\n' "$1" "$3"
    else printf '  %-32s = Item%s  ← 期望 %s\n' "$1" "$got" "$3"; BAD_ROUTE=1; fi
}
BAD_ROUTE=0
check_int  'ALC Capture Max PGA'          "$MAXPGA"
check_int  'ALC Capture Min PGA'          "$MINPGA"
check_int  'Capture Digital Volume'       '192,192'
check_int  'Left Channel Capture Volume'  4
check_int  'Right Channel Capture Volume' 4
check_enum 'Left PGA Mux'   "$([ "$PROFILE" = headset ] && echo 0 || echo 1)" "$MUX_L"
check_enum 'Right PGA Mux'  "$([ "$PROFILE" = headset ] && echo 0 || echo 1)" "$MUX_R"
check_enum 'Differential Mux' 0 "$DIFF"
if [ "$BAD_ROUTE" = "1" ]; then
    echo "  → 有出入。路由不对时测出来的电平不能当结论，"
    echo "    先跑 --apply-profile $PROFILE 把设置写对再测。"
else
    echo "  → 与厂商 profile 一致，下面的电平测试有意义。"
fi
hr

# ----------------------------------------------------- 2. 3.5mm 插孔检测
# 这两个是 **CARD 级**控件：`sget`/`cget name=` 都找不到它们，只能从
# `amixer contents` 里按 name 找。access=r------- 只读 —— 内核的插孔检测。
jack_state() {
    amixer -c "$CARD" contents 2>/dev/null | python3 -c '
import re, sys
want = sys.argv[1]
name = None
for line in sys.stdin:
    m = re.search(r"name=.([^\x27]+).", line)
    if m:
        name = m.group(1)
    m = re.search(r":\s*values=(\w+)", line)
    if m and name == want:
        print(m.group(1))
        break
' "$1"
}

echo "== 3.5mm 插孔检测 =="
JACK_HP="$(jack_state 'Headphone Jack')"
JACK_MIC="$(jack_state 'Headset Mic Jack')"
printf '  Headphone Jack   = %-4s （耳机那一极）\n' "${JACK_HP:-?}"
printf '  Headset Mic Jack = %-4s （麦克风那一极）\n' "${JACK_MIC:-?}"
if [ "$JACK_HP" = "on" ] && [ "$JACK_MIC" != "on" ]; then
    echo "  → 插着的是**三极耳机，没有麦克风**。hw:$CARD,0 采集只会是本底噪声。"
fi
hr

# ------------------------------------------------------------- 3. 播放功放
echo "== 播放功放开关 =="
for n in 'hp switch' 'spk switch'; do
    printf '  %-12s 回读 = %s\n' "$n" \
        "$(amixer -c "$CARD" sget "$n" 2>/dev/null | grep -o '\[on\]\|\[off\]' | tail -1 | tr -d '[]')"
done
echo "  !! 这两个值**回读不可信**：写 on 会报成功，读回来仍是 off，且没有守护进程在改。"
echo "     用 --tone 靠耳朵判断，别信这里的 off。"
hr

# --------------------------------------------------------- 4. 逐个采集设备体检
list_capture_devs() {
    arecord -l 2>/dev/null | awk '
        /^card [0-9]+:/ {
            c = $2; sub(":", "", c)
            nm = $0; sub(/^card [0-9]+: /, "", nm); sub(/ \[.*/, "", nm)
        }
        /device [0-9]+:/ {
            if (match($0, /device [0-9]+:/))
                print "hw:" c "," substr($0, RSTART + 7, RLENGTH - 8) "|" nm
        }'
}

# 录 N 秒，回一行 "samples peak rms pct transient_peak"；打不开回 FAIL，空回 EMPTY。
#
# **必须丢掉开头的起振瞬态，否则这个函数会把「没接麦克风」判成「有信号」。**
# 实测（es8388, hw:2,0, 浮空输入，即 3.5mm 插着三极耳机）：
#     第 0 秒  peak=32768（削顶！）  rms 24.8%   DC=-4390   ← ADC/codec 上电起振
#     第 1 秒  peak=  1397          rms  0.34%   DC=   -4   ← 稳态：50Hz 工频 + 400Hz 空闲音
#     第 2 秒  peak=  1385          rms  0.34%
# 整段平均 = 12.76%，**正好落在本文件开头写的「USB 摄像头麦克风 12.6%」那一档**，
# 于是脚本高高兴兴打印「→ **有信号**」并推荐 hw:2,0 —— 而那个口上根本没有麦克风。
# 一次瞬态就能把结论整个翻过来，所以判定只认稳态，瞬态单独报出来给人看。
# **必须是整数**：arecord 的 `-d` 只认整数秒，给 `-d 3.0` 它直接打不开设备，
# 而失败长得跟"这台设备不能用"一模一样 —— 我第一版写成 1.0，结果两个设备全 FAIL。
STEADY_SKIP_S=1

measure() {
    local dev="$1" ch="$2" secs="$3" raw="/tmp/check_audio.$$.raw"
    rm -f "$raw"
    # 多录 STEADY_SKIP_S 秒，供上面那段被丢掉
    local total=$((secs + STEADY_SKIP_S))
    local err="/tmp/check_audio.$$.err"
    # **stderr 不能扔。** "打不开"有两种，长得一模一样但结论相反：
    #   - 设备不存在/不支持这种声道数 → 真的不能用；
    #   - "Device or resource busy"     → 设备好好的，只是**别人正拿着它**。
    # 后一种在实机上就是常态：device_agent 跑着的时候它一直开着采集
    # （A 路麦克风），这时你来体检，看到的是 Subdevices: 0/1。
    # 早先这里把 stderr 吞了，于是 agent 正在用的 USB 麦克风被报成
    # "两种声道配置都打不开"，最后结论成了"没找到能用的麦克风"—— 完全反了。
    if ! timeout $((secs + 10)) arecord -D "$dev" -f S16_LE -r 48000 -c "$ch" \
            -d "$total" -t raw "$raw" >/dev/null 2>"$err"; then
        if grep -qi 'busy' "$err"; then echo BUSY; else echo FAIL; fi
        rm -f "$err"
        return
    fi
    rm -f "$err"
    python3 - "$raw" "$ch" "$STEADY_SKIP_S" <<'PY'
import array, math, sys
a = array.array('h')
a.frombytes(open(sys.argv[1], 'rb').read())
if not a:
    print('EMPTY'); raise SystemExit
ch = int(sys.argv[2])
skip = int(float(sys.argv[3]) * 48000) * ch          # 按**帧**跳，不是按采样点
transient = max(abs(x) for x in a[:skip]) if skip and skip < len(a) else 0
steady = a[skip:] if skip < len(a) else a
if not steady:
    print('EMPTY'); raise SystemExit
peak = max(abs(x) for x in steady)
rms = math.sqrt(sum(float(x) * x for x in steady) / len(steady))
print('%d %d %.1f %.2f %d'
      % (len(steady), peak, rms, 100.0 * rms / 32768, transient))
PY
    rm -f "$raw"
}

echo "== 采集设备体检（每台录 2 秒稳态 + 1 秒起振瞬态，判定只看稳态）=="
BEST_DEV=""
BUSY_DEV=""
i=0
while IFS='|' read -r dev name; do
    [ -n "$dev" ] || continue
    i=$((i + 1))
    printf '  [%d] %s  %s\n' "$i" "$dev" "${name:-?}"

    # 单声道先试，不行再试立体声。**两种都得试**：es8388 的采集只支持立体声
    # （-c 1 报 "Channels count non available"），USB 摄像头反过来只支持单声道。
    got=0
    for ch in 1 2; do
        out="$(measure "$dev" "$ch" 2)"
        case "$out" in
            BUSY)  BUSY_DEV="$dev"; echo "      → 设备被占用（busy）—— 不是坏了，是有人正开着它"
                   echo "         device_agent 跑着的时候就是这个样子，它的采集分支一直占着。"
                   echo "         想知道有没有信号：先停 agent 再测，或者直接看它日志里那行"
                   echo "         「麦克风 <dev> 电平探测：...」，那是同一个探针。"
                   got=1; break ;;
            FAIL)  continue ;;
            EMPTY) echo "      -c $ch: 录到 0 字节"; continue ;;
        esac
        got=1
        # shellcheck disable=SC2086
        set -- $out
        # $5 是被丢掉的起振瞬态的峰值。它比稳态大得离谱时要**说出来** ——
        # 否则下次有人看到稳态 0.34% 会以为脚本坏了，或者反过来拿瞬态当信号。
        note=""
        if [ -n "${5:-}" ] && [ "${5:-0}" -gt 1000 ] 2>/dev/null \
           && [ "${5:-0}" -gt $(( ${2:-0} * 4 + 100 )) ] 2>/dev/null; then
            note="（起步瞬态 peak=$5，已剔除、不计入判定）"
        fi
        if python3 -c "import sys; sys.exit(0 if $4 >= $MIC_MIN_PCT else 1)"; then
            printf '      -c %d: %s 帧  peak=%s  rms=%s (%s%% 满量程)  → **有信号**%s\n' \
                   "$ch" "$1" "$2" "$3" "$4" "$note"
            [ -z "$BEST_DEV" ] && BEST_DEV="$dev"
        else
            printf '      -c %d: %s 帧  peak=%s  rms=%s (%s%% 满量程)  → 静默/浮空%s\n' \
                   "$ch" "$1" "$2" "$3" "$4" "$note"
        fi
        break
    done
    [ "$got" = "0" ] && echo "      → 两种声道配置都打不开"
done < <(list_capture_devs)
hr

# ------------------------------------------------------------- 5. 实时电平条
# 自动化测不出"这根线后面到底是不是麦克风"以外的信息，但**人是很好的仪器**：
# 让 TA 对着麦克风说话，电平条会动就是活的。浮空的输入怎么说话都不动。
if [ "$LIVE" -gt 0 ] 2>/dev/null; then
    echo "== 实时电平（$LIVE 秒，请对着麦克风说话 / 敲一敲）=="
    for dev in $BEST_DEV; do
        echo "  --- $dev ---"
        ch=1
        arecord -D "$dev" -f S16_LE -r 48000 -c 1 -d "$LIVE" -t raw /tmp/check_audio_live.raw >/dev/null 2>&1 \
            || { ch=2; arecord -D "$dev" -f S16_LE -r 48000 -c 2 -d "$LIVE" -t raw /tmp/check_audio_live.raw >/dev/null 2>&1; }
        python3 - /tmp/check_audio_live.raw <<'PY'
import array, math, sys
a = array.array('h')
a.frombytes(open(sys.argv[1], 'rb').read())
w = 12000   # 0.25 秒
bars = []
for off in range(0, len(a) - w, w):
    s = a[off:off + w]
    bars.append(math.sqrt(sum(float(x) * x for x in s) / len(s)))
if not bars:
    print('    没录到数据'); raise SystemExit
top = max(bars) or 1.0
for i, r in enumerate(bars):
    n = int(round(40 * r / top))
    print('    %4.2fs |%-40s| %6.0f' % (i * 0.25, '#' * n, r))
print('    峰值 %.0f   最低 %.0f   峰/底 = %.1f' % (top, min(bars), top / (min(bars) or 1)))
PY
        rm -f /tmp/check_audio_live.raw
    done
    hr
fi

# ------------------------------------------------------------------ 6. 结论
echo "== 结论 =="
if [ "$JACK_MIC" = "on" ]; then
    echo "  检测到四极耳麦（Headset Mic Jack=on）→ 用 hw:$CARD,0"
    echo "      板端参数：--mic-device hw:$CARD,0"
elif [ -n "$BEST_DEV" ]; then
    echo "  3.5mm 口没有麦克风，改用它：$BEST_DEV"
    echo "      板端参数：--mic-device $BEST_DEV"
elif [ -n "$BUSY_DEV" ]; then
    # **有信号和"测不了"是两回事，别混成一句"没找到麦克风"。**
    echo "  体检没测出可用麦克风，但 $BUSY_DEV 是**被占用**（不是坏的）。"
    echo "  多半是 device_agent 正开着它 —— 那反而说明它可用。"
    echo "      想看它的电平：先停 agent 重跑本脚本，或看 agent 日志里"
    echo "      「麦克风 $BUSY_DEV 电平探测：...」那一行。"
else
    echo "  **没找到能用的麦克风。** 别就这么往下走 —— 板子会推一路本底噪声上去，"
    echo "  而浏览器那边听起来只是「有点沙沙声」，很容易被当成 WebRTC 的问题。"
fi

if [ "$TONE" = "1" ]; then
    hr
    echo "== 播放试听（1 秒 440Hz → hw:$CARD,0 耳机）=="
    python3 - <<'PY'
import math, struct, wave
with wave.open('/tmp/check_audio_tone.wav', 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
    w.writeframes(b''.join(
        struct.pack('<hh', int(20000 * math.sin(2 * math.pi * 440 * i / 48000)),
                            int(20000 * math.sin(2 * math.pi * 440 * i / 48000)))
        for i in range(48000)))
PY
    aplay -D hw:"$CARD",0 /tmp/check_audio_tone.wav; echo "  aplay exit=$?"
    echo "  **耳机里听到了吗？** 这是判断 hp switch 有没有生效的**唯一**办法。"
    rm -f /tmp/check_audio_tone.wav
fi
