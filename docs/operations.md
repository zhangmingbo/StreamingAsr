# 流式 ASR 网关 + MRCP 链路运维指南

**适用版本：** Gateway V5（2026-09-13）
**配套文档：** [deployment.md](deployment.md)（部署手册）

本指南面向现场运维与研发排查，覆盖三端：

```
FreeSWITCH (MRCP 客户端) ──→ UniMRCP Server (mrcpfunasr 插件) ──→ argosasr-streaming-delivery 容器（网关 + Runtime）
```

---

## 1. 日志体系总览

| 组件 | 日志位置 | 内容 |
|------|----------|------|
| 网关容器（Gateway） | `docker logs argosasr-streaming-delivery` | License、会话、speech_start/end、音频 chunk、热参数 |
| 网关容器（持久文件） | 宿主机 `/var/lib/docker/containers/99f74e*/*-json.log` | 容器 stdout 的 json.log（`docker logs` 的数据源） |
| FreeSWITCH | `/usr/local/freeswitch/log/freeswitch.log` | 呼叫、dialplan、MRCP 信令、detect_speech_result |
| UniMRCP 插件 | `/usr/local/unimrcp/log/unimrcp.log` | MRCP 消息流、插件内部状态、网关 WS 连接 |
| 识别音频落盘 | `/usr/local/unimrcp/var/` | 每轮识别的 utter 音频文件（回放复现用） |

---

## 2. 日志管理

### 2.1 网关容器日志

```bash
# 实时跟踪
docker logs -f argosasr-streaming-delivery

# 查看最近 200 行
docker logs --tail 200 argosasr-streaming-delivery

# 带时间戳
docker logs -t argosasr-streaming-delivery
```

**时区注意：** `docker logs -t` 输出的是 **UTC 时间**，比北京时间（CST）慢 8 小时。与 FreeSWITCH / UniMRCP 日志（CST）对照时，网关时间需 **+8 小时**。

**大日志排查：** 容器运行久了 json.log 会很大，直接访问宿主机 json.log 配合 grep 更高效：

```bash
# 最近的 speech_start / speech_end（json.log 每行一条记录）
grep "speech_start\|speech_end" /var/lib/docker/containers/99f74e*/*-json.log | tail -50

# 抓某个会话的全部日志（sid 为 mrcp- 开头的 ID）
grep "mrcp-bb5fe35f" /var/lib/docker/containers/99f74e*/*-json.log
```

> json.log 由 Docker 自动轮转（默认 10MB 一个文件，保留数个），文件名形如 `*-json.log`、`*-json.log.1`。容器 ID 前缀 `99f74e` 用 `docker ps` 确认。长期运行建议启动时加 `--log-opt max-size=50m --log-opt max-file=5` 控制磁盘占用。

### 2.2 FreeSWITCH 日志

```bash
# 实时控制台（/log 7 打开 DEBUG，/log 0 关闭）
fs_cli

# 文件日志
tail -f /usr/local/freeswitch/log/freeswitch.log

# 只抓 MRCP 相关
grep -i "mrcp\|detect_speech" /usr/local/freeswitch/log/freeswitch.log | tail -50
```

关键日志行：
- `======ASR-RESULT: ...` —— dialplan 打出的识别结果（文本 + Completion-Cause）
- `START-OF-INPUT` —— 语音打断触发（提示音停止）
- `RECOGNITION-COMPLETE` —— 识别完成返回
- `no-input-timeout` —— 静音超时（Completion-Cause 002）

### 2.3 UniMRCP 插件日志

```bash
tail -f /usr/local/unimrcp/log/unimrcp.log
```

- MRCP 消息流：RECOGNIZE → START-OF-INPUT → RECOGNITION-COMPLETE
- 插件与网关内部 WS（5003）的连接建立/断开状态
- 识别音频落盘路径（utter 文件）：`/usr/local/unimrcp/var/` 下，可用 sox / Audacity 回放复现问题

### 2.4 日志轮转与磁盘

| 日志 | 轮转方式 | 现状 |
|------|----------|------|
| 网关 json.log | Docker 自动轮转 | 启动加 `--log-opt max-size=50m --log-opt max-file=5` |
| freeswitch.log / unimrcp.log | logrotate（已部署） | `/etc/logrotate.d/asr-services`：50MB 触发、保留 7 天、copytruncate + 压缩 |
| utter 音频文件 | cron 每日清理（已部署） | `/etc/cron.daily/clean-unimrcp-utter`：删除 7 天前文件 |

**已部署的 logrotate 配置**（`/etc/logrotate.d/asr-services`）：

