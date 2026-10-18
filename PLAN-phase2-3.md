# 阶段二 / 阶段三 实施计划

阶段一（视频）已跑通。这份文件是二、三阶段的实现计划。

- **阶段二**：双向语音 —— 板子耳机能听见浏览器，浏览器能听见板子
- **阶段三**：遥操作 —— 浏览器打字 → 板子"执行" → **画面里出现这行字** + 回执

两步各自独立可跑、可演示、可提交，和阶段一的划分方式一致。

---

## 一、起点：阶段一留下了什么

先说清楚现有接口，二、三阶段是在这上面加，不是重写。

| 文件 | 现状 | 二/三阶段要动的地方 |
|---|---|---|
| `src/pipeline.py` | 只有视频分支，接 `sink_0`。`_raw_caps()` 已抽出，注释里写明"音频接 sink_1" | 加音频分支 + `pad-added` 回程接线 + `textoverlay` |
| `src/session.py` | 无 DataChannel、无音频。已有 `_send_threadsafe()` 跨线程投递模式 | 加 `OnDataChannel` / `create-data-channel` / 回程 pad |
| `src/web_server.py` | **`--tls-cert/--tls-key` 已经写好了，只是从没用过**（当前跑在 8080 明文） | 只差一张证书和启动脚本 |
| `web/monitor.html` | 只有画面 + getStats + diag 上报；`<video muted autoplay>` | 加麦克风、加命令台、去掉 muted |
| `tools/analyze_diag.py` | 12 条判据 | 加音频判据、加遥操作判据 |
| `certs/` | **空目录** | 放自签证书 |
| `docs/` | `architecture.md` 已补（原始需求第 3 条） | 阶段三结束后追加遥操作那一节 |

**信令通路不用改。** 浏览器 → `/ws/view` → 板子 → VPS，已经是双向透传，
二、三阶段新增的都是媒体面和 DataChannel 面的事。

---

## 二、今天实测出来的两件事（会改设计，先看这个）

### 1. 插孔里那副耳机**没有麦克风**

```
Headphone Jack     = on      ← 耳机插着
Headset Mic Jack   = off     ← 但麦克风那一极没检测到
Headset Mic Switch = off
```

把 `Headset Mic Switch` 开/关各录 2 秒，采集电平**没有区别**（rms 51.7 vs 50.0）。

→ 结论：插的是**三极立体声耳机**，ADC 输入是浮空的，录到的是本底噪声。
`arecord` 能跑通、有波形、rms 两百多，**看起来完全正常** —— 这是最容易
"调半天以为是 WebRTC 的问题"的一类坑，所以第一步就得把它排掉。

板端采集有两条路，**都留着，做成参数**：

| 方案 | 设备 | 实测 | 代价 |
|---|---|---|---|
| (a) 换**四极耳麦**（TRRS 带麦） | `hw:2,0` | —— | 要买/找一副 |
| (b) 用 **USB 摄像头自带麦克风** | `hw:3,0` | 单声道，8000~48000Hz，48k 下 peak=24422 / rms=5164，信号很实 | 无，现在就能用 |

### 2. 播放开关也是关的

```
hp switch  = off     ← 耳机功放
spk switch = off     ← 喇叭功放
```

两个都是 off。**不开 `hp switch` 就没有声音出来**，和 `Headset Mic Switch` 是同一类坑。
播放设备本身是有的：`card 2 ES8323 device 0`，`aplay -l` 看得到。

> 两个 switch 是运行期 mixer 状态。要确认重启后是否复位；会复位就写进启动脚本，
> 别指望手工 `amixer` 设一次就永久生效。

---

## 三、阶段二：双向语音

### 两个方向，别搞混

| 方向 | 谁在采集 | 需要安全上下文吗 |
|---|---|---|
| **A. 板子 → 浏览器**（你听板子） | 板子的 `alsasrc` | **不需要**，浏览器只收 |
| **B. 浏览器 → 板子**（板子听你） | **浏览器的 `getUserMedia`** | **需要 https** ← 唯一的卡点 |

