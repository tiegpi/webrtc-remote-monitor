#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_detect.py —— 人像检测预处理/后处理的单测（宿主机就能跑，不需要 GStreamer）

    python3 tools/check_detect.py

覆盖的是最容易静默出错的三段：

  · letterbox：尺寸、补边位置、缩放比例。补错边会让框整体上下偏移；
  · frame_stride：从 buffer 大小反推行对齐。推错会出现斜切画面，而且
    同时会踩到一条更毒的坑 —— 别用 GstVideo.VideoInfo.from_caps()，见
    src/detect.py 里的注释；
  · too_soon：检测线程的兜底限流判据。videorate 在采集段重建后会有一段时间
    不按 fps 丢帧，靠这个判据把多出来的帧挡在推理之前；
  · decode：YOLOv5 三尺度 + anchor 解码。通道顺序或 anchor 写错时，
    模型照样输出张量、代码照样不报错，只是框全错 —— 这里用合成张量把
    期望坐标算死，错一位就红；
  · NMS / normalize：去重和归一化。漏了 NMS 会把同一个人画好几层框；
    归一化错则页面上的框和视频对不齐。

板子上有模型时，还可以跑一遍真实推理（不接摄像头）：

    python3 tools/check_detect.py --model /userdata/.../yolov5s-640-640.rknn
