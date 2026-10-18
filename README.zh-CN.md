# WebRTC Remote Monitor

在 Orange Pi 5（RK3588）上把摄像头、麦克风和一条遥操作通道送进浏览器：画面、双向语音、
DataChannel 命令台，全部走标准 WebRTC，不装插件、不依赖厂商 SDK。

推流端（`device_agent.py`）**主动连出去**，一个入站 TCP 端口都不开；媒体走
DTLS/SRTP + ICE，局域网直连，跨网时由 coturn 中继兜底。同一份代码既能跑
"局域网最小形态"，也能跑当前的公网部署。

> 英文概览见 [README.md](README.md)；设计细节、线程模型和全部踩坑记录见
> [docs/architecture.md](docs/architecture.md)。这份文件是中文使用说明。

---

## 能做什么

| 功能 | 说明 |
|---|---|
| **720p 实时画面** | MIPI（OV13850）或 USB 摄像头，RK3588 硬件 H.264 编码 |
| **运行期切换摄像头** | 从页面点按钮即可换采集段。**不重新协商、不重建 PeerConnection**，音频不断；实测最长停顿 367 ms（一个 GOP） |
| **双向语音** | 板子麦克风 → 浏览器；浏览器麦克风 → 板子 3.5 mm 输出。两个方向分别计数 |
| **遥操作** | 白名单命令走 DataChannel，每条都有 ACK/NACK；命令同时被写进**视频画面左上角**，文字只能由板子产生 |
| **局域网 / 公网两种部署** | 同一个 `signal_server.py`：板子上跑就是局域网模式，VPS 上跑配 coturn 就是公网模式 |
| **可验证** | 13 / 17 / 22 条验收判据、切换专项 13 条、设备 fd 回收检查、无头对端自测 |

单观看端是**设计约束**，不是遗漏：遥操作场景只需要一个操作者，所以只有一条会话、一条
管线，也没有多路广播和 PiP。

## 拓扑

```
        ┌───────────────────── 公网 VPS（可选）──────────────────────────┐
        │  nginx :443 ──┬── /ws/publish ───────────────→ 127.0.0.1:8443  │
        │               ├── /ws/view /ice.json /diag ──→ 127.0.0.1:8443  │
        │               └── /webrtc /config.json /camera → 127.0.0.1:8444│
        │  signal_server.py :8443          web_server.py :8444           │
        │  coturn :3478（STUN/TURN 兜底）                                │
        └───────────────▲──────────────────────────▲────────────────────┘
                        │ wss（推流）               │ wss（观看）
        ┌───────────────┴──────────────────────────┴─────────────────────┐
        │ 板子                                                           │
        │   device_agent.py  ← 只连出去，零入站 TCP 端口                  │
        │   web_server.py :8443（局域网页面 + 同源代理）                  │
        │   GStreamer: v4l2src → mpph264enc → rtph264pay → webrtcbin     │
        │              alsasrc → opusenc → rtpopuspay ────↗              │
        │              webrtcbin → opusdec → alsasink（回程音频）        │
        └───────────────▲────────────────────────────────────────────────┘
                        │ DTLS/SRTP：直连，或必要时 TURN 中继
                  浏览器（局域网 / 外网）
```

**信令与媒体是两条路。** `signal_server.py` 只负责撮合与下发 TURN 凭证；RTP 不经过它，
除非 ICE 选了中继。局域网浏览器不直连 VPS，而是让板子代理信令 —— 这样浏览器只需要信任
板子一张自签证书。

## 关键设计

### 1. 切换摄像头为什么不需要重新协商

管线拆成**常驻骨架**和**可替换采集段**，切分点在 `h264parse` 之后：

```
骨架（建一次，永不动）                     采集段（每次切换整段换掉）
webrtcbin ← vq ← rtph264pay ←ghost─ v4l2src ! [mppjpegdec] ! mpph264enc ! h264parse
  ↑ 音频 aq ← rtpopuspay
```

`rtph264pay` 留在骨架里，RTP 时间戳基准和 SSRC 就不会重来一遍；两路摄像头又都被强制成
同一份编码 caps。这两个条件同时成立，"切换"才只是换掉采集段，浏览器看到的始终是同一条流。

### 2. 板子没有 3A 时怎么把画面救回来

Orange Pi 5 上没有 Rockchip 的 `rkaiq_3A_server`，AE/AWB 都在那个用户态服务里。于是
**一个根因、三个症状**：黑（增益停在驱动默认值）、绿（AWB 没跑）、乱（增益给多了、
大量像素过曝）。

- `camera.py` 在 ISP self-path 上量亮度，对 sensor 的 `analogue_gain` / `exposure`
  做闭环；
- `whitebalance.py` 用 `appsink → numpy → appsrc` 做灰世界校正，前 15 帧校准后冻结；
  太暗或过曝的校准帧会被拒绝，而不是硬算出一个"自信的错误答案"；
- USB 摄像头自带 AE/AWB，所以对它的 `quality_fix` 明确关闭。

### 3. 判据必须能失败

每条验收判据都对应一个**踩过的谎**：

