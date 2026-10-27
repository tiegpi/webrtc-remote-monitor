# 架构与流程

> 作品集入口见 [README.md](../README.md)（英文概览）与
> [README.zh-CN.md](../README.zh-CN.md)（中文快速上手）。这份文档是深潜版。

这份文档回答三个问题：**东西是怎么搭起来的**、**为什么这么搭**、**怎么证明它真的在工作**。

代码里的注释写的是"这一行为什么长这样"，这份文档写的是"这台机器为什么长这样"。
两者有重叠，但视角不同：遇到具体实现细节，注释是权威；遇到模块边界和取舍，这份是。

---

## 一、这是什么

Orange Pi 5（RK3588）上接了一个 MIPI 摄像头和一个 USB 摄像头，浏览器可以：

- 看到板子上的实时画面（硬件编码的 H.264，720p）
- 听到板子上的声音，也能对着板子说话（双向语音）
- 在页面上切换用哪个摄像头
- 往输入框里打一条命令，板子"执行"后**画面左上角出现这行字**，同时收到回执

三个阶段刻意做成**各自独立可跑、可演示、可提交**：阶段一只有视频，阶段二加上语音，
阶段三加上遥操作。每一步都能单独验收，不必等下一步。

| 阶段 | 内容 | 状态 |
|---|---|---|
| 一 | 视频：摄像头 → H.264 → WebRTC → 浏览器 | 已交付 |
| 二 | 语音：双向（板子麦克风 → 浏览器；浏览器麦克风 → 板子扬声器） | 已交付 |
| 二·补 | 双摄像头 + 页面切换 | 已交付 |
| 三 | 遥操作：浏览器打字 → 板子"执行"→ **画面里出现这行字** + 回执 | 已交付 |

---

## 二、部署拓扑

系统跑在两台机器上。**分辨"哪个进程在哪台机器上"是读懂这套代码的第一步**，因为
几个看起来该在服务器的职责其实在板子上。

```
        ┌─ 公网 VPS（<VPS_IP>）──────────────────────────────┐
        │  nginx :443         外网唯一入口（TLS，自签证书）     │
        │    /ws/publish          → 127.0.0.1:8443（token）  │
        │    /ws/view /diag /ice.json → 8443（Basic Auth）   │
        │    /webrtc /config.json /camera → 8444（Basic Auth）│
        │    /                    → 404（不暴露入口）         │
        │  signal_server.py :8443  撮合（只监听回环）          │
        │  web_server.py    :8444  外网观看页（只监听回环）     │
        │  coturn  :3478          STUN/TURN（兜底）           │
        └────────────────────────────────────────────────────┘
                  ▲                    ▲                 ▲
           wss /ws/publish      wss /ws/view       https://<VPS_IP>/webrtc
                  │                    │                 │
        ┌─ 板子（<BOARD_IP>）───────┐ │                 │
        │  device_agent.py  ←───────┼─┘  主动连出去       │
        │  web_server.py    ←───────┘                    │
        │      ▲                                          │
        └──────┼──────────────────────────────────────────┘
               │ http / ws（同源，局域网）     外网浏览器 ──┘
           局域网浏览器 ─── RTP 直连 ───> 板子
           外网浏览器   ─── RTP（直连或 TURN 中继）───> 板子
```

两条观看入口**并存**，页面是同一份 `monitor.html`：

| 入口 | 页面来源 | 适合 | 鉴权 |
|---|---|---|---|
| `https://<板子IP>:8443/` | 板子的 `web_server.py` | 局域网，不依赖 VPS 也能看 | 无（局域网） |
| `https://<VPS_IP>/webrtc` | VPS 的 `web_server.py`（nginx 之后） | 外网 / 跨网 | HTTP Basic Auth |

信令在两种情况下都走 VPS 的 8443；媒体始终是浏览器与板子之间的 ICE 路径，
**不经过任何一个 `web_server.py`**。

### 外网访问控制

VPS 的 IP 是公开的，"路径不好猜"不构成权限。因此 nginx 在 443 上对**所有观看
相关路径**要求 HTTP Basic Auth（htpasswd 在 `/etc/nginx/webrtc-view.htpasswd`）：

- 受保护：`/webrtc`、`/monitor.html`、`/config.json`（房间号）、`/ice.json`
  （TURN 凭证）、`/diag`、`/diag/clear`、`/camera`、`/ws/view`
- **不受保护**：`/ws/publish`。推流端只认 `signal_server.py` 的 token；不套
  Basic Auth 是刻意的——观看密码和推流凭证分开，改密码不会打断推流。
- 根路径 `/` 直接 404，不再 302；其他未列出的路径也一律 404。
- `/webrtc/` 仍 301 回 `/webrtc`（nginx 的 `return` 在鉴权之前执行，但它只回一个
  Location，目标仍然要登录）。

浏览器登录页面后会把凭证缓存到该 origin，同源的 `fetch` 和 WebSocket 握手都会
自动带上，页面代码不需要改。实测（CDP 驱动 Chrome，走真实 401 挑战）：
页面、`/config.json`、`/ice.json`、`/ws/view` 全部通过，视频正常出帧。

凭证由 `tools/deploy_web_vps.sh` 首次随机生成，之后复用；明文只存在
`/root/webrtc-view.credentials`（600），htpasswd 是 apr1 哈希（640 root:www-data）。

**换密码用 `tools/set_view_password.sh`**（推荐，不重部署、不打断推流）：

```bash
bash tools/set_view_password.sh '新密码'        # 用户名默认 viewer
VIEW_USER=gtie bash tools/set_view_password.sh '新密码'
```

它一次做完四件事：VPS htpasswd + 明文凭证文件 + `nginx -t`/reload、板子 `.env`
的 `SIGNAL_AUTH`、重启板子两个进程，最后自检「未登录 401 / 登录 200 / 板子
`/ice.json` 有 TURN / agent 日志拿到 ICE 配置」。密码限制为
`[A-Za-z0-9._-]`（板子 `.env` 会被 bash source，其他字符会弄坏它）。
也可以走完整部署：`VIEW_PASS='新密码' bash tools/deploy_web_vps.sh`，但它会重启
信令服务，而且**不会自动改板子**。

**板子侧也要带凭证**：局域网的 `web_server.py` 会替浏览器去 VPS 取
`/ice.json`、`/diag`，并代理 `/ws/view`；`device_agent.py` 也会取
`/ice.json` 和 POST `/diag`。这两条上游请求现在都在 auth_basic 后面，所以板子
`.env` 里必须有 `SIGNAL_AUTH=user:pass`，由 `tools/run-web.sh` /
`tools/run-agent.sh` 传成 `--signal-auth`。局域网浏览器本身不需要登录板子；
凭证只在板子和 VPS 之间使用。推流 WebSocket 仍然只认 `TOKEN`，不要给它加
Basic Auth。

