#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sources.py —— 这块板子上有哪些摄像头，各自要什么

接了两个摄像头之后，"用哪个"不该变成散落在各处的 if/else：设备节点、原生格式、
要不要解码、要不要画质兜底 —— 这几件事是**同一个决定**，集中在下面那张表里，
别的地方只认一个名字（"mipi" / "usb"）。

摄像头是**运行期可换的**（观看页上有切换按钮），所以这张表是"可选项清单"，
不是"启动参数"。当前用的是哪一个由 device_agent 记着。

**几何尺寸刻意不在这张表里。** 宽高帧率在命令行上（--width/--height/--fps），
对所有源一视同仁，因为「切换不用重新协商」这条性质**依赖两路编码输出的 caps
完全相同**。要是让每个源各带一份尺寸，就可能被配成不一样，而那种错配的症状是
"切过去浏览器黑屏/只显示一角"，离真正的原因很远 —— 与其靠约定，不如让它结构上
不可能。要改分辨率就是两边一起改。
"""

import logging
import os
from dataclasses import dataclass

log = logging.getLogger("sources")


@dataclass(frozen=True)
class CameraSource:
    name: str          # 命令行和 HTTP 上用这个名字
    label: str         # 给页面显示
    device: str        # V4L2 节点
    kind: str          # "raw" = ISP 直接出 YUV；"mjpeg" = 摄像头出 MJPG，要解码
    fourcc: str        # 给 v4l2-ctl / 报错信息用
    quality_fix: bool  # 板子没 3A 才需要 camera.py 的亮度闭环
    hint: str          # 按钮上的悬停说明。放在这里而不是页面里：
                       # "切过去会怎样"是源的属性，页面不该替每一路背它


# 两个源都必须能出同一份编码输出（1280x720 H.264），否则"切换不用重新协商"
# 就不成立了。已经实测确认：
#   mipi  UYVY  → mpph264enc 直接吃（ISP 原生输出就是 UYVY，中间不用转换）
#   usb   MJPG  → mppjpegdec 解成 NV12 → mpph264enc 也直接吃
# 中间**一次 videoconvert 都不需要**，这是两路能共用同一个打包器的前提。
SOURCES = (
    CameraSource(
        name="mipi",
        label="MIPI（板载）",
        device="/dev/video11",
        kind="raw",
        fourcc="UYVY",
        # 板子上没有 rkaiq 3A 服务，AE/AWB 都不跑：增益顶到上限 248、曝光 1648
        # 也只有 Y≈78，而且整体偏绿。所以要 camera.py 闭环 + whitebalance 桥。
        quality_fix=True,
        hint="板载 MIPI。板子没有 3A，画面靠软件兜底，"
             "所以切回这一路有几秒钟画面在爬（曝光闭环 + 白平衡重跑）",
    ),
    CameraSource(
        name="usb",
        label="USB 摄像头",
        device="/dev/video20",
        kind="mjpeg",
        fourcc="MJPG",
        # 这颗自带 AE/AWB（实测能到 Y=226），**兜底反而要跳过** ——
        # 那些控件是写 MIPI sensor subdev 的，对 USB 摄像头没意义。
        quality_fix=False,
        hint="USB 摄像头。自带 AE/AWB，切过去一步到位，画面更亮、视角也不同",
    ),
)

_BY_NAME = {s.name: s for s in SOURCES}


def names():
    return [s.name for s in SOURCES]


def resolve(name):
    """名字 → 源。不认识就抛 KeyError，错误信息里直接列出可选项。

    别退化成"不认识就用默认的"：那样页面上点错一个名字会静默看另一路画面，
    比报错难查得多。
    """
    src = _BY_NAME.get((name or "").strip().lower())
    if src is None:
        raise KeyError("未知摄像头 %r；可选：%s" % (name, ", ".join(names())))
    return src


def message_of(exc):
    """取 KeyError 里那句人话。

    `str(KeyError("未知摄像头 'x'"))` 是 `'"未知摄像头 \\'x\\'"'` —— 带一层引号，
    直接打进日志和 HTTP 响应体会多一对莫名其妙的引号。
    """
    return exc.args[0] if exc.args else str(exc)


def initial(name):
    """启动时定初始源。给的名字不认识就退回第一个，并明确告警。"""
    try:
        return resolve(name)
    except KeyError as exc:
        log.warning("%s —— 退回 %s", message_of(exc), SOURCES[0].name)
        return SOURCES[0]


def available():
    """给 GET /camera 用的清单。

    `present` 是**设备节点在不在**，不是"能不能出图" —— 摄像头拔了就是这个
    字段为 false，页面据此把按钮置灰。真的打开失败会在切换时报出来。
    """
    return [
        {
            "name": s.name,
            "label": s.label,
            "device": s.device,
            "present": os.path.exists(s.device),
            "hint": s.hint,
        }
        for s in SOURCES
    ]


def apply_quality(src, args, log_=None):
    """切到某个源之前，跑一次它需要的画质兜底。

    只对 quality_fix 的源跑：USB 摄像头自带 3A，跑这套既没意义又会去写
    MIPI sensor 的 subdev 控件。

    **调用时机是"拆旧段之前、建新段之前"**，不是切换之后：闭环要独占
    /dev/video12（camera.PROBE_DEVICE，selfpath），此刻旧段已经不再占着
    /dev/video11，量亮度不会和正在推的流抢 ISP。

    另外 args.camera 在这里被同步成源的设备节点 —— 它是"当前设备节点"的派生值，
    session.py 的报错路径（_explain_camera_failure 跑 v4l2-ctl）读的就是它，
    不同步的话报错会指着一个已经不在用的节点。
    """
    log_ = log_ or log
    args.camera = src.device

    if not src.quality_fix:
        log_.info("源 %s（%s）自带 3A/AWB，跳过画质兜底", src.name, src.device)
        return

    import camera  # 惰性：只有真的要跑兜底时才需要它

    camera.setup(args)
