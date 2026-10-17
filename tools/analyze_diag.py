#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_diag.py —— 判据检查器

它读服务器上攒下的诊断样本（浏览器 getStats 摘要 + 板子侧的发送计数），
逐条断言，**任何一条不过就以非零码退出**。

这个脚本存在的理由，说白了就一句话：**"画面在动"不是证据。**
旧项目第一版在画面冻住的情况下照样打印了成功日志，所以这里的每条判据都
对应一个具体的、踩过的谎：

  候选对字节持续增长      防 iceConnectionState 报 connected 而实际零媒体
  framesDecoded 增长      防"收到 RTP 包"被当成"解出了帧"
  字节守恒 ≈ 1.0          防只有单向通了却被当成双向通
  稳态窗口采样            防把启动斜坡算进去，量出假的 5fps
  selectedCandidatePair   防 ICE 重启后拿着冻住的旧候选对算速率

用法：
    python3 tools/analyze_diag.py --url http://127.0.0.1:8080 --room demo
    python3 tools/analyze_diag.py --url https://124.223.11.112 \
        --auth viewer:密码 --insecure --room 70ef7d99
    python3 tools/analyze_diag.py --file samples.json
"""

import argparse
import json
import ssl
import sys
import urllib.request
from collections import namedtuple
from urllib.parse import urlsplit, urlunsplit

Check = namedtuple("Check", "name ok detail")

# 稳态窗口：丢掉开头这么多秒。启动阶段有编码器预热、ICE 打洞、关键帧等待，
# 混进来会把平均值拉低到毫无意义（旧项目实测：全域采样算出 5fps，
# 真实值是 25fps，差别全在这个斜坡上）。
DEFAULT_WARMUP = 15.0


def ssl_context(args):
    """给 https:// 的信令服务器用。

    信令服务器在 VPS 上时用的是自签证书，默认信任库里没有它。
    优先用 --cafile 显式信任那张证书 —— 和 device_agent.py 的 --tls-ca 一样，
    是"把证书当信任锚"，不是"关掉校验"。--insecure 只留给一次性的临时排查，
    因为它会让证书 SAN 配错了也照样连上，把问题推到更晚才暴露。
    """
    if not (args.url or "").startswith("https://"):
        return None
    if args.cafile:
        return ssl.create_default_context(cafile=args.cafile)
    if args.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def fetch(url, ctx=None, auth=None):
    """读 /diag。

    VPS 上的外网入口（/webrtc 那一套）现在要求 HTTP Basic Auth，直接
    urlopen 会拿到 401 的 HTML，json 解析炸在一句和权限毫无关系的
    "Expecting value: line 1 column 1" 上。给 --auth 时走带凭证的 opener；
    局域网板子的 /diag 不需要，保持原样。
    """
    if auth:
        user, _, password = auth.partition(":")
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, user, password)
        opener = urllib.request.build_opener(
            urllib.request.HTTPBasicAuthHandler(mgr),
            urllib.request.HTTPSHandler(context=ctx),
        )
        with opener.open(url, timeout=10) as r:
            return json.load(r)
    with urllib.request.urlopen(url, timeout=10, context=ctx) as r:
        return json.load(r)


def diag_url(base, room):
    """把 --url 归一化成 /diag 端点。

    --url 给的是信令服务器的**根地址**（http://127.0.0.1:8080 这样），
    而样本在 /diag 上。早先这里直接把 room 拼在根地址后面，请求打到的是
    首页 —— 拿到一坨 HTML，然后 json 解析炸在 "Expecting value: line 1
    column 1"，看起来像服务器坏了，其实只是路径没拼上。
    给的是完整端点（.../diag）时原样使用。
    """
    p = urlsplit(base)
    path = p.path
    if path in ("", "/"):
        path = "/diag"
    query = p.query
    sep = "&" if query else ""
    return urlunsplit((p.scheme, p.netloc, path,
                       "%s%sroom=%s" % (query, sep, room), ""))


def load_samples(args):
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = fetch(diag_url(args.url, args.room), ssl_context(args),
                     args.auth)
    # 两种形状都得认：`--url` 拿到的 /diag 是 {"items": [...]}，而
    # `--file` 常常是别人自己拼的一个裸列表（比如把浏览器侧和板子侧的样本
    # 合并成一个文件）。写成 data.get("items", data if isinstance(...)) 不行 ——
    # 默认值判得再对也没用，`data.get` 在 list 上先就抛 AttributeError 了。
    samples = data if isinstance(data, list) else data.get("items", [])
    for s in samples:
        # 时间戳有两个名字，取决于样本从哪来：经过信令服务器落盘的叫 `_at`
        # （signal_server.handle_diag 收下时打的），而页面自己 POST 的原文里
        # 只有 `ts`。合并两边样本时要是没补这一步，后面每个用 _at 的地方都会
        # KeyError —— 报错指向稳定窗口那一行，和真正的原因（时间戳字段名）
        # 隔着十万八千里。
        if "_at" not in s and "ts" in s:
            s["_at"] = s["ts"]
    return samples


def split(samples):
    """按 role 分开。早期样本没有 role 字段，按浏览器看待（只有页面会漏写）。"""
    browser = [s for s in samples if s.get("role", "browser") == "browser"]
    device = [s for s in samples if s.get("role") == "device"]
    browser.sort(key=lambda s: s.get("_at", s.get("ts", 0)))
    device.sort(key=lambda s: s.get("_at", 0))
    return browser, device


def steady(samples, warmup, key="_at"):
    """砍掉开头的启动斜坡。"""
    if not samples:
        return []
    t0 = samples[0].get(key) or 0
    return [s for s in samples if (s.get(key) or 0) - t0 >= warmup]


def live_runs(samples, key="bytesReceived"):
    """把浏览器样本按"统计量到底有没有在动"切成几段，返回 (所有段, 最后一段)。

    /diag 是**累积**的，跨会话不清空 —— 板子重启、浏览器刷新、页面在推流
    中断期间照样每秒上报。而且断流时页面上报的是上一份 lastStats 的**原样
    拷贝**：`bytesReceived` 一动不动，只有时间戳在往前走。

    所以这里**不能按时间戳切**（时间戳一直在动，切不出来），要按统计量本身：

        变小  → 换了一轮会话（新 PeerConnection 从零开始）
        相同  → 重复上报，丢掉

    不切会怎样，是实测过的：整片一起算，真实 20.8fps 被算成 **0.9fps**，
    字节守恒算成 **0.65** —— 分子分母来自两个不同会话、中间还夹着十几分钟
    死时间。三个判据同时"失败"，指向的却是一个根本不存在的故障。
    这正是这个脚本要防的那类谎，只不过这次是脚本自己在说谎。
    """
    runs = [[]]
    prev = None
    for s in samples:
        v = (s.get("video") or {}).get(key)
        if v is None:
            continue
        if prev is not None:
            if v < prev:
                runs.append([])
            elif v == prev:
                continue
        runs[-1].append(s)
        prev = v
    runs = [r for r in runs if r]
    return runs, (runs[-1] if runs else [])


def last_time_run(samples, max_gap, key="_at"):
    """设备侧样本按时间断层切，只取最后一段。

    设备遥测在会话不存在时是**完全不上报**的（不是重复上报），所以它那边
    表现为实实在在的时间断层，按间隔切就够，不需要看统计量。
    """
    if not samples:
        return []
    out = [samples[-1]]
    for s in reversed(samples[:-1]):
        if (out[-1].get(key) or 0) - (s.get(key) or 0) <= max_gap:
            out.append(s)
        else:
            break
    out.reverse()
    return out


FreezeReport = namedtuple(
    "FreezeReport",
    "longest total count source sampled_total sampled_count span")


def sampled_freeze(win, key="framesDecoded"):
    """靠"相邻样本帧增量是否为 0"量冻结，返回 (最长, 合计, 次数)。

    **这个方法有个硬地板，必须记住**：分辨率就是采样间隔。1Hz 采样时看不见
    任何短于约 0.95 秒的停顿 —— 那段时间里只要解出 1 帧，增量就不是 0。

    实测的漏算幅度：同一段 557 秒的 Chrome 窗口，这个方法算出 19 次 / 18.7s，
    而浏览器自己记的账是 30 次 / 47.6s —— **少算 2.5 倍**。所以"最长冻结"这个
    数字在接近采样间隔时是不可信的（真实值只会更大），报结论时要带上这句。

    相邻的零帧间隔并成一次事件：一帧没解出来连续 2 秒是**一次**卡顿，不是两次。
    分开数会把"卡了多久"高估成"卡了几回"。
    """
    seq = []
    for a, b in zip(win, win[1:]):
        dt = (b.get("_at") or 0) - (a.get("_at") or 0)
        if dt <= 0 or dt > 10:
            continue
        d = ((b.get("video") or {}).get(key) or 0) - \
            ((a.get("video") or {}).get(key) or 0)
        seq.append((dt, d))

    longest = total = 0.0
    count = 0
    run = 0.0
    prev_zero = False
    for dt, d in seq:
        if d <= 0:
            run += dt
            total += dt
            if not prev_zero:
                count += 1
            prev_zero = True
            longest = max(longest, run)
        else:
            run = 0.0
            prev_zero = False
    return longest, total, count


def freeze_report(win):
    """冻结的三个数，优先用浏览器自己记的账。

    Chrome 的 inbound-rtp 给 freezeCount / totalFreezesDuration，是按帧计的，
    **不受采样频率限制**，能抓到采样间隔跨过去的短卡顿 —— 有它就该用它。
    Firefox 不实现这两个字段（取回来是 undefined），只能退回采样法。
    Chrome 也没给"单次最长"，最长那一项任何浏览器都只能靠采样法算。

    返回的 sampled_total 是采样法的值，留着是为了能算出漏算倍数 ——
    这个倍数本身就是结论的一部分。
    """
    longest, sampled_total, sampled_count = sampled_freeze(win)
    span = ((win[-1].get("_at") or 0) - (win[0].get("_at") or 0)) if len(win) > 1 else 0.0
    v0 = (win[0].get("video") or {})
    v1 = (win[-1].get("video") or {})
    if v1.get("freezeCount") is not None:
        total = (v1.get("totalFreezesDuration") or 0) - (v0.get("totalFreezesDuration") or 0)
        count = v1["freezeCount"] - (v0.get("freezeCount") or 0)
        return FreezeReport(longest, total, max(0, count), "freezeCount(Chrome)",
                            sampled_total, sampled_count, span)
    return FreezeReport(longest, sampled_total, sampled_count, "帧增量采样",
                        sampled_total, sampled_count, span)


def longest_freeze(win, key="framesDecoded"):
    """窗口内最长的一段"一帧都没解出来"，单位秒。

    平均值会把冻结藏起来：20 秒里冻 8 秒、剩下 12 秒跑满 30fps，平均还有
    18fps，看起来只是"稍微有点低"，实际是画面整整停了 8 秒。

    实测踩到的成因：H.264 是 P 帧参考链，一次突发丢包会打断参考链，解码器
    得等到下一个 IDR 才能恢复 —— `gop=60` 在实测的 20fps 下就是 3 秒。症状
    非常反直觉：**板子侧发送速率平滑得挑不出毛病，浏览器侧却连续几秒零帧**。
    只看平均帧率和字节守恒的话，这两条都过得去，画面却在卡。
    """
    return sampled_freeze(win, key)[0]


def nearest(samples, t, key="_at"):
    """时间上最接近 t 的那条样本。"""
    if not samples:
        return None
    return min(samples, key=lambda s: abs((s.get(key) or 0) - t))


# --------------------------------------------------------------------------- #
# 判据
# --------------------------------------------------------------------------- #
def audio_checks(add, first, last, device, args):
    """阶段二：两路音频各自算，别让视频的绿灯替它们背书。

    四个计数器，方向别搞混 —— 这正是"哪半边没通"最容易看反的地方：

      board   audio_bytes_out / audio_packets_out  板子的麦克风**发**出去
      board   audio_frames_decoded                 板子**收**进来并解出的 opus 帧
      browser audio.inBytesReceived                A 路的落地端
      browser audio.outBytesSent                   浏览器麦克风**发**出去

    其中 board 的 audio_frames_decoded 是四个里最硬的一个：它是 opusdec
    真的吐出了帧，不是"收到了包"。B 路出问题时的症状（板子一个中文报错
    都没有、ICE 也 connected）见 src/pipeline.py 里 attach_audio_return
    的说明。
    """
    a0 = first.get("audio")
    a1 = last.get("audio")
    if not a0 or not a1:
        add("浏览器样本带音频统计", False,
            "样本里没有 audio 字段。页面的 collectStats 要带音频那几项，"
            "否则 A 路收没收到、麦克风有没有真的在送，都无从判断")
        return

    # ---- A 路：板子 → 浏览器 ----
    got = (a1.get("inBytesReceived") or 0) - (a0.get("inBytesReceived") or 0)
    lost = (a1.get("inPacketsLost") or 0) - (a0.get("inPacketsLost") or 0)
    add("浏览器收到板子的音频", got > 0,
        "inbound audio %d 字节（%.1f kbps），窗口内丢包 %d"
        % (got, a1.get("inKbps") or 0, lost))

    # ---- B 路：浏览器 → 板子 ----
    # hasMicTrack 单独判：页面在 getUserMedia 失败时也能建出 PeerConnection，
    # 那一路上行字节永远是 0，而 outBytesSent 的判据只会说"没在送"，
    # 说不清是"没麦克风"还是"有麦克风但没接上"。
    add("页面拿到了麦克风", bool(a1.get("hasMicTrack")),
        "hasMicTrack=%s（页面上的「开始对讲」点了吗）" % a1.get("hasMicTrack"))
    sent = (a1.get("outBytesSent") or 0) - (a0.get("outBytesSent") or 0)
    add("浏览器在往板子送音频", sent > 0,
        "outbound audio %d 字节（%.1f kbps）" % (sent, a1.get("outKbps") or 0))

    # ---- A 路字节守恒：需要板子侧的发送计数 ----
    if not device:
        add("音频字节守恒可计算", False, "没有设备侧样本（role=device）")
        return
    d_start = nearest(device, first["_at"])
    d_end = nearest(device, last["_at"])
    d_audio = (d_end or {}).get("audio_bytes_out", 0) - \
              (d_start or {}).get("audio_bytes_out", 0)
    if d_audio <= 0:
        add("音频字节守恒可计算", False,
            "板子侧音频字节在窗口内没增长（%d）—— 板子没在发音频" % d_audio)
    else:
        ratio = got / d_audio
        # **音频这条的容差必须比视频宽**，不是"数据脏"，是一处**固定的口径差**：
        # 实测（Chrome，32kbps/20ms）板子 audio_bytes_out 是 92.00 字节/包
        # —— 80 字节 Opus 载荷 + 12 字节 RTP 头，正好是理论值；而浏览器
        # inbound-rtp 报的是 80.00 字节/包。80/92 = 0.870，也就是说这条判据的
        # "完美值"本来就是 0.87 而不是 1.0，用 15% 容差去卡它只剩 2 个点余量，
        # 一次正常抖动就能把它打成红的。
        # 视频那边没这个问题（包大，12 字节头淹没在噪声里，实测 0.983）。
        add("音频字节守恒 ≈ 1.0", abs(ratio - 1.0) <= args.audio_tolerance,
            "收到/发出 = %.3f（%d / %d 字节；音频天然偏低约 0.87 —— "
            "浏览器的音频 bytesReceived 只算载荷，板子那侧算整个 RTP 包，"
            "容差 ±%.0f%%）"
            % (ratio, got, d_audio, args.audio_tolerance * 100))

    # ---- B 路落地：板子真的解出来了 ----
    d_frames = (d_end or {}).get("audio_frames_decoded", 0) - \
               (d_start or {}).get("audio_frames_decoded", 0)
    add("板子解出了麦克风的音频帧", d_frames > 0,
        "板端 opusdec 解出 %d 帧（窗口 %.1fs，约 %.0f 帧/秒；Opus 20ms 一帧，"
        "满速是 50）"
        % (d_frames, last["_at"] - first["_at"],
           d_frames / max(1e-6, last["_at"] - first["_at"])))


def run_checks(browser, device, args):
    checks = []

    def add(name, ok, detail=""):
        checks.append(Check(name, ok, detail))

    # ---- 样本量 ----
    add("样本数量充足", len(browser) >= 3,
        "浏览器样本 %d 条，设备样本 %d 条" % (len(browser), len(device)))
    if not browser:
        return checks

    # 先只留最后一段真正在推流的会话，再在里面砍掉启动斜坡。
    # 顺序不能反：先砍斜坡再切段的话，段首的基准点会是上一段会话的样本。
    runs, br = live_runs(browser)
    if not br:
        add("浏览器样本形成连续会话", False,
            "所有样本的 video.bytesReceived 都没增长过，说明一次都没真收到过媒体")
        return checks
    # 说清楚"前面那些段被扔了"，否则看日志的人会把样本总量当成分析量。
    dropped = ("，前 %d 段是历史会话/断流期，已排除" % (len(runs) - 1)) if len(runs) > 1 else ""
    add("浏览器样本形成连续会话", True,
        "共 %d 段，取最后一段：%d 条，跨度 %.1fs%s"
        % (len(runs), len(br), (br[-1]["_at"] - br[0]["_at"]) if len(br) > 1 else 0,
           dropped))
    browser = br
    device = last_time_run(device, args.max_gap)

    win = steady(browser, args.warmup)
    if len(win) < 2:
        add("稳态窗口足够长", False,
            "预热 %.0fs 之后只剩 %d 条样本。让页面多开一会儿，"
            "或用 --warmup 调小" % (args.warmup, len(win)))
        return checks
    add("稳态窗口足够长", True,
        "预热 %.0fs，窗口内 %d 条，跨度 %.1fs"
        % (args.warmup, len(win), (win[-1]["_at"] - win[0]["_at"])))

    first, last = win[0], win[-1]

    # ---- 视频：收到包 ≠ 解出帧 ----
    v0 = (first.get("video") or {})
    v1 = (last.get("video") or {})
    if v0 and v1:
        d_frames = v1.get("framesDecoded", 0) - v0.get("framesDecoded", 0)
        add("framesDecoded 在增长", d_frames > 0,
            "稳态窗口内解出 %d 帧" % d_frames)

        d_bytes = v1.get("bytesReceived", 0) - v0.get("bytesReceived", 0)
        add("视频 bytesReceived 在增长", d_bytes > 0,
            "稳态窗口内收到 %.2f MiB" % (d_bytes / 1048576))

        # 真实帧率：只看稳态窗口
        span = last["_at"] - first["_at"]
        fps = d_frames / span if span > 0 else 0
        add("稳态帧率达标", fps >= args.min_fps,
            "%.1f fps（下限 %.1f）" % (fps, args.min_fps))

        # 平均值单独看不够 —— 见 longest_freeze 的说明。这两条要一起看：
        # 帧率达标但冻结超时，说明链路在**间歇性**断，比稳定的低帧率更糟。
        #
        # 注意"最长冻结"这一项有采样地板：1Hz 采样看不见短于约 0.95 秒的停顿，
        # 所以它接近采样间隔时只能当"至少这么长"，真实值只会更大。要量化到底
        # 漏了多少，看下一项"画面静止时间占比"里两个方法的倍数。
        fr = freeze_report(win)
        add("最长冻结时间在容忍范围内", fr.longest <= args.max_freeze,
            # 两位小数是必要的：一位小数下 2.04 会显示成 "2.0（上限 2.0）"
            # 却判 FAIL，看着像工具的 bug 而不是真超了。
            "窗口内最长 %.2f 秒一帧未解出（上限 %.1f 秒）"
            "【采样法，地板约 1 秒，真实值只会更大】"
            % (fr.longest, args.max_freeze))

        # 上面那条是"最坏一次"，这条是"总共有多久"。少算 2.5 倍的教训说明：
        # 只盯最长的那一次，会把"经常卡但每次都不久"整类问题放过去。
        # 优先用浏览器自己记的 freezeCount（按帧计，没有采样地板）。
        if fr.span > 0:
            pct = 100 * fr.total / fr.span
            if fr.source == "freezeCount(Chrome)":
                detail = ("%.1f%%（%d 次，共 %.1fs / %.0fs）—— 用 Chrome 的 "
                          "freezeCount，无采样地板" % (pct, fr.count, fr.total, fr.span))
            else:
                detail = ("%.1f%%（%d 次零帧区间，共 %.1fs / %.0fs）—— 退路："
                          "帧增量采样。这个浏览器不给 freezeCount（Firefox 就没有），"
                          "短于约 1 秒的卡顿看不见，真实占比只会更高"
                          % (pct, fr.count, fr.total, fr.span))
            add("画面静止时间占比在容忍范围内", pct <= args.max_freeze_pct,
                "%s（上限 %.1f%%）" % (detail, args.max_freeze_pct))
    else:
        add("视频统计存在", False, "样本里没有 video 字段")

    # ---- 候选对 ----
    # 取**最后一个带 selectedPair 的样本**：拆链之后写入的样本这个字段是 null，
    # 挑到它会把一次成功的验证判成失败。
    pairs = [s for s in win if s.get("selectedPair")]
    if not pairs:
        add("存在选中的候选对", False,
            "稳态窗口里没有样本带 selectedPair。"
            "注意必须顺着 transport.selectedCandidatePairId 取，"
            "不能遍历所有候选对去挑 nominated 的那个")
    else:
        p0 = pairs[0]["selectedPair"]
        p1 = pairs[-1]["selectedPair"]
        # 把页面走了哪条路一并打出来：主路是 transport.selectedCandidatePairId，
        # 退路是自己挑的。退路可能挑中 ICE 重启后冻住的旧对，下面那条
        # "字节持续增长"就是拦它的 —— 但得让人一眼看见走的是退路，
        # 不然它就成了静默兜底，把"浏览器没给这条统计"这件事盖过去。
        last_s = pairs[-1]
        src = last_s.get("pairSource") or "未标注"
        # 退路本身不是问题，分两种情况，别一律喊狼来了：
        #   浏览器压根没给 transport 统计（实测观看端是 Firefox 140，
        #   transports 字段就是 null）→ 主路根本不存在，退路是唯一可行的路，
        #   正常的；Chrome 则两种都可能，所以这里不能只按浏览器名判断。
        #   给了 transport 但 selectedCandidatePairId 是空的 → 这才可疑，
        #   说明浏览器知道当前用哪一对却不肯说，要查。
        if src == "transport":
            note = ""
        elif last_s.get("transports") is None:
            note = "（该浏览器不提供 transport 统计，走退路是常态）"
        else:
            note = "  ← transport 统计在，却没给出候选对 id，可疑！"

        def brief(c):
            if not c:
                return "?"
            return "%s/%s %s:%s" % (c.get("type"), c.get("protocol"),
                                    c.get("address"), c.get("port"))

        add("存在选中的候选对", True,
            "%s state=%s nominated=%s  来源=%s%s  本端=%s 对端=%s"
            % (p1.get("id"), p1.get("state"), p1.get("nominated"), src, note,
               brief(last_s.get("pairLocal")), brief(last_s.get("pairRemote"))))
        # 走了中继就直接点出来：字节数和帧率都看不出来，但它能解释突发丢包。
        relaying = any((last_s.get(k) or {}).get("type") == "relay"
                       for k in ("pairLocal", "pairRemote"))
        if relaying:
            add("媒体是直连的（没走 TURN 中继）", False,
                "候选对里有 relay 候选 —— 媒体绕了公网一圈。"
                "同局域网内直连本该选 host 对；绕中继会带来突发丢包，"
                "再被 H.264 的 P 帧参考链放大成数秒冻结")
        grew = (p1.get("bytesReceived") or 0) > (p0.get("bytesReceived") or 0)
        add("候选对字节持续增长", grew,
            "%d -> %d 字节" % (p0.get("bytesReceived") or 0,
                               p1.get("bytesReceived") or 0))

    # ---- 字节守恒：需要板子那侧的计数 ----
    if not device:
        add("字节守恒可计算", False,
            "没有拿到设备侧样本（role=device）。"
            "确认 device_agent.py 在跑，且它 POST 的 /diag 和这里读的是同一个服务器")
    else:
        d_start = nearest(device, first["_at"])
        d_end = nearest(device, last["_at"])
        b_start = first["video"]["bytesReceived"]
        b_end = last["video"]["bytesReceived"]
        d_bytes = (d_end or {}).get("video_bytes_out", 0) - \
                  (d_start or {}).get("video_bytes_out", 0)
        b_bytes = b_end - b_start

        if d_bytes <= 0:
            add("字节守恒可计算", False,
                "设备侧字节在窗口内没有增长（%d）。板子可能没在推流" % d_bytes)
        else:
            ratio = b_bytes / d_bytes
            # 用窗口内的**增量**做比值，而不是各自的总量：两端启动时刻有
            # 先后（板子先开始发，浏览器后开始收），拿总量比会平白引入一个
            # 偏差，窗口越长这个偏差越不该出现。
            ok = abs(ratio - 1.0) <= args.tolerance
            add("字节守恒 ≈ 1.0", ok,
                "收到/发出 = %.3f（%.2f MiB / %.2f MiB，容差 ±%.0f%%）"
                % (ratio, b_bytes / 1048576, d_bytes / 1048576,
                   args.tolerance * 100))

    # ---- 音频（阶段二）----
    if args.audio:
        audio_checks(add, first, last, device, args)
    else:
        add("音频判据未启用", True,
            "加 --audio 才检查音频。阶段二的验证请带上它 —— "
            "视频的判据全都通过、音频一路都不通，是完全可能的")

    # ---- 播放位置：区分"源头停了"和"编不动了" ----
    t0 = first.get("srcTime")
    t1 = last.get("srcTime")
    if t0 is not None and t1 is not None:
        add("浏览器端播放位置在前进", t1 > t0,
            "%.1fs -> %.1fs" % (t0, t1))

    return checks


# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(
        description="检查诊断样本是否满足判据",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url",
                     help="信令服务器根地址，如 http://127.0.0.1:8080 或 "
                          "https://1.2.3.4（会自动补 /diag）")
    src.add_argument("--file", help="直接读一个 JSON 文件")
    p.add_argument("--room", default="demo")
    p.add_argument("--cafile", default=None,
                   help="信令服务器用自签证书时，把那张证书的路径给它")
    p.add_argument("--insecure", action="store_true",
                   help="跳过证书校验（仅用于临时排查，别写进脚本）")
    p.add_argument("--auth", default=None, metavar="USER:PASS",
                   help="HTTP Basic Auth。VPS 外网入口的 /diag 需要；"
                        "局域网板子上的 /diag 不需要")
    p.add_argument("--warmup", type=float, default=DEFAULT_WARMUP,
                   help="丢掉开头这么多秒的启动斜坡")
    p.add_argument("--max-gap", type=float, default=5.0,
                   help="/diag 是累积的，设备侧样本间隔超过这么多秒就当成"
                        "换了一轮会话，只分析最后一段")
    p.add_argument("--audio", action="store_true",
                   help="阶段二：额外检查两路音频。默认不开，因为麦克风要人手点"
                        "页面上的「开始对讲」才会起来 —— 没点的时候音频判据"
                        "必然全红，那是操作问题不是链路问题")
    p.add_argument("--tolerance", type=float, default=0.15,
                   help="视频字节守恒的容差。旧项目实测直连是 1.00 / 0.99")
    p.add_argument("--audio-tolerance", type=float, default=0.20,
                   help="音频字节守恒的容差，必须比 --tolerance 宽。不是数据脏，"
                        "是两边口径不同：实测板子 audio_bytes_out 按 92 字节/包"
                        "（80 字节 Opus 载荷 + 12 字节 RTP 头）算，浏览器的音频 "
                        "inbound-rtp.bytesReceived 报 80 字节/包，所以这条判据的"
                        "「完美值」本来就是 80/92≈0.87。用 15%% 去卡只剩 2 个点"
                        "余量，正常抖一下就红")
    p.add_argument("--min-fps", type=float, default=12.0,
                   help="稳态帧率下限。板子实测的编码上限约 20.4fps"
                        "（720p UYVY，rkisp 吞吐限制 —— 不是 CPU，8 核负载 0.14，"
                        "也不是编码器，MPP 是硬编）。所以这个下限压在明显低于上限"
                        "的位置：它要抓的是「链路真的不行了」，不是「硬件就到这」")
    p.add_argument("--max-freeze", type=float, default=2.0,
                   help="最长容忍多少秒一帧未解出。H.264 的 P 帧参考链被突发"
                        "丢包打断后，要等下一个 IDR 才能恢复，gop 越大冻得越久。"
                        "注意这个指标有采样地板：1Hz 采样看不到短于约 1 秒的停顿，"
                        "所以它测的是「最坏的那一次」，不是「一共卡了多久」")
    p.add_argument("--max-freeze-pct", type=float, default=3.0,
                   help="画面静止的时间允许占窗口的百分之几。默认 3%% 是照着"
                        "「观感上基本流畅」定的。这条比 --max-freeze 更能反映观感："
                        "实测 gop=60 下最长冻结只有 2.0 秒（勉强过 --max-freeze），"
                        "但静止时间占 8.5%%，是这条抓出来的。Chrome 会给权威的"
                        "freezeCount；Firefox 不给，只能退回采样法，而采样法少算"
                        "约 2.5 倍，所以那时这个数是被低估的")
    args = p.parse_args()

    samples = load_samples(args)
    if not samples:
        print("没有样本。页面连上并推流之后再跑这个脚本。", file=sys.stderr)
        return 2

    browser, device = split(samples)
    checks = run_checks(browser, device, args)

    width = max(len(c.name) for c in checks)
    failed = 0
    print()
    for c in checks:
        mark = "PASS" if c.ok else "FAIL"
        if not c.ok:
            failed += 1
        print("  [%s] %-*s  %s" % (mark, width, c.name, c.detail))
    print()
    if failed:
        print("  %d / %d 条判据未通过" % (failed, len(checks)))
        return 1
    print("  全部 %d 条判据通过" % len(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