几条通路要分开看，它们的路径完全不同：

| 通路 | 路径 | 说明 |
|---|---|---|
| **媒体（RTP）** | 浏览器 ⇄ 板子，**直连** | ICE 选出来的路径。局域网时就是 host candidate 直连；跨网时走 TURN 中继，那时候 RTP 才经过 VPS |
| **信令（局域网页）** | 浏览器 ⇄ 板子 ⇄ VPS | 板子只做透传。浏览器不直连 VPS，省掉在浏览器里接受自签证书 |
| **信令（外网页）** | 浏览器 ⇄ VPS（443 → 8443） | 页面本身就来自 VPS，同源；由 nginx 终结 TLS |
| **控制（局域网页）** | 浏览器 ⇄ 板子（本地 unix socket） | **不经过 VPS**，见第五节 |
| **控制（外网页）** | — | VPS 上没有 `device_agent` 的 unix socket，`/camera` 明确返回 502，页面不显示切换按钮 |

### VPS 自签证书（`vps-self.crt`）

`certs/vps-self.crt` 是 VPS 自签名证书的公钥副本，仓库里没有生成它的脚本。

- **源文件**：VPS 上的 `/etc/nginx/ssl/self.crt`，由 nginx 在 443 上使用；私钥是同目录的
  `self.key`（`root:root 600`），只留在 VPS 上。
- **用途**：板子和验收工具通过 `--tls-ca` / `--cafile` 把它当信任锚。
- **属性**：自签（`CN=<VPS_IP>`）、RSA 2048、10 年有效期、`CA:TRUE`、SAN 里是 IP。
- **轮换**：换 IP 或私钥时重新签一张，把新的 `.crt` 手动放到每台机器的 `certs/` 下。

### 为什么信令要绕板子一手

因为**证书**，而且这是个静默失败：浏览器要直连 VPS，就得信任 VPS 那张自签证书，
而 `fetch()` 拿到不受信任的证书会直接 reject、`WebSocket` 连个提示都没有 ——
地址栏那种"点继续前往"的交互在 XHR/WS 上根本不存在。

让板子去连 VPS（板子已经用 `--tls-ca` 明确信任了那张证书），浏览器这一侧就只剩
板子一个来源。阶段二给板子加 HTTPS 时，也只需要接受板子一张证书，
**安全上下文和信令一并解决**，不用在两台机器上各点一次。

### 为什么两边各放一份页面服务

最初只有板子提供页面；外网观看的需求出现后，VPS 上也放了同一份
`web_server.py`（`tools/deploy_web_vps.sh` 部署到 `/opt/webrtc-web-server`，
**平铺**放 `web_server.py` + `monitor.html`，不再有 `src/`、`web/` 子目录）。
平铺后静态兜底路由只放行 html/css/js/图片/字体后缀，`GET /web_server.py` 明确 404 ——
否则源码会被当成静态文件原样下载（实测踩过）。部署自检里有一条专门卡这个。
两份页面服务互不依赖：

VPS 上的信令只有**一个**实例：`/opt/webrtc-signal/signal_server.py`，源码就是本仓库的
`src/signal_server.py`，监听 `127.0.0.1:8443`。早期 camera-pipeline 那版 8443 实例
（带 `/call`、没有 `/diag`）已经退掉；为了绕开它而临时起的 `9443` 也一并消失。

- **局域网入口**：页面和切摄像头的控制通道天然同源；VPS 挂了照样能看画面、
  能切摄像头。
- **外网入口**：页面来自 VPS，同样的同源关系；TLS 由 nginx 在 443 终结，
  浏览器需要接受一次 VPS 的自签证书，再过一次 Basic Auth。`web_server.py`
  自己只监听回环（`127.0.0.1:8444`），WAN 上没有多暴露一个端口。
- **同一个房间**：两个入口都指向信令服务器上同一个 `ROOM`。板子同时只接受
  一个观看端，所以局域网和外网**不能同时看**，后接入的那一路会被明确拒绝。
  要并行得给信令服务器加多观看端支持，超出当前的遥操作场景。

为什么不干脆让 VPS 反向代理板子的页面：板子在 NAT 后面，VPS 根本够不着它。
外网观看能成立的前提正是板子主动连出去（推流）和 TURN 兜底（媒体）。

---

## 三、模块地图

```
src/
  device_agent.py   板子侧发布端：连信令、收观看端、建会话、控制端点
  session.py        一路会话：pipeline + webrtcbin + 信令（一个观看端 = 一个）
  pipeline.py       GStreamer 管线：骨架 + 可替换的采集段
  sources.py        摄像头源表：这块板子上有哪几路、各自要什么
  camera.py         画质兜底·亮度：板子没有 3A，曝光自己闭环
  whitebalance.py   画质兜底·白平衡：同一个根因的另一半
  audio.py          音频设备探测与选择
  web_server.py     页面服务 + 同源代理（/ws/view、/ice.json、/diag、/camera）
  signal_server.py  信令服务器（局域网跑在板子上，公网跑在 VPS 上，同一个文件）
  sdpview.py        把 SDP 读成人话（板子和工具共用）
  teleop.py         遥操作协议：白名单、校验、ACK/NACK、叠加文案（纯标准库）
  detect.py         人像检测：RKNN 推理线程 + 纯 numpy 后处理（无 cv2）

web/
  monitor.html      整个观看页：一个文件，没有构建步骤

tools/
  service.sh                  板子服务管理：start / stop / restart / status / logs
  service_vps.sh              在开发机上管理 VPS 的 systemd 服务
  run-agent.sh run-web.sh     前台启动（调试用）
  gen_cert.sh deploy_vps.sh   板子证书 / 部署（VPS 那张自签证书是手工签的，见第二节）
  deploy_web_vps.sh           VPS 上的观看页服务 + nginx 路由
  analyze_diag.py             **主验收**：13 条（视频）、17 条（含音频）或 22 条（含遥操作）判据
  check_switch.sh             切换摄像头那组 13 条判据
  check_fd.sh                 会话拆除后设备 fd 有没有还回去
  check_teleop.py             遥操作协议单测（宿主机，无 GStreamer）
  check_detect.py             检测后处理单测（宿主机）；--model 时跑一次真实推理
  check_detect.sh             /detect 开关的端到端判据（帧数涨/停、错误数）
  check_textoverlay.py        textoverlay 三种采集组合的帧率实测（板子）
  check_audio.sh check_offer.py selftest.py turn_check.py freeze_stats.py …
                             单项排查工具
```

