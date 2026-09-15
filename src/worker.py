"""
ASR 推理子进程 — ONNX Runtime 推理引擎

架构说明:
  主进程（网关）通过 multiprocessing.Queue 投递任务到本进程
  本进程使用 funasr_onnx.Paraformer (INT8 量化) 进行推理
  每 chunk 独立推理，RTF≈0.08，远快于实时

队列协议:
  任务队列 (gateway → worker):
    {"type": "infer",  "sid": str, "pcm": bytes, "is_final": bool}
    {"type": "reset",  "sid": str}
    {"type": "stop"}                        # 毒丸，退出进程

  结果队列 (worker → gateway):
    {"sid": str, "text": str, "is_final": bool}
    {"sid": str, "error": str}
    {"sid": str, "audio_saved": str}        # 音频文件保存完成通知
"""
import os
import sys
import time
import wave
import signal
import traceback
import multiprocessing

# 强制 unbuffered 输出，确保 Docker 日志实时可见
os.environ['PYTHONUNBUFFERED'] = '1'
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import numpy as np

# 忽略 SIGINT，由主进程统一处理退出
signal.signal(signal.SIGINT, signal.SIG_IGN)


# ONNX 模型路径（ModelScope 缓存目录）
_ONNX_MODEL_DIR = "/root/.cache/modelscope/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx"


def _load_model():
    """加载 Paraformer ONNX 量化模型（每个子进程独立调用一次）"""
    from funasr_onnx import Paraformer
    pid = os.getpid()
    print(f"[Worker-{pid}] Loading Paraformer ONNX model from {_ONNX_MODEL_DIR}...")
    t0 = time.time()
    model = Paraformer(_ONNX_MODEL_DIR, batch_size=1, quantize=True, intra_op_num_threads=1)
    print(f"[Worker-{pid}] ONNX Model loaded in {time.time() - t0:.1f}s")
    return model


def _save_audio(pcm_bytes, sid):
    """保存完整音频到 WAV 文件"""
    try:
        audio_dir = "/opt/streaming-asr/audio"
        os.makedirs(audio_dir, exist_ok=True)
        filename = f"{audio_dir}/{sid}_{int(time.time())}.wav"
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm_bytes)
        return filename
    except Exception as e:
        print(f"[Worker-{os.getpid()}] Error saving audio: {e}")
        return None


def _calc_energy(samples):
    """计算音频 RMS 能量（float32 samples, -1~1）"""
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples ** 2)))


# VAD 参数
_VAD_THRESHOLD = 0.01    # RMS 能量阈值（约 -40dB），低于此值视为静音
_VAD_HYSTERESIS = 0.005  # 滞后阈值，防止边界抖动


def run(task_q, result_q, worker_id):
    """
    推理子进程主循环

    Args:
        task_q:    multiprocessing.Queue, 接收网关投递的任务
        result_q:  multiprocessing.Queue, 向网关返回结果
        worker_id: int, 进程编号
    """
    pid = os.getpid()

    # ── CPU 线程限制：ONNX Runtime 单线程推理 ──
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"

    print(f"[Worker-{pid}] Starting (id={worker_id}, threads=1)")

    # 加载模型
    try:
        model = _load_model()
    except Exception as e:
        print(f"[Worker-{pid}] FATAL: Model load failed: {e}")
        traceback.print_exc()
        return

    # sid → {"pcm": bytes, "prev_text": str}
    sessions = {}
    print(f"[Worker-{pid}] Ready, waiting for tasks...")

    while True:
        try:
            task = task_q.get(timeout=5.0)
        except Exception:
            # queue.Empty 或其他中断，继续循环
            continue

        if task is None or task.get("type") == "stop":
            print(f"[Worker-{pid}] Received stop signal, shutting down...")
            break

        task_type = task.get("type")
        sid = task.get("sid")

        # ─── 推理任务 ───
        if task_type == "infer":
            pcm_bytes = task["pcm"]
            is_final = task.get("is_final", False)

            # 首次收到该 sid，初始化会话
            if sid not in sessions:
                sessions[sid] = {
                    "pcm": b"",            # 完整音频（用于保存）
                    "prev_full_text": "",  # 上一轮完整识别文本（增量去重）
                    "buf": b"",            # 推理缓冲区
                    "speaking": False,     # VAD 状态：是否在说话
                }

            session = sessions[sid]
            session["pcm"] += pcm_bytes
            session["buf"] += pcm_bytes

            # ─ VAD 静音检测 ──
            chunk_samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            energy = _calc_energy(chunk_samples)

            # 带滞后的 VAD：从静音→说话用高阈值，说话→静音用低阈值
            if not session["speaking"]:
                if energy >= _VAD_THRESHOLD:
                    session["speaking"] = True
            else:
                if energy < _VAD_HYSTERESIS:
                    session["speaking"] = False

            # 只在说话状态或 is_final 时才推理
            if not session["speaking"] and not is_final:
                continue

            # ── 缓冲策略：攒够 1s 再推理 ─
            # ONNX LFR 前端需要至少 ~0.5s 音频
            min_buf = 32000  # 1s @16kHz 16bit = 32000 bytes

            if is_final or len(session["buf"]) >= min_buf:
                # 用缓冲区的音频做推理
                infer_bytes = session["buf"]
                session["buf"] = b""
                samples = np.frombuffer(infer_bytes, dtype=np.int16).astype(np.float32) / 32768.0

                try:
                    result = model(samples)

                    text = ""
                    if result and len(result) > 0:
                        preds = result[0].get('preds', ('', []))
                        text = preds[0].strip() if isinstance(preds, tuple) else str(preds).strip()

                    # 增量文本提取：用最长公共前缀对比
                    prev_full = session.get('prev_full_text', '')
                    if text and prev_full:
                        # 找最长公共前缀长度
                        n = min(len(text), len(prev_full))
                        common = 0
                        for i in range(n):
                            if text[i] == prev_full[i]:
                                common = i + 1
                            else:
                                break
                        # 只有公共前缀超过一半才认为是同一句的延续
                        if common >= len(prev_full) * 0.5:
                            text = text[common:]
                        # 否则认为是全新内容，保留全部
                    if text:
                        session['prev_full_text'] = text  # 存完整文本
                        result_q.put({
                            "sid": sid,
                            "text": text,
                            "is_final": is_final,
                        })

                    # is_final: 保存音频 + 清理会话
                    if is_final:
                        filename = _save_audio(session["pcm"], sid)
                        if filename:
                            result_q.put({"sid": sid, "audio_saved": os.path.basename(filename)})
                        sessions.pop(sid, None)

                except Exception as e:
                    print(f"[Worker-{pid}] Inference error for {sid}: {e}")
                    traceback.print_exc()
                    result_q.put({"sid": sid, "error": str(e)})
                    if is_final:
                        sessions.pop(sid, None)

        # ─── 重置任务 ───
        elif task_type == "reset":
            if sid in sessions:
                del sessions[sid]
                print(f"[Worker-{pid}] Reset session {sid}")

        else:
            print(f"[Worker-{pid}] Unknown task type: {task_type}")

    # 退出前清理所有会话
    for sid in list(sessions.keys()):
        if sessions[sid]["pcm"]:
            _save_audio(sessions[sid]["pcm"], sid)
    print(f"[Worker-{pid}] Exited, cleaned up {len(sessions)} sessions")