| 判据 | 防的是什么 |
|---|---|
| 被选中的候选对字节持续增长 | `iceConnectionState=connected` 但实际零媒体 |
| `framesDecoded` 增长，而不只是 `bytesReceived` | 把"RTP 到了"当成"解出了帧" |
| 发送 / 接收字节守恒 ≈ 1.0 | 单向通了却被当成双向通 |
| 只取稳态窗口 | 把启动斜坡算进去，量出假的 5 fps |
| 每次采样都重读 `selectedCandidatePair` | ICE 重启后还拿着冻住的旧候选对 |
| `sent == acked + nacked` | 丢了一条命令，被当成"页面没发" |
| 板端 `teleop_overlay` 跟得上 ACK | ACK 是真的，但视频那道环已经死了 |
| 板端 `teleop_rx` 和页面 `sent` 对得上 | 页面自己发、自己记账、自己判自己过 |

## 实测数据

| 指标 | 数值 | 说明 |
|---|---|---|
| 视频 | 1280×720 H.264，2500 kbps | MIPI `UYVY` 受 ISP 吞吐限制，约 **20.4 fps** |
| 切换停顿 | **367 ms** | `gop=10`；上界是一个 IDR 间隔 |
| MIPI 暗光 | gain 16 → **Y≈9.7**；gain 248 + exposure 1648 → **Y≈78** | 没有 AE，闭环会顶到 sensor 上限 |
| USB 摄像头 | **Y≈226** | 自带 AE/AWB，跳过兜底 |
| 缺 AWB | **G/R=1.71、G/B=1.55** | 灰世界校正；720p 多花约 0.4 核，帧率不变 |
| 麦克风探测 | USB 麦克风 **12.9%** 满量程 vs 浮空 3.5 mm **0.25%** | 在"静默录本底噪声"之前把它拦下来 |
| 验收 | **13 / 17 / 22 条** | 视频 / 加音频 / 加遥操作 |

## 快速开始

### 依赖

Debian/Ubuntu arm64。`mpph264enc` / `mppjpegdec` 来自板子 BSP 的 MPP GStreamer 插件，
不在 Debian 源里：

```bash
sudo apt install python3-gi python3-aiohttp python3-numpy \
    gir1.2-glib-2.0 gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0 \
    v4l-utils alsa-utils gstreamer1.0-tools
```

`numpy` 只有 `--camera-wb` 需要。可 pip 管理的部分见 [requirements.txt](requirements.txt)。

### 局域网（不需要 VPS）

```bash
bash tools/gen_cert.sh          # 自签证书；浏览器要用麦克风就必须有

# 终端 1：信令 + ICE 配置
python3 src/signal_server.py --room demo --token hunter2 --ice-mode none --port 8080

# 终端 2：推流端
python3 src/device_agent.py --signal ws://127.0.0.1:8080/ws/publish \
    --room demo --token hunter2
    # 没有麦克风就加 --no-audio

# 终端 3：页面 + 同源代理（切摄像头的按钮在这里）
python3 src/web_server.py --port 8443 --room demo --signal http://127.0.0.1:8080 \
    --tls-cert certs/board.crt --tls-key certs/board.key
```

浏览器打开 `https://<板子IP>:8443/`。房间号由页面向 `/config.json` 自动获取，
不用手写 `?room=`。第一次打开要接受一次自签证书 —— 这同时也是浏览器麦克风所需的
安全上下文。

> 只启动 `signal_server.py` 也能看画面（它自己会在 `/` 提供页面），但没有
> `web_server.py` 的 `/camera` 代理，页面上不会出现切换摄像头的按钮。

### 公网部署

```bash
# VPS
sudo bash tools/deploy_vps.sh

# 持有本仓库的机器（安装观看页服务 + nginx 路由）
bash tools/deploy_web_vps.sh

# 板子：填好 .env 后启动两个服务
cp .env.example .env && $EDITOR .env
tools/run-agent.sh
tools/run-web.sh
```

`deploy_vps.sh` 生成房间号、推流 token、TURN 密钥和观看端凭证；`deploy_web_vps.sh`
打印外网地址，并给所有观看相关路径加 HTTP Basic Auth。凭证流程、nginx 路由，以及
`/ws/publish` 为什么**不加** Basic Auth，见 [docs/architecture.md](docs/architecture.md)
第二节。

### 常用参数

| 参数 | 作用 |
|---|---|
| `--gop 10` | 切换后最多等一个 IDR；改成 60 会让"画面静止时间占比"超验收上限 |
| `--camera-wb auto` | 板子没有 AWB 时的灰世界校正；校准时镜头对着白墙/灰色表面 |
| `--camera-ctl analogue_gain=176,exposure=1648` | 直接写 sensor 控件，不做闭环 |
| `--ice-policy relay` | 强制走 TURN，用来证明中继真的生效 |
| `--no-audio` | 只推视频，适合没有可用麦克风的板子 |

## 怎么验证