### 板端管线

A 路，接 `sink_1`：

```
alsasrc device=<mic> !
  audio/x-raw,format=S16LE,rate=48000,channels=1 !
  audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 !
  opusenc bitrate=32000 ! rtpopuspay pt=111 !
  application/x-rtp,media=audio,encoding-name=OPUS,payload=111,clock-rate=48000 !
  queue name=aq  →  webrtcbin.sink_1
```

B 路，`pad-added` 里接：

```
webrtcbin.src_%u → queue name=rq ! rtpopusdepay ! opusdec !
  audioconvert ! audioresample ! alsasink device=hw:2,0
```

音频 m-line **会自动协商成 `sendrecv`**（旧项目在同板实测），接上 `sink_1` 即可，
**不需要** `add-transceiver`。

### 麦克风选谁：自动探测 + 可覆盖

默认自动判断，判不出来才报错。理由就是上面那两条实测 —— 静默跑在一个浮空输入上
是最坏的默认值：

```
--mic-device 不给时：
  Headset Mic Jack == on        → hw:2,0（四极耳麦，符合原始需求）
  否则找 USB 声卡的 capture 节点 → hw:3,0（实测可用）
  都没有                        → 明确报错，别静默录噪声
```

`--mic-device` / `--spk-device` 都要能显式覆盖，方便现场换设备。

### 安全上下文（TLS）

`web_server.py` 的 TLS 支持已经写完了，**只差一张证书**：

- `tools/gen_cert.sh` —— 自签 IP 证书，幂等（已存在就跳过）
  ```bash
  openssl req -x509 -newkey rsa:2048 -nodes -days 397 \
    -keyout certs/board.key -out certs/board.crt \
    -subj "/CN=192.168.2.187" \
    -addext "subjectAltName=IP:192.168.2.187,IP:127.0.0.1,DNS:localhost" \
    -addext "extendedKeyUsage=serverAuth"
  ```
  `-days 397` 压在 Chrome 的 398 天上限以下；SAN 三个名字都要有。
- `tools/run-web.sh` —— 带证书启动，**端口换 8443**（8080 保留明文，阶段一演示不受影响）

**连带效应已经处理好了，不用改代码**：页面里的信令地址是
`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/view`，
一并变 wss，不会撞混合内容拦截。板子那边的 `--tls-ca` 是 **VPS 的**证书，
和板子这张自签证书是两回事，互不影响。

浏览器第一次要接受一次自签证书（Chrome/Firefox 都会拦）。点过之后就是合法安全上下文。

### 页面要改的

1. **`<video>` 去掉 `muted`** —— 留着就永远听不见。
2. **加「开始对讲」按钮**。自动播放策略要求用户手势，所以：
   - 画面**照旧静音自动播**（演示时一打开就有图，不等点击）
   - 按钮做两件事：`getUserMedia({audio:{echoCancellation:true}})` + `video.muted = false`
3. **顺序不能错**（旧项目实测踩过，写错会协商成 recvonly = **单向半通**）：
   ```js
   const mic = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true}});
   pc.addTrack(mic.getAudioTracks()[0], mic);   // 必须在 setRemoteDescription 之前
   tx.direction = "sendrecv";
   await pc.setRemoteDescription({type:"offer", sdp});
   ```
4. **每次改页面都要动 `PAGE_VERSION`**（现在是 `2026-10-13.5`）。这个字段是踩过坑加的：
   改了页面、用户以为刷了、其实浏览器吃了缓存，样本里新字段是空的 —— 分不清是
   没刷、缓存、还是字段名写错。

### 三个必踩的坑

1. **`pad-added` 的 caps 可能是 NULL。** 按 caps 过滤后直接 `return` 会**静默丢掉整路音频** ——
   链路全绿，就是没声音。做法：caps 为 NULL 就挂一次性 BUFFER 探针，第一帧上读 caps 再决定。
