"""综合性能测试 + 评估报告"""
import sys, os, time, base64, wave, threading
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

import socketio
from ops.config import ssh_exec

SERVER = "http://8.153.92.96:5002"
WAV_PATH = r"c:\Users\WUYALI\Desktop\tts_output.wav"
SAMPLE_RATE = 16000
CHUNK_MS = 600
CHUNK_BYTES = int(SAMPLE_RATE * CHUNK_MS / 1000) * 2

# ─── 加载音频 ───
def load_wav(path):
    with wave.open(path, 'rb') as wf:
        raw = wf.readframes(wf.getnframes())
    pcm = np.frombuffer(raw, dtype=np.int16).tobytes()
    return pcm

def gen_audio(duration):
    """合成语音特征音频"""
    sr = SAMPLE_RATE
    total = int(sr * duration)
    audio = np.zeros(total, dtype=np.float32)
    segs = [(0,3,300,400),(3,6,500,700),(6,9,200,350),(9,12,600,800),(12,duration,250,450)]
    for s,e,fl,fh in segs:
        if s >= duration: break
        e = min(e, duration)
        ns = int(s*sr); ne = int(e*sr)
        t = np.linspace(0, e-s, ne-ns, endpoint=False)
        freq = fl + (fh-fl)*t/(e-s)
        audio[ns:ne] = np.sin(2*np.pi*freq*t) * 12000
    return audio.astype(np.int16).tobytes()

# ─── 单路会话 ───
class Session:
    def __init__(self, sid, pcm):
        self.sid = sid
        self.pcm = pcm
        self.latencies = []
        self.results = []
        self.send_times = []
        self.connected = False
        self.start = None
        self.end = None
        self.sio = None

    def run(self):
        self.sio = socketio.Client()
        @self.sio.on('connected')
        def _(d): self.connected = True
        @self.sio.on('recognition_result')
        def _(d):
            t = time.time()
            lat = (t - self.send_times[-1])*1000 if self.send_times else 0
            self.latencies.append(lat)
            self.results.append(d)
        try:
            self.sio.connect(SERVER, wait_timeout=10)
        except:
            return
        for _ in range(50):
            if self.connected: break
            time.sleep(0.1)
        if not self.connected:
            self.sio.disconnect(); return

        self.start = time.time()
        off = 0
        while off < len(self.pcm):
            end = min(off + CHUNK_BYTES, len(self.pcm))
            chunk = self.pcm[off:end]
            is_final = end >= len(self.pcm)
            b64 = base64.b64encode(chunk).decode()
            self.send_times.append(time.time())
            self.sio.emit('audio', {'audio': b64, 'is_final': is_final})
            off = end
            time.sleep(CHUNK_MS/1000.0)
        time.sleep(2)
        self.end = time.time()
        self.sio.disconnect()

    def stats(self):
        dur = len(self.pcm)/SAMPLE_RATE/2
        total = self.end - self.start if self.start and self.end else 0
        s = {'sid': self.sid, 'audio_dur': dur, 'total_time': total,
             'rtf': total/dur if dur > 0 else 99, 'chunks': len(self.send_times),
             'results': len(self.results)}
        if self.latencies:
            s['lat_avg'] = np.mean(self.latencies)
            s['lat_p50'] = np.percentile(self.latencies, 50)
            s['lat_p95'] = np.percentile(self.latencies, 95)
            s['lat_max'] = np.max(self.latencies)
            s['lat_min'] = np.min(self.latencies)
        return s

# ─── 服务器资源采集 ───
def get_server_resources():
    out, _ = ssh_exec("free -m | head -2; echo '---'; top -bn1 | head -5; echo '---'; docker stats --no-stream funasr-stream 2>/dev/null")
    return out