```conf
# UniMRCP Server stdout 日志（logger.xml CONSOLE 输出重定向到 /tmp/unimrcp.log）
/tmp/unimrcp.log {
    daily
    rotate 7
    maxsize 50M
    missingok
    notifempty
    copytruncate
    compress
    delaycompress
}

# FreeSWITCH 主日志
/usr/local/freeswitch/log/freeswitch.log {
    daily
    rotate 7
    maxsize 50M
    missingok
    notifempty
    copytruncate
    compress
    delaycompress
}
```

**要点**：
- 运行中进程持有日志 fd，必须用 `copytruncate`（默认 rename 会让新日志继续写旧 inode，轮转后日志“消失”）
- utter 清理任务：`find /usr/local/unimrcp/var/ -type f -mtime +7 -delete`，cron.daily 每日执行
- **历史教训（磁盘打满）**：unimrcpserver 控制台线程读 stdin 为 /dev/null 时，read 立即返回被当作命令输入 → 100% CPU 忙循环 + 疯狂刷 "unknown command" 日志撑爆磁盘。启动必须保持 stdin 阻塞管道（`tail -f /dev/null \| unimrcpserver`）；容器版入口脚本已内置该防护（`< <(tail -f /dev/null)`）

---

## 3. 问题分析方法

### 3.1 一次呼叫的完整时间线（三端对照法）

一次 MRCP 呼叫依次经过三端，按时间线串联即可定位问题环节：

1. **FreeSWITCH 日志**：呼叫建立 → dialplan 执行（answer / playback / play_and_detect_speech）→ 收到 START-OF-INPUT → 收到 RECOGNITION-COMPLETE（文本 + Completion-Cause）
2. **UniMRCP 日志**：收到 RECOGNIZE → 建立/复用网关 WS 连接 → 转发音频 → 收到 speech_start/end → 返回结果
3. **网关日志**（同一会话 ID，`mrcp-` 开头）：
   - `Internal WS channel started` —— 插件接入
   - `chunk 0 / chunk 1 / chunk 2 ...` —— 音频帧开始到达。**注意：只打印前 3 帧，之后不再打印，不是断流**
   - `speech_start (VAD) ... rms=0.03xx` —— 能量层触发
   - `speech_start (ASR) ... text=...` —— 识别兜底触发
   - `speech_end (final/silence) ...` —— 本轮结束、状态机复位
   - `finished` 事件 —— 最终结果返回插件

**时区换算：** FS / 插件日志为 CST（北京时间），网关 `docker logs -t` 为 UTC，对照时网关时间 +8 小时。

### 3.2 判断触发源：VAD 还是识别兜底

`speech_start` 日志行的括号标明来源：

| 日志 | 含义 | 说明 |
|------|------|------|
| `speech_start (VAD) rms=0.0452` | 能量层触发 | 音频 RMS ≥ threshold，约 100ms 延迟 |
| `speech_start (ASR) text=...` | 识别兜底触发 | VAD 漏检但 Runtime 已识别出文本（**无能量门槛**） |

排查要点：
- 误打断且为 `(VAD)` 且 rms < 0.025 → 扬声器回声 → 热调 threshold ≥ 0.03，或改用两步式 dialplan
- 误打断且为 `(ASR)` 且文本是提示音内容（如"您好欢迎使用语音识别测试系统"）→ 回声被识别器识别，能量阈值无效 → 只能两步式 dialplan（播放与检测时间隔离）
- 每轮只触发一次 speech_start（`speech_detected` 标志防重），speech_end 后复位可检测下一轮

### 3.3 抓取 rms 序列

```bash
# 从 json.log 提取所有 speech_start (VAD) 的 rms
grep "speech_start (VAD)" /var/lib/docker/containers/99f74e*/*-json.log | tail -30
```

实测参考值（2026-09 真机外放）：

| 信号 | RMS 范围 |
|------|----------|
| 正常外放音量扬声器回声 | 0.021 ~ 0.024 |
| 外放音量调大后回声 | 0.11+ |
| 正常说话 | 0.025 ~ 0.063 |
| 全静音 | < 0.005 |

### 3.4 热参数调优（无需重启）

管理接口：`GET/POST http://<服务器>:5002/admin/vad`，需携带 `X-Admin-Token` 请求头（默认 `asr-vad-debug-9f2c`，可用环境变量 `ADMIN_TOKEN` 更换）。

