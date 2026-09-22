# 流式语音识别 — 产品部署手册

**版本：** V5（VAD 热调接口 + MRCP 稳定性修复 + 运维指南）  
**更新日期：** 2026-09-13

---

## 1. 系统架构

```
FreeSWITCH (mod_unimrcp)                 浏览器/客户端
    │ MRCPv2 (SIP/TCP:8060 + RTP)            │ (SocketIO/WS)
    ▼                                        │
UniMRCP Server (mrcpfunasr 插件)             │
    │ 内部 WS (5003)                          │
    ▼                                        ▼
┌─────────────────────────────────────────────────────────┐
│              argosasr-streaming-delivery（单容器）        │
│                                                         │
│  ┌──────────────┐     localhost:10095    ┌────────────┐ │
│  │   Gateway     │  ──────────────────→  │  C++       │ │
│  │  (Python)     │  ←──────────────────  │  Runtime   │ │
│  │  端口: 5002    │   WSS (内部)           │  端口:10095 │ │
│  └──────────────┘                        └────────────┘ │
│                                                         │
│  对外端口: 5002 (WebSocket) / 5003 (内部 WS, MRCP)      │
│  模型文件: 内嵌于镜像中（无需外挂）                        │
─────────────────────────────────────────────────────────┘
```

- **单容器合并部署**：Gateway（Python Flask-SocketIO）+ Runtime（C++ 推理引擎）运行在同一容器内
- **Gateway**：负责客户端连接管理、License 鉴权、音频转发、会话管理
- **Runtime**：C++ 推理引擎，2pass 模式（流式在线 + 离线校正），返回识别结果
- **MRCP 接入**（可选）：FreeSWITCH → UniMRCP Server → mrcpfunasr 插件 → Gateway 内部 WS 5003
- **黑盒交付**：模型文件已打包进镜像，无需外挂卷，拉取镜像即可运行

---

## 2. 服务器配置要求

### 2.1 最低配置（≤5 并发）

| 配置项 | 规格 |
|--------|------|
| CPU | 4 核 |
| 内存 | 8 GB |
| 磁盘 | 20 GB（镜像约 5GB） |
| 带宽 | 10 Mbps |
| 操作系统 | Ubuntu 20.04+ |

### 2.2 推荐配置（≤20 并发）

| 配置项 | 规格 |
|--------|------|
| CPU | 16 核 |
| 内存 | 16 GB |
| 磁盘 | 50 GB |
| 带宽 | 50 Mbps |
| 操作系统 | Ubuntu 22.04 |

### 2.3 高并发配置（40 并发）

| 配置项 | 规格 |
|--------|------|
| CPU | 48 核+ |
| 内存 | 32 GB |
| 磁盘 | 100 GB |
| 带宽 | 100 Mbps |
| 操作系统 | Ubuntu 22.04 |
| 参考机型 | 阿里云 ecs.g7.12xlarge（48核/192GB） |

### 2.4 配置与并发对照表

| 并发路数 | CPU | 内存 | DECODER_THREAD_NUM | IO_THREAD_NUM |
|----------|-----|------|--------------------|---------------|
| 1-5 | 4 核 | 8 GB | 2 | 2 |
| 6-10 | 8 核 | 16 GB | 4 | 4 |
| 11-20 | 16 核 | 16 GB | 8 | 4 |
| 21-30 | 32 核 | 32 GB | 12 | 8 |
| 31-40 | 48 核 | 32 GB | 16 | 8 |

> **规则：** `DECODER_THREAD_NUM` ≈ 并发路数 / 2.5（向上取整到偶数），`IO_THREAD_NUM` ≈ `DECODER_THREAD_NUM` / 2

---

## 3. 客户快速上手

> 拿到交付包后，按以下步骤操作即可。

### 3.1 交付包内容

**核心包（必交付）**：

| 文件 | 说明 |
|------|------|
| `argosasr-streaming-delivery-v5.7.tar` | Docker 镜像文件（约 8.7GB，模型已内嵌，黑盒交付） |
| `deployment.md` | 本部署手册 |
| `operations.md` | 运维指南（日志管理、问题分析、热调命令） |
| `.env.example` | 环境变量配置样例（客户只改此文件调整参数，不接触代码） |
| License 授权 | 授权文件/授权服务（按商务合同定制，产品运行必需） |

**MRCP 附加包（可选，呼叫中心/IVR 场景）**：

