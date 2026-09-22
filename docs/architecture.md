# FunASR 流式语音识别服务 — 架构文档

**版本：** V5（VAD 热调接口 + MRCP 稳定性修复）  
**更新日期：** 2026-09-13

## 1. 系统概览

基于 FunASR C++ Runtime 的流式语音识别服务，采用 **单容器合并架构**（Gateway + C++ Runtime），通过 WebSocket 接收实时音频并返回识别结果。支持 **16kHz 浏览器麦克风** 和 **8kHz 呼叫中心电话流** 两种采样率。

提供两种接入方式：
1. **Socket.IO 直连**（浏览器/呼叫中心 SDK）：5002 端口，JSON + Base64 音频
2. **MRCPv2 标准协议**（FreeSWITCH/IVR 平台）：UniMRCP Server + mrcpfunasr 插件 → 内部 WS 5003，呼叫中心无需改造

```
FreeSWITCH (mod_unimrcp)            浏览器/呼叫中心 SDK
    │ MRCPv2 (SIP/TCP:8060 + RTP)      │ WebSocket (Socket.IO)
    ▼                                  │
┌─ UniMRCP Server ─────────────────┐   │
│ 插件 mrcpfunasr                   │   │
│ 内部 WebSocket (5003)             │   │
└────────────────┬─────────────────┘   │
                 │                     │
                 ▼                     ▼
┌──────────────────────────────────────────────────────────┐
│           单容器 argosasr-streaming-delivery (Docker)       │
│                                                         │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Gateway (Python Flask-SocketIO)                 │  │
│  │  ┌──────────┬──────────┬───────────┬──────────┐  │  │
│  │  │ License  │ 会话管理  │ Base64解码│ 8kHz→16k │  │  │
│  │  │ 鉴权     │ 限流/超时 │ PCM拼接   │ 重采样   │  │  │
│  │  └──────────┴──────────┴───────────┴──────────┘  │  │
│  └──────────────────────┬───────────────────────────┘  │
│                         │ WebSocket (localhost:10095)   │
│  ┌──────────────────────▼───────────────────────────┐  │
│  │  C++ Runtime (funasr-wss-server-2pass)           │  │
│  │  ┌────────────────────────────────────────────┐  │  │
│  │  │ ONNX INT8 推理 (2pass: 流式+离线校正)      │  │  │
│  │  │ decoder-thread-num / io-thread-num         │  │  │
│  │  └────────────────────────────────────────────┘  │  │
│  └──────────────────────────────────────────────────┘  │
│                                                         │
│  端口: 5002 (Socket.IO) / 5003 (内部 WS) / 10095 (Runtime)│
└─────────────────────────────────────────────────────────┘
```

## 2. 核心设计决策

### 2.1 为什么用单容器合并架构

| 方案 | 优点 | 缺点 |
|------|------|------|
| 双容器（Gateway + Runtime 独立） | 职责分离，可独立扩缩 | 跨容器网络开销，部署复杂 |
| **单容器合并** | **部署简单，本地 loopback 低延迟** | 无法独立扩缩 |

- 单容器合并后，Gateway → Runtime 通信走 localhost，省去跨容器网络跳转
- 并发场景墙钟时间减少 13%（23.3s → 20.3s）
- 部署只需管理一个容器，降低运维复杂度

### 2.2 为什么 Gateway 层重采样 8kHz → 16kHz

| 方案 | 优点 | 缺点 |
|------|------|------|
| Gateway 重采样 | 无需额外模型，CPU 开销极小 | 质量略低于原生 16kHz |
| 部署 8kHz 专用模型 | 电话音频质量更优 | 需额外模型文件 + Runtime 配置 |

- 呼叫中心 8kHz 音频在 Gateway 层实时上采样到 16kHz，复用现有模型
- 线性插值 2x 上采样，每 600ms chunk 约 0.1ms CPU 开销
- 16kHz 现有功能完全不受影响

## 3. 组件详解

### 3.1 Gateway (`src/stream_server.py`)

**职责**：
- WebSocket 连接管理（Flask-SocketIO + threading）
- License 鉴权（HTTP 连接宿主机授权服务）
- 会话限流（MAX_SESSIONS 上限）
- Base64 解码 → PCM bytes
- **8kHz → 16kHz 重采样**（numpy 线性插值）
- 音频转发到 Runtime（WebSocket 客户端）
- 接收 Runtime 识别结果 → Socket.IO emit 推送前端
- **内部 WS 通道**（5003 端口）：供 UniMRCP 插件接入，事件优先推送到该通道
- **no-input 超时定时器**（MRCP 场景）：静音超时后主动推送 `finished` 空文本事件
- **VAD 热参数管理接口**（`/admin/vad`，X-Admin-Token 鉴权）：运行中热调语音活动检测参数，无需重启