---

## 四、媒体管线

### 阶段一 / 二的形态

```
摄像头 ──> [mppjpegdec] ──> mpph264enc ──> rtph264pay ──┐
                                                        ├──> webrtcbin ──> 浏览器
麦克风 ──> opusenc ──> rtpopuspay ───────────────────────┘
```

反方向（浏览器 → 板子扬声器）是**运行期**才接上的：webrtcbin 的 src pad 要等远端
描述设好才出现，所以那条链不能用描述串写死。

### 拆成"骨架 + 采集段"

这是为了支持**运行期换摄像头**。切分点选在 `h264parse` 之后：

```
骨架（建一次，永不动）                    可替换段（每次切换整段换掉）
webrtcbin ← vq ← rtph264pay ←ghost─ v4l2src ! [mppjpegdec] ! mpph264enc ! h264parse
  ↑ 音频 aq ← rtpopuspay
```

**为什么切在这里**：`rtph264pay` 留着不动，RTP 的时间戳基准（90kHz 映射 + 随机初值）
和 SSRC 就不会重来一遍，浏览器的 jitterbuffer 不会把切换当成新流。
这是"时间戳连续性不需要任何额外处理"的全部原因。

**为什么编码输出必须一致**：两路采集段的编码输出 caps 完全相同（都是 H.264
1280x720），所以切换**不需要重新协商**、不重建 PeerConnection、音频不受影响，
画面最多停到下一个 IDR（gop=10，约 0.4 秒，实测 367 ms）。
USB 摄像头也按 720p 跑，不是因为它只能跑 720p，而是为了保住这个前提。

### 两路摄像头为什么画质差这么多

MIPI（OV13850）在**这块板子上**是弱势的一方，因为板子上**没有 Rockchip 的 3A 服务**
（`rkaiq_3A_server`）。Rockchip 的 AE/AWB 全在 rkaiq 的用户态库里做，内核 rkisp
只负责把参数写进寄存器。于是传感器停在驱动默认的固定曝光上：

- 又暗（增益顶到上限 248 也只有 Y≈78）
- 又绿（AWB 没跑，ISP 把原始 Bayer 直接转 YUV）

`camera.py` 和 `whitebalance.py` 是这个根因的两个补丁：前者在 `/dev/video12` 上
量亮度、闭环调增益；后者做灰世界校正。USB 摄像头自带 AE/AWB，两样都不需要
（`sources.py` 里 `quality_fix=False`），跑了反而打架。

**这解释了一个不对称**：切到 USB 是一步到位，切回 MIPI 有几秒画面在爬
（曝光闭环 + 白平衡前 15 帧要重跑）。而且顺序必须"先设曝光、再自校准" ——
校准帧曝光不对，算出的增益就是错的。

---

## 五、控制通道：切摄像头与遥操作

这一节讲两条控制链路。它们看起来都是在"遥控板子"，但**载体完全不同**，原因是
两者的约束不同：切摄像头是低频、局域、可以等一个 HTTP 往返；遥操作是高频、
外网也要用、每条都要回执。

### 为什么不走信令

信令服务器**两个方向都是白名单分派**，未知 `type` 只在 `else` 里 `log.debug` 一句
就丢掉。所以"页面发一条 `{type:"camera"}` 走 `/ws/view`"这条路不通，
除非改 VPS 上跑着的生产服务。

而且即使能改也不该改：切换摄像头是**局域网本地**的事，绕公网 VPS 一圈意味着
VPS 挂了就换不了摄像头 —— 而板子就在隔壁。

```
页面 ──POST /camera──> web_server.py（板子，8443，同源、已 TLS）
                          └──unix socket──> device_agent.py
```

用 **Unix domain socket** 而不是 `127.0.0.1:端口`：只多两行（启动时 unlink 残留 +
`chmod 0600`），但把"device_agent 一个入站 TCP 端口都不开"这条一路被写进注释和
架构说明的性质完整保住 —— 对一个反复强调"NAT 后面 / 4G 网里都能用"的项目，
不值得为省两行代码去破它。

### 页面为什么不直连 agent

直连会同时踩上"跨源 + 自签证书静默 reject"这两个坑。走同源的 `web_server.py` 代理，
两个问题一起消失。

### 摄像头列表为什么不塞进 `/config.json`

三个理由，任一条都够：

1. 列表只有 `device_agent` 知道（源表 + 设备在不在场），`/config.json` 是
   `web_server.py` 自己生成的静态配置。塞进去等于让"静态配置"接口耦合上另一个
   进程的存活。
2. 页面的 `resolveRoom()` 在 URL 带 `?room=` 时**提前 return、根本不 fetch
   `/config.json`** —— 列表放那儿的话，演示时一加 `?room=` 按钮就空了。
3. 列表是动态的（设备可能没插），和语义为"静态"的 config 不是一类东西。

所以列表走单独的 `GET /camera`。

### 外网页面没有切摄像头

VPS 上跑着同样的 `web_server.py`，但它没有 `device_agent` 的本地 unix socket，
所以 `/camera` 会明确返回 502（页面日志里会说"取摄像头列表失败"，因此不渲染
切换按钮）。视频和语音完全不受影响。

要让外网也能切，得让板子主动开一条**反向 unix socket 隧道**到 VPS，
例如 `ssh -R /run/webrtc-web/agent.sock:<板子上的 agent.sock> ...`，
再把 VPS 上 web_server 的 `--agent-socket` 指过去。这和"板子零入站端口"的
原则一致，但它引入一条新的长期 SSH 信任链，所以没有默认做。

### 遥操作走 DataChannel，不走上面那条 HTTP 路

命令要低延迟，且每条都要一个明确的回执 —— HTTP 每条一个往返、还要再做一套轮询
取回执。WebRTC 连接和 DTLS 已经建好了，DataChannel 只是在同一连接里多一条
`m=application`，不用新端口、不改信令白名单，而且天然穿 NAT / TURN，外网也能用。

代价在时序上：**通道必须在 `create-offer` 之前创建**，GStreamer 只在那时把
`m=application` 写进 SDP；本项目又只发一次 offer、不重协商，所以一个会话只有一条
通道。`create-data-channel` 早了（pipeline 还没到 PLAYING）会**静默返回 None**，
所以它放在 `_on_negotiation_needed` 里、紧挨 `create-offer` 之前，并加了闸门防止
反过来再触发一次协商。反过来，在 `on-message` 回调里建通道会和 webrtcbin 抢同一把锁，
**直接死锁**。

