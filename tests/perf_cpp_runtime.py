"""FunASR C++ Runtime 性能测试 + 与 ONNX 版本对比"""
import sys, os, time, base64, struct, math, threading
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

import socketio
from ops.config import ssh_exec

SERVER = "http://8.153.92.96:5002"
SAMPLE_RATE = 16000
CHUNK_MS = 600
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_MS / 1000)
CHUNK_BYTES = CHUNK_SAMPLES * 2  # 16-bit mono


def gen_speech_like(duration):
    """生成模拟语音特征音频（多共振峰+幅度调制）"""
    sr = SAMPLE_RATE
    total = int(sr * duration)
    audio = np.zeros(total, dtype=np.float32)
    # 模拟5个"音节"段
    segs = [
        (0, 3, 300, 400), (3, 6, 500, 700), (6, 9, 200, 350),
        (9, 12, 600, 800), (12, max(13, duration), 250, 450)
    ]
    for s, e, fl, fh in segs:
        if s >= duration: break
        e = min(e, duration)
        ns, ne = int(s*sr), int(e*sr)
        t = np.linspace(0, e-s, ne-ns, endpoint=False)
        freq = fl + (fh-fl)*t/(e-s)
        # 幅度调制模拟音节节奏
        amp = 0.5 + 0.5 * np.sin(2*np.pi*4*t)
        audio[ns:ne] = np.sin(2*np.pi*freq*t) * 16000 * amp
    return audio.astype(np.int16).tobytes()


class Session:
    """单路会话"""
    def __init__(self, sid, pcm):
        self.sid = sid
        self.pcm = pcm
        self.latencies = []
        self.results = []
        self.send_times = []
        self.recv_times = []
        self.connected = False
        self.started = False
        self.finished = False
        self.start_time = None
        self.end_time = None
        self.sio = None
        self.error = None

    def run(self):
        self.sio = socketio.Client()

        @self.sio.on('connected')
        def _(d):
            self.connected = True

        @self.sio.on('started')
        def _(d):
            self.started = True

        @self.sio.on('recognition_result')
        def _(d):
            t = time.time()
            if self.send_times:
                lat = (t - self.send_times[-1]) * 1000
                self.latencies.append(lat)
            self.recv_times.append(t)
            self.results.append(d)

        @self.sio.on('finished')
        def _(d):
            self.finished = True

        @self.sio.on('connect_error')
        def _(e):
            self.error = str(e)

        try:
            self.sio.connect(SERVER, transports=['websocket'], wait_timeout=10)
        except Exception as e:
            self.error = str(e)
            return

        # 等待连接
        for _ in range(50):
            if self.connected: break
            time.sleep(0.1)
        if not self.connected:
            self.sio.disconnect()
            return

        # 发送 start
        self.sio.emit('start', {'sample_rate': SAMPLE_RATE})
        for _ in range(30):
            if self.started: break
            time.sleep(0.1)

        self.start_time = time.time()

        # 分块发送音频
        off = 0
        chunk_size = CHUNK_BYTES
        while off < len(self.pcm):
            end = min(off + chunk_size, len(self.pcm))
            chunk = self.pcm[off:end]
            b64 = base64.b64encode(chunk).decode()
            self.send_times.append(time.time())
            self.sio.emit('audio', {'audio': b64, 'is_final': False})
            off = end
            time.sleep(CHUNK_MS / 1000.0)

        # 等待2秒让结果返回
        time.sleep(2)

        # 发送 end
        self.sio.emit('end')
        time.sleep(3)

        self.end_time = time.time()
        self.sio.disconnect()

    def stats(self):
        dur = len(self.pcm) / SAMPLE_RATE / 2
        total = (self.end_time - self.start_time) if self.start_time and self.end_time else 0
        s = {
            'sid': self.sid,
            'audio_dur': dur,
            'total_time': total,
            'rtf': total / dur if dur > 0 else 99,
            'chunks_sent': len(self.send_times),
            'results_recv': len(self.results),
            'error': self.error,
        }
        if self.latencies:
            arr = np.array(self.latencies)
            s['lat_avg'] = float(np.mean(arr))
            s['lat_p50'] = float(np.percentile(arr, 50))
            s['lat_p95'] = float(np.percentile(arr, 95))
            s['lat_p99'] = float(np.percentile(arr, 99))
            s['lat_max'] = float(np.max(arr))
            s['lat_min'] = float(np.min(arr))
        return s