2. **视频的 src pad 也会触发同一个 `pad-added`。** 阶段一板子只发不收，所以目前根本没有 src pad，
   这个坑要等阶段二才会冒出来。必须用 caps 区分，不能"来了就接"。
3. **重启推流端不要写 `pkill -f device_agent`** —— 同一条命令行里含这个字面量会把执行它的
   shell 一起杀掉。用 `pkill -f '[d]evice_agent'`。（`tools/gop_sweep.sh` 里已经是这个写法。）

### 判据（加进 `analyze_diag.py`）

现有的 12 条一条不动，加 4 条：

| 新判据 | 防的是什么 |
|---|---|
| 浏览器 `inbound-rtp kind=audio` 字节/帧增长 | 视频通了**不代表**音频通了 |
| 浏览器 `outbound-rtp kind=audio` 字节增长 | **麦克风真的在往板子送** —— 这是最易单向的一段 |
| 板端 `opusdec` 解出帧数增长 | "浏览器麦克风到了板子"的硬证据，不是靠推断 |
| 音频字节守恒 ≈ 1.0 | 复用已有的 `video_packets_out` 那套探针口径 |

### 演示怎么做

板子耳机里放一段声音（或对着 USB 麦克风说话）→ 浏览器里能听见；
浏览器对着笔记本麦克风说话 → 板子耳机里能听见。**两个方向都要当场演**，
只演一个方向说明不了"双向"。

---

## 四、阶段三：遥操作

### 目标

浏览器打字 → 板子"执行" → **画面里出现这行字** + DataChannel 回执。
原始约束是"只需要做文字命令演示，不需要实际操作"，所以执行是模拟的，
但**结果是看得见的**。

### 建链顺序（这里错一步就得重来）

- `create-data-channel` **必须在 pipeline 进 PLAYING 之后**。早了会**静默返回 None**，
  只在日志里留一行 assertion failed —— 不报错，就是没有通道。
- 放在 `_on_negotiation_needed` 里、紧挨 `create-offer` 之前，加闸门防重复触发：
  ```python
  if self.dc is None:
      self.dc = self.webrtcbin.emit("create-data-channel", "teleop", None)
      teleop.connect_message(self.dc, self._on_dc_message)
  self.webrtcbin.emit("create-offer", None, promise)
  ```
- **信号名不是 `on-message`**（2026-10-18 实测）：GStreamer 1.20.3 的
  `libgstwebrtc-1.0.so.0` 里只有 `on-message-string` / `on-message-data` /
  `on-open` / `on-close` / `on-error` / `on-buffered-amount-low`。所以用
  `teleop.connect_message()` 按 string → data → message 顺序尝试，并把实际连上的
  名字打进日志。写成 `connect("on-message", ...)` 只会在 stderr 留一行 "no signal"，
  然后命令永远收不到。
- **不能在消息回调里再 `create-data-channel`** —— webrtcbin 持同一把锁，会**死锁**。
- 浏览器侧必须用 `pc.ondatachannel`（通道是**板子**建的），**且要在 `setRemoteDescription`
  之前**挂好；挂晚了第一个通道就丢了。
- **只发一次 offer，全程不重协商。** 这是整个简化方案成立的关键。

### 命令协议

```jsonc
// 浏览器 → 板子
{"t":"cmd", "seq":7, "cmd":"FORWARD", "arg":12, "ts":1728...}
// 板子 → 浏览器
{"t":"ack", "seq":7, "cmd":"FORWARD", "ts":..., "ms":3.2}
```

白名单：`FORWARD` / `BACK` / `LEFT` / `RIGHT` / `STOP` / `SPEED`。
不在白名单里的一律拒绝并回 `{"t":"nack", ...}`，不静默丢弃。

### 可视化：为什么必须有

命令是模拟执行的，所以**必须让结果落在画面上**，否则"命令走通了"只是一句自述。
收到命令后把 `textoverlay` 的 text 改成：