**会话管理**：
```python
sessions[sid] = {
    "last_time": float,         # 最后活跃时间
    "sample_rate": int,         # 客户端采样率 (8000/16000)
    # ── 内部 WS 通道（MRCP 插件接入） ──
    "internal_ws": WSConn,      # websockets 连接对象（None=SocketIO 会话）
    "internal_loop": Loop,      # 内部 WS 事件循环（跨线程推送用）
    # ── Runtime WS ──
    "runtime_ws": WebSocketApp, # 到 Runtime 的 WS 连接
    "ws_connecting": bool,      # 是否正在建立 Runtime WS（防重复连接）
    "ws_ready": Event,          # Runtime WS 是否就绪（on_open 置位）
    "pending_audio": list,      # WS 未就绪时暂存音频
    # ── 语音活动状态机 ──
    "speech_detected": bool,    # 本轮是否已触发 speech_start
    "speech_start_time": float, # speech_start 时间戳（算 duration_ms）
    "last_speech_time": float,  # 最后一次检测到说话的时间（静音兜底）
    "result_shown": bool,       # 首个识别结果时延是否已推送
    "last_result_text": str,    # 最新识别文本缓存（静音兜底 speech_end 携带）
    "max_rms": float,           # 本轮音频最大 RMS（全静音校验，防残留文本）
    # ── Runtime WS 轮次（generation） ──
    "ws_generation": int,       # start 递增；旧连接结果按代次作废（防 start/end 竞态）
    # ── no-input 超时（MRCP） ──
    "no_input_sent": bool,      # 本轮是否已因静音超时发过 finished（防重）
    "_no_input_token": Token,   # no-input 定时器令牌（新一轮 start 复位）
}
```

### 3.2 C++ Runtime (funasr-wss-server-2pass)

**职责**：
- 运行 ONNX INT8 量化模型推理
- 2pass 模式：流式在线模型 + 离线模型校正
- 原生多线程，不受 Python GIL 限制
- 监听 10095 端口（容器内部）

**模型**：
| 模型 | 类型 | 用途 |
|------|------|------|
| paraformer-large-online-onnx | 流式在线 | 实时返回中间结果 |
| paraformer-large-onnx | 离线 | 2pass 校正，提高准确率 |
| speech_fsmn_vad-onnx | VAD | 语音活动检测 |

### 3.3 授权客户端 (`src/asr_license_client.py`)

- 启动时验证 + 每日 23:00 重检
- 连续 3 次验证失败 → 停止服务
- 通过 `host.docker.internal:9800` 连接宿主机授权服务

### 3.4 前端测试页 (`src/stream_test.html`)

- 纯 HTML/JS + Socket.IO CDN
- **采样率选择器**：16kHz（麦克风）/ 8kHz（电话/呼叫中心）
- 麦克风录音 → PCM → Base64 → WebSocket
- 实时显示 partial 和 final 结果

### 3.5 内部 WS 通道（MRCP 插件接入）

Gateway 在 **5003 端口**额外提供内部 WebSocket 服务，供 UniMRCP 的 mrcpfunasr 插件接入。同一套会话状态机与识别管线复用，事件通过 `_push_event()` 优先推送到内部通道（无内部通道时回退 Socket.IO room）。

**协议**（与 Socket.IO 协议同构，JSON 文本 + 二进制音频帧）：

| 方向 | 消息 | 说明 |
|------|------|------|
| 插件 → Gateway | `{"type":"start","sample_rate":8000,"no_input_timeout":15000}` | 开始识别，`no_input_timeout` 毫秒（0=禁用） |
| 插件 → Gateway | 二进制 PCM 帧 | 8kHz 音频流 |
| 插件 → Gateway | `{"type":"end"}` | 结束识别 |
| Gateway → 插件 | `speech_start` / `recognition_result` / `speech_end` / `finished` | 与 Socket.IO 下行事件同构 |

**no-input 超时机制**（MRCP 静音场景）：
```
插件 RECOGNIZE 携带 no-input-timeout 头
    → start 消息带 no_input_timeout 参数
    → Gateway 启动定时器（每轮 start 复位令牌）
    → 超时且未检测到语音（speech_detected=False）
    → Gateway 推送 finished {text: ""}（no_input_sent 防重）
    → 插件收到空文本 finished → 发送 Completion-Cause 002 (no-input-timeout)
    → FreeSWITCH play_and_detect_speech 返回 no-input 结果
```

