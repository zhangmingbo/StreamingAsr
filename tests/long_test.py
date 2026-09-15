"""3路并发滚动长测 - 每轮15秒，滚动30分钟"""
import sys, os, time, base64, wave, threading, json
import numpy as np
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8')

import socketio
from ops.config import ssh_exec

SERVER = "http://8.153.92.96:5002"
WAV_PATH = r"c:\Users\WUYALI\Desktop\tts_output.wav"
SAMPLE_RATE = 16000
CHUNK_MS = 600
CHUNK_BYTES = int(SAMPLE_RATE * CHUNK_MS / 1000) * 2
DURATION_MIN = 30          # 总测试时长（分钟）
BATCH_INTERVAL = 15        # 每轮间隔（秒）
SESSION_DURATION = 15      # 每路会话持续时长（秒）
CONCURRENCY = 3            # 每轮并发路数

# ─── 加载音频 ───
def load_wav(path):
    with wave.open(path, 'rb') as wf:
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).tobytes()

# ─── 单路会话（发送固定时长后断开） ───
class Session:
    def __init__(self, sid, pcm, duration):
        self.sid = sid
        self.pcm = pcm
        self.duration = duration
        self.connected = False
        self.sio = None
        self._lock = threading.Lock()
        self._stop = False
        self._pending_sends = []
        self.records = []      # (recv_timestamp, latency_ms)
        self.send_count = 0
        self.errors = 0
        self.start_time = None
        self.end_time = None

    def connect(self):
        self.sio = socketio.Client()

        @self.sio.on('connected')
        def _(d):
            self.connected = True

        @self.sio.on('recognition_result')
        def _(d):
            with self._lock:
                t = time.time()
                send_t = self._pending_sends.pop(0) if self._pending_sends else t
                lat = (t - send_t) * 1000
                self.records.append((t, lat))

        try:
            self.sio.connect(SERVER, wait_timeout=10)
        except Exception as e:
            return False

        for _ in range(30):
            if self.connected:
                break
            time.sleep(0.1)

        if not self.connected:
            try:
                self.sio.disconnect()
            except:
                pass
            return False
        return True

    def send_loop(self):
        """发送音频，持续 SESSION_DURATION 秒"""
        off = 0
        pcm_len = len(self.pcm)
        self.start_time = time.time()
        deadline = self.start_time + self.duration

        while not self._stop and time.time() < deadline:
            end = min(off + CHUNK_BYTES, pcm_len)
            chunk = self.pcm[off:end]
            is_final = (end >= pcm_len)

            b64 = base64.b64encode(chunk).decode()
            with self._lock:
                self._pending_sends.append(time.time())
                self.send_count += 1

            try:
                self.sio.emit('audio', {'audio': b64, 'is_final': is_final})
            except Exception:
                with self._lock:
                    self.errors += 1
                break

            off = end
            if is_final:
                off = 0  # 循环

            time.sleep(CHUNK_MS / 1000.0)

        # 等待最后几个结果返回
        time.sleep(2)
        self.end_time = time.time()
        try:
            self.sio.disconnect()
        except:
            pass

    def get_stats(self):
        with self._lock:
            lats = [lat for (_, lat) in self.records]
            total_recv = len(self.records)
        return {
            'sid': self.sid,
            'send_count': self.send_count,
            'recv_count': total_recv,
            'errors': self.errors,
            'latencies': lats,
        }


def get_server_stats():
    try:
        out, _ = ssh_exec("docker stats --no-stream --format '{{.MemUsage}}|{{.CPUPerc}}' funasr-stream 2>/dev/null")
        parts = out.strip().split('|')
        mem = parts[0].strip() if len(parts) > 0 else 'N/A'
        cpu = parts[1].strip() if len(parts) > 1 else 'N/A'
        return mem, cpu
    except:
        return 'N/A', 'N/A'


