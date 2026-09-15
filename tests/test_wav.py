"""用本地 tts_output.wav 测试流式 ASR 识别效果"""
import sys
import os
import time
import base64
import wave
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import socketio

SERVER = "http://8.153.92.96:5002"
WAV_PATH = r"c:\Users\WUYALI\Desktop\tts_output.wav"
SAMPLE_RATE = 16000
CHUNK_MS = 600
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_MS / 1000)
CHUNK_BYTES = CHUNK_SAMPLES * 2  # 16bit


def load_wav_as_pcm(wav_path):
    """读取 WAV 文件，转为 PCM 16kHz 16bit mono"""
    with wave.open(wav_path, 'rb') as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    print(f"  原始格式: {framerate}Hz, {n_channels}ch, {sampwidth*8}bit, {n_frames} frames")
    print(f"  原始时长: {n_frames/framerate:.2f}s")

    # 转为 int16
    if sampwidth == 2:
        samples = np.frombuffer(raw, dtype=np.int16)
    elif sampwidth == 4:
        samples = np.frombuffer(raw, dtype=np.int32)
        samples = (samples >> 16).astype(np.int16)
    elif sampwidth == 1:
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128
        samples = samples * 256
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth}")

    # 转单声道
    if n_channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1).astype(np.int16)
    elif n_channels > 2:
        samples = samples.reshape(-1, n_channels).mean(axis=1).astype(np.int16)

    # 重采样到 16kHz
    if framerate != SAMPLE_RATE:
        duration = len(samples) / framerate
        new_length = int(duration * SAMPLE_RATE)
        indices = np.linspace(0, len(samples) - 1, new_length)
        samples = np.interp(indices, np.arange(len(samples)), samples.astype(np.float64)).astype(np.int16)
        print(f"  重采样: {framerate}Hz -> {SAMPLE_RATE}Hz")

    pcm = samples.tobytes()
    print(f"  转换后: {SAMPLE_RATE}Hz, 1ch, 16bit, {len(pcm)} bytes ({len(pcm)/SAMPLE_RATE/2:.2f}s)")
    return pcm


def main():
    if not os.path.exists(WAV_PATH):
        print(f"[ERROR] 文件不存在: {WAV_PATH}")
        return

    print(f"流式 ASR 识别测试")
    print(f"  音频文件: {WAV_PATH}")
    print(f"  服务器: {SERVER}")
    print()

    pcm_data = load_wav_as_pcm(WAV_PATH)
    audio_duration = len(pcm_data) / SAMPLE_RATE / 2

    # WebSocket 测试
    sio = socketio.Client()
    results = []
    all_texts = []
    latencies = []
    chunk_times = []
    connected = False
    start_time = None
    end_time = None

    @sio.on('connected')
    def on_connected(data):
        nonlocal connected
        connected = True

    @sio.on('connect')
    def on_connect():
        print("[OK] WebSocket 已连接")

    @sio.on('recognition_result')
    def on_result(data):
        recv_time = time.time()
        text = data.get('text', '')
        is_final = data.get('is_final', False)

        if chunk_times:
            latency = (recv_time - chunk_times[-1]['send_time']) * 1000
            latencies.append(latency)

        results.append({
            'text': text,
            'is_final': is_final,
            'latency': latencies[-1] if latencies else 0,
        })
        if text:
            all_texts.append(text)
            tag = "[final]" if is_final else "[partial]"
            print(f"  {tag} '{text}' (latency: {latencies[-1]:.0f}ms)" if latencies else f"  {tag} '{text}'")

    @sio.on('error')
    def on_error(data):
        print(f"[Error] {data}")

    @sio.on('disconnect')
    def on_disconnect():
        pass

    print(f"\n连接中...")
    sio.connect(SERVER)
    time.sleep(1)

    if not connected:
        print("[ERROR] 连接失败")
        return

    # 发送音频
    print(f"\n发送音频 ({audio_duration:.1f}s, {len(pcm_data)//CHUNK_BYTES + 1} chunks)...")
    start_time = time.time()
    offset = 0
    chunk_num = 0

    while offset < len(pcm_data):
        end = min(offset + CHUNK_BYTES, len(pcm_data))
        chunk = pcm_data[offset:end]
        is_final = (end >= len(pcm_data))

        b64 = base64.b64encode(chunk).decode()
        send_time = time.time()
        sio.emit('audio', {'audio': b64, 'is_final': is_final})

        chunk_num += 1
        chunk_times.append({'chunk': chunk_num, 'send_time': send_time, 'is_final': is_final})

        offset = end
        time.sleep(CHUNK_MS / 1000.0)

    # 等待最终结果
    time.sleep(3)
    end_time = time.time()
    sio.disconnect()

    # 输出报告
    total_time = end_time - start_time
    print(f"\n{'='*60}")
    print(f"  测试结果")
    print(f"{'='*60}")
    print(f"  音频时长      : {audio_duration:.2f}s")
    print(f"  端到端耗时    : {total_time:.2f}s")
    print(f"  实时因子(RTF) : {total_time/audio_duration:.3f}")
    print(f"  发送 chunks   : {chunk_num}")
    print(f"  收到结果      : {len(results)}")

    final_results = [r for r in results if r['is_final'] and r['text']]
    print(f"  最终结果      : {len(final_results)}")

    if latencies:
        print(f"\n  延迟 (ms):")
        print(f"    最小   : {min(latencies):.1f}")
        print(f"    平均   : {np.mean(latencies):.1f}")
        print(f"    P50    : {np.percentile(latencies, 50):.1f}")
        print(f"    P95    : {np.percentile(latencies, 95):.1f}")
        print(f"    最大   : {max(latencies):.1f}")

    # 完整识别文本
    print(f"\n  完整识别文本:")
    # 拼接所有 final 结果
    final_texts = [r['text'] for r in results if r['is_final'] and r['text']]
    full_text = ''.join(final_texts) if final_texts else ''
    if not full_text:
        # 如果没有 final，用最后一个 partial
        partials = [r['text'] for r in results if r['text']]
        full_text = partials[-1] if partials else '(无识别结果)'
    print(f"    {full_text}")

    print(f"\n{'='*60}")


if __name__ == '__main__':
    main()
