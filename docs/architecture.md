# FunASR 流式语音识别服务 — 架构文档

## 1. 系统概览

基于 FunASR C++ Runtime 的流式语音识别服务，采用 **单容器合并架构**（Gateway + C++ Runtime），通过 WebSocket 接收实时音频并返回识别结果。支持 **16kHz 浏览器麦克风** 和 **8kHz 呼叫中心电话流** 两种采样率。

```
浏览器/呼叫中心
    │ WebSocket (Socket.IO)
    ▼
┌─────────────────────────────────────────────────────────┐
│              单容器 argosasr-streaming-delivery (Docker)                  │
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
│  端口: 5002 (Gateway) / 10095 (Runtime, 内部)          │
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

**会话管理**：
```python
sessions[sid] = {
    "last_time": float,         # 最后活跃时间
    "runtime_ws": WebSocketApp, # 到 Runtime 的 WS 连接
    "ws_ready": Event,          # Runtime WS 是否就绪
    "pending_audio": list,      # WS 未就绪时暂存音频
    "sample_rate": int,         # 客户端采样率 (8000/16000)
    "speech_detected": bool,    # 本轮是否已触发 speech_start
    "speech_start_time": float, # 首次检测到语音的时间戳
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

| 事件 | 方向 | 触发时机 | payload |
|------|------|---------|--------|
| `connected` | S→C | 连接成功 | `{message: "连接成功"}` |
| `started` | S→C | 会话启动 | `{message, sample_rate}` |
| `speech_start` | S→C | 首个非空识别结果到达 | `{text: "首个文字"}` |
| `recognition_result` | S→C | 每次识别结果 | `{text, is_final}` |
| `speech_end` | S→C | is_final=True | `{text, duration_ms}` |
| `finished` | S→C | 会话结束 | `{text, duration}` |
| `error` | S→C | 异常 | `{message}` |

### 4.2 IVR 打断（Barge-in）对接

ASR 服务提供 `speech_start` / `speech_end` 事件供 IVR 平台实现语音打断：

```
IVR TTS 播报中
    │
    ├─ 收到 speech_start → 停止 TTS 播放
    ├─ 收到 recognition_result (is_final=false) → 缓存中间结果
    ├─ 收到 speech_end → 准备执行意图
    └─ 收到 recognition_result (is_final=true) → 意图检测 → 执行动作
```

**职责分离**：ASR 负责检测用户开口（speech_start）和返回识别文本，意图理解由 IVR 侧完成。

## 5. 容错机制

| 场景 | 处理方式 |
|------|----------|
| Runtime 连接断开 | 自动重连（最多 5 次）+ 暂存音频 |
| 推理异常 | 返回 error 给前端 → 后续识别不受影响 |
| License 验证失败 | sys.exit(1) 严格退出，容器 --restart always 自动重启 |
| 会话超时 (15s 无音频) | 自动清理会话 |
| 容器崩溃 | Docker --restart always 自动重启 |

## 6. 配置参数

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `RUNTIME_WS_URL` | wss://localhost:10095 | Runtime WebSocket 地址 |
| `MAX_SESSIONS` | 20 | 最大并发 WebSocket 会话数 |
| `SESSION_TIMEOUT` | 15 | 会话超时秒数（无音频自动清理） |

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
│   ├── 端口: 5002 (Gateway)
│   ├── 内部端口: 10095 (Runtime)
│   └── 挂载:
│       ├── /opt/argosasr/models/iic → 模型 (共享 volume)
│       ├── /opt/streaming-asr/results → 识别结果
│       └── /opt/streaming-asr/audio   → 录音文件
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
socket.emit('audio', { audio: base64_pcm, is_final: false });

// 4. 接收识别结果
socket.on('recognition_result', (data) => {
    console.log(data.text);
});
```

## 9. 构建与部署流程

```
本地修改 → 上传到服务器:
  1. SFTP 上传变更文件到 /tmp/
  2. SSH docker cp 到容器内
  3. docker restart
  4. 等待 90s (Runtime 启动 + 模型加载)
  5. 健康检查 curl /health
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
│   ├── stream_server.py    Gateway（含 8kHz 重采样）
│   ├── asr_license_client.py  授权客户端
│   └── stream_test.html    前端测试页（含采样率选择器）
├── docker/                 Dockerfile 集合
│   ├── Dockerfile.merged   单容器合并 Dockerfile
│   ├── start_merged.sh     启动脚本（Runtime + Gateway）
│   └── docker-compose.yml  单服务编排
├── tests/                  测试脚本
│   └── perf_cpp_runtime.py C++ Runtime 性能测试
├── docs/                   文档
│   ├── architecture.md     架构文档（本文件）
│   └── perf_report.md      性能测试报告
├── ops/                    配置 + 监控
│   └── config.py           公共配置（SSH/路径/容器）
└── .env                    服务器凭证（不入库）
```