```bash
# 主验收：13 条（视频）、17 条（含音频）、22 条（含遥操作）
python3 tools/analyze_diag.py --url https://<VPS> --room <房间> \
    --cafile certs/vps-self.crt --audio --teleop --teleop-expect-nack

# 切换摄像头：13 条，设备侧 + 浏览器侧
bash tools/check_switch.sh --base https://<板子>:8443

# 会话拆除后设备 fd 有没有还回去（在板子上跑）
bash tools/check_fd.sh --cycles 3

# 遥操作协议单测（宿主机，不需要 GStreamer）
python3 tools/check_teleop.py

# 信令回归：busy 拒绝、viewer 路由与会话隔离、生命周期线程投递（不需要硬件）
python3 tools/test_signal_server.py

# 无头对端：真实 offer/answer、音频和 DataChannel，不需要浏览器
python3 tools/selftest.py --signal https://<VPS> --room <房间> --duration 20
```

诊断数据是**收集**而不是断言：页面 1 Hz 上报 `getStats()` 摘要（含一张 32×18 的亮度
缩略图），板子 1 Hz 上报自己的发送计数，样本都存在信令服务器上，由
`tools/analyze_diag.py` 离线检查。无头运行时没有人眼可看，这是唯一可靠的验收方式。

## 踩过的坑（摘要）

每条都是**静默失败**，表现成别的问题。完整记录见
[docs/architecture.md 第七节](docs/architecture.md)。

| 现象 | 根因 | 怎么定位 |
|---|---|---|
| ICE 显示 connected，零媒体 | 板子镜像缺 `libnss-mdns`，Chrome 把 host candidate 混淆成 `.local` | 看被选中候选对的字节，而不是状态字符串 |
| 板子收不到音频，`pad-added` 不触发且无报错 | webrtcbin 靠对端 SDP 的 `a=ssrc:<数字> cname:<非空>` 路由入站 RTP；GStreamer 当 answerer 时不写这一行 | 对比真实浏览器与 GStreamer 对端的 SDP |
| 第一个观看者正常，后来的一律黑屏 | `rtph264pay` 的 `config-interval` 默认 0，SPS/PPS 只发一次 | 中途加入 + 抓包；改成 `-1` |
| 切换后新采集段一个包都不出，日志干净 | `GhostPad.new()` 默认**非激活**，`add_pad()` 不会激活它 | 最小切换实验；补 `pad.set_active(True)` |
| 每轮会话漏一个设备 fd | 拉过 sample 的 appsink 攥着 v4l2 buffer，buffer pool 攥着设备 fd；pipeline 又被跨 C/Python 的闭包环保住 | `gc.get_referrers` 点名；拆会话时把采集段从管道摘掉 |
| 手动登录后报"分配缓冲失败" | BSP 只在登录 shell 导出 `GST_V4L2_USE_LIBV4L2=1`；它会让 rkisp 多平面节点走 libv4l2 单平面模拟 | 不带变量连跑 20 次全过；设成 `0` 并不能关掉它 |
| 黑 / 绿 / 过曝像三个 bug | 同一个根因：板子上没有 `rkaiq_3A_server` | 追到 sensor 状态，补亮度闭环 + 白平衡桥 |
| 加了 `textoverlay` 画面全黑 | `wait-text` 默认 true，第一次设文字之前一帧都不往下游推 | 显式 `wait-text=false` |

## 目录结构

```
src/
  device_agent.py    推流端：信令、会话生命周期、遥测、本地控制 socket
  session.py         一路会话：pipeline + webrtcbin + DataChannel + 信令
  pipeline.py        GStreamer 管线：常驻骨架 + 可替换采集段
  sources.py         摄像头表：设备节点、原生格式、要不要画质兜底
  camera.py          亮度闭环（板子没有 AE）
  whitebalance.py    灰世界白平衡桥（板子没有 AWB）
  audio.py           ALSA 设备探测与电平检查
  signal_server.py   信令 + /ice.json + 诊断样本（局域网 / VPS 同一份）
  web_server.py      页面服务 + 同源代理（/ws/view、/ice.json、/diag、/camera）
  teleop.py          命令协议：白名单、校验、ACK/NACK、画面文案
  sdpview.py         SDP → 人话（工具共用）
web/
  monitor.html       整个观看页：单文件、无构建步骤
tools/               部署、验收判据、协议测试、硬件排查
docs/architecture.md 深潜水：拓扑、线程模型、静默失败记录
```

## 已知限制

- **同时只接受一个观看端**。第二个会被明确拒绝；多观看端需要重新设计广播/SFU。
- **单次 offer、不重协商**。开启浏览器麦克风需要重连页面 —— 音轨必须在第一次 offer
  之前加进去。
- **页面不会自动重连**。板子的 `web_server` 重启后需要刷新。
- **管线骨架不会立刻回收**。设备 fd 已经不漏（有 `check_fd.sh` 判据），但跨 C/Python
  的闭包环让骨架在会话之间继续活着（每轮几百 KB）。见架构文档第十一节。
- **MIPI 画质受 sensor 限制**。暗房间里闭环也只能到 Y≈78，真正解决只能加光或换 sensor。

## 许可

MIT — 见 [LICENSE](LICENSE)。