### 命令协议

```
浏览器 → 板子   {"t":"cmd","seq":7,"cmd":"FORWARD","arg":12,"ts":1728...}
板子 → 浏览器   {"t":"ack","seq":7,"cmd":"FORWARD","ts":...,"ms":3.2}
                {"t":"nack","seq":7,"cmd":"BOGUS","reason":"unknown-command"}
```

白名单 `FORWARD` / `BACK` / `LEFT` / `RIGHT` / `STOP` / `SPEED`（`SPEED` 必须带
`arg`，范围 -1000..1000）。超过 4096 字节的消息先拒。**每种拒绝都有具体 reason**：
`too-large` / `bad-json` / `bad-type` / `bad-seq` / `bad-cmd` / `unknown-command` /
`bad-ts` / `missing-arg` / `bad-arg` / `internal`。页面按 reason 归类计数 ——
因为"命令被拒"和"命令丢了"必须能分开看，这两种现象的处理方式完全不同。

板子的 `_on_dc_message` 只做三件事：校验 → 改 overlay 文字 → 回 ack（带板端处理
耗时）。任何异常都回 `nack(reason="internal")`，**malformed 输入打不崩会话**。

### 为什么强制文字出现在画面上

命令的"执行"是模拟的（没有电机）。如果只看 ACK，"命令走通了"就只是板子自己的一面
之词。文字由板子产生、经 H.264 编进画面再回到浏览器，所以**画面里出现这行字**
同时证明了两件事：DataChannel 真的通了，视频链路也还活着。

文案是 ASCII：`CMD #7 FORWARD 12  14:32:05.1`，2 秒后清空。带序号和时间戳是为了
让旧帧伪装不成新执行。

### 叠加层插在哪、为什么换摄像头后要重绑

`textoverlay` 插在采集段描述的**最前面**（所有源和编码器之间）：

```
不开 wb:  v4l2src ! caps ! textoverlay ! mpph264enc ! ...
开  wb:  ... appsrc ! caps ! textoverlay ! mpph264enc ! ...
```

这样开不开 `--camera-wb` 走同一个位置，且它直接吃 UYVY / NV12，不需要
`videoconvert`。`wait-text=false` 必须显式设 —— 默认 true 时没有文字，整条链
会停在那里等文字。

采集段每次切换都是**整段重建**，旧的 overlay 元素随旧段销毁。所以 `switch_source()`
在 `finally` 里重新 `_bind_overlay()`；漏了它的症状很典型：切完摄像头命令还在 ACK，
画面却再也不出字。

### 板子上没有中文字体

`fc-list :lang=zh` 是空的，`textoverlay` 用 Pango 渲染，中文会变成方框。所以叠加
文案一律 ASCII —— 命令本来就是 ASCII，时间戳也只写 `HH:MM:SS.s`。要加中文得先装
字体，那是部署变更，不是代码变更。

### 1.20.3 上没有 `on-message` 这个信号

实测 `libgstwebrtc-1.0.so.0` 里只有 `on-message-string` / `on-message-data`。
`teleop.connect_message()` 按 string → data → message 的顺序尝试，把**实际连上的
信号名打进日志** —— 版本差异至少会留下证据，而不是变成"消息发出去了、板子说没收到"。

同一条通道的两端也要分别验：板子是 offerer（自己 `create-data-channel`），
`tools/selftest.py` 是 answerer。实测 GStreamer 当 answerer 时会解析
`m=application` 并触发 `on-data-channel`，而且交给回调时通道**已经是 open**
（所以回调里必须处理"已打开"这个分支）。

### 人像检测：NPU 旁路 + 页面叠加层（阶段四）

检测在板子的 RK3588 NPU 上跑（`rknn-toolkit-lite2` + YOLOv5s 的 `.rknn`），
浏览器只负责画框。三条边界先说清：

**不碰视频流。** 检测旁路从 `textoverlay` 之后的 `tee` 分出去：

```
                    ┌─ queue ─ mpph264enc ─ h264parse ─ rtph264pay ─> webrtcbin
v4l2src ! … ! textoverlay ─ tee ─┤
                    └─ queue(leaky) ─ valve(drop=true) ─ videorate ─
                       videoscale ─ videoconvert ! video/x-raw,format=RGB ─ appsink
```

- `valve` 默认 **关**：没开检测时这一支一个 buffer 都不走，空闲成本约等于零，
  也不会因为多一个消费者改变主路的时序。
- 检测支路的 `queue` 是 `leaky=downstream max-size-buffers=2`：NPU 慢的时候
  丢旧帧，**绝不反压主路**。主路一旦被检测拖住，丢的会是编码帧。
- `videoscale` 必须在 `videoconvert` 之前。`videoconvert` 不能改尺寸（实测
  在 `v4l2src` 上 `not-negotiated`），而检测输入固定 640×360（16:9 不补边）。
- `appsink` 用 `max-buffers=1 drop=true sync=false`，检测线程按 100 ms 超时
  拉帧，再由 `videorate` 限到 5 fps。
- 帧的行对齐（stride）从 buffer 大小反推（`frame_stride()`），**不用
  `GstVideo.VideoInfo.from_caps()`** —— 后者在检测线程里调过之后，会话收尾的
  `rknn.release()` 会段错误，见 7.8。

**结果走已有的 DataChannel，不新开通道。** 项目和信令都只协商一次、一个会话
只有一条通道（见上一节），检测消息和遥操作回执共用它，靠 `t` 分流：

```
板子 → 浏览器 {"t":"det","on":true,"ts":…,"ms":12.3,"n":1,
              "boxes":[[x,y,w,h,score], …]}
               {"t":"det","on":false}          // 开关状态
```

坐标是**归一化的**（0~1，相对画面左上角）。页面不需要知道摄像头是 720p 还是
1080p，CSS 尺寸变了也不用重新对齐；框的绘制与视频分辨率彻底解耦。

**后处理只用 numpy。** 板子上的 `cv2` 和 numpy 2.x 不兼容（import 时
`numpy.core.multiarray` 失败），所以 `detect.py` 自己做 letterbox、anchor 解码、
NMS 和归一化，不引 OpenCV。这三段最容易静默出错（框整体偏移、框错位、同一个人
叠几层框），`tools/check_detect.py` 用合成张量把期望坐标算死，宿主机就能跑。

