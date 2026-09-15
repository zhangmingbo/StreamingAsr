# 智能体操作规范

## 1. Docker 容器文件修改规范

### 问题
使用 `docker cp` 修改运行中容器的文件是**临时操作**，容器重启后修改会丢失，恢复为镜像中的原始版本。

### 规则
**禁止通过 `docker cp` 修改容器内文件作为最终交付手段。**

正确流程：
```
修改本地源码 → 重新构建镜像 → 停止旧容器 → 启动新容器
```

### 例外
`docker cp` 仅可用于**临时调试**（验证修改是否生效），调试完成后必须重新构建镜像。

---

## 2. 黑盒镜像构建规范

### 构建前检查清单
- [ ] 确认所有源码文件已更新到本地最新版本
- [ ] 确认 `stream_test.html` 等前端文件已同步
- [ ] 确认 `start_merged.sh` 启动脚本已同步
- [ ] 确认 Dockerfile 中的 COPY 路径与构建上下文匹配

### 构建命令
```bash
cd /opt/argosasr && docker build -f Dockerfile.blackbox -t argosasr-streaming-delivery:v5.7 .
```

### 构建后验证
```bash
# 验证前端版本
docker exec <容器名> grep "Argos ASR" /opt/streaming-asr/src/stream_test.html

# 验证源码已编译
docker exec <容器名> ls /opt/streaming-asr/src/*.pyc

# 验证无 .py 残留
docker exec <容器名> ls /opt/streaming-asr/src/*.py 2>&1 | grep -v "No such"
```

---

## 3. 部署更新流程

### 完整更新步骤
```
1. 修改本地源码文件
2. 上传源码到服务器构建上下文（/opt/argosasr/src/）
3. 重新构建镜像（docker build）
4. 停止并删除旧容器（docker stop + docker rm）
5. 用新镜像启动容器（docker run）
6. 健康检查验证（curl /health）
7. 验证容器内文件版本
```

### 禁止的快捷操作
- 禁止 `docker cp` 修改后直接重启（修改会丢失）
- 禁止只上传文件不重建镜像
- 禁止跳过构建后验证步骤

---

## 4. 镜像版本管理

- 每次代码变更后必须重新构建镜像
- 镜像标签格式：`argosasr-streaming-delivery:v{版本号}`
- 同时打 `:latest` 标签指向最新版本
- 部署手册中的镜像版本必须与构建版本一致

---

## 5. 文件同步规范

修改以下文件后，必须同步到服务器构建上下文并重新构建：
- `src/stream_server.py` — 网关核心逻辑
- `src/asr_license_client.py` — 授权客户端
- `src/stream_test.html` — 前端测试页面
- `docker/start_merged.sh` — 启动脚本
- `docker/Dockerfile.blackbox` — 黑盒构建定义