> 静音兜底 `speech_end(silence)` 携带 `last_result_text` 缓存的最后识别文本，避免插件把“已识别出文本”误判为 no-input。

### 3.6 VAD 热参数接口（/admin/vad）

Gateway 提供管理接口，语音活动检测参数可运行中热调，无需重启容器：

| 参数 | 默认值 | 取值范围 | 作用 |
|------|--------|----------|------|
| `threshold` | 0.03 | (0, 1) | 能量 VAD 阈值（RMS），超过视为说话 |
| `silence_grace_ms` | 2000 | [100, 60000] | speech_start 后持续静音多久触发 speech_end |
| `stale_text_rms` | 0.005 | (0, 0.1) | 整段音频 max_rms 低于此值 → 丢弃识别文本 |

- 鉴权：请求头 `X-Admin-Token`（默认 `asr-vad-debug-9f2c`，环境变量 `ADMIN_TOKEN` 覆盖）
- 查询：`GET /admin/vad`；修改：`POST /admin/vad`（JSON body，立即生效）
- 初始值由环境变量覆盖：`VAD_SPEECH_THRESHOLD` / `VAD_SILENCE_GRACE_MS` / `STALE_TEXT_RMS`
- 命令示例见 operations.md 第 3.4 节

### 3.7 UniMRCP Server（mrcpfunasr 插件）

UniMRCP Server 1.8.0 加载 mrcpfunasr 插件，将 FreeSWITCH 的 MRCPv2 RECOGNIZE 请求转接到网关内部 WS（5003）。

**职责**：
- 接收 MRCPv2 请求（RECOGNIZE），解析音频流与 header（no-input-timeout 等）
- 与网关建立/复用内部 WS 连接（per-channel，跨多次 RECOGNIZE 复用）
- 转发 8kHz PCM 音频帧 → 网关；接收网关事件 → 映射为 MRCP 事件
- 识别音频落盘 utter 文件（`/usr/local/unimrcp/var/`，回放复现用）

**事件映射**：

| 网关事件 | MRCP 事件 |
|----------|----------|
| `speech_start` | START-OF-INPUT（提示音停止） |
| `speech_end` + 识别文本 | RECOGNITION-COMPLETE（NLSML 结果，Completion-Cause 000） |
| `finished` 空文本（no-input 超时） | RECOGNITION-COMPLETE（Completion-Cause 002 no-input-timeout） |

**关键设计**：
- **插件本地 VAD 已禁用** —— 语音活动检测统一由网关驱动（能量 VAD + 识别兜底双层），避免两套 VAD 阈值不一致
- 插件 WS 客户端带发送互斥锁与 SO_SNDTIMEO 200ms 超时
- 插件崩溃/重启后呼叫优雅失败（channel error），FreeSWITCH 无需重启自动恢复
- 源码位置：本地 `docker/mrcp-plugin/`；服务器 `/opt/unimrcp-build/UniMRCP-unimrcp-1.8.0/plugins/mrcp-funasr/`

## 4. 消息流转

```
1. 客户端建立 WebSocket 连接
2. Gateway: handle_connect() → 创建到 Runtime 的 WS 连接 → 返回 connected
3. 客户端发送 start { sample_rate: 8000 | 16000 }
4. Gateway: 存储 sample_rate 到会话
5. 客户端发送 audio chunk (Base64)
6. Gateway: 解码 Base64 → PCM bytes
7. Gateway: 若 sample_rate == 8000 → 重采样到 16kHz
8. Gateway: 转发 PCM 到 Runtime (WebSocket)
9. Runtime: 推理 → 返回识别结果
10. Gateway: 接收结果 → 语音活动检测 → emit 事件
11. 客户端收到事件 → 更新 UI
12. 客户端发送 end → Runtime 返回最终结果 → 清理会话
```

### 4.1 SocketIO 事件协议

**上行事件（对接方 → Gateway）**：

| 事件 | 触发时机 | payload |
|------|---------|--------|
| `start` | 开始会话 | `{sample_rate: 8000\|16000}` |
| `audio_chunk` | 流式发送音频块 | `{chunk: base64 PCM, sample_rate}` |
| `end` | 结束说话，触发最终识别 | 无 |
| `reset` | 重置会话状态 | 无 |