def run_batch(batch_id, pcm):
    """运行一轮：3路并发，每路15秒"""
    sessions = []
    threads = []

    for i in range(CONCURRENCY):
        s = Session(f"b{batch_id}_s{i+1}", pcm, SESSION_DURATION)
        sessions.append(s)

    # 建立连接
    connected = 0
    for s in sessions:
        if s.connect():
            connected += 1

    if connected == 0:
        return None

    # 启动发送
    for s in sessions:
        t = threading.Thread(target=s.send_loop, daemon=True)
        t.start()
        threads.append(t)

    # 等待完成
    for t in threads:
        t.join(timeout=SESSION_DURATION + 10)

    # 收集统计
    batch_stats = {
        'batch_id': batch_id,
        'connected': connected,
        'sessions': []
    }
    all_lats = []
    for s in sessions:
        st = s.get_stats()
        batch_stats['sessions'].append(st)
        all_lats.extend(st['latencies'])

    if all_lats:
        arr = np.array(all_lats)
        batch_stats['lat_avg'] = float(np.mean(arr))
        batch_stats['lat_p50'] = float(np.percentile(arr, 50))
        batch_stats['lat_p95'] = float(np.percentile(arr, 95))
        batch_stats['lat_max'] = float(np.max(arr))
        batch_stats['lat_min'] = float(np.min(arr))
    else:
        batch_stats['lat_avg'] = 0
        batch_stats['lat_p50'] = 0
        batch_stats['lat_p95'] = 0
        batch_stats['lat_max'] = 0
        batch_stats['lat_min'] = 0

    total_send = sum(s['send_count'] for s in batch_stats['sessions'])
    total_recv = sum(s['recv_count'] for s in batch_stats['sessions'])
    batch_stats['total_send'] = total_send
    batch_stats['total_recv'] = total_recv

    return batch_stats