def get_server_resources():
    """采集服务器资源"""
    out, _ = ssh_exec(
        "echo '=== 系统 ===' && free -m | head -2 && echo '---' && "
        "nproc && echo '---' && "
        "echo '=== Docker ===' && docker stats --no-stream --format "
        "'{{.Name}}: CPU={{.CPUPerc}} MEM={{.MemUsage}}' funasr-runtime funasr-stream 2>/dev/null",
        timeout=15
    )
    return out


def main():
    print("=" * 70)
    print("  FunASR C++ Runtime 性能测试")
    print("=" * 70)
    print(f"  服务器: {SERVER}")
    print(f"  引擎: FunASR C++ Runtime (funasr-wss-server-2pass)")
    print(f"  模型: paraformer-large-onnx (INT8 quantized)")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Chunk: {CHUNK_MS}ms / {CHUNK_BYTES} bytes")
    print("=" * 70)
    print()

    all_reports = []

    # ── Test 1: 单路 15s ──
    print("[Test 1] 单路 15s ...")
    pcm = gen_speech_like(15)
    s = Session(1, pcm)
    s.run()
    st = s.stats()
    all_reports.append(('单路15s', st))
    print(f"  RTF={st['rtf']:.3f}  结果={st['results_recv']}  延迟avg={st.get('lat_avg',0):.0f}ms")

    # ── Test 2: 单路 30s ──
    print("[Test 2] 单路 30s ...")
    pcm = gen_speech_like(30)
    s = Session(1, pcm)
    s.run()
    st = s.stats()
    all_reports.append(('单路30s', st))
    print(f"  RTF={st['rtf']:.3f}  结果={st['results_recv']}  延迟avg={st.get('lat_avg',0):.0f}ms")

    # ─ Test 3: 单路 60s ──
    print("[Test 3] 单路 60s ...")
    pcm = gen_speech_like(60)
    s = Session(1, pcm)
    s.run()
    st = s.stats()
    all_reports.append(('单路60s', st))
    print(f"  RTF={st['rtf']:.3f}  结果={st['results_recv']}  延迟avg={st.get('lat_avg',0):.0f}ms")

    # ── Test 4-6: 并发 ─
    for n in [3, 5, 10]:
        print(f"[Test] {n}路并发 (15s each) ...")
        pcm = gen_speech_like(15)
        sessions = []
        threads = []
        wall_start = time.time()
        for i in range(n):
            sess = Session(i+1, pcm)
            sessions.append(sess)
            threads.append(threading.Thread(target=sess.run))
        for t in threads: t.start()
        for t in threads: t.join(timeout=120)
        wall_time = time.time() - wall_start
        stats_list = [s.stats() for s in sessions if s.start_time]
        all_reports.append((f'{n}路并发', stats_list, wall_time))
        rtfs = [s['rtf'] for s in stats_list]
        avgs = [s.get('lat_avg',0) for s in stats_list]
        print(f"  墙钟={wall_time:.1f}s  RTF_avg={np.mean(rtfs):.3f}  延迟avg={np.mean(avgs):.0f}ms")

    # ── 服务器资源 ──
    print("\n采集服务器资源...")
    resources = get_server_resources()

    # ══════════════════════════════════════════
    # 报告
    # ══════════════════════════════════════════
    print(f"\n{'='*70}")
    print(f"  FunASR C++ Runtime 性能评估报告")
    print(f"{'='*70}")

    # 单路结果表
    print(f"\n{'─'*70}")
    print(f"{'测试项':<14} {'时长':>6} {'耗时':>7} {'RTF':>7} {'Avg':>8} {'P50':>8} {'P95':>8} {'Max':>8} {'结果数':>6}")
    print(f"{'':14} {'':>6} {'':>7} {'':>7} {'(ms)':>8} {'(ms)':>8} {'(ms)':>8} {'(ms)':>8} {'':>6}")
    print(f"{'─'*70}")

    for item in all_reports:
        if isinstance(item[1], list):
            name, stats_list, wall = item[0], item[1], item[2]
            rtfs = [s['rtf'] for s in stats_list]
            avgs = [s.get('lat_avg',0) for s in stats_list]
            p50s = [s.get('lat_p50',0) for s in stats_list]
            p95s = [s.get('lat_p95',0) for s in stats_list]
            maxs = [s.get('lat_max',0) for s in stats_list]
            durs = [s['audio_dur'] for s in stats_list]
            res = [s['results_recv'] for s in stats_list]
            print(f"{name:<14} {np.mean(durs):>5.1f}s {wall:>6.1f}s {np.mean(rtfs):>7.3f} "
                  f"{np.mean(avgs):>8.0f} {np.mean(p50s):>8.0f} {np.mean(p95s):>8.0f} "
                  f"{np.mean(maxs):>8.0f} {int(np.mean(res)):>6}")
        else:
            name, s = item[0], item[1]
            print(f"{name:<14} {s['audio_dur']:>5.1f}s {s['total_time']:>6.1f}s {s['rtf']:>7.3f} "
                  f"{s.get('lat_avg',0):>8.0f} {s.get('lat_p50',0):>8.0f} {s.get('lat_p95',0):>8.0f} "
                  f"{s.get('lat_max',0):>8.0f} {s['results_recv']:>6}")

    print(f"{'─'*70}")

    # 综合评估
    single15 = all_reports[0][1]
    print(f"""
{'='*70}
  综合评估
{'='*70}

  1. 识别延迟
     - 平均响应延迟: {single15.get('lat_avg',0):.0f}ms
     - P50 延迟: {single15.get('lat_p50',0):.0f}ms
     - P95 延迟: {single15.get('lat_p95',0):.0f}ms
     - P99 延迟: {single15.get('lat_p99',0):.0f}ms
     - 最大延迟: {single15.get('lat_max',0):.0f}ms
     - 评级: {'优秀 (<300ms)' if single15.get('lat_avg',999) < 300 else '良好 (<500ms)' if single15.get('lat_avg',999) < 500 else '一般 (<1000ms)' if single15.get('lat_avg',999) < 1000 else '需优化'}

  2. 实时性 (RTF)
     - 单路15s RTF: {single15['rtf']:.3f}
     - 单路30s RTF: {all_reports[1][1]['rtf']:.3f}
     - 单路60s RTF: {all_reports[2][1]['rtf']:.3f}
     - 评级: {'优秀 (<0.8)' if single15['rtf'] < 0.8 else '良好 (<1.0)' if single15['rtf'] < 1.0 else '可接受 (<1.5)' if single15['rtf'] < 1.5 else '需优化'}

  3. 并发能力
     - 3路并发 RTF: {np.mean([s['rtf'] for s in all_reports[3][1]]):.3f}
     - 5路并发 RTF: {np.mean([s['rtf'] for s in all_reports[4][1]]):.3f}
     - 10路并发 RTF: {np.mean([s['rtf'] for s in all_reports[5][1]]):.3f}

  4. 识别结果
     - 15s 收到 {single15['results_recv']} 条结果
     - 说明: C++ Runtime 2pass 模式返回流式+离线校正结果

  5. 服务器资源
{resources}
""")

    # ══════════════════════════════════════════
    # 与 ONNX 版本对比
    # ══════════════════════════════════════════
    print(f"{'='*70}")
    print(f"  C++ Runtime vs ONNX Runtime 对比")
    print(f"{'='*70}")
    print(f"""
  {'指标':<20} {'ONNX版':>15} {'C++ Runtime':>15} {'变化':>12}
  {'─'*62}
  {'推理引擎':<20} {'ONNX Runtime':>15} {'C++ (websocket)':>15} {'架构升级':>12}
  {'单chunk延迟':<20} {'~80ms':>15} {'{:.0f}ms'.format(single15.get('lat_avg',0)):>15}
  {'单路RTF(15s)':<20} {'~0.08':>15} {'{:.3f}'.format(single15['rtf']):>15}
  {'并发上限':<20} {'受GIL限制':>15} {'多线程C++':>15}
  {'部署方式':<20} {'Python进程':>15} {'Docker容器':>15}
  {'模型格式':<20} {'INT8 ONNX':>15} {'INT8 ONNX':>15} {'相同':>12}
  {'内存占用':<20} {'~2GB':>15} {'见上方':>15}

  说明:
  - ONNX版 RTF 0.08 为纯推理时间，不含网络传输
  - C++ Runtime RTF 包含: 网络传输 + 网关转发 + 推理 + 结果返回
  - C++ Runtime 优势: 无需Python推理进程，原生多线程，部署简单
  - C++ Runtime 2pass模式: 流式在线模型 + 离线模型校正，精度更高
""")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
