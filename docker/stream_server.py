"""
FunASR WebSocket 流式语音识别服务 (funasr_onnx)
基于 ONNX Runtime，使用 Paraformer-large streaming 模型

启动：python stream_server.py
"""

import os
import json
import time
import base64
import wave
import sys
import numpy as np
from flask import Flask, request, send_from_directory
from flask_socketio import SocketIO, emit
from funasr_onnx import ParaformerStreaming

# ─── License 验证（通过HTTP连接宿主机授权服务） ─────────────────────────────
sys.path.insert(0, '/opt/funasr/license-service')
try:
    from asr_license_client import init_license_client, get_license_client

    license_client = init_license_client(product='streaming_asr')
    if license_client is None:
        print('[License] 流式 ASR 授权验证失败，服务退出')
        print('[License] 请检查:')
        print('  1. 宿主机授权服务是否运行 (默认 http://host.docker.internal:9800)')
        print('  2. 环境变量 LICENSE_SERVER 是否正确配置')
        print('  3. 许可证是否包含 streaming_asr 产品授权')
        sys.exit(1)

    print(f'[License] 流式 ASR 授权通过')
    print(f'  客户: {license_client.customer}')
    print(f'  到期: {license_client.expire_date}')
    for fname, fval in license_client.features.items():
        print(f'  {fname}: {fval}')
except ImportError:
    print('[License] 警告: asr_license_client 模块未找到，跳过许可验证')
    license_client = None
except Exception as e:
    print(f'[License] 警告: 许可验证异常: {e}，跳过许可验证')
    license_client = None

# ─── 配置（通过环境变量覆盖） ─────────────────────────────────────────────

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 5002))
MODEL_DIR = os.environ.get("MODEL_DIR", "/opt/funasr/models/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx")
SAMPLE_RATE = 16000
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", 20))

# ─── 初始化 ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = "funasr-streaming"
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=10 * 1024 * 1024,
    logger=False,
    engineio_logger=False,
)

