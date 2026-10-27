#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_detect_mode.py —— detect 模式预处理/后处理/白平衡的单测

宿主机就能跑，不需要 GStreamer、RKNN，也不接摄像头：

    python3 tools/check_detect_mode.py

覆盖的是最容易静默出错、而且错了也"看起来在跑"的几段：

  · letterbox：尺寸、补边位置、缩放比例。补错边会让框整体上下偏移；
  · frame_stride：从 buffer 大小反推行对齐。推错会出斜切画面，还会踩到
    GstVideo.from_caps() 那个段错误的老坑（本模块刻意不碰 GstVideo）；
  · decode：YOLOv5 三尺度 + anchor 解码。通道顺序或 anchor 写错时，模型
    照样输出张量、代码照样不报错，只是框全错 —— 这里用合成张量把期望
    坐标算死，错一位就红；
  · NMS / to_frame_boxes：去重、映射回原帧、裁边；
  · ColorBalance：固定增益和自校准两条路。自校准用"偏绿"的合成帧，
    校验算出来的 R/B 增益确实把三通道拉平。

板子上有模型时，还可以跑一遍真实推理（不接摄像头）：

    python3 -s tools/check_detect_mode.py \\
        --model /userdata/rknn_yolov5_demo/model/RK3588/yolov5s-640-640.rknn \\
        --image /userdata/rknn_yolov5_demo/model/bus.jpg