**下行事件（Gateway → 对接方）**：

| 事件 | 触发时机 | payload |
|------|---------|--------|
| `connected` | 连接成功 | `{message: "连接成功"}` |
| `started` | 会话启动 | `{message, sample_rate}` |
| `speech_start` | 能量 VAD 检测到说话（双层：识别结果兜底） | `{text, detect_time, rms?}` |
| `recognition_result` | 每次识别结果 | `{text, is_final, result_latency, server_time}` |
| `speech_end` | 最终结果 / 静音兜底 / 会话关闭 | `{text, duration_ms, reason}` |
| `finished` | 会话结束 | `{text, duration}` |
| `reset_complete` | reset 应答 | 无 |
| `error` | 异常 | `{message}` |

`speech_end.reason` 取值：
- `final` — Runtime 返回最终结果（2pass offline / is_final 标志）
- `silence` — 静音兜底（speech_start 后持续静音 2 秒，携带缓存识别文本）

`recognition_result` 字段说明：
- `result_latency` — 语音到达网关 → Runtime 返回文本的时间（ms），仅本轮首个非空文本的消息非 null，其余消息为 null
- `server_time` — 网关发出消息的 Unix 时间戳（秒，浮点），对接方可用 `本地时间 - server_time` 计算下发段延迟
- 会话超时（15s 无音频）时收到 `{text: "", is_final: true, error: "会话超时"}`

### 4.2 IVR 打断（Barge-in）对接

ASR 服务提供 `speech_start` / `speech_end` 事件供 IVR 平台实现语音打断：

```
IVR TTS 播报中
    │
    ├─ 收到 speech_start → 停止 TTS 播放
    ├─ 收到 recognition_result (is_final=false) → 缓存中间结果
    ├─ 收到 speech_end → 准备执行意图（reason=final 有完整文本；reason=silence 为静音兜底）
    └─ 收到 recognition_result (is_final=true) → 意图检测 → 执行动作
```

**职责分离**：ASR 负责检测用户开口（speech_start）和返回识别文本，意图理解由 IVR 侧完成。

**语音活动状态机**（双层触发，每轮一次）：

```
speech_start:
  第一层：Gateway 能量 VAD —— 音频块 RMS ≥ vad_threshold（默认 0.03，可热调）即触发（~100ms 延迟）
  第二层：识别结果兜底 —— VAD 漏检时，Runtime 首个非空文本触发

speech_end（每次触发后状态机复位，可检测下一轮说话）:
  1. Runtime 返回最终结果（2pass offline / is_final 标志）
  2. 静音兜底 —— speech_start 后持续静音超过 vad_silence_grace_ms（默认 2000ms，可热调）
  3. 会话关闭 / 重置
```

**稳定性机制**（V5）：
- **全静音校验**：整段音频 `max_rms < stale_text_rms`（默认 0.005）→ 丢弃 Runtime 残留文本，防跨会话状态残留“幻听”
- **轮次机制**：每次 `start` 递增 `ws_generation` 并作废旧 Runtime WS 连接，旧连接迟到结果按代次丢弃，防 start/end 竞态串话
- **事件循环防阻塞**：耗时清理/关闭操作移入后台线程，禁止阻塞内部 WS 事件循环

**FreeSWITCH 打断验证**（2026-09）：`play_and_detect_speech` 播放提示音时，用户开口 1.6 秒内触发 START-OF-INPUT，提示音自动停止，识别结果正常返回（Completion-Cause 000）。

**回声实测结论**（2026-09）：正常外放音量扬声器回声 rms 0.021~0.024，`threshold=0.03` 可过滤；外放音量调大后回声达 0.11+，能量阈值方案无法根治；生产建议两步式 dialplan（播放与检测时间隔离，详见 deployment.md 6.2/6.6）。

### 4.3 对接方消息格式（完整时序示例）

音频格式：**8kHz / 16bit / 单声道 PCM**（base64 编码放入 `chunk` 字段），也支持 16kHz。

一次完整交互的消息时序：

```json
→ start {"sample_rate": 8000}
← started {"message": "识别已开始", "sample_rate": 8000}
→ audio_chunk × N（持续流式发送）

← speech_start {"text": "", "detect_time": 1757822.345, "rms": 0.0234}
← recognition_result {"text": "我要", "is_final": false, "result_latency": null, "server_time": 1757822.5}
← recognition_result {"text": "我要查网", "is_final": false, "result_latency": null, ...}
← recognition_result {"text": "我要查网点", "is_final": true, "result_latency": 3200, "server_time": 1757825.7}
← speech_end {"text": "我要查网点", "duration_ms": 4800, "reason": "final"}

→ end
← finished {"text": "", "duration": 0}
```