| 文件 | 说明 |
|------|------|
| `mrcp-bridge-v1.0.tar` | UniMRCP Server + mrcpfunasr 插件黑盒镜像（内置配置模板，环境变量接入网关，见 6.7） |
| FreeSWITCH 配置样例 | `unimrcp.conf.xml` + dialplan 示例（9196 两步式，见第 6 章） |
| 提示音文件 | 可选：`silence40.wav`（两步式检测段垫音）、`prompt8k.wav`（调试分机提示音）。IVR barge-in 模式（边播边检）不需要，见 6.2 |
| `mrcp_deploy.md` | MRCP 场景部署说明（deployment.md 第 6 章） |

**建议附赠**：

| 文件 | 说明 |
|------|------|
| `architecture.md` | 架构文档（客户技术对接参考） |
| `perf_report.md` | 性能测试报告（选型/验收参考） |
| `docker-compose.yml` | 单服务编排（简化部署，可选） |

**不交付**：源码（黑盒交付原则）、内部开发文档（agent_spec.md）、内部调试脚本（ops/、deploy/ 系列仅供研发运维使用）。

### 3.2 第一步：确认环境

```bash
# 确认已安装 Docker（需 20.10+）
docker --version

# 确认磁盘空间充足（需 ≥ 15GB 空闲）
df -h /
```

如未安装 Docker：
```bash
curl -fsSL https://get.docker.com | sh
```

### 3.3 第二步：加载镜像

将 tar 文件上传到服务器后执行：
```bash
docker load -i argosasr-streaming-delivery-v5.7.tar
```

加载成功后会显示：
```
Loaded image: argosasr-streaming-delivery:v5.7
```

### 3.4 第三步：启动服务

```bash
docker run -d \
  --name argosasr-streaming-delivery \
  --restart always \
  --privileged \
  --add-host=host.docker.internal:host-gateway \
  -p 5002:5002 \
  -e DECODER_THREAD_NUM=2 \
  -e IO_THREAD_NUM=2 \
  -e MAX_SESSIONS=20 \
  -e SESSION_TIMEOUT=15 \
  argosasr-streaming-delivery:v5.7
```

> **参数说明：**
>
> | 参数 | 说明 |
> |------|------|
> | `-d` | 后台运行 |
> | `--name argosasr-streaming-delivery` | 容器名称 |
> | `--restart always` | 开机自动启动，异常自动重启 |
> | `--privileged` | 特权模式，Runtime 引擎需要 |
> | `--add-host=host.docker.internal:host-gateway` | 让容器能访问宿主机上的 License 授权服务。如果 License 在其他服务器上，将 `host-gateway` 改为实际 IP，如 `192.168.1.100` |
> | `-p 5002:5002` | 端口映射，容器内 5002 端口映射到宿主机 5002 端口 |
> | `-e DECODER_THREAD_NUM=2` | 推理线程数，决定并发能力。5并发用2，10并发用4，20并发用8 |
> | `-e IO_THREAD_NUM=2` | IO 线程数，一般设为推理线程数的一半 |
> | `-e MAX_SESSIONS=20` | 最大并发会话数，建议设为目标并发数的 1.25 倍 |
> | `-e SESSION_TIMEOUT=15` | 会话超时秒数，客户端 15 秒无音频自动断开 |
> | `argosasr-streaming-delivery:v5.7` | 镜像名:版本号 |

### 3.5 第四步：验证

```bash
# 1. 确认容器运行中
docker ps

# 2. 健康检查（应返回 {"status":"ok"}）
curl http://localhost:5002/health

# 3. 浏览器打开测试页
# http://<服务器IP>:5002/
# 点击「开始说话」测试识别功能
```

### 3.6 日常运维

```bash
# 查看日志
docker logs argosasr-streaming-delivery --tail 100

# 重启服务
docker restart argosasr-streaming-delivery

# 停止服务
docker stop argosasr-streaming-delivery

# 查看资源占用
docker stats argosasr-streaming-delivery
```

---

## 4. 环境变量参数

所有参数通过 `docker run -e` 传入容器：

### 4.1 并发与性能

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `DECODER_THREAD_NUM` | `2` | 推理线程数，控制并发上限 |
| `IO_THREAD_NUM` | `2` | IO 线程数 |
| `MAX_SESSIONS` | `20` | 最大并发会话数 |
| `SESSION_TIMEOUT` | `15` | 会话超时秒数（无音频后自动清理） |

