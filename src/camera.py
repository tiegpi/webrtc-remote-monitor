#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
camera.py —— 摄像头画质兜底（亮度这一半）

这块板子上**没有 Rockchip 的 3A 服务**（`rkaiq_3A_server` 之类），而 Rockchip 的
AE/AWB 全在 rkaiq 的用户态库里做，内核 rkisp 只负责把参数写进寄存器。于是 OV13850
会停在驱动默认的那个固定曝光上，症状是**链路全绿、帧率正常、浏览器里却又黑又绿**。

偏绿那一半在 whitebalance.py，这个模块只管亮度。

**控件挂在 subdev 上，不在 /dev/video\\* 上。** 在 /dev/video11（rkisp 主通道）上
`--list-ctrls` 只能看到只读的 pixel_rate，曝光和增益一个都看不见，得去
/dev/v4l-subdev* 里找那个名字带 sensor 型号的
（板子上是 /dev/v4l-subdev2  m00_b_ov13850 7-0010）。

为什么要闭环而不是写死一个增益：**同一个增益在不同房间里差得很远**。实测这块板子
（UYVY 1280x720 折算 Y 均值）：

    gain= 16（驱动默认）→ Y= 9.7     <- 眼睛看就是全黑，这是"没配过"的样子
    gain=112            → Y=39.0
    gain=176            → Y=57.8
    gain=248（控件上限）→ Y=78.1     <- 增益和曝光都顶满了

而那间暗房间里的 248，换到一间亮约 4 倍的屋子里会把 **27% 的像素打到过曝**，
用户看到的是"色彩有点乱"（其实是四分之一幅面变白）。所以写死任何常数都只是在赌
"这次和上次一样亮"。闭环每次开机自己量一遍，暗房间顶到上限、亮房间自己退下来。