**对接要点**：
1. 最终文本以 `is_final: true` 的 `recognition_result` 为准（2pass 离线校正最准），中间结果仅用于实时上屏
2. `speech_end` 后状态机自动复位，无需额外操作即可继续下一轮说话
3. `detect_time` / `server_time` 是 Unix 时间戳（秒，浮点），可用于测量对接方与网关间的网络延迟
4. `rms` 仅 VAD 能量触发时携带（speech_start 的 ASR 兜底触发时无此字段）

**时延字段计算**：
- 推理段 = `result_latency`：语音到达网关 → Runtime 返回文本
- 下发段 = `本地时间 - server_time`：网关发出 → 对接方收到
- 总延迟（收到语音 → 看到文本）= 推理段 + 下发段

## 5. 容错机制

| 场景 | 处理方式 |
|------|----------|
| Runtime 连接断开 | 自动重连（最多 5 次）+ 暂存音频 |
| 推理异常 | 返回 error 给前端 → 后续识别不受影响 |
| License 验证失败 | sys.exit(1) 严格退出，容器 --restart always 自动重启 |
| 会话超时 (15s 无音频) | 自动清理会话 |
| 容器崩溃 | Docker --restart always 自动重启 |
| MRCP 静音无输入 | Gateway no-input 定时器 → `finished` 空文本 → Completion-Cause 002 |
| UniMRCP Server 崩溃/重启 | 呼叫优雅失败（channel error），FreeSWITCH 无需重启自动恢复 |
| Runtime 状态残留（幻听） | 全静音校验：max_rms < stale_text_rms 丢弃残留文本 |
| start/end 竞态（串话） | ws_generation 轮次机制：旧连接结果按代次作废 |
| 事件循环阻塞 | 耗时清理/关闭操作移后台线程执行 |

## 6. 配置参数

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `RUNTIME_WS_URL` | wss://localhost:10095 | Runtime WebSocket 地址 |
| `INTERNAL_WS_PORT` | 5003 | 内部 WS 端口（MRCP 插件接入） |
| `MAX_SESSIONS` | 20 | 最大并发 WebSocket 会话数 |
| `SESSION_TIMEOUT` | 15 | 会话超时秒数（无音频自动清理） |
| `VAD_SPEECH_THRESHOLD` | 0.03 | 能量 VAD 阈值（RMS 0~1，可热调） |
| `VAD_SILENCE_GRACE_MS` | 2000 | 静音兜底 speech_end 时长（毫秒，可热调） |
| `STALE_TEXT_RMS` | 0.005 | 全静音判定阈值（防幻听，可热调） |
| `ADMIN_TOKEN` | asr-vad-debug-9f2c | /admin/vad 管理接口鉴权 token |

**Runtime 参数**（start_merged.sh）：
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--decoder-thread-num` | 2 | 推理线程数（并发上限） |
| `--io-thread-num` | 2 | IO 线程数 |
| `--port` | 10095 | Runtime 监听端口 |

**内存规划**：
```
总内存 = Gateway (~40MB) + Runtime (~1GB) + 系统 (~1GB)
推荐: 4GB+ 内存
```

## 7. 部署架构

```
云服务器 (8.153.92.96)
├── Docker 容器: argosasr-streaming-delivery (单容器合并)
│   ├── 基础镜像: funasr-runtime-sdk-online-cpu-0.1.4
│   ├── 服务代码: /opt/streaming-asr/src/
│   │   ├── stream_server.py    (Gateway)
│   │   ├── asr_license_client.py
│   │   └── stream_test.html
│   ├── 启动脚本: /opt/streaming-asr/start_merged.sh
│   ├── 端口: 5002 (Socket.IO) / 5003 (内部 WS)
│   ├── 内部端口: 10095 (Runtime)
│   └── 挂载:
│       ├── /opt/argosasr/models/iic → 模型 (共享 volume)
│       ├── /opt/streaming-asr/results → 识别结果
│       └── /opt/streaming-asr/audio   → 录音文件
│
├── FreeSWITCH (mod_unimrcp) ── MRCPv2 SIP/TCP:8060 ──→ UniMRCP Server
│   ├── 配置文件: /usr/local/freeswitch/conf/autoload_configs/unimrcp.conf.xml
│   ├── dialplan: /usr/local/freeswitch/conf/dialplan/default.xml (9196 测试分机)
│   └── UniMRCP 插件: /opt/unimrcp-build/UniMRCP-unimrcp-1.8.0/plugins/mrcp-funasr/
│       └── mrcpfunasr.so → 内部 WS 5003 连接容器 Gateway
│
├── 授权服务: http://host.docker.internal:9800
└── 网络配置: --add-host=host.docker.internal:host-gateway
```

## 8. 音频采样率支持

| 采样率 | 来源 | 处理方式 |
|--------|------|----------|
| **16kHz** | 浏览器麦克风 | 直接转发到 Runtime |
| **8kHz** | 呼叫中心电话流 | Gateway 层线性插值重采样到 16kHz |

**呼叫中心对接协议**：
```javascript
// 1. 连接 WebSocket
const socket = io('ws://SERVER:5002');