**为什么框不烧进视频流。** 烧录（例如 `cairooverlay`）的好处是任何客户端、
任何截图都带框；代价是每帧都要过 Cairo、开关会改变编码输入、而且画面里从此
分不出"检测到的"和"录下来的"。现在选的是页面叠加层：检测开关不碰编码参数，
录像/截图是干净画面，关掉之后一个框都不剩。要改成烧录，改的是 pipeline 的
采集段，不是页面。

**线程与生命周期。** 检测线程自己拉 appsink、跑 NPU，但结果**不能直接发
DataChannel** —— 通道属于 GLib 线程，跨线程碰它是未定义行为。所以检测线程
回调里用 `GLib.idle_add` 投回 GLib 线程再 `_dc_send`（和遥操作的规则相同）。

拆会话/换摄像头时顺序是死的：**先 `detector.stop()` 再动采集段**。旧 appsink
手里可能还攥着旧 buffer，会把旧采集段的 buffer pool 拖到析构不了 —— 正是
7.5 那条 fd 泄漏链路。`release()`（放 NPU 上下文）必须早于 pipeline dispose；
换源后 `switch_source()` 在 `finally` 里重新绑 `detsink`/`dvalve`，检测开着的话
把线程挂到**新** appsink 上并重新开 valve。

**控制入口。** `GET/POST /detect` 和 `/camera` 同一条路：页面 → 同源
`web_server.py` → 板子上的 unix socket（0600）→ `device_agent`。GET 返回
`available`（模型/依赖在不在）、当前开关和会话里的帧数/错误数；POST
`{"enabled": bool}` 在 GLib 线程上开关。两条状态要分开：

- `_detect_requested` 是**意图**，跟着进程走。没有观看端时 POST 只更新它，
  下一次建会话时自动开启；
- `detect_enabled` 是**实际**状态（线程在跑、valve 开着），只有真的挂上
  appsink 才为真。

模型加载是懒的：没开检测就不建 NPU 上下文。模型缺失时 `/detect` 报不可用、
页面按钮置灰，视频链路照常 —— 这是刻意的，检测不是视频的前置条件。

**模型不入库。** `.rknn` 文件体积大、和板子/工具链版本绑定，仓库里只写默认
路径（`/userdata/rknn_yolov5_demo/model/RK3588/yolov5s-640-640.rknn`）。
YOLOv5 的代码和权重是 AGPL-3.0，自己转换的权重同样受约束；本项目的 MIT
只覆盖仓库里的代码。演示和部署时用 `--detect-model` 指到实际文件。

---

## 六、线程模型

这块最容易踩，所以单独讲。

```
┌─ 主线程 ──────────── asyncio 事件循环，跑 WebSocket 收发
│                        on_signal() 在这里被调用
│
├─ glib-main 线程 ──── GLib.MainLoop，GStreamer 的回调都在这里
│
└─ person-detect 线程 ─ 拉检测 appsink、跑 NPU（阶段四，按需创建）
```

GStreamer 的信号回调**只能**在 GLib 线程里跑，而 WebSocket 只能在 asyncio 线程里写。
跨线程的通道**各有一处，且只有一处**：

| 方向 | 在哪 |
|---|---|
| asyncio → GLib | `on_signal()` 里 emit webrtcbin 的信号，会同步派发到 GLib 线程 |
| GLib → asyncio | `_send_threadsafe()` 用 `run_coroutine_threadsafe` 投回去 |
| 检测线程 → GLib | `GLib.idle_add(self._send_detect_result, …)`，再 `_dc_send` |

不要在别的任何地方跨线程碰对方的对象。切摄像头是第三个例子：它只能在 GLib 线程
调用，所以 HTTP 端点用 `GLib.idle_add` 投递、用 `loop.call_soon_threadsafe` 回填结果。
检测线程和 appsink 的关系同理：`Detector.start/stop` 只碰 appsink 和它自己的线程，
真正发消息的那一步永远在 GLib 线程。

**单观看端**：本项目按遥操作场景设计，同一时刻只允许一个观看端，所以代码里没有
viewer id 那套东西。第二个观看端会被回"本设备同时只接受一个观看端"。
这也让"切换摄像头"没有"多个观看者各看各的"这种歧义，不需要多路广播。

---

## 七、踩过的坑（按根因归类）

这一节是这份文档里最实用的部分。每条都对应一个**不报错、只表现成别的问题**的现象。

### 7.1 环境变量只在登录 shell 生效

BSP 在登录 shell 里导出了 `GST_V4L2_USE_LIBV4L2=1`，留着它会让 rkisp 报
"分配缓冲失败"。**"我测全过、你一跑就炸"先查登录/非登录 shell 的差异。**
`pipeline.py` 因此在模块导入时（**早于 `Gst.init()`**）就把这个变量清掉。

### 7.2 状态显示连接了，但一个包都没有

`iceConnectionState = connected` **不等于**有媒体。判据要看候选对上的字节有没有在涨。

### 7.3 webrtcbin 靠对端 SDP 里的 `a=ssrc` 认入站 RTP

板子收不到音频、`pad-added` 不触发**且没有任何报错**，根因常常在对端 ——
GStreamer 当 answerer 时不吐那一行。

### 7.4 切换摄像头：ghost pad 默认是非激活的

`Gst.GhostPad.new()` 出来的 ghost pad 默认不激活，`add_pad()` 之后**仍然不激活**，
必须显式 `pad.set_active(True)`。漏了的症状是**切换后新段一个包都不出、日志干干净净**。

### 7.5 每轮会话漏一个设备 fd

这是最隐蔽的一个，因为它不影响任何观感，只有跑几十轮之后才发作。

现象：每建一次会话，`/dev/video11` 上就少还一个 fd。根因是一条三环链：

1. 被**拉过 sample** 的 appsink 会一直攥着一个 v4l2 buffer（没人拉过就不会），
   而那个 buffer 不让 v4l2 的 buffer pool 析构 —— pool 握着设备 fd。
2. appsink 要析构，得先让它的父 bin 析构。
3. 父 bin 挂在 pipeline 上，而 pipeline 因为**横跨 C 和 Python 的闭包环**
   （C 侧的 signal handler 抓着 Python 闭包，闭包抓着 pipeline）永远到不了
   refcount 0 —— `gc.collect()` 穿不过去。

修法：拆会话时**把采集段从管道里摘掉**（和切换时走同一条路），把链断在最上面一环。
`tools/check_fd.sh` 是它的判据。

> 顺带解释了为什么**切换从来不漏**：那条路本来就走 `VideoSegment.teardown()`，
> 里面就有 `pipeline.remove(self.bin)`。

### 7.6 测电平的三个坑

ADC 起振瞬态、设备 busy、整数秒对齐 —— 任何一个都会把结论测反。见 `check_audio.sh`。