**这颗 sensor 是有天花板的**：上面那组是当前这间屋子的实测，248 + 曝光 1648
（两者都到顶）也只有 Y=78。到这个份上就没得调了 —— 要么加光，要么换摄像头。
setup() 会把这件事明确打在日志里，而不是让人对着一个"已经拉满但还是偏暗"的画面猜。
"""

import array
import logging
import os
import re
import subprocess
import tempfile
import time

log = logging.getLogger("camera")

# 驱动默认就是 16，也就是最暗那一档。
GAIN_MIN, GAIN_MAX = 16, 248

# 闭环目标亮度（UYVY 折算的 Y 均值）。210 以上基本就是白墙，110 左右看着正常；
# 这颗 sensor 在暗房间里够不着，那就顶在上限上。
TARGET_Y = 110.0

# 亮到 250 以上（基本算白）的像素超过这个比例就往下调。残留的约 1% 是画面里的灯本身。
CLIP_LIMIT = 0.05

# 量亮度用的 ISP 节点。默认用 selfpath：主通道 /dev/video11 正在推流，而两条路径
# 共用同一个 sensor，所以在 selfpath 上量到的亮度就代表正在推的那一路。推流还没
# 开始时两者都空着，用什么都不影响。
PROBE_DEVICE = "/dev/video12"

CTL_HINT = "--camera-ctl analogue_gain=176,exposure=1648"

# 每种原始格式里 Y/U/V 的起始位置和步长。
#     UYVY = [Cb, Y0, Cr, Y1]   ← 色度在前
#     YUYV = [Y0, Cb, Y1, Cr]   ← 亮度在前
# 色度都是水平 2:1 抽样的：一个色度样本管两个像素。量均值时不用管这件事
# （每个样本都算一次），但做逐像素校正时就必须按像素对算，见 whitebalance.py。
_LAYOUT = {
    "UYVY": (1, 2, 0, 4, 2, 4),
    "YUYV": (0, 2, 1, 4, 3, 4),
}


# --------------------------------------------------------------------------- #
# 找控件挂在哪
# --------------------------------------------------------------------------- #
def driver_name(device):
    """读 /sys 里这个 V4L2 节点的名字（rkisp_mainpath / Q8 HD Webcam / …）。

    video 节点和 v4l-subdev 节点共用这一个 sysfs 属性，所以找 sensor subdev 时
    也复用它。读不到（节点没了、没权限）就返回空串。
    """
    try:
        node = os.path.basename(os.path.realpath(device))
        with open("/sys/class/video4linux/%s/name" % node) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def subdevs():
    """系统里的 V4L2 subdev 节点：[(路径, 名字), ...]。"""
    try:
        nodes = sorted(n for n in os.listdir("/dev") if n.startswith("v4l-subdev"))
    except OSError:
        return []
    out = []
    for node in nodes:
        name = driver_name("/dev/" + node)
        if name:
            out.append(("/dev/" + node, name))
    return out


def ctl_names(path):
    """这个 subdev 认得哪些控件（名字列表）。读不到返回空列表。

    先问一遍再写，而不是直接 `--set-ctrl` 撞上去：撞错了只从 stderr 里捞一句
    "unknown control"，看不出到底哪一步不对；问过之后就能把"系统里根本没这个
    控件"和"有这个控件但写失败"分开报。
    """
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", path, "--list-ctrls"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    # 形如 "              analogue_gain 0x009e0903 (int)    : min=16 max=248 …"
    return re.findall(r"^\s+(\w+)\s+0x[0-9a-fA-F]{8}\b", out.stdout or "", re.M)


def ctl_owners(name):
    """哪些 subdev 认这个控件：[(路径, subdev 名字), ...]。

    逐个问"认不认"而不是猜哪个是传感器：节点名（m00_b_ov13850 7-0010）跟着
    sensor 型号和 dts 走，写死不得；而"谁认这个控件"内核最清楚。
    """
    return [(p, s) for p, s in subdevs() if name in ctl_names(p)]


# --------------------------------------------------------------------------- #
# 读写控件
# --------------------------------------------------------------------------- #
def read_ctl(path, name):
    """读回控件的实际值。读不到返回 None。"""
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", path, "--get-ctrl", name],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r":\s*(-?\d+)", out.stdout or "")
    return int(m.group(1)) if m else None


def write_ctl(path, name, value):
    """写控件并**读回**，返回 (实际生效的值, 错误信息)。

    读回是必须的：内核会把超范围的值默默夹进合法区间再返回成功 —— 实测
    `analogue_gain=9999` 返回 0，写进去的其实是 248。不读回的话，"我明明设了
    9999"和"画面没变化"就成了两笔糊涂账。
    """
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", path, "--set-ctrl", "%s=%d" % (name, value)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "写 %s=%d @ %s 失败: %s" % (name, value, path, exc)
    if out.returncode != 0:
        return None, "%s=%d @ %s: %s" % (
            name, value, path, (out.stderr or out.stdout or "").strip().replace("\n", " "))
    return read_ctl(path, name), None


def parse_ctl(text):
    """把 `analogue_gain=176,exposure=1648` 解析成 [(名字, 值), ...]。

    返回 (控件表, 错误信息)，两个里正好一个不是 None。
    """
    ctls = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, value = item.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name:
            return None, "看不懂 '%s'：要写成 名字=整数，多个用逗号分隔" % item
        try:
            ctls.append((name, int(value, 0)))
        except ValueError:
            return None, "%s 的值 '%s' 不是整数" % (name, value)
    if not ctls:
        return None, "里面一个控件都没有"
    return ctls, None


def apply_ctls(ctls):
    """把控件写进认得它们的 subdev，返回 (实际生效的, 错误信息)。

    实际生效的每项是 (subdev 路径, subdev 名字, 控件名, 要的值, 读回的值)。
    找不到任何一个认得该控件的 subdev 就算错误 —— 用户是**明确要求**设这个值的，
    悄悄跳过只会让他对着一块黑屏猜。
    """
    got = subdevs()
    if not got:
        return None, ("系统里没有 /dev/v4l-subdev* —— 没法写传感器控件。"
                      "（USB 摄像头的增益/曝光常常也在 subdev 上，先确认节点存在）")

    avail = {path: ctl_names(path) for path, _ in got}
    done, problems = [], []
    for name, value in ctls:
        targets = [path for path, names in avail.items() if name in names]
        if not targets:
            where = "、".join("%s(%s) 只有 %s" % (
                path, sub, "、".join(avail[path]) or "什么控件都没有")
                for path, sub in got)
            problems.append("%s：没有 subdev 认这个控件。%s" % (name, where))
            continue
        for path in targets:
            actual, err = write_ctl(path, name, value)
            if err:
                problems.append(err)
                continue
            done.append((path, next(s for p, s in got if p == path),
                         name, value, actual))
    return done, ("\n".join(problems) if problems else None)


# --------------------------------------------------------------------------- #
# 量画面
# --------------------------------------------------------------------------- #
def grab(device, width, height, fourcc, count=2, timeout=20):
    """抓 count 帧原始数据（拼在一起）。失败返回 b""。

    抓两帧而不是一帧：刚改完增益，第一帧常常还是旧的曝光。
    """
    fd, path = tempfile.mkstemp(suffix=".raw", prefix="camprobe-")
    os.close(fd)
    try:
        subprocess.run(
            ["v4l2-ctl", "-d", device,
             "--set-fmt-video=width=%d,height=%d,pixelformat=%s" % (width, height, fourcc),
             "--stream-mmap", "--stream-count=%d" % count,
             "--stream-to=%s" % path],
            capture_output=True, timeout=timeout)
        with open(path, "rb") as fh:
            return fh.read()
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("从 %s 抓帧失败: %s", device, exc)
        return b""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def measure(data, fourcc, width, height):
    """量一帧的亮度和偏色，返回 dict 或 None。

    data 可以是多帧拼接，只量**最后一帧**。折算 R/G/B 用的是全范围 BT.601
    （这条链上标出来的是 1:3:5:1，也就是 0-255 全范围 + BT.601 矩阵），
    偏色看 G/R、G/B 两个比值就够了。

    只用标准库：量亮度这条路不该因为板子上少个 numpy 就走不通。
    """
    if fourcc not in _LAYOUT:
        return None
    n = width * height * 2
    if len(data) < n:
        return None
    a = array.array("B")
    a.frombytes(data[len(data) - n:])
    yo, ys, uo, us, vo, vs = _LAYOUT[fourcc]
    y, u, v = a[yo::ys], a[uo::us], a[vo::vs]
    ny, nc = len(y), len(u)
    my, mu, mv = sum(y) / ny, sum(u) / nc, sum(v) / nc
    clip = sum(1 for s in y if s > 250) / ny
    return {
        "y": my, "clip": clip, "u": mu, "v": mv,
        "r": my + 1.402 * (mv - 128.0),
        "g": my - 0.344136 * (mu - 128.0) - 0.714136 * (mv - 128.0),
        "b": my + 1.772 * (mu - 128.0),
    }


def describe(m):
    """把一次测量打成人看得懂的一行。"""
    if not m:
        return "量不到"
    return ("Y=%.1f  过曝=%.2f%%  R/G/B=%.1f/%.1f/%.1f  G/R=%.2f G/B=%.2f"
            % (m["y"], m["clip"] * 100, m["r"], m["g"], m["b"],
               m["g"] / max(m["r"], 1e-6), m["g"] / max(m["b"], 1e-6)))


# --------------------------------------------------------------------------- #
# 亮度闭环
# --------------------------------------------------------------------------- #
def auto_brightness(device, width, height, fourcc="UYVY", target=TARGET_Y,
                    lo=GAIN_MIN, hi=GAIN_MAX, exposure=None, tries=5):
    """把模拟增益调到画面亮度接近 target，返回 (最终增益, 最后一次测量)。

    增益在线性域直接乘在光量上，所以一步 `gain * target / Y` 基本就到位；
    多做几轮是因为 ISP 那边还有一路 gamma，Y 和增益不是严格成正比。
    """
    owners = ctl_owners("analogue_gain")
    if not owners:
        log.warning("没有 subdev 认 analogue_gain，跳过亮度闭环")
        return None, None
    path = owners[0][0]

    if exposure is not None:
        got, err = write_ctl(path, "exposure", exposure)
        if err:
            log.warning("设曝光失败：%s", err)
        else:
            log.info("曝光设为 %s", got)

    gain = read_ctl(path, "analogue_gain")
    if gain is None:
        log.warning("读不到 analogue_gain，跳过亮度闭环")
        return None, None

    last = None
    for i in range(tries):
        time.sleep(0.8)                     # 等新增益在 sensor 上生效
        last = measure(grab(device, width, height, fourcc), fourcc, width, height)
        if last is None:
            log.warning("从 %s 抓不到帧，亮度闭环停在 gain=%d", device, gain)
            return gain, None
        log.info("亮度闭环 %d/%d：gain=%3d → %s", i + 1, tries, gain, describe(last))

        if last["clip"] > CLIP_LIMIT:
            # 过曝优先：亮度够了但四分之一幅面发白，看着是"颜色乱"，比暗更糟。
            want = gain * (1.0 - min(0.5, (last["clip"] - CLIP_LIMIT) * 4))
        elif abs(last["y"] - target) / target < 0.12:
            break
        else:
            want = gain * target / max(last["y"], 1.0)

        new = int(round(max(lo, min(hi, want))))
        if new == gain:
            break                           # 已经顶到上/下限了
        gain = new
        _, err = write_ctl(path, "analogue_gain", gain)
        if err:
            log.warning("写增益失败：%s", err)
            return gain, last

    return gain, last


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def running_3a():
    """系统里有没有 Rockchip 的 3A 守护进程在跑。有就返回它的命令行。

    直接扫 /proc 而不是 pgrep：少一个进程依赖，也不受 pgrep 匹配方式的摆布。
    """
    try:
        pids = os.listdir("/proc")
    except OSError:
        return ""
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if "rkaiq" in cmd:
            return cmd
    return ""


def setup(args):
    """推流开始前调一次。返回最终生效的增益（没动过返回 None）。

    args.camera_ctl 给了就照写不闭环 —— 那是用户明确指定的值，替他"优化"掉
    只会让画面和他说的话对不上。没给就走闭环。
    """
    device = args.camera_probe or args.camera
    if not os.path.exists(device):
        # selfpath 不一定每块板子都有；退回主通道。推流还没开始，主通道是空的。
        if args.camera_probe and os.path.exists(args.camera):
            log.info("%s 不存在，改用主通道 %s 量亮度", args.camera_probe, args.camera)
            device = args.camera
        else:
            log.warning("%s 不存在，跳过画质兜底", device)
            return None

    if not running_3a():
        log.info("板子上没有 rkaiq 3A 服务，摄像头不会自己曝光/白平衡，"
                 "所以由本进程兜底")
    else:
        log.info("检测到 3A 服务在跑，画质兜底可能和它打架")

    if args.camera_ctl:
        ctls, bad = parse_ctl(args.camera_ctl)
        if bad:
            log.error("--camera-ctl 解析失败：%s", bad)
            return None
        applied, problem = apply_ctls(ctls)
        if problem:
            log.error("--camera-ctl 部分失败：%s", problem)
        for path, sub, name, want, actual in applied or []:
            flag = "" if actual == want else "  <- 被内核夹到了这里"
            log.info("  %s @ %s(%s) = %s%s", name, path, sub, actual, flag)
        log.info("画质兜底：按 --camera-ctl 写死，不做闭环。调不对就 %s", CTL_HINT)
        return None

    if args.camera_no_auto:
        log.info("--camera-no-auto：跳过亮度闭环（板子没 AE，画面会是驱动默认的暗）")
        return None

    log.info("亮度闭环：在 %s 上量，目标 Y≈%.0f", device, args.camera_target_y)
    gain, last = auto_brightness(
        device, args.width, args.height,
        exposure=args.camera_exposure, target=args.camera_target_y)
    if gain is None:
        return None

    if last and last["y"] < args.camera_target_y * 0.8 and gain >= GAIN_MAX:
        # 明说天花板在哪，省得对着"已经拉满但还是暗"的画面怀疑是软件没生效。
        log.warning("增益已经顶到上限 %d，Y 才 %.1f（目标 %.0f）—— "
                    "**这颗 sensor 在这间屋子里就到这了**，想更亮只能加光或换摄像头",
                    GAIN_MAX, last["y"], args.camera_target_y)
    else:
        log.info("亮度闭环结束：gain=%d  %s", gain, describe(last))
    return gain
