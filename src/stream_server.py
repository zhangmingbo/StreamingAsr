"""
FunASR C++ Runtime WebSocket 网关 — 流式语音识别服务

==========================================================================
 架构概览
==========================================================================
  前端 (浏览器/电话)
    ↓ SocketIO (WS)
  stream_server.py (本网关)
    ↓ WebSocket (每会话一条连接)
  FunASR Runtime (C++, ws://localhost:10095)
    → FSMN-VAD + Paraformer Streaming + 标点 + 热词

  本网关职责:
    - License 鉴权
    - SocketIO 连接管理、限流、超时清理
    - Base64 解码音频
    - 每会话建立独立 WS 连接到 Runtime
    - 转发音频 chunk → Runtime
    - 接收 Runtime 结果 → 推送前端
    - HTTP 健康检查 / 前端页面 / 结果列表
==========================================================================
"""
import os
import sys
import json
import time
import base64
import signal
import threading

import numpy as np

from flask import Flask, request, send_from_directory
from flask_socketio import SocketIO, emit

# ========== License 验证 ==========
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from asr_license_client import init_license_client

    license_client = init_license_client(product='streaming_asr')
    if license_client is None:
        print('[License] 流式 ASR 授权验证失败，服务退出')
        sys.exit(1)
    if not license_client.verify():
        print('[License] 流式 ASR 授权验证失败，服务退出')
        sys.exit(1)
    print(f'[License] 流式 ASR 授权通过')
    print(f'  客户: {license_client.customer}')
    print(f'  到期: {license_client.expire_date}')
except SystemExit:
    raise
except Exception as e:
    print(f'[License] 授权验证异常: {e}')
    sys.exit(1)

# ─── 配置 ────────────────────────────────────────────────────────────────────

HOST = "0.0.0.0"
PORT = 5002

# FunASR Runtime WebSocket 地址
RUNTIME_WS_URL = os.environ.get("RUNTIME_WS_URL", "wss://localhost:10095")

# 最大并发会话数
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "20"))

# 会话超时（秒）
SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT", "15"))


# ─── 重采样工具 ─────────────────────────────────────────────────────────────

def _resample_8k_to_16k(pcm_bytes: bytes) -> bytes:
    """8kHz 16bit mono PCM → 16kHz 16bit mono PCM（线性插值上采样 2x）"""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    if len(samples) <= 1:
        return pcm_bytes
    # 线性插值：每两个样本之间插入一个平均值
    upsampled = np.zeros(len(samples) * 2 - 1, dtype=np.int16)
    upsampled[0::2] = samples
    upsampled[1::2] = (samples[:-1].astype(np.int32) + samples[1:].astype(np.int32)) // 2
    return upsampled.tobytes()


# ─── Flask + SocketIO ───────────────────────────────────────────────────────

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    logger=False, engineio_logger=False)

# ─── 会话管理 ────────────────────────────────────────────────────────────────
# sid → {
#   "last_time": float,
#   "runtime_ws": websocket.WebSocketApp or None,
#   "ws_thread": threading.Thread or None,
#   "ws_ready": threading.Event,
#   "pending_audio": list[bytes],   # WS 未就绪时暂存音频
#   "speech_detected": bool,        # 本轮是否已触发 speech_start
#   "speech_start_time": float,     # 首次检测到语音的时间戳
# }
sessions = {}
_sessions_lock = threading.Lock()
_shutdown_event = threading.Event()