# 结果保存目录
RESULT_DIR = "/opt/funasr/results"
AUDIO_DIR = "/opt/funasr/audio"
os.makedirs(RESULT_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

# 流式会话管理
sessions = {}

print("=" * 60)
print("FunASR 流式语音识别服务 (funasr_onnx + Paraformer-large)")
print(f"WebSocket 地址: ws://{HOST}:{PORT}/stream")
print(f"模型目录: {MODEL_DIR}")
print(f"最大会话数: {MAX_SESSIONS}")
print("=" * 60)

# ─── 加载模型 ────────────────────────────────────────────────────────────────

print("\n⏳ 加载 Paraformer-large streaming 模型...")
asr_model = ParaformerStreaming(
    model_dir=MODEL_DIR,
    batch_size=1,
    quantize=True,
    intra_op_num_threads=4,
)
print("✅ Paraformer-large streaming 模型加载完成\n")


# ─── 音频处理工具 ────────────────────────────────────────────────────────────

def pcm_to_float32(pcm_bytes):
    """PCM int16 字节转 float32 numpy 数组"""
    if isinstance(pcm_bytes, (list, tuple)):
        pcm_bytes = bytes(pcm_bytes)
    audio = np.frombuffer(pcm_bytes, dtype=np.int16)
    audio = audio.astype(np.float32) / 32768.0
    return audio


# ─── WebSocket 事件处理 ──────────────────────────────────────────────────────

@socketio.on("connect")
def handle_connect():
    """客户端连接"""
    sid = request.sid
    
    if len(sessions) >= MAX_SESSIONS:
        print(f"[拒绝] 超过最大会话数 {MAX_SESSIONS}")
        emit("error", {"message": f"服务繁忙，最大并发 {MAX_SESSIONS}"})
        return False
    
    sessions[sid] = {
        "cache": {},  # funasr_onnx 流式缓存
        "full_pcm": b"",
        "recognized_text": "",
        "partial_text": "",
        "start_time": time.time(),
        "chunk_count": 0,
    }
    print(f"[连接] 客户端 {sid} 已连接 (当前 {len(sessions)}/{MAX_SESSIONS})")
    emit("connected", {"message": "流式识别服务已就绪 (引擎: funasr_onnx)", "sid": sid})


@socketio.on("disconnect")
def handle_disconnect():
    """客户端断开"""
    sid = request.sid
    if sid in sessions:
        session = sessions[sid]
        duration = time.time() - session["start_time"]
        print(
            f"[断开] 客户端 {sid}，"
            f"识别 {session['chunk_count']} 个 chunk，"
            f"耗时 {duration:.1f}s (剩余 {len(sessions)-1}/{MAX_SESSIONS})"
        )
        del sessions[sid]


@socketio.on("start")
def handle_start(data):
    """开始流式识别"""
    sid = request.sid
    if sid not in sessions:
        emit("error", {"message": "会话不存在"})
        return

    session = sessions[sid]
    # 重置会话状态
    session["cache"] = {}  # 清空流式缓存
    session["full_pcm"] = b""
    session["recognized_text"] = ""
    session["partial_text"] = ""
    session["chunk_count"] = 0
    session["start_time"] = time.time()

    print(f"[开始] 客户端 {sid} 开始流式识别")
    emit("started", {
        "message": "流式识别已启动",
        "sample_rate": SAMPLE_RATE,
    })


@socketio.on("audio")
def handle_audio(data):
    """接收音频片段"""
    sid = request.sid
    if sid not in sessions:
        return

    session = sessions[sid]

    # 获取音频数据
    if isinstance(data, dict):
        audio_data = data.get("audio", "")
        is_final = data.get("is_final", False)
    else:
        audio_data = data
        is_final = False

    if not audio_data:
        return

    # 解码音频
    if isinstance(audio_data, str):
        pcm_bytes = base64.b64decode(audio_data)
    else:
        pcm_bytes = audio_data

    # 转 float32
    samples = pcm_to_float32(pcm_bytes)
    
    # 累积完整音频（用于保存）
    session["full_pcm"] += pcm_bytes
    session["chunk_count"] += 1

    # funasr_onnx 流式推理
    try:
        # chunk_size: [left_chunk, center_chunk, right_chunk] = [0, 10, 5] => 600ms
        chunk_size = [0, 10, 5]
        encoder_chunk_look_back = 4
        decoder_chunk_look_back = 1
        
        result = asr_model.generate(
            input=samples,
            cache=session["cache"],
            is_final=is_final,
            chunk_size=chunk_size,
            encoder_chunk_look_back=encoder_chunk_look_back,
            decoder_chunk_look_back=decoder_chunk_look_back,
        )
        
        # 获取识别结果
        if result and len(result) > 0:
            text = result[0].get("text", "").strip()
            
            if text and text != session["partial_text"]:
                session["partial_text"] = text
                
                # 如果是最终结果
                if is_final:
                    session["recognized_text"] += text
                    emit("result", {
                        "text": text,
                        "full_text": session["recognized_text"],
                        "is_final": True,
                        "chunk": session["chunk_count"],
                    })
                else:
                    # 中间结果
                    emit("result", {
                        "text": text,
                        "partial": True,
                        "is_final": False,
                        "chunk": session["chunk_count"],
                    })

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[错误] 推理失败: {e}")
        print(f"[错误] 堆栈:\n{tb}")
        emit("error", {"message": str(e)})


@socketio.on("end")
def handle_end(data):
    """结束流式识别"""
    sid = request.sid
    if sid not in sessions:
        return

    session = sessions[sid]
    
    # funasr_onnx 不需要特殊处理，cache 会自动管理
    duration = time.time() - session["start_time"]
    print(
        f"[结束] 客户端 {sid} 识别完成，"
        f"共 {session['chunk_count']} 个 chunk，"
        f"耗时 {duration:.1f}s"
    )

    # 保存结果
    save_session_result(sid, session, duration)

    emit("finished", {
        "text": session["recognized_text"],
        "duration": round(duration, 2),
        "chunks": session["chunk_count"],
    })


def save_session_result(sid, session, duration):
    """保存识别结果"""
    now = __import__('datetime').datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    text = session["recognized_text"].strip()
    file_tag = f"{timestamp}_{sid[:8]}"

    if not text:
        return

    # 保存 JSON
    session_file = os.path.join(RESULT_DIR, f"{file_tag}.json")
    session_data = {
        "sid": sid,
        "time": now.isoformat(),
        "text": text,
        "duration": round(duration, 2),
        "chunks": session["chunk_count"],
    }
    with open(session_file, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)

    # 追加到汇总文件
    all_file = os.path.join(RESULT_DIR, "all_results.jsonl")
    with open(all_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(session_data, ensure_ascii=False) + "\n")

    print(f"[保存] 结果: {session_file}")


# ─── HTTP 接口 ───────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    info = {
        "status": "ok",
        "engine": "funasr_onnx",
        "model": "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx",
        "sessions": len(sessions),
        "max_sessions": MAX_SESSIONS,
    }
    if license_client:
        info["license"] = {
            "valid": license_client.is_valid(),
            "customer": license_client.customer,
            "expireDate": license_client.expire_date,
        }
    return info


@app.route("/test")
def test_page():
    return send_from_directory("/opt/funasr", "stream_test.html")


@app.route("/")
def index():
    return f"""
    <html>
    <head><title>FunASR 流式识别 (Paraformer-large)</title></head>
    <body>
        <h2>FunASR WebSocket 流式识别 (ONNX Runtime)</h2>
        <p><a href="/test">打开测试页面</a></p>
        <p>WebSocket: ws://服务器IP:{PORT}/stream</p>
        <p>健康检查: <a href="/health">/health</a></p>
        <p>当前会话: {len(sessions)} / {MAX_SESSIONS}</p>
        <p>引擎: sherpa-onnx (ONNX Runtime)</p>
    </body>
    </html>
    """


# ─── 启动 ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # SSL 配置 (iOS/Apple 设备要求 HTTPS 才能使用麦克风)
    ssl_ctx = None
    ssl_cert = "/opt/funasr/server.crt"
    ssl_key = "/opt/funasr/server.key"
    if os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        ssl_ctx = (ssl_cert, ssl_key)
        print(f"\n🔒 SSL 已启用 (https://{HOST}:{PORT})")
    else:
        print(f"\n🚀 启动流式识别服务: http://{HOST}:{PORT}")
    
    socketio.run(
        app,
        host=HOST,
        port=PORT,
        debug=False,
        allow_unsafe_werkzeug=True,
        ssl_context=ssl_ctx,
    )
