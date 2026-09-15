"""
流式 ASR 性能测试
测试指标：
  1. 单路延迟 (Latency) — 每个 chunk 从发送到收到结果的耗时
  2. 实时因子 (RTF)     — 处理耗时 / 音频时长，< 1 表示比实时快
  3. 端到端耗时         — 完整音频从开始发送到最终结果的总耗时
  4. 并发路数           — 多路同时识别的稳定性

用法：
  python tests/perf_test.py                    # 默认单路测试
  python tests/perf_test.py --concurrent 5     # 5路并发测试
  python tests/perf_test.py --duration 30      # 使用30秒音频
"""
import sys
import os
import time
import base64
import argparse
import threading
import numpy as np
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import socketio

SERVER = "http://8.153.92.96:5002"
SAMPLE_RATE = 16000
CHUNK_MS = 600  # 每 chunk 毫秒数
CHUNK_BYTES = int(SAMPLE_RATE * CHUNK_MS / 1000) * 2  # 16bit = 2 bytes/sample


def load_pcm_from_server(duration=15):
    """从服务器下载 WAV 并转为 PCM 16kHz 16bit mono"""
    from ops.config import ssh_exec

    # 用 ffmpeg 在服务器端转换 1.wav 为指定时长的 PCM
    out, err = ssh_exec(
        f"ffmpeg -y -i /opt/funasr/1.wav -ar 16000 -ac 1 -f s16le "
        f"-t {duration} - 2>/dev/null | base64"
    )
    if not out.strip():
        print(f"[WARN] 服务器 WAV 转换失败，使用合成音频")
        return generate_speech_like_audio(duration)

    pcm = base64.b64decode(out.strip())
    print(f"[OK] 从服务器加载音频: {len(pcm)} bytes ({len(pcm)/SAMPLE_RATE/2:.1f}s)")
    return pcm


def generate_speech_like_audio(duration=15):
    """生成模拟语音的测试音频（多频率叠加 + 静音段）"""
    total_samples = int(SAMPLE_RATE * duration)
    audio = np.zeros(total_samples, dtype=np.float32)

    # 生成多段不同频率的信号模拟语音
    segments = [
        (0, 3, 300, 400),    # 0-3s: 300-400Hz
        (3, 6, 500, 700),    # 3-6s: 500-700Hz
        (6, 9, 200, 350),    # 6-9s: 200-350Hz
        (9, 12, 600, 800),   # 9-12s: 600-800Hz
        (12, 15, 250, 450),  # 12-15s: 250-450Hz
    ]
    for start, end, f_low, f_high in segments:
        s = int(start * SAMPLE_RATE)
        e = int(end * SAMPLE_RATE)
        t = np.linspace(0, end - start, e - s, endpoint=False)
        freq = f_low + (f_high - f_low) * t / (end - start)
        segment = np.sin(2 * np.pi * freq * t) * 12000
        audio[s:e] = segment

    return (audio.astype(np.int16)).tobytes()


class PerfSession:
    """单路性能测试会话"""

    def __init__(self, session_id, pcm_data):
        self.session_id = session_id
        self.pcm_data = pcm_data
        self.latencies = []       # 每个 chunk 的延迟 (ms)
        self.results = []         # 识别结果
        self.chunk_times = []     # 每个 chunk 的发送/接收时间戳
        self.start_time = None
        self.end_time = None
        self.connected = False
        self.sio = None

    def run(self):
        """执行单路测试"""
        self.sio = socketio.Client()
        self._setup_handlers()

        try:
            self.sio.connect(SERVER)
        except Exception as e:
            print(f"  [Session {self.session_id}] 连接失败: {e}")
            return

        # 等待连接确认
        for _ in range(50):
            if self.connected:
                break
            time.sleep(0.1)

        if not self.connected:
            print(f"  [Session {self.session_id}] 连接超时")
            self.sio.disconnect()
            return

        # 发送音频 chunks
        self.start_time = time.time()
        offset = 0
        chunk_num = 0

        while offset < len(self.pcm_data):
            end = min(offset + CHUNK_BYTES, len(self.pcm_data))
            chunk = self.pcm_data[offset:end]
            is_final = (end >= len(self.pcm_data))

            b64 = base64.b64encode(chunk).decode()
            send_time = time.time()

            self.sio.emit('audio', {'audio': b64, 'is_final': is_final})

            chunk_num += 1
            # 记录发送时间，等待结果
            self.chunk_times.append({
                'chunk': chunk_num,
                'send_time': send_time,
                'is_final': is_final,
            })

            offset = end
            # 模拟实时流：按音频时长的比例等待
            time.sleep(CHUNK_MS / 1000.0)

        # 等待最后的结果
        time.sleep(2)
        self.end_time = time.time()

        self.sio.disconnect()

    def _setup_handlers(self):
        @self.sio.on('connected')
        def on_connected(data):
            self.connected = True

        @self.sio.on('recognition_result')
        def on_result(data):
            recv_time = time.time()
            # 计算延迟：当前接收时间与最近一次发送时间的差
            if self.chunk_times:
                last_send = self.chunk_times[-1]['send_time']
                latency = (recv_time - last_send) * 1000
                self.latencies.append(latency)

            text = data.get('text', '')
            is_final = data.get('is_final', False)
            self.results.append({
                'text': text,
                'is_final': is_final,
                'recv_time': recv_time,
            })

    def get_stats(self):
        """返回性能统计"""
        total_time = self.end_time - self.start_time if self.start_time and self.end_time else 0
        audio_duration = len(self.pcm_data) / SAMPLE_RATE / 2  # 16bit = 2 bytes

        stats = {
            'session_id': self.session_id,
            'audio_duration': audio_duration,
            'total_time': total_time,
            'rtf': total_time / audio_duration if audio_duration > 0 else 0,
            'chunks_sent': len(self.chunk_times),
            'results_count': len(self.results),
            'final_results': sum(1 for r in self.results if r['is_final']),
        }

        if self.latencies:
            stats['latency_avg'] = np.mean(self.latencies)
            stats['latency_p50'] = np.percentile(self.latencies, 50)
            stats['latency_p95'] = np.percentile(self.latencies, 95)
            stats['latency_p99'] = np.percentile(self.latencies, 99)
            stats['latency_max'] = np.max(self.latencies)
            stats['latency_min'] = np.min(self.latencies)

        # 最终识别文本
        final_texts = [r['text'] for r in self.results if r['is_final'] and r['text']]
        stats['final_text'] = ' '.join(final_texts) if final_texts else '(无识别结果)'

        return stats