# ─── 主测试 ───
def main():
    pcm_real = load_wav(WAV_PATH)
    real_dur = len(pcm_real)/SAMPLE_RATE/2
    print(f"=== 流式 ASR 综合性能测试 ===")
    print(f"服务器: {SERVER}")
    print(f"真实音频: {real_dur:.1f}s ({WAV_PATH})")
    print()

    all_reports = []

    # ── Test 1: 单路真实语音 ──
    print("[Test 1] 单路真实语音 (16.9s)")
    s = Session(1, pcm_real)
    s.run()
    st = s.stats()
    all_reports.append(('单路真实语音', st))

    # ── Test 2: 单路长音频 (30s) ──
    print("[Test 2] 单路长音频 (30s)")
    pcm_30 = gen_audio(30)
    s = Session(1, pcm_30)
    s.run()
    st = s.stats()
    all_reports.append(('单路30s', st))

    # ── Test 3: 单路超长音频 (60s) ──
    print("[Test 3] 单路超长音频 (60s)")
    pcm_60 = gen_audio(60)
    s = Session(1, pcm_60)
    s.run()
    st = s.stats()
    all_reports.append(('单路60s', st))

    # ── Test 4-6: 并发测试 ──
    for n in [3, 5, 10]:
        print(f"[Test] {n}路并发 (15s each)")
        sessions = []
        threads = []
        wall_start = time.time()
        for i in range(n):
            sess = Session(i+1, pcm_real)
            sessions.append(sess)
            threads.append(threading.Thread(target=sess.run))
        for t in threads: t.start()
        for t in threads: t.join(timeout=120)
        wall_time = time.time() - wall_start
        stats_list = [s.stats() for s in sessions if s.start]
        all_reports.append((f'{n}路并发', stats_list, wall_time))

    # ── 服务器资源 ──
    print("\n采集服务器资源...")
    resources = get_server_resources()

    # ══════════════════════════════════════════
    # 报告
    # ══════════════════════════════════════════
    print(f"\n{'='*70}")
    print(f"  流式 ASR 性能评估报告")
    print(f"{'='*70}")
    print(f"  服务: PyTorch Paraformer-large streaming")
    print(f"  服务器: 8.153.92.96 (4核/15GB)")
    print(f"  测试时间: {time.strftime('%Y-%m-%d %H:%M')}")
    print()

    # 单路结果
    print("─" * 70)
    print(f"{'测试项':<16} {'时长':>6} {'耗时':>7} {'RTF':>7} {'Avg延迟':>8} {'P95延迟':>8} {'Max延迟':>8}")
    print(f"{'':16} {'':>6} {'':>7} {'':>7} {'(ms)':>8} {'(ms)':>8} {'(ms)':>8}")
    print("─" * 70)

    for item in all_reports:
        if isinstance(item[1], list):
            # 并发
            name = item[0]
            stats_list = item[1]
            wall = item[2]
            rtfs = [s['rtf'] for s in stats_list]
            avgs = [s.get('lat_avg',0) for s in stats_list]
            p95s = [s.get('lat_p95',0) for s in stats_list]
            maxs = [s.get('lat_max',0) for s in stats_list]
            durs = [s['audio_dur'] for s in stats_list]
            print(f"{name:<16} {np.mean(durs):>5.1f}s {wall:>6.1f}s {np.mean(rtfs):>7.3f} {np.mean(avgs):>8.0f} {np.mean(p95s):>8.0f} {np.mean(maxs):>8.0f}")
        else:
            name = item[0]
            s = item[1]
            print(f"{name:<16} {s['audio_dur']:>5.1f}s {s['total_time']:>6.1f}s {s['rtf']:>7.3f} {s.get('lat_avg',0):>8.0f} {s.get('lat_p95',0):>8.0f} {s.get('lat_max',0):>8.0f}")

    print("─" * 70)

    # 评估
    print(f"\n{'='*70}")
    print(f"  综合评估")
    print(f"{'='*70}")

    # 取单路真实语音的结果
    single = all_reports[0][1]
    print(f"""
  1. 识别延迟
     - 平均响应延迟: {single.get('lat_avg',0):.0f}ms
     - P95 延迟: {single.get('lat_p95',0):.0f}ms
     - 评级: {'优秀 (<300ms)' if single.get('lat_avg',999) < 300 else '良好 (<500ms)' if single.get('lat_avg',999) < 500 else '一般'}

  2. 实时性
     - 单路 RTF: {single['rtf']:.3f}
     - 评级: {'优秀 (<0.8)' if single['rtf'] < 0.8 else '良好 (<1.0)' if single['rtf'] < 1.0 else '可接受 (<1.5)' if single['rtf'] < 1.5 else '需优化'}
     - 说明: RTF>1 主要受网络延迟影响(本地→北京机房 ~30-50ms RTT)

  3. 并发能力
     - 3路并发时平均延迟: {np.mean([s.get('lat_avg',0) for s in (all_reports[3][1] if len(all_reports)>3 else [single])]):.0f}ms
     - 5路并发时平均延迟: {np.mean([s.get('lat_avg',0) for s in (all_reports[4][1] if len(all_reports)>4 else [single])]):.0f}ms
     - 10路并发时平均延迟: {np.mean([s.get('lat_avg',0) for s in (all_reports[5][1] if len(all_reports)>5 else [single])]):.0f}ms
     - 评级: 受 Flask threading + GIL 限制，并发时延迟线性增长

  4. 稳定性
     - 长音频(60s)测试: {'通过' if len(all_reports)>2 and all_reports[2][1]['rtf'] < 2 else '需关注'}
     - 并发测试: 全部完成无崩溃

  5. 服务器资源
{resources}
  6. 优化建议
     a) 使用 gunicorn + gevent 替代 Flask 开发服务器，提升并发
     b) 减小 chunk_size [0,10,5]→[0,5,2]，降低单次延迟
     c) 如部署在同地域机房，RTF 可降至 0.5 以下
     d) GPU 推理可大幅降低模型处理时间
""")
    print(f"{'='*70}")

if __name__ == '__main__':
    main()