### 7.7 工具自己造出来的"产品缺陷"

写验证工具时踩的，但表现得像产品 bug，值得记：

- `pkill -f "<模式>"` 会匹配到 **ssh 命令自己的命令行**，把自己的 shell 杀掉
  （表现成静默无输出或退出码 144/255）。用方括号绕开：`pkill -f "device_agent[.]py"`。
- 验证用的反向代理如果两个方向的 pump 用 `gather` 等待，浏览器被强杀时上游 WS
  永远不关，板子一直以为还有观看端 —— 然后报"已有一个观看端，拒绝新的连接"，
  看起来完全像产品缺陷。

### 7.8 检测线程里调 `GstVideo.VideoInfo.from_caps()`，收尾时 NPU 释放段错误

这个坑的误导性在于：**现场看起来完全是 NPU 的问题**，可凶手是一个毫不相干的
GStreamer 调用。

现象：开着检测的会话一结束，agent 进程就没了（`EXIT=139`），日志尾巴上只有
一屏 `gst_segment_to_running_time_full: assertion 'stop != -1' failed`，没有
Python traceback；`/detect` 随后返回"连不上推送端"。视频/音频/遥操作在断开
之前全部正常，所以很容易被当成"检查脚本把服务停了"。

用 `PYTHONFAULTHANDLER=1` 重跑就能拿到现场：

```
Fatal Python error: Segmentation fault
Thread … (most recent call first):
  File "rknnlite/api/rknn_lite.py", line 273 in release
  File "src/detect.py",   line … in release
  File "src/session.py",  line … in _stop_on_glib
```

隔离实验（全部在板子上实跑过）：

| 实验 | 结果 |
|---|---|
| 检测线程里 `from_caps()`，**一帧都不推理** | 收尾照炸 |
| 主线程里 `from_caps()` 调 40 次，再 `release()` | 没事 |
| 300 次推理（不碰 GStreamer），再 `release()` | 没事 |
| 真管线 + 推理，把 `from_caps()` 换成从 buffer 大小推 stride | 连跑多轮不炸 |

结论：**librknnrt 的 release 和 `from_caps()` 在非 GLib 线程里的调用**合在一起
才会炸，和推理次数、模型都不相干；具体是 librknnrt 内部还是 PyGObject 的
marshalling 就不值得再追了 —— 绕开它更便宜。

修法：`frame_stride()` 从 buffer 大小反推（一帧 RGB 正好是 `stride*height`），
检测线程完全不碰 GstVideo。判据是 `tools/check_detect.sh` 跑完 agent 还在，
`tools/check_fd.sh` 连跑 3 轮都过；单条 `check_detect.py` 用真实的 GstSample
覆盖这条路径。

> 教训：**"报错的那一行不是凶手"**。段错误发生在 `release`，不代表原因在
> NPU —— 用 faulthandler 拿 Python 栈，再用"最小可证伪差异"（加回/去掉
> `from_caps`）逐段二分，比盯着 NPU 日志猜快得多。

### 7.9 USB 摄像头重新枚举之后，`/dev/videoNN` 不再是原来那个

现象：页面没画面，会话照建但 0 包；日志里没有明显的摄像头错误，只有 MIPI 的
白平衡桥在喊"5 秒没等到帧"。`dmesg` 里能看到 USB 设备掉了一次又回来：

```
usb 3-1: cannot submit urb 0, error -19: no device
usb 3-1: new high-speed USB device number 3 using ehci-platform
usb 3-1: Found UVC 1.00 device Q8 HD Webcam (1bcf:2281)
```

UVC 摄像头会占**两个**节点（采集 + metadata），节点号由枚举顺序决定。重新枚举
一次，`video20` 就没了，新节点变成 `video21`/`video22` —— 源表里写死的
`/dev/video20` 从此指向不存在的设备。

修法：USB 源用 udev 的稳定链接，不用裸节点号：

```bash
ls -l /dev/v4l/by-id/            # usb-<型号>-video-index0 -> ../../video21
```

`src/sources.py` 里 `usb.device` 就是这个 by-id 路径；重新枚举后链接自动指向新
节点，`/camera` 的 `present` 也跟着对。**换不同型号的摄像头时要改这一行** ——
链接名里带型号。

> 另一件相关的事：重启后初始源默认是 mipi。板载 MIPI 排线有问题时，每次重启
> 都是"页面又黑了"，还得再点一次切换 —— 在 `.env` 里写 `CAMERA_SOURCE=usb`
> 把这一步省掉（`run-agent.sh` 会翻译成 `--camera-source`）。

### 7.10 切完摄像头，videorate 有一阵子不按 fps 丢帧

现象：开着检测切换摄像头之后，检测帧数按采集帧率涨（实测约 32 帧/秒，配置是
5），持续十几秒才回到 5。`errors` 一直是 0，页面上的框也在更新 —— 唯一不对的
就是 NPU 的占用是配置值的六倍，很容易被当成"检测就是这么费"。

定位时先在**脱离 agent 的脚本**里用 `pipeline.segment_desc()` 建同一段采集链，
把 `videorate` 的 src caps 打出来：正常时是 `framerate=(fraction)5/1`，appsink
也是 ~5 帧/秒；问题只在"采集段被重建过"的那条链上出现。

| 场景 | videorate 出口 |
|---|---|
| 建段时 valve 就开 / PLAYING 后再开阀（同一个段） | 都是 5/秒 |
| 切源重建采集段（阀在重建后立刻开） | 前 ~19 秒按采集帧率全放行，之后 5/秒 |

所以不是 caps 写错，是采集段重建后的协商时机问题。和 GStreamer 的这段时机
较劲的性价比不高：限流本身只需要"推理不超过 fps"，而这个判据可以直接用墙钟
时间做，与管线状态无关。

修法在检测线程里兜底（`detect.too_soon()`）：距上一帧不足 `0.9 / fps` 秒就
跳过，不推理。appsink 是 `max-buffers=1 drop=true`，多拉的那几帧丢掉不积压；
`0.9` 是给 videorate 的正常抖动留的余量（实测它偶尔出 6 帧/秒）。第一次触发
和之后每 500 帧会打一条警告，`/detect` 里的 `skipped` 是累计数：平时只有零星
几个（videorate 的正常抖动），切源/开关之后会涨一截 —— 那也是同一个时机问题。
**如果 `skipped` 长期和 `frames` 是同一量级**，说明上游限流彻底失效了。

### 7.11 切到 MIPI 一片黑：检测的 appsink 把新采集段卡在 PAUSED