def print_report(all_stats):
    """打印性能报告"""
    print("\n" + "=" * 70)
    print("  流式 ASR 性能测试报告")
    print("=" * 70)

    for stats in all_stats:
        sid = stats['session_id']
        print(f"\n── Session {sid} ──────────────────────────────────")
        print(f"  音频时长      : {stats['audio_duration']:.1f}s")
        print(f"  端到端耗时    : {stats['total_time']:.2f}s")
        print(f"  实时因子(RTF) : {stats['rtf']:.3f} {'[OK] 实时' if stats['rtf'] < 1 else '[!!] 超时'}")
        print(f"  发送 chunks   : {stats['chunks_sent']}")
        print(f"  收到结果      : {stats['results_count']} (final: {stats['final_results']})")

        if 'latency_avg' in stats:
            print(f"\n  延迟统计 (ms):")
            print(f"    最小   : {stats['latency_min']:.1f}")
            print(f"    平均   : {stats['latency_avg']:.1f}")
            print(f"    P50    : {stats['latency_p50']:.1f}")
            print(f"    P95    : {stats['latency_p95']:.1f}")
            print(f"    P99    : {stats['latency_p99']:.1f}")
            print(f"    最大   : {stats['latency_max']:.1f}")

        print(f"\n  识别文本: {stats['final_text'][:100]}")

    # 汇总
    if len(all_stats) > 1:
        print(f"\n── 汇总 ──────────────────────────────────────")
        rtfs = [s['rtf'] for s in all_stats]
        print(f"  并发路数    : {len(all_stats)}")
        print(f"  RTF 范围    : {min(rtfs):.3f} ~ {max(rtfs):.3f}")
        print(f"  RTF 平均    : {np.mean(rtfs):.3f}")
        all_pass = all(s['rtf'] < 1 for s in all_stats)
        print(f"  全部实时    : {'[OK] PASS' if all_pass else '[!!] FAIL'}")

    print("\n" + "=" * 70)


def main():
    parser = argparse.ArgumentParser(description='流式 ASR 性能测试')
    parser.add_argument('--duration', type=int, default=15, help='测试音频时长(秒)')
    parser.add_argument('--concurrent', type=int, default=1, help='并发路数')
    parser.add_argument('--synthetic', action='store_true', help='使用合成音频而非真实音频')
    args = parser.parse_args()

    print(f"流式 ASR 性能测试")
    print(f"  服务器: {SERVER}")
    print(f"  音频时长: {args.duration}s")
    print(f"  并发路数: {args.concurrent}")
    print(f"  Chunk 大小: {CHUNK_MS}ms ({CHUNK_BYTES} bytes)")
    print()

    # 加载测试音频
    if args.synthetic:
        print("[INFO] 使用合成音频")
        pcm_data = generate_speech_like_audio(args.duration)
    else:
        pcm_data = load_pcm_from_server(args.duration)

    print(f"  音频数据: {len(pcm_data)} bytes ({len(pcm_data)/SAMPLE_RATE/2:.1f}s)")
    print()

    # 执行测试
    if args.concurrent <= 1:
        # 单路测试
        print("── 单路性能测试 ──")
        session = PerfSession(1, pcm_data)
        session.run()
        stats = session.get_stats()
        print_report([stats])
    else:
        # 并发测试
        print(f"── {args.concurrent} 路并发测试 ──")
        sessions = []
        threads = []

        for i in range(args.concurrent):
            s = PerfSession(i + 1, pcm_data)
            sessions.append(s)
            t = threading.Thread(target=s.run)
            threads.append(t)

        # 同时启动所有线程
        start = time.time()
        for t in threads:
            t.start()

        # 等待全部完成
        for t in threads:
            t.join(timeout=args.duration * 3 + 30)

        total_wall = time.time() - start
        print(f"  总耗时 (wall clock): {total_wall:.2f}s")

        all_stats = [s.get_stats() for s in sessions if s.start_time]
        print_report(all_stats)


if __name__ == '__main__':
    main()