### 4.2 License 授权

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `LICENSE_SERVER_IP` | `host.docker.internal` | License 服务器 IP |
| `LICENSE_SERVER_PORT` | `9800` | License 服务端口 |

> 如果 License 服务与 Docker 不在同一台服务器，需同时修改 `--add-host` 和 `LICENSE_SERVER_IP`：
> ```bash
> --add-host=host.docker.internal:192.168.1.100 \
> -e LICENSE_SERVER_IP=host.docker.internal
> ```

### 4.3 端口

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `SERVICE_PORT` | `5002` | 对外服务端口（需与 `-p` 映射一致） |
| `RUNTIME_PORT` | `10095` | Runtime 内部端口（无需修改） |

### 4.4 语音活动检测（VAD，MRCP 场景）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `VAD_SPEECH_THRESHOLD` | `0.03` | 网关能量 VAD 阈值（RMS 0~1），超过视为说话 |
| `VAD_SILENCE_GRACE_MS` | `2000` | speech_start 后持续静音多久触发 speech_end（毫秒） |
| `STALE_TEXT_RMS` | `0.005` | 全静音判定阈值：整段音频 max_rms 低于此值丢弃识别文本（防幻听） |
| `ADMIN_TOKEN` | `asr-vad-debug-9f2c` | `/admin/vad` 管理接口鉴权 token |

> 以上参数均可运行中热调（见 6.5 节），无需重启容器。

---

## 5. 网络与安全

### 5.1 端口说明

| 端口 | 服务 | 是否对外 | 说明 |
|------|------|----------|------|
| 5002 | Gateway | 是 | 客户端 WebSocket 连接 |
| 5003 | Gateway 内部 WS | 否 | UniMRCP 插件接入（MRCP 场景） |
| 10095 | Runtime | 否 | 容器内本地，Gateway 调用 |

### 5.2 安全组规则

仅需开放 **5002 端口**（TCP）：

| 方向 | 协议 | 端口 | 来源 |
|------|------|------|------|
| 入站 | TCP | 5002 | 0.0.0.0/0 |

---

## 6. MRCP 对接（FreeSWITCH 呼叫中心）

可选功能：呼叫中心（FreeSWITCH/IVR 平台）无需改造，通过标准 **MRCPv2 协议**接入 ASR。

### 6.1 组件清单

| 组件 | 说明 |
|------|------|
| FreeSWITCH + mod_unimrcp | MRCPv2 客户端，`play_and_detect_speech` 触发识别 |
| UniMRCP Server (1.8.0) + mrcpfunasr 插件（黑盒镜像 `mrcp-bridge`） | MRCPv2 服务端，将 RECOGNIZE 转接到 Gateway 内部 WS 5003 |
| Gateway（本容器） | 内部 WS 5003 端口接收插件接入 |

### 6.2 关键配置

1. **FreeSWITCH** `conf/autoload_configs/unimrcp.conf.xml`：`default-asr-profile` 指向 MRCP profile，`server-ip`/`server-port` 指向 UniMRCP Server（默认 8060）
2. **dialplan 示例**：
   - **9196（两步式，生产推荐）**：先 `playback` 播放提示音，播完再进入检测，提示音回声不会进入识别（详见 6.6）：
     ```xml
     <extension name="mrcp_asr_test">
       <condition field="destination_number" expression="^9196$">
         <action application="answer"/>
         <action application="playback" data="/tmp/prompt8k.wav"/>
         <action application="play_and_detect_speech"
                 data="/tmp/silence40.wav detect:unimrcp {start-input-timers=true,no-input-timeout=30000,recognition-timeout=30000}builtin:speech/transcribe"/>
         <action application="log" data="INFO ======ASR-RESULT: ${detect_speech_result}"/>
         <action application="hangup"/>
       </condition>
     </extension>
     ```
   - **9195（单步打断测试）**：`play_and_detect_speech` 播放提示音的同时检测（barge-in 打断测试用），外放音量较大时提示音回声可能被识别。
   > 语法要点：`<提示音> detect:<engine> {参数}grammar`，参数在 engine 与 grammar 之间，grammar 用 `builtin:speech/transcribe`
   - **IVR 应用接入（barge-in 边播边检）**：IVR 通过 ESL/Lua 调 `play_and_detect_speech` 时，播放文件直接传 **IVR 自己的提示音**（必须是真实 WAV，禁止 `silence_stream://100` 等虚拟流，否则检测立即失败）。本包两个提示音文件**均不需要**：
     - `prompt8k.wav`：仅为 9196 调试分机的测试提示音，IVR 用自己的提示音
     - `silence40.wav`：仅两步式"检测段"静音垫音；若客户现场存在外放场景导致 barge-in 误打断（见 6.6），切换到两步式时会用到
