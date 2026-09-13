# ============================================================
# argosasr-streaming 流式 ASR 服务 — 交付版 Dockerfile
# 源码编译为 .pyc 字节码，镜像内不保留任何 .py 源文件
# 集成模型文件，无需外部挂载
# ============================================================
FROM python:3.9-slim

# ─── 系统依赖 ───
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# ─── Python 依赖 ───
RUN pip install --no-cache-dir \
    torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    funasr modelscope && \
    flask flask-socketio && \
    onnxruntime librosa pyyaml jieba numpy soundfile && \
    requests

# ─── 复制源码（仅用于编译，最终会删除） ───
COPY stream_server.py /opt/argosasr-streaming/stream_server.py
COPY asr_license_client.py /opt/funasr/license-service/

WORKDIR /opt/argosasr-streaming

# ============================================================
# 黑盒处理：编译 → 验证 → 删除源码
# ============================================================

# 1. 编译 Python 文件为 .pyc 字节码（-b 标志生成同名 .pyc）
RUN python3 -m compileall -b /opt/argosasr-streaming/stream_server.py && \
    python3 -m compileall -b /opt/funasr/license-service/asr_license_client.py

# 2. 验证编译产物存在
RUN test -f /opt/argosasr-streaming/stream_server.pyc && \
    echo "[OK] Black-box compilation verified"

# 3. 删除所有 .py 源文件及 __pycache__
RUN rm -f /opt/argosasr-streaming/stream_server.py && \
    rm -f /opt/funasr/license-service/asr_license_client.py && \
    find /opt/argosasr-streaming -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; \
    find /opt/funasr -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; \
    echo "[OK] Source files removed"

# 4. 最终验证：确认无 .py 残留
RUN test ! -f /opt/argosasr-streaming/stream_server.py && \
    echo "[OK] Black box confirmed - no source code remains"

# ─── 预下载模型（构建上下文中的 models/ 目录） ───
COPY models/ /opt/funasr/models/

# ─── 运行时目录 ───
RUN mkdir -p /opt/argosasr-streaming/results /opt/argosasr-streaming/audio /opt/argosasr-streaming/license

EXPOSE 5002

# 使用 .pyc 字节码启动服务
CMD ["python3", "/opt/argosasr-streaming/stream_server.pyc"]