现象：开着（或关着）检测，从 USB 切到 MIPI，页面一直黑屏、`video_packets_out`
冻住、`detect_frames` 0；日志里只有一行

```
白平衡桥：5 秒没等到帧，线程收工（pipeline 照常跑，只是不再校正）
```

没有任何 GStreamer 报错。反证很干净：把 agent 停掉、同一个 `/dev/video11` 用
`gst-launch-1.0 ... num-buffers=10 ! fakesink` 能出帧，dmesg 里 ISP 也一直
`stream ON` —— 设备、排线、sensor 都没问题。

定位靠 `GST_STATES`：换源时新采集段是 `sync_state_with_parent()` 单独接进
**已经在 PLAYING** 的管线的，这时 bin 的状态迁移不再是"整条管线一起上"，
而是**每个 sink 各自 preroll 完才算了事**。日志里 bin 的 PAUSED→PLAYING 返回
`ASYNC`，`detsink` 一直 `changing state asynchronously`。

`detsink` 前面那个 valve 默认关着（`drop=true`），它永远收不到第一帧，于是
整个 bin 停在 PAUSED —— 连已经收到 preroll 的 `wbsink` 也不消费。mjpeg 源的
主路不经过任何 appsink，照样出帧，所以"切 USB 好好的"；raw 源的主路是
`wbsink →（桥线程）→ wbsrc`，`wbsink` 一停，桥线程就等不到帧，视频自然 0 包。
**首段不受影响**（整条管线 NULL→PLAYING，live 源不要求 preroll），只有换源
这条路会中招 —— 这也是它只在加检测旁路之后才出现的原因。

| 场景 | 结果 |
|---|---|
| 首段直接以 MIPI 起（NULL→PLAYING 一次上去） | 正常 |
| 换源到 MIPI，`detsink` 默认（`async=true`） | bin 停在 PAUSED，桥 5 秒超时，0 包 |
| 换源到 MIPI，`detsink async=false` | 立刻出帧（前 15 帧自校准都跑完） |
| 换源到 MIPI，`detsink` + `wbsink` 都 `async=false` | 同样正常，不必要 |

修法：`detsink` 加 `async=false`。检测本来就不需要 preroll（有帧就拉、没有就
等），关掉没有副作用；`wbsink` 保留默认，因为它确实要等第一帧 —— 摄像头真的
不出帧时，让段停在 PAUSED 比假装在播更接近事实。

**这类"换源才出现、首段正常"的问题，先怀疑异步状态机，别先怀疑设备。**
判据是 `GST_STATES` 里那个 `ASYNC`，不是日志里有没有 ERROR（这次一条都没有）。

---

## 八、怎么证明它真的在工作

**核心原则：「画面在动」不是证据。** 每条判据都对应一个具体的、踩过的谎：

| 判据 | 防的是什么 |
|---|---|
| 候选对字节持续增长 | 防 `iceConnectionState` 报 connected 而实际零媒体 |
| `framesDecoded` 增长 | 防"收到 RTP 包"被当成"解出了帧" |
| 字节守恒 ≈ 1.0 | 防只有单向通了却被当成双向通 |
| 稳态窗口采样 | 防把启动斜坡算进去，量出假的 5fps |
| `selectedCandidatePair` | 防 ICE 重启后拿着冻住的旧候选对算速率 |
| 每条命令都有回执（`sent == acked + nacked`） | 防丢一条命令被当成"页面没发" |
| 板端 `teleop_overlay` 跟得上 ACK | 防 ACK 是真的、画面那道环是假的 |
| 板端 `teleop_rx` 与页面 `sent` 对得上 | 防页面自说自话（自己发的、自己记的） |
| 检测开启后 `frames` 涨、关闭后停 | 防"POST 成功了"被当成"NPU 真的在跑/真的停了" |
| 检测会话拆掉后 agent 还活着（不是 `EXIT=139`） | 防"检测跑通了"被当成"收尾没问题"——段错误只在拆会话那一刻发作（7.8） |
| `/detect` 的 `skipped` 只零星涨，不和 `frames` 同量级 | 防上游限流失效（切源后 videorate 会放行一阵），NPU 被按 6 倍占用却没人发现（7.10） |

### 三个验收工具

```bash
# 主验收：13 条（视频）、17 条（含双向音频）或 22 条（含遥操作）
python3 tools/analyze_diag.py --url https://<信令服务器> --room <房间> \
        --cafile certs/vps-self.crt --audio --teleop --teleop-expect-nack

# 切换摄像头：13 条（设备侧证明"那一段真的换了"，浏览器侧证明"解出来的是新画面"）
tools/check_switch.sh --base https://<板子>:8443

# 人像检测：可用性 → 开启后帧数在涨 → 关闭后帧数停住
tools/check_detect.sh --base https://<板子>:8443

# 检测后处理单测（宿主机）；在板子上加 --model 跑一次真实推理
python3 tools/check_detect.py

# 会话拆除后设备 fd 有没有还回去（**在板子上跑**，它要读本机 /proc）
tools/check_fd.sh --cycles 3
```

**`--audio` 默认不开**：它要人手在页面上点「开始对讲」才会起麦克风，没点的时候
音频判据必然全红 —— 那是操作问题不是链路问题。所以不点的时候它会明说
"音频判据未启用"，**而不是静默跳过**。

### 数据从哪来

`/diag` 是**累积**的：浏览器侧 1Hz 上报 `getStats()` 摘要（含画面亮度均值和一个
32×18 的亮度缩略图），板子侧 1Hz 上报自己的发送计数。分析器按 `bytesReceived`
回退来切分"哪一段是哪一轮会话"。样本存在信令服务器上，`POST /diag/clear` 清空。

**调 WebRTC 统计先用 Chrome**：Firefox 少给 `transport` 和 `freezeCount`，
而"画面静止时间占比"这条判据没有 `freezeCount` 就只能退回采样法，
采样法会少算约 2.5 倍。

### 判据必须能失败

一个从不失败的判据等于没有判据。`check_fd.sh` 写完之后的验证方式是
**把修好的那一步去掉、确认它真的报错**，再改回来：

```
第 1 轮  断开之后：1 个  ✘ 比基线多了 1 个（每轮 +1 = 每轮漏一个）
第 2 轮  断开之后：2 个  ✘ 比基线多了 2 个
退出码: 1
```

同样地，`check_fd.sh` 每轮都先在**会话进行中**量一次 fd 并断言 ≥1 —— 因为如果
WS 根本没连上、会话压根没建起来，那么"断开之后 0 个"是白送的，
判据会以最漂亮的方式骗过你。