3. **UniMRCP Server**：黑盒镜像部署（见 6.7），`GW_HOST`/`GW_PORT` 环境变量指定 Gateway 内部 WS 地址

### 6.3 识别结果

- 识别完成：`detect_speech_result` 含 Completion-Cause `000 success` + NLSML 识别文本
- 静音无输入：`no-input-timeout` 秒后返回 Completion-Cause `002 no-input-timeout`
- 语音打断（barge-in）：用户开口即触发 START-OF-INPUT，提示音自动停止

### 6.4 验证结果（2026-09）

| 测试项 | 结果 |
|--------|------|
| 正常语音识别（8kHz 电话流） | 通过：Completion-Cause 000，文本「我要查询话费余额」 |
| 语音打断（1.6s barge-in） | 通过：START-OF-INPUT，提示音停止 |
| 静音 no-input（15s） | 通过：Completion-Cause 002，精确 15 秒触发 |
| 2 路并发 | 通过 |
| UniMRCP Server 崩溃恢复 | 通过：重启后无需重启 FreeSWITCH 自动恢复 |

### 6.5 VAD 热参数接口（无需重启）

语音打断（barge-in）依赖网关能量 VAD + 识别兜底两层触发。以下参数可运行中热调：

| 参数 | 默认值 | 取值范围 | 作用 |
|------|--------|----------|------|
| `threshold` | `0.03` | `(0, 1)` | RMS 能量阈值，超过视为说话 |
| `silence_grace_ms` | `2000` | `[100, 60000]` | speech_start 后持续静音多久触发 speech_end |
| `stale_text_rms` | `0.005` | `(0, 0.1)` | 整段音频 max_rms 低于此值 → 丢弃识别文本（防幻听） |

查询当前值（需携带鉴权头）：

```bash
curl -H 'X-Admin-Token: asr-vad-debug-9f2c' http://localhost:5002/admin/vad
```

热调（Linux/bash）：

```bash
curl -X POST \
  -H 'X-Admin-Token: asr-vad-debug-9f2c' \
  -H 'Content-Type: application/json' \
  -d '{"threshold": 0.04}' \
  http://localhost:5002/admin/vad
```

> 注意：5002 为 Gateway 对外端口；管理接口仅限内网访问，生产环境务必通过 `ADMIN_TOKEN` 环境变量更换 token。

### 6.6 回声与打断实测结论（2026-09）

| 场景 | 实测 RMS | 结论 |
|------|----------|------|
| 正常外放音量扬声器回声 | 0.021 ~ 0.024 | `threshold=0.03` 可过滤 |
| 外放音量调大后回声 | 0.11+ | 回声能量随音量任意增大，**能量阈值无法根治** |
| 正常说话 | 0.025 ~ 0.063 | 不受 0.03 阈值影响 |

- 提示音回声被 Paraformer 识别出内容时，识别兜底层（无能量门槛）仍会触发 speech_start——单步 `play_and_detect_speech`（播放与检测同时）在外放音量大的场景下必然误打断。
- **生产建议使用两步式 dialplan（6.2）**：播放提示音与检测在时间上隔离，回声零风险。
- 网关已内置两道防线：`stale_text_rms` 全静音校验（丢弃无真实语音时的残留文本）+ `ws_generation` 轮次机制（旧连接结果按代次作废），防止跨会话状态残留。

### 6.7 UniMRCP Server 容器化部署（黑盒镜像）

UniMRCP Server + mrcpfunasr 插件以黑盒镜像交付（`mrcp-bridge:v1.0`），现场无需编译：

