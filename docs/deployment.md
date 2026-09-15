# 流式语音识别 — 产品部署手册

**版本：** V3（C++ Runtime 2pass，黑盒交付版）  
**更新日期：** 2026-09-14

---

## 1. 系统架构

```
┌─────────────────────────────────────────────────────────┐
│              argosasr-streaming-delivery（单容器）        │
│                                                         │
│  ┌──────────────┐     localhost:10095    ┌────────────┐ │
│  │   Gateway     │  ──────────────────→  │  C++       │ │
│  │  (Python)     │  ←──────────────────  │  Runtime   │ │
│  │  端口: 5002    │   WSS (内部)           │  端口:10095 │ │
│  └──────────────┘                        └────────────┘ │
│                                                         │
│  对外端口: 5002 (WebSocket)                              │
│  模型文件: 内嵌于镜像中（无需外挂）                        │
─────────────────────────────────────────────────────────┘
         ↑
    浏览器/客户端 (SocketIO/WS)
```

- **单容器合并部署**：Gateway（Python Flask-SocketIO）+ Runtime（C++ 推理引擎）运行在同一容器内
- **Gateway**：负责客户端连接管理、License 鉴权、音频转发、会话管理
- **Runtime**：C++ 推理引擎，2pass 模式（流式在线 + 离线校正），返回识别结果
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

| 文件 | 说明 |
|------|------|
| `argosasr-streaming-delivery-v5.7.tar` | Docker 镜像文件（约 8.7GB） |
| `deployment.md` | 本部署手册 |

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

---

## 5. 网络与安全

### 5.1 端口说明

| 端口 | 服务 | 是否对外 | 说明 |
|------|------|----------|------|
| 5002 | Gateway | 是 | 客户端 WebSocket 连接 |
| 10095 | Runtime | 否 | 容器内本地，Gateway 调用 |

### 5.2 安全组规则

仅需开放 **5002 端口**（TCP）：

| 方向 | 协议 | 端口 | 来源 |
|------|------|------|------|
| 入站 | TCP | 5002 | 0.0.0.0/0 |

---

## 6. 性能调优

### 6.1 延迟优化

| 优化项 | 效果 | 操作 |
|--------|------|------|
| 同地域部署 | RTF 降至 0.8 以下 | 服务器与客户端在同一地域 |
| 增大线程数 | 提升并发能力 | 调整 `DECODER_THREAD_NUM` |
| 减小 chunk 间隔 | 降低首包延迟 | 前端 chunk_ms 从 600 降至 300 |

### 6.2 资源监控

```bash
# 实时查看容器资源
docker stats argosasr-streaming-delivery

# 查看容器日志
docker logs argosasr-streaming-delivery --tail 50
```

---

## 7. 故障排查

### 7.1 无识别结果

| 可能原因 | 排查方法 | 解决方案 |
|----------|----------|----------|
| Runtime 未启动 | `docker ps` 查看状态 | `docker restart argosasr-streaming-delivery` |
| 内存不足 | `docker stats argosasr-streaming-delivery` | 增加服务器内存 |
| License 未授权 | `docker logs argosasr-streaming-delivery \| grep License` | 检查授权服务 |

### 7.2 连接失败

| 可能原因 | 排查方法 | 解决方案 |
|----------|----------|----------|
| Gateway 未启动 | `docker ps` | `docker restart argosasr-streaming-delivery` |
| 端口未开放 | `curl http://localhost:5002/health` | 检查安全组规则 |
| License 不通 | `docker exec argosasr-streaming-delivery bash -c '(echo > /dev/tcp/host.docker.internal/9800) 2>/dev/null && echo OK'` | 检查 `--add-host` 和授权服务 |

### 7.3 并发性能差

| 可能原因 | 排查方法 | 解决方案 |
|----------|----------|----------|
| 线程数不足 | `docker logs argosasr-streaming-delivery \| grep thread` | 增大 `DECODER_THREAD_NUM` |
| CPU 瓶颈 | `docker stats argosasr-streaming-delivery` | 升级 CPU 核数 |
| MAX_SESSIONS 限制 | 检查环境变量 | 增大 `MAX_SESSIONS` |

---

## 8. 客户现场部署清单

### 8.1 部署步骤

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

### 8.2 并发参数对照表

| 目标并发 | DECODER_THREAD_NUM | IO_THREAD_NUM | MAX_SESSIONS |
|----------|--------------------|---------------|--------------|
| 1-5 | 2 | 2 | 10 |
| 6-10 | 4 | 2 | 15 |
| 11-20 | 8 | 4 | 25 |
| 21-30 | 12 | 6 | 40 |
| 31-40 | 16 | 8 | 50 |

> **规则：** `DECODER_THREAD_NUM` ≈ 目标并发数 / 2.5（向上取整到偶数），`IO_THREAD_NUM` ≈ DECODER / 2，`MAX_SESSIONS` = 并发数 × 1.25

### 8.3 部署示例（20 并发）

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

### 8.4 部署检查清单

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

## 9. Docker 管理平台部署

如果使用 Portainer / Rancher 等 Docker 管理平台，无需命令行，直接在 UI 中配置：

### 9.1 创建容器

| 配置项 | 值 |
|--------|-----|
| 镜像 | `argosasr-streaming-delivery:v5.7` |
| 容器名 | `argosasr-streaming-delivery` |
| 端口映射 | `5002:5002` |
| 特权模式 | 开启 |

### 9.2 环境变量

| 变量 | 值 | 说明 |
|------|-----|------|
| `DECODER_THREAD_NUM` | `2` | 推理线程数 |
| `IO_THREAD_NUM` | `2` | IO 线程数 |
| `MAX_SESSIONS` | `20` | 最大并发 |
| `SESSION_TIMEOUT` | `15` | 超时秒数 |
| `LICENSE_SERVER_IP` | `host.docker.internal` | License 服务器 IP |
| `LICENSE_SERVER_PORT` | `9800` | License 端口 |

### 9.3 额外配置

| 配置项 | 值 |
|--------|-----|
| Extra Hosts | `host.docker.internal:host-gateway` |

> 如果 License 在不同服务器：`host.docker.internal:192.168.1.100`

---

## 10. 版本信息

| 组件 | 版本 |
|------|------|
| FunASR Runtime SDK | 0.1.4 (online-cpu) |
| 交付镜像 | `argosasr-streaming-delivery:v5.7`（约 8.7GB） |
| 模型 | paraformer-large-onnx (INT8)，已内嵌 |
| 识别模式 | 2pass（流式 + 离线校正） |
| 采样率 | 16kHz（浏览器麦克风）/ 8kHz（呼叫中心电话流） |
