"""
电话场景模拟测试 — 多路并发 × 3分钟真实语音流

模拟真实电话场景:
  - 每路通话 3 分钟
  - 20ms 帧发送（320 samples × 2 bytes = 640 bytes/frame）
  - 实时发送速率（不突发，模拟真实网络）
  - 每 10 秒记录一次延迟快照
  - 最终生成性能报告

用法:
  python tests/phone_call_test.py [--sessions 3] [--duration 180]
"""
import sys
import os
import time
import base64
import json
import threading
import argparse
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    import socketio
except ImportError:
    import subprocess
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'python-socketio[client]'], capture_output=True)
    import socketio

import numpy as np

# ─── 配置 ───
SERVER = 'http://8.153.92.96:5002'
PCM_FILE = os.path.join(os.path.dirname(__file__), 'test_speech_3min.pcm')
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit
FRAME_MS = 20     # 20ms per frame (电话标准)
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 320 samples
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH      # 640 bytes


class PhoneCallSession:
    """模拟一路电话通话"""

    def __init__(self, session_id, server_url, pcm_data, duration_sec):
        self.session_id = session_id
        self.server_url = server_url
        self.pcm_data = pcm_data
        self.duration_sec = duration_sec
        self.sio = None
        self.lock = threading.Lock()

        # 统计
        self.send_count = 0
        self.recv_count = 0
        self.total_latency = 0.0
        self.max_latency = 0.0
        self.min_latency = float('inf')
        self.latency_samples = []  # (timestamp, latency)
        self.text_results = []
        self.errors = []
        self.connected = False
        self.start_time = None
        self.end_time = None

    def run(self):
        """执行一路通话"""
        self.sio = socketio.Client()
        result_event = threading.Event()

        @self.sio.on('connected')
        def on_connected(data):
            self.connected = True

        @self.sio.on('recognition_result')
        def on_result(data):
            text = data.get('text', '')
            is_final = data.get('is_final', False)
            err = data.get('error', '')

            with self.lock:
                if err:
                    self.errors.append(err)
                elif text:
                    self.recv_count += 1
                    # 计算延迟：从发送到收到结果
                    now = time.time()
                    # 使用最近的发送时间
                    if self._last_send_time:
                        latency = now - self._last_send_time
                        self.total_latency += latency
                        self.max_latency = max(self.max_latency, latency)
                        self.min_latency = min(self.min_latency, latency)
                        self.latency_samples.append((now - self.start_time, latency))
                    self.text_results.append(text)

        self._last_send_time = None
        self.start_time = time.time()

        try:
            self.sio.connect(self.server_url, wait_timeout=10)
            time.sleep(0.3)

            # 按实时速率发送音频帧
            total_frames = self.duration_sec * 1000 // FRAME_MS  # 总帧数
            pcm_offset = 0
            pcm_len = len(self.pcm_data)

            for frame_idx in range(total_frames):
                # 取帧（循环使用音频数据）
                start = pcm_offset % pcm_len
                end = start + FRAME_BYTES
                if end > pcm_len:
                    frame = self.pcm_data[start:] + self.pcm_data[:end - pcm_len]
                else:
                    frame = self.pcm_data[start:end]
                pcm_offset += FRAME_BYTES

                # Base64 编码并发送
                is_final = (frame_idx == total_frames - 1)
                pcm_b64 = base64.b64encode(frame).decode()
                self._last_send_time = time.time()
                self.sio.emit('audio', {'audio': pcm_b64, 'is_final': is_final})

                with self.lock:
                    self.send_count += 1

                # 实时等待（20ms 间隔）
                time.sleep(FRAME_MS / 1000.0)

            # 等待最后的结果
            time.sleep(2)
            self.end_time = time.time()

        except Exception as e:
            self.errors.append(str(e))
        finally:
            try:
                self.sio.disconnect()
            except:
                pass

    def snapshot(self):
        """返回当前统计快照"""
        with self.lock:
            elapsed = (self.end_time or time.time()) - self.start_time if self.start_time else 0
            avg_latency = self.total_latency / self.recv_count if self.recv_count > 0 else 0
            return {
                'session': self.session_id,
                'elapsed': f"{elapsed:.0f}s",
                'sent_frames': self.send_count,
                'recv_results': self.recv_count,
                'avg_latency': f"{avg_latency:.3f}s",
                'max_latency': f"{self.max_latency:.3f}s",
                'min_latency': f"{self.min_latency:.3f}s" if self.min_latency < float('inf') else 'N/A',
                'errors': len(self.errors),
                'connected': self.connected,
            }