"""

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import detect  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILS.append(name)
        print("FAIL  %-38s %s" % (name, exc))
    except Exception as exc:                     # noqa: BLE001
        FAILS.append(name)
        print("FAIL  %-38s 未预期的异常：%r" % (name, exc))
    else:
        print("PASS  %s" % name)


def np():
    return detect.require_numpy()


# --------------------------------------------------------------------------- #
# letterbox
# --------------------------------------------------------------------------- #
def test_letterbox_16x9():
    canvas, scale, px, py = detect.letterbox(np().zeros((360, 640, 3),
                                                        dtype=np().uint8))
    assert canvas.shape == (640, 640, 3), canvas.shape
    assert scale == 1.0, scale
    assert (px, py) == (0, 140), (px, py)
    # 上下补的是 114 灰边，中间是原图（全 0）
    assert int(canvas[0, 0, 0]) == 114, canvas[0, 0, 0]
    assert int(canvas[139, 10, 0]) == 114
    assert int(canvas[140, 10, 0]) == 0
    assert int(canvas[499, 10, 0]) == 0
    assert int(canvas[500, 10, 0]) == 114


def test_letterbox_downscale():
    canvas, scale, px, py = detect.letterbox(np().zeros((720, 1280, 3),
                                                        dtype=np().uint8))
    assert canvas.shape == (640, 640, 3)
    assert abs(scale - 0.5) < 1e-9, scale
    assert (px, py) == (0, 140), (px, py)


def test_letterbox_bad_shape():
    try:
        detect.letterbox(np().zeros((10, 10), dtype=np().uint8))
    except ValueError:
        pass
    else:
        raise AssertionError("非 HxWx3 应当抛 ValueError")


def test_frame_stride_from_buffer_size():
    # 640*3 已经 4 字节对齐：紧凑
    assert detect.frame_stride(640, 360, 1920 * 360) == 1920
    # 638*3=1914，按 4 字节对齐成 1916（行尾有 2 字节填充）
    assert detect.frame_stride(638, 360, 1916 * 360) == 1916
    # 除不尽（尾巴不是整行）：按紧凑算，不瞎猜
    assert detect.frame_stride(640, 360, 1920 * 360 + 999) == 1920
    # 高度非法时不抛异常，退回紧凑
    assert detect.frame_stride(640, 0, 0) == 1920


def test_too_soon_rate_gate():
    # 第一帧：没有上一帧，必须放行
    assert detect.too_soon(None, 100.0, 5) is False
    # 5 fps → 200ms 间隔；正常抖动（180ms 以上）放行
    assert detect.too_soon(100.0, 100.18, 5) is False
    assert detect.too_soon(100.0, 100.20, 5) is False
    # 采集段重建后 videorate 全放行（~32/s，间隔 31ms）：跳过
    assert detect.too_soon(100.0, 100.031, 5) is True
    # 恰好落在阈值上（0.9*200ms=180ms）不算太快
    assert detect.too_soon(100.0, 100.179, 5) is True
    assert detect.too_soon(100.0, 100.181, 5) is False
    # fps 非法时按 1 fps 保守限流，不除零
    assert detect.too_soon(100.0, 100.5, 0) is True


# --------------------------------------------------------------------------- #
# decode
# --------------------------------------------------------------------------- #
def _blank_outputs():
    n = np()
    return [
        n.zeros((1, 255, 80, 80), dtype=n.float32),
        n.zeros((1, 255, 40, 40), dtype=n.float32),
        n.zeros((1, 255, 20, 20), dtype=n.float32),
    ]


def _put(out, anchor, gy, gx, xywh=(0.5, 0.5, 0.5, 0.5), obj=0.9,
         cls_idx=0, cls_p=0.95):
    """往 (1,255,h,w) 的一个格子写一份检测。"""
    n = np()
    a = out.reshape(3, 5 + detect.NUM_CLASSES, out.shape[2], out.shape[3])
    a[anchor, 0, gy, gx] = xywh[0]
    a[anchor, 1, gy, gx] = xywh[1]
    a[anchor, 2, gy, gx] = xywh[2]
    a[anchor, 3, gy, gx] = xywh[3]
    a[anchor, 4, gy, gx] = obj
    a[anchor, 5 + cls_idx, gy, gx] = cls_p


def test_decode_known_box():
    outs = _blank_outputs()
    # stride 8、anchor (10,13)、grid (x=20, y=10)，四个 sigmoid 值都是 0.5：
    #   cx = (0.5*2 - 0.5 + 20) * 8 = 164
    #   cy = (0.5*2 - 0.5 + 10) * 8 = 84
    #   w  = (0.5*2)^2 * 10 = 10
    #   h  = (0.5*2)^2 * 13 = 13
    _put(outs[0], 0, 10, 20)
    dets = detect.decode(outs)
    assert len(dets) == 1, "期望 1 个框，实际 %d" % len(dets)
    score, cls, x, y, w, h = dets[0]
    assert cls == detect.PERSON_CLASS
    assert abs(score - 0.855) < 1e-4, score
    assert abs(x - 159.0) < 1e-4, x
    assert abs(y - 77.5) < 1e-4, y
    assert abs(w - 10.0) < 1e-4, w
    assert abs(h - 13.0) < 1e-4, h


def test_decode_ignores_other_classes():
    outs = _blank_outputs()
    _put(outs[0], 0, 10, 20, cls_idx=1, cls_p=0.99)   # 不是 person
    assert detect.decode(outs) == [], "非 person 类不该出框"


def test_decode_confidence_floor():
    outs = _blank_outputs()
    _put(outs[0], 0, 10, 20, obj=0.2, cls_p=0.9)      # 0.18 < 0.35
    assert detect.decode(outs) == []


def test_decode_bad_layout():
    outs = _blank_outputs()
    outs[0] = np().zeros((1, 84, 80, 80), dtype=np().float32)
    try:
        detect.decode(outs)
    except ValueError:
        pass
    else:
        raise AssertionError("输出通道数不对时应当抛 ValueError")


# --------------------------------------------------------------------------- #
# NMS / normalize
# --------------------------------------------------------------------------- #
def test_nms_keeps_best():
    # 两个高度重叠的框（IoU > 0.45），保留分数高的那个
    a = [0.9, 0, 100, 100, 50, 50]
    b = [0.8, 0, 102, 102, 50, 50]
    keep = detect.nms([b, a], iou=0.45)
    assert len(keep) == 1 and keep[0][0] == 0.9, keep


def test_nms_keeps_disjoint():
    a = [0.9, 0, 0, 0, 10, 10]
    b = [0.8, 0, 100, 100, 10, 10]
    keep = detect.nms([a, b], iou=0.45)
    assert len(keep) == 2, keep
    assert keep[0][0] == 0.9, "结果应按 score 降序"


def test_normalize_maps_padding():
    # 640×360 原帧、letterbox 到 640×640（上下各补 140）
    # 输入空间里的 (100, 300, 80, 100) → 原帧 (100, 160, 80, 100)
    out = detect.normalize([[0.9, 0, 100, 300, 80, 100]],
                           scale=1.0, pad_x=0, pad_y=140,
                           frame_w=640, frame_h=360)
    assert len(out) == 1, out
    x, y, w, h, score = out[0]
    assert abs(x - 100 / 640) < 1e-4, x
    assert abs(y - 160 / 360) < 1e-4, y
    assert abs(w - 80 / 640) < 1e-4, w
    assert abs(h - 100 / 360) < 1e-4, h
    assert abs(score - 0.9) < 1e-4


def test_normalize_drops_box_in_padding():
    # y=84 在顶部灰边里（84 < 140），映射回原帧后高度为 0，应当丢掉
    out = detect.normalize([[0.9, 0, 159, 77.5, 10, 13]],
                           scale=1.0, pad_x=0, pad_y=140,
                           frame_w=640, frame_h=360)
    assert out == [], out


def test_normalize_clamps_to_frame():
    out = detect.normalize([[0.9, 0, -5, 400, 700, 300]],
                           scale=1.0, pad_x=0, pad_y=140,
                           frame_w=640, frame_h=360)
    assert len(out) == 1, out
    x, y, w, h, _ = out[0]
    assert 0 <= x <= 1 and 0 <= y <= 1 and 0 <= w <= 1 and 0 <= h <= 1
    assert abs(x - 0.0) < 1e-6, x
    assert abs(x + w - 1.0) < 1e-6, (x, w)


# --------------------------------------------------------------------------- #
# 可选：真实模型推理
# --------------------------------------------------------------------------- #
def _synthetic_sample(Gst, width=640, height=360):
    """造一帧真实的 RGB GstSample，专门走 _infer 的 stride/caps 路径。

    **不手搓 Gst.Buffer**：这一段要验证的恰恰是 caps 和 buffer 大小 →
    stride 的推导，只有真实的 GStreamer sample 才有意义。顺手也验证了
    GstApp 的包装类型（没 import 时 appsink 没有 try_pull_sample）。
    """
    import gi
    gi.require_version("GstApp", "1.0")
    from gi.repository import GstApp  # noqa: F401

    if not Gst.is_initialized():
        Gst.init(None)

    pipe = Gst.parse_launch(
        "videotestsrc num-buffers=2 ! video/x-raw,format=RGB,width=%d,height=%d "
        "! appsink name=s max-buffers=1 drop=true" % (width, height))
    sink = pipe.get_by_name("s")
    pipe.set_state(Gst.State.PLAYING)
    try:
        sample = sink.try_pull_sample(2 * Gst.SECOND)
    finally:
        pipe.set_state(Gst.State.NULL)
    if sample is None:
        raise RuntimeError("videotestsrc 没有出帧（GStreamer base 插件装了？）")
    return sample


def live_check(model):
    problem = detect.check_available(model)
    if problem:
        print("SKIP  真实模型推理（%s）" % problem.splitlines()[0])
        return
    try:
        Gst = detect.require_gst()
        detect.prepare_appsink()
    except RuntimeError as exc:
        print("SKIP  真实模型推理（%s）" % exc)
        return

    det = detect.Detector(model, fps=5)
    try:
        sample = _synthetic_sample(Gst)
        t0 = time.perf_counter()
        result = det._infer(sample, Gst)
        ms = (time.perf_counter() - t0) * 1000.0
        # testsrc 的图案不是人，但**不断言** n==0：那是模型的判断，不是
        # 这条测试的对象。这里要证明的是"整条 _infer 路径跑通了"。
        assert result["t"] == "det" and isinstance(result["boxes"], list)
        print("PASS  真实模型推理（%.1f ms，%d 个框）" % (ms, result["n"]))
    finally:
        det.release()


def main(argv=None):
    ap = argparse.ArgumentParser(description="人像检测后处理单测")
    ap.add_argument("--model", default=None,
                    help="给了就跑一次真实推理（板子上才有用）")
    args = ap.parse_args(argv)

    tests = [(name[5:], fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        check(name, fn)

    if args.model:
        live_check(args.model)

    print()
    if FAILS:
        print("%d / %d 项失败" % (len(FAILS), len(tests)))
        return 1
    print("全部 %d 项通过" % len(tests))
    return 0


if __name__ == "__main__":
    sys.exit(main())