```bash
docker load -i mrcp-bridge-v1.0.tar

# 推荐 host 网络（免 RTP UDP 端口映射；FreeSWITCH 在宿主机）
docker run -d \
  --name mrcp-bridge \
  --restart always \
  --network host \
  -e GW_HOST=127.0.0.1 \
  -e GW_PORT=5003 \
  -e SERVER_IP=<本机IP> \
  -v /opt/mrcp-bridge/log:/usr/local/unimrcp/log \
  -v /opt/mrcp-bridge/var:/usr/local/unimrcp/var \
  mrcp-bridge:v1.0
```

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `GW_HOST` | 127.0.0.1 | ASR 网关地址（插件内部 WS 连接目标；同机部署填 127.0.0.1，异机填网关服务器 IP） |
| `GW_PORT` | 5003 | ASR 网关内部 WS 端口 |
| `SERVER_IP` | 空 | MRCP 服务端 IP（sip-uas server-ip），与 FreeSWITCH `unimrcp.conf.xml` 的 server-ip 对应 |

**部署要点**：
- **host 网络模式**：SIP/TCP 8060 与 RTP UDP 端口无需映射，FreeSWITCH 按本机访问；bridge 模式需 `-p 8060:8060` 并固定 RTP 端口范围映射大段 UDP，运维繁琐，不推荐
- **日志与 utter 音频必须挂卷持久化**（排障关键证据）
- 同机部署时网关容器需 `-p 5003:5003` 映射内部 WS 端口供插件连接

---

## 7. 性能调优

### 7.1 延迟优化

| 优化项 | 效果 | 操作 |
|--------|------|------|
| 同地域部署 | RTF 降至 0.8 以下 | 服务器与客户端在同一地域 |
| 增大线程数 | 提升并发能力 | 调整 `DECODER_THREAD_NUM` |
| 减小 chunk 间隔 | 降低首包延迟 | 前端 chunk_ms 从 600 降至 300 |

### 7.2 资源监控

```bash
# 实时查看容器资源
docker stats argosasr-streaming-delivery

# 查看容器日志
docker logs argosasr-streaming-delivery --tail 50
```

---

## 8. 故障排查

### 8.1 无识别结果

| 可能原因 | 排查方法 | 解决方案 |
|----------|----------|----------|
| Runtime 未启动 | `docker ps` 查看状态 | `docker restart argosasr-streaming-delivery` |
| 内存不足 | `docker stats argosasr-streaming-delivery` | 增加服务器内存 |
| License 未授权 | `docker logs argosasr-streaming-delivery \| grep License` | 检查授权服务 |

### 8.2 连接失败

| 可能原因 | 排查方法 | 解决方案 |
|----------|----------|----------|
| Gateway 未启动 | `docker ps` | `docker restart argosasr-streaming-delivery` |
| 端口未开放 | `curl http://localhost:5002/health` | 检查安全组规则 |
| License 不通 | `docker exec argosasr-streaming-delivery bash -c '(echo > /dev/tcp/host.docker.internal/9800) 2>/dev/null && echo OK'` | 检查 `--add-host` 和授权服务 |

### 8.3 并发性能差

| 可能原因 | 排查方法 | 解决方案 |
|----------|----------|----------|
| 线程数不足 | `docker logs argosasr-streaming-delivery \| grep thread` | 增大 `DECODER_THREAD_NUM` |
| CPU 瓶颈 | `docker stats argosasr-streaming-delivery` | 升级 CPU 核数 |
| MAX_SESSIONS 限制 | 检查环境变量 | 增大 `MAX_SESSIONS` |

### 8.4 MRCP 场景（FreeSWITCH 呼叫中心）

| 症状 | 排查方法 | 解决方案 |
|------|----------|----------|
| 每次呼叫提前挂断/打断 | 查网关日志 `speech_start (VAD)` 的 rms 值（< 0.025 即回声误触发） | 热调 `threshold` ≥ 0.03，或改用两步式 dialplan（6.2） |
| 静音却识别出提示音文本 | 查网关日志确认整段 max_rms 接近 0 | `stale_text_rms` 已默认拦截（V5 修复）；确认该参数未被热调改小 |
| 识别结果混入上一通内容 | 网关日志出现旧会话文本 | 已由 `ws_generation` 轮次机制拦截（V5 修复）；升级网关代码 |
| 识别结果延迟明显 | 查 UniMRCP 日志 utterance 处理时间 | 检查 `DECODER_THREAD_NUM` 与 CPU 占用 |

> 详细的日志管理与问题分析方法见 **《运维指南》docs/operations.md**。

---

## 9. 客户现场部署清单

### 9.1 部署步骤