def _create_runtime_ws(sid):
    """为会话创建到 FunASR Runtime 的 WebSocket 连接（含自动重连）"""
    import websocket
    import ssl as _ssl

    ws_url = RUNTIME_WS_URL
    session = sessions.get(sid)
    if not session:
        return

    _sslopt = {"cert_reqs": _ssl.CERT_NONE}
    _max_retries = 5
    _retry_delay = 1.0

    def on_message(ws, message):
        """接收 Runtime 识别结果，推送给前端（含 speech_start / speech_end 事件）"""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        text = data.get("text", "")
        is_final = data.get("is_final", False) or data.get("final", False)
        if not text:
            text = data.get("sentence", {}).get("text", "") if isinstance(data.get("sentence"), dict) else ""
        if not is_final:
            is_final = data.get("is_final", False) or data.get("final", False)

        session = sessions.get(sid)

        # ── speech_start 检测：首个非空识别结果 → 用户开始说话 ──
        if text and session and not session.get("speech_detected"):
            session["speech_detected"] = True
            session["speech_start_time"] = time.time()
            try:
                socketio.emit('speech_start', {'text': text}, room=sid)
            except Exception:
                pass
            print(f"[Gateway] speech_start for {sid}: {text}")
            sys.stdout.flush()

        # ── speech_end 检测：is_final → 用户说完一句话 ──
        if is_final and session:
            duration_ms = 0
            if session.get("speech_start_time"):
                duration_ms = int((time.time() - session["speech_start_time"]) * 1000)
            try:
                socketio.emit('speech_end', {
                    'text': text,
                    'duration_ms': duration_ms,
                }, room=sid)
            except Exception:
                pass
            print(f"[Gateway] speech_end for {sid}: {text} ({duration_ms}ms)")
            sys.stdout.flush()
            # 重置语音活动状态，准备下一轮
            session["speech_detected"] = False
            session["speech_start_time"] = None

        # ── 推送识别结果（原有逻辑）──
        if text or is_final:
            try:
                socketio.emit('recognition_result', {
                    'text': text,
                    'is_final': is_final,
                }, room=sid)
            except Exception:
                pass
            sys.stdout.flush()

        if is_final and sid in sessions:
            print(f"[Gateway] Session {sid} final: {text}")
            sys.stdout.flush()

    def on_error(ws, error):
        print(f"[Gateway] Runtime WS error for {sid}: {error}")
        sys.stdout.flush()

    def on_close(ws, close_status_code, close_msg):
        print(f"[Gateway] Runtime WS closed for {sid}: {close_status_code} {close_msg}")
        sys.stdout.flush()
        if sid in sessions:
            sessions[sid]["runtime_ws"] = None
            # 注意：不在这里设置 ws_ready！
            # ws_ready 只在 on_open 中设置，表示连接真正可用
            # 如果连接关闭，音频应该等待重连，而不是堆积在 pending 中

    def on_open(ws):
        print(f"[Gateway] Runtime WS connected for {sid}")
        sys.stdout.flush()
        session["runtime_ws"] = ws
        session["ws_ready"].set()  # 只有真正连接成功才设置

        # 发送 FunASR Runtime 初始配置（2pass 模式：流式 + 离线校正）
        init_msg = json.dumps({
            "mode": "2pass",
            "wav_name": sid,
            "wav_format": "pcm",
            "chunk_size": [5, 10, 5],
            "is_speaking": True,
            "audio_fs": 16000,
            "itn": True,
        })
        ws.send(init_msg)
        print(f"[Gateway] Sent init config for {sid}")
        sys.stdout.flush()

        # 发送已暂存的音频
        for chunk in session.get("pending_audio", []):
            try:
                ws.send(chunk, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                print(f"[Gateway] Error sending pending audio for {sid}: {e}")
                sys.stdout.flush()
        session["pending_audio"] = []

    # 带重连的连接循环
    for attempt in range(_max_retries):
        if sid not in sessions:
            return  # 会话已关闭

        if attempt > 0:
            print(f"[Gateway] Reconnecting Runtime WS for {sid} (attempt {attempt}/{_max_retries})")
            sys.stdout.flush()
            time.sleep(_retry_delay)
            _retry_delay = min(_retry_delay * 2, 10)  # 指数退避

        # 重置 ws_ready（重连前清除旧状态）
        session["ws_ready"] = threading.Event()

        ws_app = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        ws_app.run_forever(sslopt=_sslopt, ping_interval=30, ping_timeout=10)

        # run_forever 返回说明连接已断开
        print(f"[Gateway] Runtime WS run_forever returned for {sid}")
        sys.stdout.flush()

        # 检查会话是否还在
        if sid not in sessions:
            return

    # 所有重试耗尽
    print(f"[Gateway] Runtime WS max retries exhausted for {sid}")
    sys.stdout.flush()
    if sid in sessions:
        sessions[sid]["runtime_ws"] = None


def _send_audio_to_runtime(sid, pcm_bytes):
    """发送音频 chunk 到 Runtime（如果 WS 未就绪则暂存）"""
    session = sessions.get(sid)
    if not session:
        return

    # 等待 WS 就绪（最多 5 秒）
    if not session["ws_ready"].wait(timeout=5.0):
        # WS 未就绪（连接失败或重连中），暂存音频
        session.setdefault("pending_audio", []).append(pcm_bytes)
        print(f"[Gateway] Audio queued for {sid} (WS not ready, pending={len(session['pending_audio'])})")
        sys.stdout.flush()
        return

    ws = session.get("runtime_ws")
    if ws is None:
        # ws_ready 被设置但 ws 为 None（连接已关闭），暂存等待重连
        session.setdefault("pending_audio", []).append(pcm_bytes)
        print(f"[Gateway] Audio re-queued for {sid} (WS closed, pending={len(session['pending_audio'])})")
        sys.stdout.flush()
        return

    try:
        import websocket
        ws.send(pcm_bytes, opcode=websocket.ABNF.OPCODE_BINARY)
    except Exception as e:
        print(f"[Gateway] Error sending audio for {sid}: {e}")
        sys.stdout.flush()


def _close_runtime_ws(sid):
    """关闭会话的 Runtime WebSocket 连接（先发送结束信号，等待最终结果）"""
    session = sessions.pop(sid, None)
    if not session:
        return

    ws = session.get("runtime_ws")
    if ws:
        try:
            # 发送结束信号，触发 Runtime 返回 final 结果
            end_msg = json.dumps({"is_speaking": False})
            ws.send(end_msg)
            print(f"[Gateway] Sent end signal for {sid}")
            sys.stdout.flush()
        except Exception:
            pass
        # 给 Runtime 一点时间返回最终结果
        time.sleep(1.0)
        try:
            ws.close()
        except Exception:
            pass

    # 清理日志计数
    _audio_log_count.pop(sid, None)
    session["ws_ready"].set()  # Unblock any waiting sends
    print(f"[Gateway] Closed Runtime WS for {sid}")
    sys.stdout.flush()


# ─── 超时清理线程 ────────────────────────────────────────────────────────────

def _session_timeout_monitor():
    """定期检查超时会话并清理"""
    while not _shutdown_event.is_set():
        _shutdown_event.wait(timeout=5.0)
        if _shutdown_event.is_set():
            break

        now = time.time()
        with _sessions_lock:
            expired_sids = [
                sid for sid, info in sessions.items()
                if now - info["last_time"] > SESSION_TIMEOUT
            ]
        for sid in expired_sids:
            print(f"[Gateway] Session {sid} timeout ({SESSION_TIMEOUT}s), cleaning up")
            _close_runtime_ws(sid)
            try:
                socketio.emit('recognition_result', {
                    'text': '', 'is_final': True,
                    'error': '会话超时'
                }, room=sid)
            except Exception:
                pass


# ─── 信号处理 ────────────────────────────────────────────────────────────────

def _handle_signal(signum, frame):
    print(f"\n[Gateway] Received signal {signum}, shutting down...")
    _shutdown_event.set()
    with _sessions_lock:
        for sid in list(sessions.keys()):
            _close_runtime_ws(sid)
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ─── WebSocket 事件处理 ─────────────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    print(f"[+] SocketIO connect: {sid}")
    sys.stdout.flush()
    with _sessions_lock:
        if len(sessions) >= MAX_SESSIONS:
            emit('error', {'message': '服务器繁忙，请稍后重试'})
            return False

        sessions[sid] = {
            "last_time": time.time(),
            "runtime_ws": None,
            "ws_thread": None,
            "ws_ready": threading.Event(),
            "pending_audio": [],
            "_cleanup_pending": False,
            "speech_detected": False,
            "speech_start_time": None,
        }

    # 异步建立 Runtime WS 连接
    threading.Thread(
        target=_create_runtime_ws, args=(sid,),
        daemon=True, name=f"ws-init-{sid}",
    ).start()

    print(f"[+] Connected: {sid} (total: {len(sessions)})")
    sys.stdout.flush()
    emit('connected', {'message': '连接成功'})
    return True


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid not in sessions:
        return

    duration = time.time() - sessions[sid]["last_time"]
    print(f"[-] Disconnected: {sid}, duration: {duration:.1f}s")
    sys.stdout.flush()

    # 延迟清理，避免旧连接 disconnect 事件误杀新会话
    def _delayed_cleanup():
        time.sleep(1.0)
        if sid in sessions:
            # 只有当 session 没有被重置（新连接）时才清理
            if sessions[sid].get("_cleanup_pending", False):
                _close_runtime_ws(sid)
            else:
                print(f"[Gateway] Skip cleanup for {sid} (session was reset)")
                sys.stdout.flush()

    # 标记当前 session 待清理
    sessions[sid]["_cleanup_pending"] = True
    threading.Thread(target=_delayed_cleanup, daemon=True, name=f"cleanup-{sid}").start()


_audio_log_count = {}

@socketio.on('audio')
def handle_audio_chunk(data):
    """接收音频分片，转发到 Runtime"""
    sid = request.sid
    if sid not in sessions:
        return

    sessions[sid]["last_time"] = time.time()

    try:
        if isinstance(data, dict):
            audio_b64 = data.get('audio', '')
        else:
            audio_b64 = data

        pcm_bytes = base64.b64decode(audio_b64)

        # 8kHz → 16kHz 重采样
        sample_rate = sessions[sid].get("sample_rate", 16000)
        if sample_rate == 8000:
            pcm_bytes = _resample_8k_to_16k(pcm_bytes)

        # 前3个音频块打日志，之后静默
        cnt = _audio_log_count.get(sid, 0)
        if cnt < 3:
            print(f"[Gateway] Audio chunk #{cnt+1} for {sid}: {len(pcm_bytes)} bytes")
            sys.stdout.flush()
            _audio_log_count[sid] = cnt + 1

        _send_audio_to_runtime(sid, pcm_bytes)

    except Exception as e:
        print(f"[Gateway] Error processing audio for {sid}: {e}")
        sys.stdout.flush()
        emit('error', {'message': f'音频处理失败: {str(e)}'})


@socketio.on('start')
def handle_start(data):
    """前端开始录音 — Runtime WS 已在 connect 时建立"""
    sid = request.sid
    if sid not in sessions:
        return
    sample_rate = data.get('sample_rate', 16000) if data else 16000
    sessions[sid]["last_time"] = time.time()
    sessions[sid]["sample_rate"] = sample_rate
    print(f"[Gateway] Session {sid} started (sample_rate={sample_rate})")
    sys.stdout.flush()
    emit('started', {'message': '识别已开始', 'sample_rate': sample_rate})


@socketio.on('end')
def handle_end():
    """前端停止录音 — 发送结束信号到 Runtime，等待最终结果"""
    sid = request.sid
    if sid not in sessions:
        return

    print(f"[Gateway] Session {sid} end signal")
    sys.stdout.flush()

    # 发送 is_speaking=false 给 Runtime，但不立即关闭连接
    session = sessions.get(sid)
    ws = session.get("runtime_ws") if session else None
    if ws:
        try:
            end_msg = json.dumps({"is_speaking": False})
            ws.send(end_msg)
            print(f"[Gateway] Sent end signal for {sid}, waiting for final result...")
            sys.stdout.flush()
        except Exception:
            pass

    # 延迟关闭，等 Runtime 返回最终结果
    def _delayed_close():
        time.sleep(3.0)
        if sid in sessions:
            _close_runtime_ws(sid)
            try:
                socketio.emit('finished', {'text': '', 'duration': 0}, room=sid)
            except Exception:
                pass

    threading.Thread(target=_delayed_close, daemon=True, name=f"end-close-{sid}").start()


@socketio.on('reset')
def handle_reset():
    """重置会话"""
    sid = request.sid
    if sid not in sessions:
        return

    # 关闭并重建 Runtime WS
    _close_runtime_ws(sid)

    with _sessions_lock:
        sessions[sid] = {
            "last_time": time.time(),
            "runtime_ws": None,
            "ws_thread": None,
            "ws_ready": threading.Event(),
            "pending_audio": [],
            "_cleanup_pending": False,
            "speech_detected": False,
            "speech_start_time": None,
        }

    threading.Thread(
        target=_create_runtime_ws, args=(sid,),
        daemon=True, name=f"ws-init-{sid}",
    ).start()

    emit('reset_complete')


# ─── HTTP 路由 ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('/opt/streaming-asr/src', 'stream_test.html')


@app.route('/health')
def health():
    with _sessions_lock:
        active = len(sessions)
    return {
        'status': 'ok',
        'engine': 'funasr-runtime-cpp',
        'model': 'paraformer-large-online-onnx',
        'runtime_ws': RUNTIME_WS_URL,
        'sessions': active,
        'max_sessions': MAX_SESSIONS,
        'architecture': 'gateway + runtime-proxy',
    }


@app.route('/results')
def results():
    results_dir = '/opt/streaming-asr/results'
    if not os.path.exists(results_dir):
        return []
    files = []
    for f in sorted(os.listdir(results_dir), reverse=True):
        if f.endswith('.json'):
            files.append(f)
    return files


# ─── 启动服务 ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"\n{'='*60}")
    print(f"FunASR Runtime WebSocket Gateway")
    print(f"{'='*60}")
    print(f"Host         : {HOST}")
    print(f"Port         : {PORT}")
    print(f"Runtime WS   : {RUNTIME_WS_URL}")
    print(f"Max Sessions : {MAX_SESSIONS}")
    print(f"Session T/O  : {SESSION_TIMEOUT}s")
    print(f"{'='*60}\n")

    # 启动超时监控线程
    threading.Thread(
        target=_session_timeout_monitor,
        daemon=True, name="session-monitor",
    ).start()
    print("[Gateway] Session timeout monitor started")

    print(f"\n[Gateway] Listening on {HOST}:{PORT}\n")

    try:
        socketio.run(app, host=HOST, port=PORT, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_event.set()
    print(f"FunASR Runtime WebSocket Gateway")
    print(f"{'='*60}")
    print(f"Host         : {HOST}")
    print(f"Port         : {PORT}")
    print(f"Runtime WS   : {RUNTIME_WS_URL}")
    print(f"Max Sessions : {MAX_SESSIONS}")
    print(f"Session T/O  : {SESSION_TIMEOUT}s")
    print(f"{'='*60}\n")

    # 启动超时监控线程
    threading.Thread(
        target=_session_timeout_monitor,
        daemon=True, name="session-monitor",
    ).start()
    print("[Gateway] Session timeout monitor started")

    print(f"\n[Gateway] Listening on {HOST}:{PORT}\n")

    try:
        socketio.run(app, host=HOST, port=PORT, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_event.set()