带 --image 时会要求至少检出一个 person（bus.jpg 里有 4 个）。没带 --image
就只验证模型能加载、能推理、输出布局对。
"""

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import detect_mode  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILS.append(name)
        print("FAIL  %-42s %s" % (name, exc))
    except Exception as exc:                     # noqa: BLE001
        FAILS.append(name)
        print("FAIL  %-42s 未预期的异常：%r" % (name, exc))
    else:
        print("PASS  %s" % name)


def np():
    return detect_mode.require_numpy()


def cv2():
    return detect_mode.require_cv2()


# --------------------------------------------------------------------------- #
# letterbox / stride
# --------------------------------------------------------------------------- #
def test_letterbox_16x9():
    canvas, scale, px, py = detect_mode.letterbox_bgr(
        np().zeros((360, 640, 3), dtype=np().uint8))
    assert canvas.shape == (640, 640, 3), canvas.shape
    assert abs(scale - 1.0) < 1e-9, scale
    assert (px, py) == (0, 140), (px, py)
    # 上下补的是 114 灰边，中间是原图（全 0）
    assert int(canvas[0, 0, 0]) == 114, canvas[0, 0, 0]
    assert int(canvas[139, 10, 0]) == 114
    assert int(canvas[140, 10, 0]) == 0
    assert int(canvas[499, 10, 0]) == 0
    assert int(canvas[500, 10, 0]) == 114


def test_letterbox_downscale():
    canvas, scale, px, py = detect_mode.letterbox_bgr(
        np().zeros((720, 1280, 3), dtype=np().uint8))
    assert canvas.shape == (640, 640, 3)
    assert abs(scale - 0.5) < 1e-9, scale
    assert (px, py) == (0, 140), (px, py)


def test_letterbox_bad_shape():
    try:
        detect_mode.letterbox_bgr(np().zeros((10, 10), dtype=np().uint8))
    except ValueError:
        pass
    else:
        raise AssertionError("非 HxWx3 应当抛 ValueError")


def test_frame_stride_from_buffer_size():
    # 640*3 已经 4 字节对齐：紧凑
    assert detect_mode.frame_stride(640, 360, 1920 * 360) == 1920
    # 638*3=1914，按 4 字节对齐成 1916（行尾有 2 字节填充）
    assert detect_mode.frame_stride(638, 360, 1916 * 360) == 1916
    # 除不尽（尾巴不是整行）：按紧凑算，不瞎猜
    assert detect_mode.frame_stride(640, 360, 1920 * 360 + 999) == 1920
    # 高度非法时不抛异常，退回紧凑
    assert detect_mode.frame_stride(640, 0, 0) == 1920


# --------------------------------------------------------------------------- #
# decode / NMS / 映射
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
    a = out.reshape(3, 5 + detect_mode.NUM_CLASSES, out.shape[2], out.shape[3])
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
    # 返回的 x/y 是左上角，所以是 (159, 77.5)。
    _put(outs[0], 0, 10, 20)
    dets = detect_mode.decode(outs)
    assert len(dets) == 1, "期望 1 个框，实际 %d" % len(dets)
    score, cls, x, y, w, h = dets[0]
    assert cls == detect_mode.PERSON_CLASS
    assert abs(score - 0.855) < 1e-4, score
    assert abs(x - 159.0) < 1e-4, x
    assert abs(y - 77.5) < 1e-4, y
    assert abs(w - 10.0) < 1e-4, w
    assert abs(h - 13.0) < 1e-4, h


def test_decode_ignores_other_classes():
    outs = _blank_outputs()
    _put(outs[0], 0, 10, 20, cls_idx=1, cls_p=0.99)   # 不是 person
    assert detect_mode.decode(outs) == [], "非 person 类不该出框"


def test_decode_confidence_floor():
    outs = _blank_outputs()
    _put(outs[0], 0, 10, 20, obj=0.2, cls_p=0.9)      # 0.18 < 0.35
    assert detect_mode.decode(outs) == []


def test_decode_bad_layout():
    outs = _blank_outputs()
    outs[0] = np().zeros((1, 84, 80, 80), dtype=np().float32)
    try:
        detect_mode.decode(outs)
    except ValueError:
        pass
    else:
        raise AssertionError("输出通道数不对时应当抛 ValueError")


def test_nms_keeps_best():
    a = [0.9, 0, 100, 100, 50, 50]
    b = [0.8, 0, 102, 102, 50, 50]
    keep = detect_mode.nms([b, a], iou=0.45)
    assert len(keep) == 1 and keep[0][0] == 0.9, keep


def test_nms_keeps_disjoint():
    a = [0.9, 0, 0, 0, 10, 10]
    b = [0.8, 0, 100, 100, 10, 10]
    keep = detect_mode.nms([a, b], iou=0.45)
    assert len(keep) == 2, keep
    assert keep[0][0] == 0.9, "结果应按 score 降序"


def test_to_frame_boxes_maps_padding():
    # 640×360 原帧、letterbox 到 640×640（上下各补 140）
    # 输入空间里的 (100, 300, 80, 100) → 原帧 (100, 160, 80, 100)
    out = detect_mode.to_frame_boxes([[0.9, 0, 100, 300, 80, 100]],
                                     scale=1.0, pad_x=0, pad_y=140,
                                     frame_w=640, frame_h=360)
    assert len(out) == 1, out
    x1, y1, x2, y2, score = out[0]
    assert (x1, y1, x2, y2) == (100, 160, 180, 260), out[0]
    assert abs(score - 0.9) < 1e-6


def test_to_frame_boxes_drops_box_in_padding():
    # y=84 在顶部灰边里（84 < 140），映射回原帧后高度为 0，应当丢掉
    out = detect_mode.to_frame_boxes([[0.9, 0, 159, 77.5, 10, 13]],
                                     scale=1.0, pad_x=0, pad_y=140,
                                     frame_w=640, frame_h=360)
    assert out == [], out


def test_to_frame_boxes_clamps_to_frame():
    out = detect_mode.to_frame_boxes([[0.9, 0, -5, 400, 700, 300]],
                                     scale=1.0, pad_x=0, pad_y=140,
                                     frame_w=640, frame_h=360)
    assert len(out) == 1, out
    x1, y1, x2, y2, _ = out[0]
    assert 0 <= x1 < x2 <= 640 and 0 <= y1 < y2 <= 360, out[0]
    assert (x1, y1, x2, y2) == (0, 260, 640, 360), out[0]


def test_draw_boxes_changes_pixels():
    frame = np().zeros((120, 160, 3), dtype=np().uint8)
    detect_mode.draw_boxes(frame, [(10, 20, 100, 110, 0.9)])
    # 框线是 BGR 绿 (0,255,0)
    assert tuple(int(v) for v in frame[20, 10]) == (0, 255, 0), frame[20, 10]
    assert tuple(int(v) for v in frame[60, 60]) == (0, 0, 0), "框内不该被填满"


# --------------------------------------------------------------------------- #
# 骨架描述串
# --------------------------------------------------------------------------- #
class _FakeArgs:
    """只填 skeleton_desc() 真正读到的字段。"""
    gop = 10
    bitrate = 2_000_000
    width = 1280
    height = 720
    fps = 30
    mic_device = "hw:0,0"
    no_audio = False
    audio_bitrate = 32000


def test_skeleton_includes_audio_branch():
    """audio_enabled 时描述串里必须有 `aq`。

    这里曾经漏掉过：build_detect() 无条件按 sink_1 链接 `aq`，而骨架里
    没写音频分支，get_by_name("aq") 返回 None —— 有麦克风的板子一建会话就
    抛 AttributeError，日志只有一行"建 pipeline 失败"。这个用例把它钉住。
    """
    a = _FakeArgs()
    desc = detect_mode.skeleton_desc(a)
    assert "appsrc name=detsrc" in desc
    assert "videoconvert n-threads=2" in desc
    assert "queue name=aq" in desc, "有麦克风时骨架必须带音频分支"
    assert "rtph264pay" in desc

    a.no_audio = True
    assert "queue name=aq" not in detect_mode.skeleton_desc(a)

    a.no_audio = False
    a.mic_device = None
    assert "queue name=aq" not in detect_mode.skeleton_desc(a)


# --------------------------------------------------------------------------- #
# ColorBalance
# --------------------------------------------------------------------------- #
def test_colorbalance_fixed():
    cb = detect_mode.ColorBalance("1.5,1.2")
    assert not cb.auto
    assert cb.desc == "R×1.50 B×1.20", cb.desc
    frame = np().full((8, 8, 3), 100, dtype=np().uint8)
    out = cb.apply(frame)
    # B×1.2, G×1, R×1.5（LUT 四舍五入）
    assert tuple(int(v) for v in out[0, 0]) == (120, 100, 150), out[0, 0]


def _greenish(frames=1, w=64, h=48):
    n = np()
    frame = n.empty((h, w, 3), dtype=n.uint8)
    frame[..., 0] = 60      # B
    frame[..., 1] = 93      # G
    frame[..., 2] = 54      # R
    return [frame.copy() for _ in range(frames)]


def test_colorbalance_auto_freezes_and_flattens():
    cb = detect_mode.ColorBalance("auto")
    assert cb.auto and cb.lut is None
    assert cb.desc == "自校准中", cb.desc
    for frame in _greenish(detect_mode.whitebalance.WB_CALIB_FRAMES):
        cb.apply(frame)
    assert cb.lut is not None, "校准完应当建出 LUT"
    assert cb.desc.startswith("R×"), "冻结后 desc 要给出实际增益：%r" % cb.desc
    assert abs(cb.kr - 93 / 54) < 0.02, cb.kr
    assert abs(cb.kb - 93 / 60) < 0.02, cb.kb
    out = cb.apply(_greenish()[0])
    means = [float(out[..., i].mean()) for i in range(3)]
    assert max(means) - min(means) <= 3.0, "灰世界校准后三通道应当接近：%r" % means


def test_colorbalance_auto_dark_falls_back():
    cb = detect_mode.ColorBalance("auto")
    dark = np().full((8, 8, 3), 2, dtype=np().uint8)
    for _ in range(detect_mode.whitebalance.WB_CALIB_FRAMES):
        cb.apply(dark)
    assert cb.lut is not None
    assert (cb.kr, cb.kb) == detect_mode.whitebalance.WB_DEFAULT_GAINS, (
        cb.kr, cb.kb)


# --------------------------------------------------------------------------- #
# 可选：真实模型推理
# --------------------------------------------------------------------------- #
def live_check(model, image=None):
    print("\n真实推理：%s" % model)
    try:
        runner = detect_mode.RknnRunner(model)
    except Exception as exc:                     # noqa: BLE001
        print("SKIP  模型/运行时不可用：%s" % exc)
        return
    try:
        if image:
            frame = cv2().imread(image)
            if frame is None:
                raise AssertionError("读不出图片: %s" % image)
        else:
            frame = np().full((360, 640, 3), 114, dtype=np().uint8)
        t0 = time.perf_counter()
        boxes, ms = runner.infer(frame)
        total = (time.perf_counter() - t0) * 1000.0
        print("  帧 %dx%d，推理 %.1f ms，端到端 %.1f ms，检出 %d 个 person"
              % (frame.shape[1], frame.shape[0], ms, total, len(boxes)))
        for x1, y1, x2, y2, score in boxes:
            print("    person %.3f  (%d,%d)-(%d,%d)" % (score, x1, y1, x2, y2))
        if image:
            assert boxes, "bus.jpg 里应当至少检出一个人"
    finally:
        runner.release()


# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(description="detect 模式纯函数单测")
    p.add_argument("--model", default=None,
                   help="给了就跑一遍真实 RKNN 推理（板子上）")
    p.add_argument("--image", default=None,
                   help="配合 --model：用这张图推理，并断言至少检出一个人")
    args = p.parse_args(argv)

    for name, fn in [
        ("letterbox 16:9 补边", test_letterbox_16x9),
        ("letterbox 720p 下采样", test_letterbox_downscale),
        ("letterbox 拒绝非 3 通道", test_letterbox_bad_shape),
        ("frame_stride 反推", test_frame_stride_from_buffer_size),
        ("decode 已知框", test_decode_known_box),
        ("decode 忽略非 person", test_decode_ignores_other_classes),
        ("decode 置信度门槛", test_decode_confidence_floor),
        ("decode 拒绝错误布局", test_decode_bad_layout),
        ("NMS 保留高分", test_nms_keeps_best),
        ("NMS 保留不重叠", test_nms_keeps_disjoint),
        ("to_frame_boxes 去补边", test_to_frame_boxes_maps_padding),
        ("to_frame_boxes 丢掉灰边里的框", test_to_frame_boxes_drops_box_in_padding),
        ("to_frame_boxes 裁到画面内", test_to_frame_boxes_clamps_to_frame),
        ("draw_boxes 改像素", test_draw_boxes_changes_pixels),
        ("骨架含音频分支", test_skeleton_includes_audio_branch),
        ("ColorBalance 固定增益", test_colorbalance_fixed),
        ("ColorBalance 自校准拉平", test_colorbalance_auto_freezes_and_flattens),
        ("ColorBalance 暗帧退回默认", test_colorbalance_auto_dark_falls_back),
    ]:
        check(name, fn)

    if args.model:
        live_check(args.model, args.image)

    print()
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("全部通过（%d 项纯函数测试）" % 18)
    return 0


if __name__ == "__main__":
    sys.exit(main())