```bash
# 1. 加载镜像
docker load -i argosasr-streaming-delivery.tar

# 2. 启动服务（根据并发需求调整参数）
docker run -d \
  --name argosasr-streaming-delivery \
  --restart always \
  --privileged \
  --add-host=host.docker.internal:host-gateway \
  -p 5002:5002 \
  -e DECODER_THREAD_NUM=2 \
  -e IO_THREAD_NUM=2 \
  -e MAX_SESSIONS=20 \
  -e SESSION_TIMEOUT=15 \
  argosasr-streaming-delivery:v5.7

# 3. 验证
curl http://localhost:5002/health
```

### 9.2 并发参数对照表

| 目标并发 | DECODER_THREAD_NUM | IO_THREAD_NUM | MAX_SESSIONS |
|----------|--------------------|---------------|--------------|
| 1-5 | 2 | 2 | 10 |
| 6-10 | 4 | 2 | 15 |
| 11-20 | 8 | 4 | 25 |
| 21-30 | 12 | 6 | 40 |
| 31-40 | 16 | 8 | 50 |

> **规则：** `DECODER_THREAD_NUM` ≈ 目标并发数 / 2.5（向上取整到偶数），`IO_THREAD_NUM` ≈ DECODER / 2，`MAX_SESSIONS` = 并发数 × 1.25

### 9.3 部署示例（20 并发）

```bash
docker run -d \
  --name argosasr-streaming-delivery \
  --restart always \
  --privileged \
  --add-host=host.docker.internal:host-gateway \
  -p 5002:5002 \
  -e DECODER_THREAD_NUM=8 \
  -e IO_THREAD_NUM=4 \
  -e MAX_SESSIONS=25 \
  -e SESSION_TIMEOUT=15 \
  argosasr-streaming-delivery:v5.7
```

### 9.4 部署检查清单

部署完成后逐项确认：

- [ ] Docker 已安装（`docker --version`）
- [ ] 镜像已加载（`docker images | grep argosasr-streaming-delivery`）
- [ ] License 授权服务运行中（`curl http://localhost:9800/`）
- [ ] 安全组已开放 5002 端口
- [ ] 容器启动正常（`docker ps` 显示 argosasr-streaming-delivery）
- [ ] 健康检查通过（`curl http://localhost:5002/health`）
- [ ] 浏览器可访问（`http://<服务器IP>:5002/`）
- [ ] 语音识别功能正常（前端测试页录音测试）
- [ ] 启动日志确认参数生效（`docker logs argosasr-streaming-delivery`）

---

## 10. Docker 管理平台部署

如果使用 Portainer / Rancher 等 Docker 管理平台，无需命令行，直接在 UI 中配置：

### 10.1 创建容器

| 配置项 | 值 |
|--------|-----|
| 镜像 | `argosasr-streaming-delivery:v5.7` |
| 容器名 | `argosasr-streaming-delivery` |
| 端口映射 | `5002:5002` |
| 特权模式 | 开启 |

### 10.2 环境变量

| 变量 | 值 | 说明 |
|------|-----|------|
| `DECODER_THREAD_NUM` | `2` | 推理线程数 |
| `IO_THREAD_NUM` | `2` | IO 线程数 |
| `MAX_SESSIONS` | `20` | 最大并发 |
| `SESSION_TIMEOUT` | `15` | 超时秒数 |
| `LICENSE_SERVER_IP` | `host.docker.internal` | License 服务器 IP |
| `LICENSE_SERVER_PORT` | `9800` | License 端口 |

### 10.3 额外配置

| 配置项 | 值 |
|--------|-----|
| Extra Hosts | `host.docker.internal:host-gateway` |

> 如果 License 在不同服务器：`host.docker.internal:192.168.1.100`

---

## 11. 版本信息

| 组件 | 版本 |
|------|------|
| FunASR Runtime SDK | 0.1.4 (online-cpu) |
| 交付镜像 | `argosasr-streaming-delivery:v5.7`（约 8.7GB） |
| 模型 | paraformer-large-onnx (INT8)，已内嵌 |
| 识别模式 | 2pass（流式 + 离线校正） |
| 采样率 | 16kHz（浏览器麦克风）/ 8kHz（呼叫中心电话流） |
| MRCP 对接 | FreeSWITCH mod_unimrcp + UniMRCP 1.8.0 + mrcpfunasr 插件（内部 WS 5003） |
| VAD 热参数 | threshold / silence_grace_ms / stale_text_rms，POST `/admin/vad` 热调 |