```
CMD #7 FORWARD 12  14:32:05.1
```

（`序号 命令 arg 时刻`，时刻是 `HH:MM:SS.s`，纯 ASCII —— 板子上没有中文字体。）

2 秒后清空。这段文字**只能由板子产生**，所以它在画面里出现，同时证明了两件事：
命令走通了 DataChannel，以及视频链路是活的。带序号和时间戳是为了让旧帧伪装不成新执行。

**放在管线的哪儿**：插在 `pipeline.py` 的 `tail` 最前面（`mpph264enc` 之前），
这样**开不开 `--camera-wb` 走的是同一条路**：

```
不开 wb:  v4l2src ! caps ! textoverlay ! mpph264enc ! ...
开  wb:  ... appsrc ! caps ! textoverlay ! mpph264enc ! ...
```

`textoverlay` 直接吃 UYVY（旧项目实测），不需要 `videoconvert`。
**但"接在 appsrc 之后"是新组合，阶段一没有 textoverlay** —— 这条要单独验：
跑起来先确认 caps 协商通过，且桥的帧率不掉。

改 text 不打断码流这件事旧项目实测过（改 3 次，稳定 30fps 无 error）。

### 验证怎么搭：复用现成的，不引入新基础设施

宿主机（WSL）上**没有** headless 浏览器（无 chrome / chromium / playwright / node），
`tools/gop_sweep.sh` 用的就是"人开着浏览器 + 脚本轮询 `/diag`"这个模式。阶段三沿用：

- 页面把遥操作统计塞进**已有的 1Hz diag 样本**里：
  `{sent, acked, p50Rtt, p95Rtt, lastAckSeq, lastCmd}`
- `analyze_diag.py` 加判据断言全部 ACK 且 p95 RTT < 300ms

**加分项（已实现）**：`tools/selftest.py` —— 无头 GStreamer 对端（webrtcbin 应答，
靠 `on-data-channel` 收板子建的通道），发 N 条命令读 ACK。好处是**没人开浏览器时
也能自动跑**，适合放进 CI 或提交前的自检。它顺带把"GStreamer 当 answerer 会不会
答 SCTP"这件事实测掉了（会；而且通道交给回调时已经是 open）。

真正开浏览器那条路，WSL 里也可以调 Windows 的 Chrome：
`/mnt/c/Program Files/Google/Chrome/Application/chrome.exe --headless=new
--dump-dom --virtual-time-budget=60000 <url>`。注意 `--virtual-time-budget` 会让
页面里的 `performance.now()` 和 `getStats()` 时间一起"快进"，测出来的 RTT / 帧率
不可当真（实测 p95 879ms，而板端处理耗时只有 0.2ms）—— 它只适合验"流程走通"，
不适合验性能。用这个方法还需要把探针脚本附到页面副本上，跑完把副本删掉。

### 判据

| 新判据 | 防的是什么 |
|---|---|
| 每条命令都有回执（`sent == acked + nacked`，无缺号） | 丢一条就是丢一个操作 |
| 回执与命令一一对应（无陌生 seq、无回显不一致） | 配对逻辑坏了，链路却是通的 —— 最容易看成"延迟高" |
| p95 RTT < 300ms | 遥操作的可用性底线 |
| 白名单外的命令被拒且回 nack | 别把"静默忽略"当成"安全" |
| 板端 `teleop_rx` / `teleop_ack` 与页面一致 | 页面自己记的数，页面自己也能全对 |
| 板端 `teleop_overlay` 跟得上 ACK | ACK 是真的、画面那道环是假的 |
| 人工：画面里出现 `CMD #N ...` | 唯一能证明"命令真的生效了"的证据 |

---

## 五、实施顺序

每一步都能单独跑、单独提交。

**阶段二**
1. `tools/check_audio.sh` —— 先验硬件：jack 状态、`hp switch`、两路各录 2 秒报 RMS。
   **先跑这个**，它决定后面用哪个麦克风。
