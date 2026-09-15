#!/bin/bash
# FunASR 单容器合并启动脚本
# 后台启动 C++ Runtime，前台启动 Python Gateway
# 所有可调参数通过环境变量传入，客户只需编辑 .env 文件
set -e

# ─── 从环境变量读取配置（含默认值）───
DECODER_THREAD_NUM=${DECODER_THREAD_NUM:-2}
IO_THREAD_NUM=${IO_THREAD_NUM:-2}
RUNTIME_PORT=${RUNTIME_PORT:-10095}

echo "============================================"
echo "  FunASR 流式语音识别服务（单容器合并版）"
echo "============================================"
echo "  decoder-thread-num: ${DECODER_THREAD_NUM}"
echo "  io-thread-num:      ${IO_THREAD_NUM}"
echo "  runtime-port:       ${RUNTIME_PORT}"
echo "============================================"

# ─── 后台启动 C++ Runtime ───
echo "[1/3] 启动 C++ Runtime..."
cd /workspace/FunASR/runtime/websocket/build/bin

./funasr-wss-server-2pass \
  --download-model-dir /workspace/models \
  --model-dir damo/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-onnx \
  --online-model-dir damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx \
  --vad-dir damo/speech_fsmn_vad_zh-cn-16k-common-onnx \
  --decoder-thread-num ${DECODER_THREAD_NUM} \
  --io-thread-num ${IO_THREAD_NUM} \
  --port ${RUNTIME_PORT} &

RUNTIME_PID=$!
echo "  Runtime PID: $RUNTIME_PID"

# ── 等待 Runtime 就绪 ───
echo "[2/3] 等待 Runtime 就绪..."
for i in $(seq 1 60); do
    if (echo > /dev/tcp/localhost/${RUNTIME_PORT}) 2>/dev/null; then
        echo "  Runtime 已就绪（${i}s）"
        break
    fi
    if [ $i -eq 60 ]; then
        echo "  [警告] Runtime 启动超时（60s），继续启动 Gateway..."
    fi
    sleep 1
done

# ─── 前台启动 Python 网关 ──
echo "[3/3] 启动 Python Gateway..."
export RUNTIME_WS_URL=wss://localhost:${RUNTIME_PORT}

# 自动检测：黑盒模式用 .pyc，开发模式用 .py
if [ -f /opt/streaming-asr/src/stream_server.pyc ]; then
    exec python3 -u /opt/streaming-asr/src/stream_server.pyc
else
    exec python3 -u /opt/streaming-asr/src/stream_server.py
fi