---

## 九、怎么跑起来

### 局域网（最小形态，不需要 VPS）

`run-agent.sh` / `run-web.sh` 是为当前公网部署写的：它们要求 `.env` 里的
`SIGNAL_HOST` / `SIGNAL_AUTH` 指向 VPS。**纯局域网**时直接起三个进程：

```bash
bash tools/gen_cert.sh                     # 浏览器麦克风需要安全上下文

# 1) 信令（板子上，8080 明文；局域网不需要 STUN/TURN）
python3 src/signal_server.py --room demo --token hunter2 --ice-mode none --port 8080

# 2) 推流端
python3 src/device_agent.py --signal ws://127.0.0.1:8080/ws/publish \
    --room demo --token hunter2            # 没有麦克风就加 --no-audio

# 3) 页面 + 同源代理（切换摄像头的按钮依赖这一步）
python3 src/web_server.py --port 8443 --room demo --signal http://127.0.0.1:8080 \
    --tls-cert certs/board.crt --tls-key certs/board.key
```

浏览器打开 `https://<板子IP>:8443/`；房间号由 `/config.json` 自动提供，
不用手写 `?room=`。

只跑 `signal_server.py` 也能出画面（它自己在 `/` 提供页面），但没有 `/camera`
代理，页面上不会出现切换摄像头的按钮。

### 公网（当前部署形态）

VPS 上：`sudo bash tools/deploy_vps.sh` 装 coturn，并生成
`/etc/webrtc-signal.env`（房间号、token、TURN 密钥）。

再在开发机上跑 `bash tools/deploy_web_vps.sh`。它把 VPS 收敛成两个目录：

- `/opt/webrtc-signal`：**唯一**的信令服务，源码就是本仓库的
  `src/signal_server.py`，监听 `127.0.0.1:8443`
- `/opt/webrtc-web-server`：观看页服务，**平铺**放 `web_server.py` + `monitor.html`，
  监听 `127.0.0.1:8444`

并配置 nginx 的 443 分派与观看端 Basic Auth（详见第二节"外网访问控制"）。
旧副本目录 `/opt/webrtc-signal-next`、`/opt/webrtc-signal-monitor`、
`/opt/webrtc_web_server` 会被清掉；旧的 8443 unit 和源码会备份到
`/root/webrtc-consolidation-backup-*`。

注意：统一后的信令源码**没有** camera-pipeline 版的 `/call`、`/media`、`/ws/call`，
那套双人通话演示不再由这台 VPS 的 8443 提供；要恢复得回 camera-pipeline 项目单独部署。

板子上：填好 `.env`（`ROOM`、`TOKEN`、`SIGNAL_HOST`、`SIGNAL_AUTH`）后用
`tools/service.sh start` 起两个服务（`start|stop|restart|status|logs`，进程在
`run/*.pid`，日志在 `run/*.log`）。`tools/run-agent.sh` / `tools/run-web.sh` 仍然
可用，但它们是**前台**进程，适合调试。两个入口都从 `.env` 读，`SIGNAL_AUTH`
必须和 VPS 上部署脚本打印的那组观看端凭证一致 ——
板子替浏览器访问 VPS 的 `/ice.json`、`/diag`、`/ws/view` 时要带上它。
直接传命令行参数也可以，但 `.env` 是唯一推荐方式（两边不会写岔）。

完整的从零部署步骤（VPS 的 nginx 443 前置、证书、代码同步、`.env` 四项）见
[README.zh-CN.md 的"公网部署"](../README.zh-CN.md#公网部署当前形态)。

浏览器：

- 局域网：`https://<板子>:8443/?room=<房间>`
- 外网：`https://<VPS>/webrtc`，**需要登录**（用户名/密码见部署脚本输出，或
  `/root/webrtc-view.credentials`）。房间号由 `/config.json` 自动给出，不用手写。
  根路径 `/` 返回 404，不再跳转；`/webrtc/` 会 301 回 `/webrtc`。

### 关键参数

| 参数 | 值 | 为什么 |
|---|---|---|
| `--gop` | 10 | 切换后最多等一个 GOP 才出新画面。20fps 下约 0.5 秒 |
| 分辨率 | 1280x720 | 两路一致是"切换不用重新协商"的前提 |
| 码率 | 2500 kbps | MIPI 的 UYVY 走 rkisp 的吞吐上限约 20.4 fps —— 不是 CPU 也不是编码器（MPP 是硬编） |
| `--detect-fps` | 5 | 检测支路在 `videorate` 里限帧；主路仍是 20 fps，互不影响 |
| 检测输入 | 640×360 | 16:9 不补边，`letterbox` 到 640×640 后正好上下各补 140 |
| 检测 `valve` | 默认关 | 没开检测时支路零 buffer，空闲成本约等于零 |

---

## 十、明确不做的

和代码注释里同样的取舍风格，写下来是为了避免"这是不是漏了"的反复讨论：

- **不做两路同时推**（用户已排除），所以没有多路广播、没有 PiP。
- **不做多观看端**：遥操作场景下一个操作者就够了。
- **不改信令服务器来承载控制命令**：理由见第五节。
- **不用 TURN 承载局域网媒体**：局域网 host candidate 直连，`--ice-mode none` 就够。
- **USB 摄像头不上 1080p**：会破坏"切换不用重新协商"的前提。
- **阶段三的"执行"是模拟的**：原始约束就是"只需要做文字命令演示，不需要实际操作"，
  但**结果是看得见的**（画面里出现那行字）。
- **检测框不烧进视频流**：走页面叠加层，保证录像/截图是干净画面。要烧录得改采集段
  （见第五节的取舍），不是改页面。
- **检测只做"人"这一类，只显示不告警**：框和人数，没有录像、没有推送。

---

## 十一、已知遗留

- **pipeline 对象本身每轮会话不回收**。fd 已经不漏了（见 7.5），但 pipeline 自己
  因为那条跨 C/Python 的闭包环仍然析构不了，所以每轮的骨架元素
  （webrtcbin、rtph264pay、队列、音频分支）不会立刻还给内存分配器。
  不泄漏 fd、不影响功能，量级是每轮几百 KB。要根治得让那些闭包不再捕获 `pipeline`，
  或者在拆除时主动断开 webrtcbin 的 handler / probe。
- **页面不会自动重连**。板子的 `web_server.py` 重启后，已经打开的页面
  信令 WS 就断了，页面不会自己接回来，需要刷新。
- **切回 MIPI 有几秒画面在爬**（见第四节），这是传感器本身决定的，不打算修，
  但按钮旁边应该标一句让用户有预期。
