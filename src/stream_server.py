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
    - Base64 解码音频 / 8kHz→16kHz 重采样
    - 每会话建立独立 WS 连接到 Runtime
    - 转发音频 chunk → Runtime
    - 接收 Runtime 结果 → 语音活动状态机 → 推送前端
    - HTTP 健康检查 / 前端页面 / 结果列表

==========================================================================
 语音活动状态机（speech_start / speech_end）
==========================================================================
  speech_start（双层触发，每轮只触发一次）:
    第一层：Gateway 能量 VAD —— 音频块 RMS ≥ 阈值即触发（~100ms 延迟）
    第二层：识别结果兜底 —— VAD 漏检时，Runtime 首个非空文本触发

  speech_end（三种触发）:
    1. Runtime 返回最终结果（2pass offline / is_final 标志）
    2. 静音兜底 —— speech_start 后持续静音超过 _VAD_SILENCE_GRACE_MS
    3. 会话关闭 / 重置

  每次 speech_end 后状态机复位，可重复检测下一轮说话。
==========================================================================
"""
import os
import sys
import json
import time
import base64
import signal
import threading
import itertools
import asyncio
import uuid

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

# 内部 WS 通道端口（MRCP 插件接入，仅内网访问）
INTERNAL_WS_PORT = int(os.environ.get("INTERNAL_WS_PORT", "5003"))

# 会话超时（秒）
SESSION_TIMEOUT = int(os.environ.get("SESSION_TIMEOUT", "15"))

# 2pass 流式参数：左缓存/块大小/右缓存（单位 60ms 帧）
# [5, 10, 5] = 300ms 左缓存 + 600ms 块 + 300ms 右缓存（准确率优先）
CHUNK_SIZE = [5, 10, 5]


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


def _calc_rms(pcm_bytes: bytes) -> float:
    """计算 PCM 音频的 RMS 能量（0~1 范围）"""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples ** 2)))


# ─── 语音活动状态机参数（运行时可热调） ─────────────────────────────────────

# 热调参数字典：通过 POST /admin/vad 即时修改，无需重启容器。
# 初始值优先取环境变量，便于容器部署时定制。
_RUNTIME_CFG = {
    # RMS 能量阈值（约 -30dB），超过视为说话。
    # 实测依据（2026-09-17 真机外放）：扬声器回音 rms 稳定在 0.021~0.024，
    # 0.02 会误触发打断；0.03 可过滤该回音，正常说话 rms 0.025~0.063 不受影响，
    # 轻声说话漏检由"识别结果兜底"机制覆盖（speech_start 双层触发第二层）。
    "vad_threshold": float(os.environ.get("VAD_SPEECH_THRESHOLD", "0.03")),
    # speech_start 后持续静音多久 → 静音兜底 speech_end（毫秒）
    "vad_silence_grace_ms": int(os.environ.get("VAD_SILENCE_GRACE_MS", "2000")),
    # 全静音判定阈值：整段音频 max_rms 低于此值 → 判定为无真实语音，
    # 丢弃 Runtime 残留文本（防跨会话状态残留导致"幻听"，如静音识别出提示音全文）。
    "stale_text_rms": float(os.environ.get("STALE_TEXT_RMS", "0.005")),
}

# 管理接口鉴权 token：访问 /admin/vad 需携带 X-Admin-Token 请求头。
# 环境变量 ADMIN_TOKEN 优先；未设置时用内置默认值（调试服务器专用）。
_ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "asr-vad-debug-9f2c")


# ─── Flask + SocketIO ───────────────────────────────────────────────────────

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    logger=False, engineio_logger=False)


# ─── 会话管理 ────────────────────────────────────────────────────────────────

sessions = {}
_sessions_lock = threading.Lock()
_shutdown_event = threading.Event()


def _new_session():
    """创建会话状态字典（唯一初始化入口，保证字段一致）"""
    return {
        # ── 基础 ──
        "last_time": time.time(),
        "sample_rate": 16000,
        # ── 内部 WS 通道（MRCP 插件接入） ──
        "internal_ws": None,             # websockets 连接对象（内部通道，None=SocketIO 会话）
        "internal_loop": None,           # 内部 WS 事件循环（跨线程推送用）
        # ── Runtime WS ──
        "runtime_ws": None,              # websocket.WebSocketApp or None
        "ws_connecting": False,          # 是否正在建立 Runtime WS（防重复连接）
        "ws_ready": threading.Event(),   # Runtime WS 就绪信号（on_open 置位）
        "pending_audio": [],             # WS 未就绪时暂存的音频
        # ── 连接生命周期 ──
        "_cleanup_pending": False,       # disconnect 延迟清理标记
        "_end_token": None,              # end 延迟关闭令牌（防竞态）
        # ── 语音活动状态机 ──
        "speech_detected": False,        # 本轮是否已触发 speech_start
        "speech_start_time": None,       # speech_start 时间戳（算 duration_ms）
        "last_speech_time": None,        # 最后一次检测到说话的时间（静音兜底）
        "result_shown": False,           # 首个识别结果时延是否已推送
        "last_result_text": "",           # 最新识别文本缓存（静音兜底 speech_end 携带）
        "max_rms": 0.0,                  # 本轮音频最大 RMS（全静音校验用）
        # ── Runtime WS 轮次（generation） ──
        "ws_generation": 0,              # start 递增；旧连接结果按代次作废
        # ── no-input 超时（MRCP） ──
        "no_input_sent": False,          # 本轮是否已因静音超时发过 finished（防重）
        "_no_input_token": None,         # no-input 定时器令牌（新一轮 start 复位）
    }


# ─── 语音活动状态机 ──────────────────────────────────────────────────────────

def _push_event(sid, session, event, payload):
    """统一事件推送：内部 WS 通道（MRCP 插件）优先，否则走 SocketIO room"""
    if session is None:
        return
    ws = session.get("internal_ws")
    loop = session.get("internal_loop")
    if ws is not None and loop is not None:
        # 内部通道：跨线程投递到事件循环执行发送
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps({"event": event, "data": payload}, ensure_ascii=False)),
                loop,
            )
        except Exception:
            pass
        return

    try:
        socketio.emit(event, payload, room=sid)
    except Exception:
        pass


def _emit_speech_start(sid, session, text="", rms=None):
    """触发 speech_start 事件（每轮只触发一次，由 speech_detected 标志保证）"""
    detect_time = time.time()
    session["speech_detected"] = True
    session["speech_start_time"] = detect_time
    session["last_speech_time"] = detect_time
    session["result_shown"] = False

    payload = {"text": text, "detect_time": detect_time}
    if rms is not None:
        payload["rms"] = round(rms, 4)

    _push_event(sid, session, 'speech_start', payload)

    src = "VAD" if rms is not None else "ASR"
    print(f"[Gateway] speech_start ({src}) for {sid}: "
          f"rms={payload.get('rms')} text={text} detect_time={detect_time:.3f}")
    sys.stdout.flush()


def _emit_speech_end(sid, session, text="", reason="final"):
    """触发 speech_end 事件并复位状态机，准备下一轮检测"""
    duration_ms = 0
    if session.get("speech_start_time"):
        duration_ms = int((time.time() - session["speech_start_time"]) * 1000)

    _push_event(sid, session, 'speech_end', {
        'text': text,
        'duration_ms': duration_ms,
        'reason': reason,
    })

    print(f"[Gateway] speech_end ({reason}) for {sid}: {text} ({duration_ms}ms)")
    sys.stdout.flush()
    _reset_speech_state(session)


def _reset_speech_state(session):
    """复位语音活动状态机（不触发任何事件）"""
    session["speech_detected"] = False
    session["speech_start_time"] = None
    session["last_speech_time"] = None
    session["result_shown"] = False
    session["last_result_text"] = ""


# ─── Runtime WebSocket 连接管理 ─────────────────────────────────────────────

def _create_runtime_ws(sid, my_gen=None):
    """为会话创建到 FunASR Runtime 的 WebSocket 连接（含自动重连）

    my_gen: 连接所属的会话轮次。start 会递增 generation 并作废旧连接；
    旧连接线程收到的结果因代次不匹配而被丢弃（防跨轮状态残留）。
    """
    import websocket
    import ssl as _ssl

    session = sessions.get(sid)
    if not session:
        return  # 会话不存在
    if my_gen is None:
        my_gen = session.get("ws_generation", 0)
    # 同代连接已在建立中 → 防重
    if session.get("ws_connecting") and session.get("ws_connecting_gen") == my_gen:
        return

    session["ws_connecting"] = True
    session["ws_connecting_gen"] = my_gen
    _sslopt = {"cert_reqs": _ssl.CERT_NONE}
    _max_retries = 5
    _retry_delay = 1.0

    def _session_valid():
        """会话是否仍属于本连接线程（防止 reset/新轮次后僵尸连接复活）"""
        return (sid in sessions and sessions[sid] is session
                and session.get("ws_generation") == my_gen)

    def on_message(ws, message):
        """接收 Runtime 识别结果 → 语音活动状态机 → 推送前端"""
        if not _session_valid():
            return

        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        mode = str(data.get("mode", ""))
        text = data.get("text", "")
        if not text:
            sentence = data.get("sentence")
            if isinstance(sentence, dict):
                text = sentence.get("text", "")

        # 最终结果判定：is_final 标志 或 2pass 的 offline 校正结果
        is_final = bool(data.get("is_final") or data.get("final") or "offline" in mode)

        # ── 全静音校验：整段音频无真实语音 → Runtime 文本为跨会话残留，丢弃 ──
        if text and session.get("max_rms", 0.0) < _RUNTIME_CFG["stale_text_rms"]:
            print(f"[Gateway] Dropping stale text for {sid}: {text} "
                  f"(max_rms={session.get('max_rms', 0.0):.4f} < {_RUNTIME_CFG['stale_text_rms']})")
            sys.stdout.flush()
            text = ""

        # ── speech_start（ASR 兜底）：能量 VAD 漏检时由首个非空文本触发 ──
        was_detected = session["speech_detected"]
        if text and not was_detected:
            _emit_speech_start(sid, session, text=text)

        # ── 推送识别结果 ──
        if text or is_final:
            session["last_result_text"] = text  # 缓存：静音兜底 speech_end 携带
            result_latency = None
            if (text and was_detected
                    and session.get("speech_start_time")
                    and not session["result_shown"]):
                # 首个识别结果：speech_start → 识别结果 的时延
                result_latency = int((time.time() - session["speech_start_time"]) * 1000)
                session["result_shown"] = True
            _push_event(sid, session, 'recognition_result', {
                'text': text,
                'is_final': is_final,
                'result_latency': result_latency,
                'server_time': time.time(),   # 后端发出时刻（前端算下发延迟）
            })
            sys.stdout.flush()

        # ── speech_end：最终结果 → 用户说完一句话，复位状态机 ──
        if is_final:
            _emit_speech_end(sid, session, text=text, reason="final")
            print(f"[Gateway] Session {sid} final: {text}")
            sys.stdout.flush()

    def on_error(ws, error):
        print(f"[Gateway] Runtime WS error for {sid}: {error}")
        sys.stdout.flush()

    def on_close(ws, close_status_code, close_msg):
        print(f"[Gateway] Runtime WS closed for {sid}: {close_status_code} {close_msg}")
        sys.stdout.flush()
        if _session_valid():
            session["runtime_ws"] = None
            session["ws_ready"].clear()  # 标记未就绪：音频将暂存，等待重连

    def on_open(ws):
        if not _session_valid():
            # 会话已被重建，本连接是僵尸，直接关闭
            print(f"[Gateway] Runtime WS for {sid} superseded, closing stale connection")
            sys.stdout.flush()
            try:
                ws.close()
            except Exception:
                pass
            return

        print(f"[Gateway] Runtime WS connected for {sid}")
        sys.stdout.flush()
        session["runtime_ws"] = ws
        session["ws_ready"].set()

        # 发送 FunASR Runtime 初始配置（2pass 模式：流式 + 离线校正）
        init_msg = json.dumps({
            "mode": "2pass",
            "wav_name": sid,
            "wav_format": "pcm",
            "chunk_size": CHUNK_SIZE,
            "is_speaking": True,
            "audio_fs": 16000,
            "itn": True,
        })
        ws.send(init_msg)
        print(f"[Gateway] Sent init config for {sid}")
        sys.stdout.flush()

        # 补发暂存的音频
        for chunk in session.get("pending_audio", []):
            try:
                ws.send(chunk, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                print(f"[Gateway] Error sending pending audio for {sid}: {e}")
                sys.stdout.flush()
        session["pending_audio"] = []

    try:
        # 带重连的连接循环
        for attempt in range(_max_retries):
            if not _session_valid():
                return  # 会话已关闭或重建

            if attempt > 0:
                print(f"[Gateway] Reconnecting Runtime WS for {sid} (attempt {attempt}/{_max_retries})")
                sys.stdout.flush()
                time.sleep(_retry_delay)
                _retry_delay = min(_retry_delay * 2, 10)  # 指数退避

            session["ws_ready"].clear()  # 重连前标记未就绪

            ws_app = websocket.WebSocketApp(
                RUNTIME_WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws_app.run_forever(sslopt=_sslopt, ping_interval=30, ping_timeout=10)

            # run_forever 返回说明连接已断开
            print(f"[Gateway] Runtime WS run_forever returned for {sid}")
            sys.stdout.flush()

            if not _session_valid():
                return

        # 所有重试耗尽
        print(f"[Gateway] Runtime WS max retries exhausted for {sid}")
        sys.stdout.flush()
        if _session_valid():
            session["runtime_ws"] = None
            session["ws_ready"].clear()
    finally:
        if (sid in sessions and sessions[sid] is session
                and session.get("ws_generation") == my_gen):
            session["ws_connecting"] = False


def _start_runtime_ws_thread(sid, my_gen=None):
    """启动 Runtime WS 连接线程（generation 防重）"""
    threading.Thread(
        target=_create_runtime_ws, args=(sid, my_gen),
        daemon=True, name=f"ws-init-{sid}",
    ).start()


def _quiet_close_ws(ws):
    """后台线程安全地关闭废弃连接（不等待、不抛异常）"""
    try:
        ws.close()
    except Exception:
        pass


def _send_audio_to_runtime(sid, pcm_bytes):
    """转发音频到 Runtime（WS 未就绪时暂存，不阻塞事件循环）"""
    session = sessions.get(sid)
    if not session:
        return

    ws = session.get("runtime_ws")
    if ws is None or not session["ws_ready"].is_set():
        # 未就绪：暂存，连接建立后 on_open 会补发
        session["pending_audio"].append(pcm_bytes)
        if len(session["pending_audio"]) == 1:
            print(f"[Gateway] Audio queued for {sid} (WS not ready)")
            sys.stdout.flush()
        return

    try:
        import websocket
        ws.send(pcm_bytes, opcode=websocket.ABNF.OPCODE_BINARY)
    except Exception as e:
        print(f"[Gateway] Error sending audio for {sid}: {e}")
        sys.stdout.flush()
        # 发送失败：标记未就绪并暂存，等待重连补发
        session["runtime_ws"] = None
        session["ws_ready"].clear()
        session["pending_audio"].append(pcm_bytes)


def _close_runtime_ws(sid, session=None):
    """关闭会话的 Runtime WebSocket（保留 session，只复位连接与语音状态）

    session 参数允许调用方先将会话移出 sessions（阻止重连线程复活）后，
    再对同一对象执行关闭。
    """
    if session is None:
        session = sessions.get(sid)
    if not session:
        return

    ws = session.get("runtime_ws")
    if ws:
        try:
            # 发送结束信号，触发 Runtime 返回 final 结果
            ws.send(json.dumps({"is_speaking": False}))
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

    _audio_log_count.pop(sid, None)
    session["runtime_ws"] = None
    session["ws_ready"].clear()
    session["pending_audio"] = []
    _reset_speech_state(session)
    print(f"[Gateway] Closed Runtime WS for {sid} (session preserved)")
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
            session = sessions.get(sid)
            _close_runtime_ws(sid)
            sessions.pop(sid, None)  # 超时会话彻底删除
            _push_event(sid, session, 'recognition_result', {
                'text': '', 'is_final': True,
                'error': '会话超时'
            })


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


# ─── SocketIO 事件处理 ──────────────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    print(f"[+] SocketIO connect: {sid}")
    sys.stdout.flush()
    with _sessions_lock:
        if len(sessions) >= MAX_SESSIONS:
            emit('error', {'message': '服务器繁忙，请稍后重试'})
            return False
        sessions[sid] = _new_session()

    # 异步建立 Runtime WS 连接
    _start_runtime_ws_thread(sid)

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
                sessions.pop(sid, None)  # 断开连接彻底删除
            else:
                print(f"[Gateway] Skip cleanup for {sid} (session was reset)")
                sys.stdout.flush()

    # 标记当前 session 待清理
    sessions[sid]["_cleanup_pending"] = True
    threading.Thread(target=_delayed_cleanup, daemon=True, name=f"cleanup-{sid}").start()


_audio_log_count = {}


def _process_audio(sid, pcm_bytes):
    """音频处理核心：能量 VAD → speech_start / 静音兜底 → speech_end → 转发 Runtime

    SocketIO（浏览器）与内部 WS（MRCP 插件）共用此逻辑，保证状态机行为一致。
    """
    session = sessions.get(sid)
    if not session:
        return

    session["last_time"] = time.time()

    # 8kHz → 16kHz 重采样
    if session.get("sample_rate") == 8000:
        pcm_bytes = _resample_8k_to_16k(pcm_bytes)

    rms = _calc_rms(pcm_bytes)
    now = time.time()

    # ── 本轮最大 RMS 统计（全静音校验：整段无声则丢弃 Runtime 残留文本） ──
    if rms > session.get("max_rms", 0.0):
        session["max_rms"] = rms

    # ── 静音兜底：speech_start 后持续静音超时 → speech_end（携带已缓存文本） ──
    if session["speech_detected"]:
        last_speech = session.get("last_speech_time")
        if last_speech and (now - last_speech) * 1000 >= _RUNTIME_CFG["vad_silence_grace_ms"]:
            cached = session.get("last_result_text") or ""
            _emit_speech_end(sid, session, text=cached, reason="silence")

    # ── 能量 VAD：检测到说话 → 更新最后说话时间 / 首次触发 speech_start ──
    if rms >= _RUNTIME_CFG["vad_threshold"]:
        session["last_speech_time"] = now
        if not session["speech_detected"]:
            _emit_speech_start(sid, session, rms=rms)

    # 前3个音频块打日志，之后静默
    cnt = _audio_log_count.get(sid, 0)
    if cnt < 3:
        print(f"[Gateway] Audio chunk #{cnt+1} for {sid}: {len(pcm_bytes)} bytes rms={rms:.4f}")
        sys.stdout.flush()
        _audio_log_count[sid] = cnt + 1

    _send_audio_to_runtime(sid, pcm_bytes)


@socketio.on('audio')
def handle_audio_chunk(data):
    """SocketIO 音频入口：Base64 解码后进入共用处理逻辑"""
    sid = request.sid
    if sid not in sessions:
        return

    try:
        audio_b64 = data.get('audio', '') if isinstance(data, dict) else data
        pcm_bytes = base64.b64decode(audio_b64)
        _process_audio(sid, pcm_bytes)
    except Exception as e:
        print(f"[Gateway] Error processing audio for {sid}: {e}")
        sys.stdout.flush()
        emit('error', {'message': f'音频处理失败: {str(e)}'})


def _handle_start(sid, sample_rate, no_input_timeout=0):
    """开始识别 — 复位语音状态；Runtime WS 已关闭则重建连接（SocketIO / 内部 WS 共用）

    no_input_timeout: 毫秒。MRCP 场景：静音超时且未检测到语音时，
    主动发 finished（空文本）→ 插件转为 NO-INPUT-TIMEOUT 完成事件。
    """
    session = sessions.get(sid)
    if not session:
        return None

    session["last_time"] = time.time()
    session["sample_rate"] = sample_rate
    session["_end_token"] = None      # 取消上一轮 end 的延迟关闭（防竞态）
    _reset_speech_state(session)
    session["pending_audio"] = []
    session["no_input_sent"] = False
    session["_no_input_token"] = None
    session["max_rms"] = 0.0           # 新一轮：重置全静音校验计数

    # ── 轮次递增 + 作废上一轮 Runtime 连接（防跨轮状态残留污染） ──
    session["ws_generation"] = session.get("ws_generation", 0) + 1
    old_ws = session.get("runtime_ws")
    if old_ws is not None:
        session["runtime_ws"] = None
        session["ws_ready"].clear()
        threading.Thread(target=_quiet_close_ws, args=(old_ws,), daemon=True).start()
        print(f"[Gateway] Session {sid} superseded old Runtime WS (gen={session['ws_generation']})")
        sys.stdout.flush()

    # ── no-input 定时器：静音超时且未检测到语音 → finished 空文本 ──
    if no_input_timeout and no_input_timeout > 0:
        token = next(_end_token)
        session["_no_input_token"] = token

        def _no_input_check():
            time.sleep(no_input_timeout / 1000.0)
            if sid not in sessions or sessions[sid] is not session:
                return  # 会话已清理/重建
            if session.get("_no_input_token") != token:
                return  # 新一轮 start 已复位本定时器
            if session.get("speech_detected") or session.get("no_input_sent"):
                return  # 已检测到语音 / 已发送
            session["no_input_sent"] = True
            print(f"[Gateway] No-input timeout ({no_input_timeout}ms) for {sid}")
            sys.stdout.flush()
            _push_event(sid, session, 'finished', {'text': '', 'reason': 'no_input'})

        threading.Thread(
            target=_no_input_check, daemon=True, name=f"noinput-{sid}",
        ).start()

    # Runtime WS 已关闭且没有同代连接线程在建立中 → 重建
    if session.get("runtime_ws") is None and not session.get("ws_connecting"):
        print(f"[Gateway] Session {sid} rebuilding Runtime WS (gen={session['ws_generation']})")
        sys.stdout.flush()
        _start_runtime_ws_thread(sid, my_gen=session["ws_generation"])

    print(f"[Gateway] Session {sid} started (sample_rate={sample_rate}, no_input_timeout={no_input_timeout}ms)")
    sys.stdout.flush()
    return session


@socketio.on('start')
def handle_start(data):
    """前端开始录音（SocketIO 入口）"""
    sid = request.sid
    sample_rate = data.get('sample_rate', 16000) if data else 16000
    session = _handle_start(sid, sample_rate)
    if session:
        emit('started', {'message': '识别已开始', 'sample_rate': sample_rate})


_end_token = itertools.count()


def _handle_end(sid):
    """结束识别 — 发送结束信号，延迟关闭 Runtime WS（SocketIO / 内部 WS 共用）"""
    session = sessions.get(sid)
    if not session:
        return

    print(f"[Gateway] Session {sid} end signal")
    sys.stdout.flush()

    ws = session.get("runtime_ws")
    if ws:
        try:
            ws.send(json.dumps({"is_speaking": False}))
            print(f"[Gateway] Sent end signal for {sid}, waiting for final result...")
            sys.stdout.flush()
        except Exception:
            pass

    # 延迟关闭，等 Runtime 返回最终结果
    # token 防竞态：若期间用户重新 start，则跳过本次关闭
    token = next(_end_token)
    session["_end_token"] = token

    def _delayed_close():
        time.sleep(3.0)
        if sid in sessions and sessions[sid].get("_end_token") == token:
            _close_runtime_ws(sid)
            _push_event(sid, sessions.get(sid), 'finished', {'text': '', 'duration': 0})

    threading.Thread(target=_delayed_close, daemon=True, name=f"end-close-{sid}").start()


@socketio.on('end')
def handle_end():
    """前端停止录音（SocketIO 入口）"""
    sid = request.sid
    _handle_end(sid)


@socketio.on('reset')
def handle_reset():
    """重置会话：关闭旧连接，用全新会话状态重建"""
    sid = request.sid
    if sid not in sessions:
        return

    _close_runtime_ws(sid)

    with _sessions_lock:
        sessions[sid] = _new_session()

    _start_runtime_ws_thread(sid)

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


@app.route('/admin/vad', methods=['GET', 'POST'])
def admin_vad():
    """VAD 参数热调接口（需 X-Admin-Token 鉴权，即时生效，无需重启）

    GET  /admin/vad                           → 返回当前 VAD 参数
    POST /admin/vad {"threshold": 0.035}      → 热调能量阈值（0~1 之间）
    POST /admin/vad {"silence_grace_ms": 3000} → 热调静音兜底时长（100~60000 毫秒）
    """
    if request.headers.get('X-Admin-Token', '') != _ADMIN_TOKEN:
        return {'error': 'forbidden'}, 403
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        if 'threshold' in data:
            try:
                t = float(data['threshold'])
            except (TypeError, ValueError):
                return {'error': 'threshold must be a float in (0, 1)'}, 400
            if not 0.0 < t < 1.0:
                return {'error': 'threshold must be in (0, 1)'}, 400
            _RUNTIME_CFG['vad_threshold'] = t
            print(f"[Admin] vad_threshold hot-updated to {t}")
        if 'silence_grace_ms' in data:
            try:
                g = int(data['silence_grace_ms'])
            except (TypeError, ValueError):
                return {'error': 'silence_grace_ms must be an int'}, 400
            if not 100 <= g <= 60000:
                return {'error': 'silence_grace_ms must be in [100, 60000]'}, 400
            _RUNTIME_CFG['vad_silence_grace_ms'] = g
            print(f"[Admin] vad_silence_grace_ms hot-updated to {g}")
        if 'stale_text_rms' in data:
            try:
                s = float(data['stale_text_rms'])
            except (TypeError, ValueError):
                return {'error': 'stale_text_rms must be a float in (0, 0.1)'}, 400
            if not 0.0 < s < 0.1:
                return {'error': 'stale_text_rms must be in (0, 0.1)'}, 400
            _RUNTIME_CFG['stale_text_rms'] = s
            print(f"[Admin] stale_text_rms hot-updated to {s}")
    return {
        'vad_threshold': _RUNTIME_CFG['vad_threshold'],
        'vad_silence_grace_ms': _RUNTIME_CFG['vad_silence_grace_ms'],
        'stale_text_rms': _RUNTIME_CFG['stale_text_rms'],
    }


# ─── 内部 WS 通道（MRCP 插件接入） ───────────────────────────────────────────
#
# 协议（极简，便于 C++ 插件实现）:
#   上行 文本帧  : {"type": "start", "sample_rate": 8000} / {"type": "end"}
#   上行 二进制帧: PCM 音频（8kHz 16bit mono，G.711 解码后）
#   下行 文本帧  : {"event": "speech_start|recognition_result|speech_end|finished|error",
#                   "data": {...}}
#
# 每个内部 WS 连接 = 一个识别会话，复用全部现有逻辑（License 限流、
# 语音状态机、8kHz 重采样、Runtime 转发），不重复实现。

async def _internal_ws_handler(ws):
    """内部 WS 通道会话处理"""
    sid = f"mrcp-{uuid.uuid4().hex[:8]}"

    with _sessions_lock:
        if len(sessions) >= MAX_SESSIONS:
            try:
                await ws.send(json.dumps({
                    "event": "error",
                    "data": {"message": "服务器繁忙，请稍后重试"},
                }, ensure_ascii=False))
                await ws.close()
            except Exception:
                pass
            return
        session = _new_session()
        session["internal_ws"] = ws
        session["internal_loop"] = asyncio.get_running_loop()
        sessions[sid] = session

    _start_runtime_ws_thread(sid)
    print(f"[Internal] WS connected: {sid} (total: {len(sessions)})")
    sys.stdout.flush()

    try:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                # 二进制帧：PCM 音频 → 共用音频处理逻辑
                _process_audio(sid, bytes(message))
                continue

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue

            mtype = data.get("type")
            if mtype == "start":
                sample_rate = data.get("sample_rate", 8000)
                no_input_timeout = int(data.get("no_input_timeout", 0) or 0)
                _handle_start(sid, sample_rate, no_input_timeout=no_input_timeout)
                _push_event(sid, session, 'started', {
                    'message': '识别已开始',
                    'sample_rate': sample_rate,
                })
            elif mtype == "end":
                _handle_end(sid)
    except Exception as e:
        print(f"[Internal] WS error for {sid}: {e}")
        sys.stdout.flush()
    finally:
        # 先将会话移出 sessions（阻止 Runtime WS 重连线程复活），再异步关闭。
        # 注意：_close_runtime_ws 内含 sleep(1)，必须在后台线程执行，
        # 否则会阻塞内部 WS 事件循环（所有并发 MRCP 会话的收发都会卡住）。
        session = sessions.pop(sid, None)
        if session:
            session["internal_ws"] = None
            session["internal_loop"] = None
        threading.Thread(target=_close_runtime_ws, args=(sid, session), daemon=True).start()
        print(f"[Internal] WS closed: {sid} (total: {len(sessions)})")
        sys.stdout.flush()


def _start_internal_ws_server():
    """启动内部 WS 服务器（独立线程 + asyncio 事件循环）"""

    async def _run():
        import websockets
        try:
            async with websockets.serve(
                _internal_ws_handler, "0.0.0.0", INTERNAL_WS_PORT,
                max_size=8 * 1024 * 1024,
            ):
                await asyncio.Future()  # 永久运行
        except Exception as e:
            print(f"[Internal] WS server fatal error: {e}")
            sys.stdout.flush()

    try:
        asyncio.run(_run())
    except Exception as e:
        print(f"[Internal] WS server thread error: {e}")
        sys.stdout.flush()


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
    print(f"Internal WS  : 0.0.0.0:{INTERNAL_WS_PORT}")
    print(f"{'='*60}\n")

    # 启动超时监控线程
    threading.Thread(
        target=_session_timeout_monitor,
        daemon=True, name="session-monitor",
    ).start()
    print("[Gateway] Session timeout monitor started")

    # 启动内部 WS 通道（MRCP 插件接入，仅内网访问）
    threading.Thread(
        target=_start_internal_ws_server,
        daemon=True, name="internal-ws",
    ).start()
    print(f"[Gateway] Internal WS channel listening on 0.0.0.0:{INTERNAL_WS_PORT}")

    print(f"\n[Gateway] Listening on {HOST}:{PORT}\n")

    try:
        socketio.run(app, host=HOST, port=PORT, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_event.set()
    print("[Gateway] Shutdown complete")