def main():
    pcm = load_wav(WAV_PATH)
    pcm_dur = len(pcm) / SAMPLE_RATE / 2
    total_sec = DURATION_MIN * 60
    total_batches = total_sec // BATCH_INTERVAL

    print(f"{'='*60}")
    print(f"  3路并发滚动长测")
    print(f"{'='*60}")
    print(f"  服务器: {SERVER}")
    print(f"  音频: {pcm_dur:.1f}s (循环发送)")
    print(f"  并发: {CONCURRENCY} 路/轮")
    print(f"  每轮持续: {SESSION_DURATION}s")
    print(f"  轮间隔: {BATCH_INTERVAL}s")
    print(f"  总时长: {DURATION_MIN} 分钟 ({total_batches} 轮)")
    print(f"  开始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    all_batches = []
    start_time = time.time()

    for batch_id in range(1, total_batches + 1):
        elapsed = time.time() - start_time
        if elapsed >= total_sec:
            break

        # 运行一轮
        t0 = time.time()
        result = run_batch(batch_id, pcm)
        batch_time = time.time() - t0

        if result is None:
            print(f"  [{batch_id:3d}/{total_batches}] 连接失败，跳过")
            continue

        # 采集服务器资源
        mem, cpu = get_server_stats()
        result['mem'] = mem
        result['cpu'] = cpu
        result['wall_time'] = batch_time

        all_batches.append(result)

        # 实时打印
        print(f"  [{batch_id:3d}/{total_batches}] t={elapsed/60:.1f}min | "
              f"avg={result['lat_avg']:>6.0f} p95={result['lat_p95']:>6.0f} "
              f"max={result['lat_max']:>6.0f}ms | "
              f"send={result['total_send']} recv={result['total_recv']} | "
              f"mem={mem} cpu={cpu} | {batch_time:.1f}s")
        sys.stdout.flush()

        # 等待到下一轮
        next_batch_time = batch_id * BATCH_INTERVAL
        wait = next_batch_time - (time.time() - start_time)
        if wait > 0:
            time.sleep(wait)

    elapsed_total = time.time() - start_time

    # ══════════════════════════════════════════
    # 报告
    # ══════════════════════════════════════════
    print(f"\n{'='*60}")
    print(f"  3路并发滚动长测报告")
    print(f"{'='*60}")
    print(f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  实际耗时: {elapsed_total/60:.1f} 分钟")
    print(f"  完成轮数: {len(all_batches)}/{total_batches}")
    print()

    # 汇总所有延迟
    all_lats = []
    for b in all_batches:
        for s in b['sessions']:
            all_lats.extend(s['latencies'])

    total_send = sum(b['total_send'] for b in all_batches)
    total_recv = sum(b['total_recv'] for b in all_batches)
    total_errors = sum(s['errors'] for b in all_batches for s in b['sessions'])

    if all_lats:
        arr = np.array(all_lats)
        print(f"  ── 全局延迟统计 ──")
        print(f"  总发送: {total_send} chunks")
        print(f"  总接收: {total_recv} results")
        print(f"  丢结果率: {(1 - total_recv/max(total_send,1))*100:.1f}%")
        print(f"  错误数: {total_errors}")
        print(f"  平均延迟:  {np.mean(arr):.0f} ms")
        print(f"  P50 延迟:  {np.percentile(arr, 50):.0f} ms")
        print(f"  P90 延迟:  {np.percentile(arr, 90):.0f} ms")
        print(f"  P95 延迟:  {np.percentile(arr, 95):.0f} ms")
        print(f"  P99 延迟:  {np.percentile(arr, 99):.0f} ms")
        print(f"  最大延迟:  {np.max(arr):.0f} ms")
        print(f"  最小延迟:  {np.min(arr):.0f} ms")
        print(f"  标准差:    {np.std(arr):.0f} ms")
        print()

        # 每5分钟趋势
        print(f"  ── 延迟趋势（每5分钟） ──")
        print(f"  {'时段':<12} {'Avg':>8} {'P50':>8} {'P95':>8} {'Max':>8} {'轮数':>6}")
        print(f"  {'-'*50}")
        for minute in range(0, DURATION_MIN, 5):
            t_start = minute * 60
            t_end = (minute + 5) * 60
            window_lats = []
            window_batches = 0
            for b in all_batches:
                b_time = (b['batch_id'] - 1) * BATCH_INTERVAL
                if t_start <= b_time < t_end:
                    window_batches += 1
                    for s in b['sessions']:
                        window_lats.extend(s['latencies'])
            if window_lats:
                a = np.array(window_lats)
                print(f"  {minute:>2}-{minute+5:<5}min {np.mean(a):>7.0f} {np.percentile(a,50):>7.0f} {np.percentile(a,95):>7.0f} {np.max(a):>7.0f} {window_batches:>6}")
        print()

        # 每轮 avg/p95 趋势（简化输出，每10轮一行）
        print(f"  ── 每轮延迟趋势（每10轮） ──")
        print(f"  {'轮次':<10} {'Avg':>8} {'P95':>8} {'Max':>8} {'Recv/Send':>10}")
        print(f"  {'-'*44}")
        for i in range(0, len(all_batches), 10):
            chunk = all_batches[i:i+10]
            avgs = [b['lat_avg'] for b in chunk if b['lat_avg'] > 0]
            p95s = [b['lat_p95'] for b in chunk if b['lat_p95'] > 0]
            maxs = [b['lat_max'] for b in chunk if b['lat_max'] > 0]
            sends = sum(b['total_send'] for b in chunk)
            recvs = sum(b['total_recv'] for b in chunk)
            if avgs:
                label = f"#{i+1:>3}-{i+len(chunk):>3}"
                recv_rate = f"{recvs}/{sends}" if sends > 0 else "N/A"
                print(f"  {label:<10} {np.mean(avgs):>7.0f} {np.mean(p95s):>7.0f} {np.mean(maxs):>7.0f} {recv_rate:>10}")
        print()

        # 服务器资源
        if all_batches:
            last = all_batches[-1]
            print(f"  ── 服务器资源（最后一次采样） ──")
            print(f"  内存: {last['mem']}")
            print(f"  CPU:  {last['cpu']}")
            print()

        # 综合评估
        p95 = np.percentile(arr, 95)
        avg = np.mean(arr)
        print(f"  ── 综合评估 ──")
        if p95 < 300:
            rating = "卓越"
        elif p95 < 500:
            rating = "优秀"
        elif p95 < 800:
            rating = "良好"
        elif p95 < 1500:
            rating = "可接受"
        else:
            rating = "需优化"
        print(f"  3路并发(15s/轮) P95: {p95:.0f}ms")
        print(f"  3路并发(15s/轮) Avg: {avg:.0f}ms")
        print(f"  稳定性评级: {rating}")
        print(f"  丢结果率: {(1 - total_recv/max(total_send,1))*100:.1f}%")
    else:
        print("  无延迟数据！")

    print(f"\n{'='*60}")

    # 保存 JSON
    report_path = Path(__file__).parent / f"longtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_data = {
        'config': {
            'server': SERVER,
            'duration_min': DURATION_MIN,
            'concurrency': CONCURRENCY,
            'batch_interval': BATCH_INTERVAL,
            'session_duration': SESSION_DURATION,
        },
        'summary': {
            'elapsed_sec': elapsed_total,
            'total_batches': len(all_batches),
            'total_send': total_send,
            'total_recv': total_recv,
            'avg_latency': float(np.mean(arr)) if all_lats else 0,
            'p95_latency': float(np.percentile(arr, 95)) if all_lats else 0,
        },
        'batches': [{
            'batch_id': b['batch_id'],
            'lat_avg': b['lat_avg'],
            'lat_p95': b['lat_p95'],
            'lat_max': b['lat_max'],
            'total_send': b['total_send'],
            'total_recv': b['total_recv'],
            'mem': b['mem'],
            'cpu': b['cpu'],
        } for b in all_batches]
    }
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2)
    print(f"  数据已保存: {report_path}")


if __name__ == '__main__':
    main()