// 2. 发送 start，指定 sample_rate
socket.emit('start', { sample_rate: 8000 });

// 3. 发送 8kHz PCM 音频 (Base64)
socket.emit('audio_chunk', { chunk: base64_pcm, sample_rate: 8000 });

// 4. 接收识别结果
socket.on('recognition_result', (data) => {
    console.log(data.text);
});
```

## 9. 构建与部署流程

```
Gateway（容器黑盒模式）:
  1. deploy/upload_stream_server.py：SFTP 上传 → docker cp 进容器
  2. 容器内 python3 py_compile 生成 .pyc（黑盒模式优先运行 .pyc）
  3. docker restart → 健康检查 curl /health
UniMRCP 插件（研发调试链路）:
  1. SFTP 上传 mrcp_funasr_engine.c → make && make install
  2. pkill unimrcpserver → nohup 重启
  （正式交付走黑盒镜像 docker/Dockerfile.unimrcp，现场无需编译）
FreeSWITCH 配置:
  1. 修改 conf/autoload_configs/unimrcp.conf.xml、conf/dialplan/default.xml
  2. fs_cli -x 'reloadxml'（或重启 FreeSWITCH）
```

## 10. 性能基准 (4核/15GB 服务器)

### 单容器合并架构（当前）

| 场景 | 平均延迟 | P50 延迟 | RTF | 墙钟时间 |
|------|----------|----------|-----|----------|
| 单路 15s | 960ms | 142ms | 1.334 | 20.0s |
| 单路 30s | 607ms | 188ms | 1.168 | 35.0s |
| 单路 60s | 392ms | 161ms | 1.085 | 65.1s |
| 3路并发 | 1074ms | 353ms | 1.336 | 20.3s |
| 5路并发 | 1097ms | 415ms | 1.334 | 20.3s |
| 10路并发 | 1105ms | 459ms | 1.335 | 20.3s |

**注**：RTF > 1 主要受跨地域网络延迟影响（本地→北京机房 ~30-50ms RTT），同机房部署可降至 0.5 以下。

## 11. 目录结构

```
streaming-asr/
├── src/                    运行时代码（部署到容器内）
│   ├── stream_server.py    Gateway（8kHz 重采样、内部 WS 5003、no-input 定时器、VAD 热参数）
│   ├── asr_license_client.py  授权客户端
│   ├── stream_test.html    前端测试页（含采样率选择器）
│   └── worker.py           Worker Pool 备选方案（ONNX 子进程推理，当前未启用）
├── docker/                 Dockerfile 集合
│   ├── Dockerfile.merged   单容器合并 Dockerfile
│   ├── start_merged.sh     启动脚本（Runtime + Gateway）
│   ├── docker-compose.yml  单服务编排
│   └── mrcp-plugin/        UniMRCP 插件（mrcpfunasr）
│       └── src/mrcp_funasr_engine.c  插件引擎（含 no-input 参数传递）
├── deploy/                 部署脚本
│   └── upload_stream_server.py  Gateway 部署（SFTP + docker cp + pyc 编译 + 重启）
├── tests/                  测试脚本
│   └── perf_cpp_runtime.py C++ Runtime 性能测试
├── docs/                   文档
│   ├── architecture.md     架构文档（本文件）
│   ├── deployment.md       部署手册（含 MRCP 对接、热参数接口）
│   ├── operations.md       运维指南（日志管理、问题分析）
│   ├── agent_spec.md       开发规约手册（智能体操作规范）
│   └── perf_report.md      性能测试报告
├── ops/                    配置 + 监控
│   └── config.py           公共配置（SSH/路径/容器）
└── .env                    服务器凭证（不入库）
```