2. 板端 A 路（`alsasrc` → `sink_1`）→ 浏览器能听见板子。**这一步不需要 TLS。**
3. `tools/gen_cert.sh` + `tools/run-web.sh` → 页面走 https。
4. 页面加「开始对讲」按钮 + `getUserMedia` → B 路。
5. 板端 B 路（`pad-added` → `alsasink`）。
6. 加音频判据，跑 `analyze_diag.py`。

**阶段三**（全部完成）
7. ✅ `src/teleop.py` + `tools/check_teleop.py` —— 白名单、校验、ACK/NACK、overlay 文案，宿主机单测。
8. ✅ `pipeline.py` 插 `textoverlay` + `tools/check_textoverlay.py`（三种采集组合实测帧率）；
   `session.py` 建 DataChannel + `tools/check_offer.py --expect-datachannel`（SCTP spike）。
9. ✅ 页面命令台 + 回执统计 + teleop diag 块；`tools/selftest.py` 无头对端收发命令。
10. ✅ `analyze_diag.py --teleop` 共 22 条判据。

---

## 六、明确不做的（和阶段一一样的取舍风格）

- **`webrtcdsp` / 回声消除**：耳机不外放，板端声学回声很弱，主要回声在浏览器侧，
  由 `echoCancellation` 处理。**改成外放音箱时必须回来加**，届时写进 `docs/`。
- **多观看端**：遥操作场景不该有两个操作端同时指挥一台设备。单端是设计，不是偷懒。
- **真实电机控制**：原始约束明确说只要文字命令演示。
- **重传（RTX）**：阶段一已定案不可能 —— webrtcbin 1.20.3 的 offer 里没有 `a=rtx`/`apt=` 行，
  没有独立 RTX 载荷类型，NACK 用不了。不是配置问题。

---

## 七、遗留风险

| 风险 | 说明 |
|---|---|
| **画面还是暗** | 阶段一遗留的硬件天花板：MIPI 这条路增益+曝光双顶格才 Y≈78（同房间 USB 摄像头是 226）。音视频都通了画面也不会变亮，只能加光或换摄像头。**演示时先解释这句**，否则会被当成链路问题。 |
| **麦克风没插** | 3.5mm 口上现在是三极耳机。不换四极耳麦就得用 USB 麦克风 —— 见第二节。 |
| `hp switch` 复位 | mixer 状态若重启复位，启动脚本里要带上。 |
| ~~`textoverlay` + `appsrc` 是新组合~~ | 已用 `tools/check_textoverlay.py` 在板子上验过 mipi（±wb）与 usb 三种组合：caps 协商通过，改文字不打断码流。 |
| gop 还没定 | 现在靠命令行传 `--gop 10`，`tools/run-agent.sh` 里没有。裸跑会退回默认 60。**这个值要拍板**。 |
| 帧率数字对不上 | 同样 gop=10，判据测到 19.7fps，画质修完的健康检查测到 29.3fps。**没有结论**，别当事实用。 |

---

## 八、交付物清单

- [x] `tools/check_audio.sh` + `tools/gen_cert.sh`
- [x] `tools/run-web.sh`（带 TLS 启动观看页服务）+ `tools/run-agent.sh`
- [x] `src/teleop.py` + `tools/check_teleop.py`（协议单测，宿主机可跑）
- [x] `src/pipeline.py` 音频分支 + `textoverlay`（+ `tools/check_textoverlay.py`）
- [x] `src/session.py` 音频回程 + DataChannel（+ `check_offer.py --expect-datachannel`）
- [x] `web/monitor.html` 对讲 + 命令台（`PAGE_VERSION` = `2026-10-18.1`）
- [x] `tools/analyze_diag.py` 新增判据（`--teleop`）
- [x] `tools/selftest.py`（无头对端，收发命令 + `--auth`）
- [x] **`docs/architecture.md`** —— 原始需求第 3 条，阶段三一节已补（第五节 + 第六节线程模型不变）