def run_test(num_sessions=3, duration_sec=180):
    """执行多路并发测试"""
    # 加载 PCM 音频
    if not os.path.exists(PCM_FILE):
        print(f"ERROR: PCM file not found: {PCM_FILE}")
        print("Run tests/gen_speech.py first to generate test audio.")
        return

    with open(PCM_FILE, 'rb') as f:
        pcm_data = f.read()

    pcm_duration = len(pcm_data) / (SAMPLE_RATE * SAMPLE_WIDTH)
    print(f"=" * 70)
    print(f" 电话场景模拟测试 — ONNX 流式语音识别")
    print(f"=" * 70)
    print(f" Server       : {SERVER}")
    print(f" Audio file   : {PCM_FILE}")
    print(f" Audio dur    : {pcm_duration:.1f}s")
    print(f" Sessions     : {num_sessions}")
    print(f" Duration     : {duration_sec}s ({duration_sec/60:.1f} min)")
    print(f" Frame size   : {FRAME_MS}ms ({FRAME_BYTES} bytes)")
    print(f" Total frames : {num_sessions * duration_sec * 1000 // FRAME_MS}")
    print(f"=" * 70)

    # 检查服务器健康
    from ops.config import ssh_exec
    health_out, _ = ssh_exec(f"curl -s http://localhost:5002/health 2>/dev/null")
    if health_out and '"status"' in health_out:
        health = json.loads(health_out)
        print(f"\n Server status: {health['status']}")
        print(f" Engine: {health['engine']}, Workers: {health['workers']['alive']}/{health['workers']['total']}")
    else:
        print(" WARNING: Could not check server health!")

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Starting {num_sessions} concurrent calls...")

    # 启动并发会话
    sessions = []
    threads = []
    for i in range(num_sessions):
        s = PhoneCallSession(i, SERVER, pcm_data, duration_sec)
        sessions.append(s)
        t = threading.Thread(target=s.run, name=f"call-{i}")
        threads.append(t)

    # 同时启动所有通话
    t0 = time.time()
    for t in threads:
        t.start()

    # 每 10 秒打印一次快照
    snapshot_interval = 10
    snapshots = []
    while True:
        elapsed = time.time() - t0
        if elapsed >= duration_sec + 10:
            break
        time.sleep(snapshot_interval)

        snap_time = f"{elapsed:.0f}s"
        print(f"\n  [{snap_time:>5s}] ──── Snapshot ────")
        total_recv = 0
        for s in sessions:
            snap = s.snapshot()
            total_recv += s.recv_count
            print(f"    Call-{snap['session']}: sent={snap['sent_frames']:>4d} recv={snap['recv_results']:>3d} "
                  f"avg={snap['avg_latency']:>7s} max={snap['max_latency']:>7s} err={snap['errors']}")
        snapshots.append({
            'time': snap_time,
            'total_recv': total_recv,
            'details': [s.snapshot() for s in sessions],
        })

    # 等待所有线程结束
    for t in threads:
        t.join(timeout=30)

    total_time = time.time() - t0

    # ─── 最终报告 ───
    print(f"\n{'=' * 70}")
    print(f" 测试报告 — ONNX 流式语音识别性能")
    print(f"{'=' * 70}")
    print(f" Test time    : {total_time:.1f}s")
    print(f" Sessions     : {num_sessions}")
    print(f" Duration/call: {duration_sec}s")

    total_sent = sum(s.send_count for s in sessions)
    total_recv = sum(s.recv_count for s in sessions)
    total_errors = sum(len(s.errors) for s in sessions)
    all_latencies = []
    for s in sessions:
        all_latencies.extend([lat for _, lat in s.latency_samples])

    print(f"\n ─── 总体统计 ───")
    print(f" Total frames sent : {total_sent}")
    print(f" Total results recv: {total_recv}")
    print(f" Total errors      : {total_errors}")
    print(f" Result rate       : {total_recv/total_sent*100:.1f}%" if total_sent > 0 else " N/A")

    if all_latencies:
        avg_lat = sum(all_latencies) / len(all_latencies)
        max_lat = max(all_latencies)
        min_lat = min(all_latencies)
        p50 = np.percentile(all_latencies, 50)
        p90 = np.percentile(all_latencies, 90)
        p95 = np.percentile(all_latencies, 95)
        p99 = np.percentile(all_latencies, 99)

        print(f"\n ─── 延迟统计 ───")
        print(f" Average  : {avg_lat:.3f}s")
        print(f" Min      : {min_lat:.3f}s")
        print(f" Max      : {max_lat:.3f}s")
        print(f" P50      : {p50:.3f}s")
        print(f" P90      : {p90:.3f}s")
        print(f" P95      : {p95:.3f}s")
        print(f" P99      : {p99:.3f}s")

    print(f"\n ─── 各路详情 ───")
    for s in sessions:
        snap = s.snapshot()
        print(f" Call-{snap['session']}: sent={snap['sent_frames']} recv={snap['recv_results']} "
              f"avg={snap['avg_latency']} max={snap['max_latency']} err={snap['errors']}")

    # 识别文本采样
    print(f"\n ─── 识别文本采样（前 5 条）───")
    all_texts = []
    for s in sessions:
        all_texts.extend(s.text_results[:5])
    for i, text in enumerate(all_texts[:5]):
        print(f"   [{i+1}] {text[:60]}")

    # 服务器状态
    print(f"\n ─── 服务器最终状态 ───")
    health_out, _ = ssh_exec(f"curl -s http://localhost:5002/health 2>/dev/null")
    if health_out:
        print(f" {health_out}")

    # 服务器日志
    print(f"\n ─── 服务器日志（最后 10 行）───")
    logs, _ = ssh_exec("docker logs funasr-stream --tail 10 2>&1")
    for line in logs.strip().split('\n')[-10:]:
        print(f" {line}")

    print(f"\n{'=' * 70}")
    print(f" 测试完成！ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 70}")

    # 保存 JSON 报告
    report = {
        'test_time': datetime.now().isoformat(),
        'server': SERVER,
        'engine': 'funasr-onnx',
        'sessions': num_sessions,
        'duration_per_call': duration_sec,
        'frame_ms': FRAME_MS,
        'total_time': total_time,
        'total_sent': total_sent,
        'total_recv': total_recv,
        'total_errors': total_errors,
        'latency': {
            'avg': avg_lat if all_latencies else None,
            'min': min_lat if all_latencies else None,
            'max': max_lat if all_latencies else None,
            'p50': p50 if all_latencies else None,
            'p90': p90 if all_latencies else None,
            'p95': p95 if all_latencies else None,
            'p99': p99 if all_latencies else None,
        },
        'snapshots': snapshots,
    }
    report_path = os.path.join(os.path.dirname(__file__), 'phone_call_report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n Report saved: {report_path}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sessions', type=int, default=3, help='并发通话数')
    parser.add_argument('--duration', type=int, default=180, help='每路通话时长(秒)')
    args = parser.parse_args()
    run_test(args.sessions, args.duration)