| 参数 | 默认 | 范围 | 作用 |
|------|------|------|------|
| `threshold` | 0.03 | (0, 1) | 能量 VAD 阈值：调高过滤回声，调低更灵敏 |
| `silence_grace_ms` | 2000 | [100, 60000] | speech_start 后持续静音多久判定 speech_end |
| `stale_text_rms` | 0.005 | (0, 0.1) | 整段音频 max_rms 低于此值丢弃文本（防幻听） |

Linux / bash：

```bash
# 查询当前值
curl -s -H 'X-Admin-Token: asr-vad-debug-9f2c' http://localhost:5002/admin/vad

# 热调阈值
curl -s -X POST -H 'X-Admin-Token: asr-vad-debug-9f2c' \
  -H 'Content-Type: application/json' \
  -d '{"threshold": 0.04}' http://localhost:5002/admin/vad
```

Windows PowerShell（注意：`curl` 是 Invoke-WebRequest 别名，必须用 `curl.exe`；JSON 引号需转义）：

```powershell
curl.exe -X POST -H "X-Admin-Token: asr-vad-debug-9f2c" -H "Content-Type: application/json" -d '{\"threshold\": 0.04}' http://8.153.92.96:5002/admin/vad
```

调优经验：
- 回声误触发（rms 0.021~0.024）：threshold 调 0.03
- 外放音量大会产生 0.11+ 回声：能量阈值无效，改用两步式 dialplan
- 轻声说话漏检：不要一味调高 threshold，识别兜底层会补上（表现为 `speech_start (ASR)`）

### 3.5 常见故障排查表

| 症状 | 判断方法 | 处置 |
|------|----------|------|
| 每次呼叫提前挂断 | 网关日志见 `speech_start (VAD) rms≈0.02x` | 回声误触发 → 热调 threshold ≥ 0.03；外放大音量场景改两步式 dialplan |
| 静音却返回提示音全文 | 网关日志 `speech_start (ASR)` 文本为提示音内容且无真实语音 | 已由 stale_text_rms 拦截（V5 修复）；确认该参数 ≥ 0.005 |
| 网关日志只有 3 帧音频 | `chunk 0/1/2` 后无输出 | 正常设计（只打印前 3 帧），用 utter 文件确认音频完整 |
| 识别结果混入上一通内容 | 日志出现旧会话文本 | 已由 ws_generation 轮次机制 + 全静音校验拦截（V5 修复）；升级网关代码 |
| 热调不生效 | GET /admin/vad 看当前值 | 确认带 X-Admin-Token；参数名 threshold 勿拼错；改完立即生效 |
| 热调接口 403 | 未带 token 或 token 错误 | 带 X-Admin-Token 请求头；忘记 token 查 ADMIN_TOKEN 环境变量 |
| FS 正常但插件无响应 | unimrcp.log 看 WS 连接状态 | 确认网关 5003 端口可达（内网）、容器运行中 |
| 三端日志时间对不上 | 时区不一致 | 网关 docker logs 为 UTC，+8 小时换算成北京时间 |

---

## 4. 网关代码更新部署流程（研发）

本地改完 `src/stream_server.py` 后，热更新到服务器（无需重建镜像）：

1. **上传**：sftp 上传 `src/stream_server.py` 至服务器 `/tmp/stream_server_new.py`
2. **拷入容器**：
   ```bash
   docker cp /tmp/stream_server_new.py argosasr-streaming-delivery:/opt/streaming-asr/src/stream_server.py
   ```
3. **编译**（启动脚本优先加载 .pyc）：
   ```bash
   docker exec argosasr-streaming-delivery python3 /opt/streaming-asr/src/_compile_gw.py
   ```
4. **重启**：
   ```bash
   docker restart argosasr-streaming-delivery
   ```
5. **验证**：
   ```bash
   curl http://localhost:5002/health
   curl -s -H 'X-Admin-Token: asr-vad-debug-9f2c' http://localhost:5002/admin/vad
   ```

> **回滚：** 更新前保留上一版 `stream_server.py.bak`，出问题后 `docker cp` 回旧文件 + 重编译 + 重启即可。

---

## 5. 快速参考

```bash
# 三端日志
docker logs -t --tail 200 argosasr-streaming-delivery          # 网关（UTC）
tail -100 /usr/local/freeswitch/log/freeswitch.log              # FS（CST）
tail -100 /usr/local/unimrcp/log/unimrcp.log                    # 插件（CST）

# 会话追踪（把 ID 换成实际 mrcp-xxx）
grep "mrcp-xxxxx" /var/lib/docker/containers/99f74e*/*-json.log

# 热参数
curl -s -H 'X-Admin-Token: asr-vad-debug-9f2c' http://localhost:5002/admin/vad

# 健康检查
curl http://localhost:5002/health
```
