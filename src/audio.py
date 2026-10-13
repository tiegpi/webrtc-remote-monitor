#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio.py —— 板子侧音频设备的探测与选择

板子的音频有两条**静默失败**路径，判错了不会报错，只会安静地推一路本底噪声
上去，或者干脆没声音出来（完整实测见 tools/check_audio.sh）：

  1. 3.5mm 口里插的是三极耳机（没有麦克风那一极）时，`hw:2,0` 采到的只是
     浮空输入的本底噪声。实测在**厂商 headset profile + 满采集增益**下
     rms 只有 0.25% 满量程，而 USB 麦克风是 12.9% —— 差近 100 倍。
     注意这个 0.25% 不是"增益给少了"：Capture Digital Volume 已经是 192（满），
     PGA Mux 也已经按厂商脚本设成 Line 1L/1R 了。
  2. 播放功放 `hp switch` 的回读不可信 —— 写 on 会报成功，读回来仍是 off，
     且板子上没有 pulseaudio/pipewire/wireplumber 之类的守护进程在改它。
     也就是说脚本没法确认功放开没开，只能靠耳朵（tools/check_audio.sh --tone）。

所以这里的原则是：**宁可明确报错，也不要静默跑在一个没人说话的输入上**。
"""

import re
import subprocess

# 3.5mm 那一路的声卡名。板子上 `aplay -l` 显示为 rockchipes8388。
# 不写死卡号：换个内核、插拔 USB 设备，卡号会变。
ES8388_NAME = "rockchipes8388"


class AudioUnavailable(RuntimeError):
    """没有可用的音频设备。这是参数类错误，重连解决不了。"""


# --------------------------------------------------------------------------- #
# 跑外部命令
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=5):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _list(kind):
    """解析 `arecord -l`（kind='cap'）或 `aplay -l`（kind='play'）。

    输出形如：

        card 2: rockchipes8388 [rockchip-es8388], device 0: dailink-...[...]

    返回 [(card, device, card_name)]。

    **坑在这行的形状上。** 一张卡只有一个设备时，`device 0:` 就接在卡行
    **同一行**里（板子这三张卡全是这种形式）；有多个设备时才另起一行。
    所以不能写成"匹配到 card 行就 continue，等下一行的 device" ——
    那样一个设备都收不到，板子上实测结果是**空列表**，接着 card_of 返回
    None、插孔检测拿到 "amixer -c None" 的报错也返回空，整条探测链
    一路静默降级到"没有麦克风"。症状和"真没插麦克风"完全一样。
    """
    out = _run(["arecord", "-l"]) if kind == "cap" else _run(["aplay", "-l"])
    devs = []
    card = name = None
    for line in out.splitlines():
        m = re.match(r"card (\d+): (\S+)", line)
        if m:
            card, name = int(m.group(1)), m.group(2)
        if card is None:
            continue
        # 卡行本身和后续行都要扫，见上面那段说明。
        for d in re.finditer(r"device (\d+):", line):
            devs.append((card, int(d.group(1)), name or ""))
    return devs


def card_of(name_part):
    """找卡名里含 name_part 的卡号，找不到回 None。"""
    for card, _dev, name in _list("play"):
        if name_part in name:
            return card
    return None


def jack_state(card, control):
    """读 CARD 级的插孔检测控件，回 'on' / 'off' / ''。

    这两个控件（'Headphone Jack' / 'Headset Mic Jack'）是 **CARD 级**的，
    `amixer cget name=` 找不到它们（simple mixer 里没有），只能从
    `amixer contents` 里按 name 找。access=r------- 只读，是内核的检测结果。
    """
    out = _run(["amixer", "-c", str(card), "contents"])
    want = None
    for line in out.splitlines():
        m = re.search(r"name='([^']+)'", line)
        if m:
            want = m.group(1)
            continue
        m = re.search(r":\s*values=(\w+)", line)
        if m and want == control:
            return m.group(1)
    return ""


# --------------------------------------------------------------------------- #
# 电平探测
# --------------------------------------------------------------------------- #
# ADC/codec 上电后有个很大的起振瞬态，判定必须把它丢开。
# 实测（hw:2,0 浮空输入，3.5mm 插着三极耳机，也就是**没有麦克风**）：
#     第 0 秒  peak=32768（削顶）rms 24.8%   ← 起振
#     第 1 秒  peak= 1397         rms  0.34%  ← 稳态，只有 50Hz 工频 + 400Hz 空闲音
# 只录 1 秒的话，录到的**全是**瞬态，于是"没接麦克风"会被报成 12~25% 的"有信号"。
PROBE_SKIP_S = 1


def probe_level(device, seconds=1):
    """录一小段，回 (peak, rms_pct)；打不开回 None。

    只用来在启动时给人一句"这个设备到底有没有信号"的判断依据，
    不用来当开关 —— 一个安静的房间里，真麦克风的电平也可能很低，
    所以这里只告警，不否决。

    返回的 peak/rms **只统计稳态**（丢掉开头 PROBE_SKIP_S 秒的起振瞬态），
    否则一次上电瞬态就能把"没接麦克风"判成"有信号"。
    """
    raw = "/tmp/_audio_probe_%d.raw" % abs(hash(device))
    for channels in (1, 2):
        # 两种声道数都得试：ES8388 的采集只支持立体声（单声道报
        # "Channels count non available"），USB 摄像头反过来只支持单声道。
        # 多录 PROBE_SKIP_S 秒，供下面丢掉。**秒数必须是整数**：arecord 的
        # `-d` 不认小数，给 `-d 2.0` 会直接打不开设备，伪装成"设备不能用"。
        total = int(seconds) + PROBE_SKIP_S
        try:
            r = subprocess.run(
                ["arecord", "-D", device, "-f", "S16_LE", "-r", "48000",
                 "-c", str(channels), "-d", str(total), "-t", "raw", raw],
                capture_output=True, timeout=total + 6)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode != 0:
            continue
        try:
            with open(raw, "rb") as f:
                data = f.read()
        except OSError:
            continue
        finally:
            try:
                import os
                os.unlink(raw)
            except OSError:
                pass
        if not data:
            continue
        import array
        import math
        samples = array.array("h")
        samples.frombytes(data)
        if not samples:
            continue
        # 按**帧**跳，不是按采样点：立体声时一帧两个采样。
        skip = 48000 * PROBE_SKIP_S * channels
        steady = samples[skip:] if skip < len(samples) else samples
        if not steady:
            continue
        peak = max(abs(x) for x in steady)
        rms = math.sqrt(sum(float(x) * x for x in steady) / len(steady))
        return peak, 100.0 * rms / 32768
    return None


# --------------------------------------------------------------------------- #
# 选设备
# --------------------------------------------------------------------------- #
def detect_mic(log):
    """挑采集设备。返回设备字符串，挑不出来返回 None。

    顺序：四极耳麦（插孔检测说 on）→ USB 声卡 → 失败。
    """
    es = card_of(ES8388_NAME)
    if es is not None:
        state = jack_state(es, "Headset Mic Jack")
        if state == "on":
            log.info("检测到四极耳麦（Headset Mic Jack=on）→ hw:%d,0", es)
            return "hw:%d,0" % es
        log.info("3.5mm 口的麦克风那一极没检测到（Headset Mic Jack=%s）",
                 state or "读不到")

    # 剩下的采集卡里挑一个。排除 ES8388 自己（它的采集是浮空的）。
    for card, dev, name in _list("cap"):
        if card == es:
            continue
        log.info("用 USB 采集卡 card %d（%s）→ hw:%d,%d", card, name, card, dev)
        return "hw:%d,%d" % (card, dev)
    return None


def detect_spk(log):
    """挑播放设备。本项目就认 3.5mm 那一路（其余是 HDMI/DP，演示用不上）。"""
    es = card_of(ES8388_NAME)
    if es is None:
        return None
    return "hw:%d,0" % es


def resolve(args, log):
    """把 --mic-device / --spk-device 定下来，写回 args。

    在**启动时**做一次，不是每次建 pipeline 时做：公网模式下每来一个观看端
    就会建一条 pipeline，拖到那时候才发现"没麦克风"会按观看者数重复报错。
    """
    if args.mic_device:
        log.info("麦克风由 --mic-device 指定：%s", args.mic_device)
    else:
        args.mic_device = detect_mic(log)
        if not args.mic_device:
            raise AudioUnavailable(
                "找不到可用的麦克风。3.5mm 口里插的是三极耳机（没有麦克风那一极），"
                "也没有 USB 采集设备。\n"
                "  先跑 tools/check_audio.sh 看硬件，然后：\n"
                "    - 插一个 USB 麦克风，或换一副**四极**耳麦；或\n"
                "    - 用 --mic-device 显式指定一个设备；或\n"
                "    - 用 --no-audio 只推视频（阶段一的行为）"
            )

    if args.spk_device:
        log.info("扬声器由 --spk-device 指定：%s", args.spk_device)
    else:
        args.spk_device = detect_spk(log)
        if not args.spk_device:
            raise AudioUnavailable(
                "找不到播放设备（期望 %s 那张卡）。"
                "用 --spk-device 显式指定，或用 --no-audio 只推视频。" % ES8388_NAME
            )

    # 电平探测只为了在日志里给一句人话。**不否决** ——
    # 一个安静的房间里真麦克风的电平也可能很低，那不是错误。
    got = probe_level(args.mic_device)
    if got is None:
        log.warning("麦克风 %s 打不开。arecord 试过单声道和立体声都不行。",
                    args.mic_device)
    else:
        peak, pct = got
        log.info("麦克风 %s 电平探测：peak=%d  rms=%.2f%% 满量程",
                 args.mic_device, peak, pct)
        if pct < 1.0:
            log.warning(
                "  麦克风 %s 只有 %.2f%% 满量程，和「浮空输入」的特征一致"
                "（USB 麦克风实测 12.9%%）。浏览器那边听起来会只是沙沙声。"
                "跑 tools/check_audio.sh 确认插的是不是没有麦克风的三极耳机。",
                args.mic_device, pct)

    return args
