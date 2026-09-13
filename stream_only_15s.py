"""仅运行流式 ASR 15秒音频压力测试"""
import paramiko, time, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SERVER_IP = '8.153.92.96'
SERVER_USER = 'root'
SERVER_PASS = 'myegoo@3466'

def ssh_exec(cmd, timeout=600):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SERVER_IP, username=SERVER_USER, password=SERVER_PASS, timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    client.close()
    return out, err

def upload_and_run(script_content, filename, timeout=1800):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SERVER_IP, username=SERVER_USER, password=SERVER_PASS, timeout=30)
    sftp = client.open_sftp()
    with sftp.open(f'/tmp/{filename}', 'w') as f:
        f.write(script_content)
    sftp.close()
    client.close()
    return ssh_exec(f'python3 /tmp/{filename}', timeout=timeout)

STREAM_SCRIPT = r'''
import time, json, threading, base64, wave, sys, subprocess
import numpy as np
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

try:
    import socketio
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "python-socketio[client]"], capture_output=True)
    import socketio

SERVER_URL = "http://127.0.0.1:5002"
SAMPLE_RATE = 16000
CHUNK_MS = 600
AUDIO_PATH = "/opt/funasr/tmp/stress_test_15s.wav"
DURATION = 15

import os
if not os.path.exists(AUDIO_PATH):
    print("生成 15 秒测试音频...")
    sr = 16000
    t = np.linspace(0, 15, sr * 15, endpoint=False)
    signal = (np.sin(2 * np.pi * 300 * t) * 0.3 +
              np.sin(2 * np.pi * 600 * t) * 0.2 +
              np.sin(2 * np.pi * 1200 * t) * 0.1 +
              np.random.randn(len(t)) * 0.05)
    audio = (signal * 10000).astype(np.int16)
    wf = wave.open(AUDIO_PATH, 'wb')
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(sr)
    wf.writeframes(audio.tobytes())
    wf.close()

def read_wav(path):
    with wave.open(path, 'rb') as wf:
        pcm = wf.readframes(wf.getnframes())
        rate = wf.getframerate()
    audio = np.frombuffer(pcm, dtype=np.int16)
    return audio, rate

audio, rate = read_wav(AUDIO_PATH)
audio_duration = len(audio) / rate
chunk_samples = int(rate * CHUNK_MS / 1000)
n_chunks = (len(audio) + chunk_samples - 1) // chunk_samples
print(f"测试音频: {audio_duration:.1f}s, 分块: {n_chunks} x {CHUNK_MS}ms")

def run_streaming_session(idx):
    sio = socketio.Client()
    res = {"first": None, "partial": 0, "final": 0, "text": "", "done": False, "start": None, "error": None}
    ev = threading.Event()

    @sio.on("result")
    def on_result(data):
        if res["first"] is None:
            res["first"] = time.time() - res["start"]
        if data.get("is_final"):
            res["final"] += 1
            res["text"] = data.get("full_text", "")
        else:
            res["partial"] += 1

    @sio.on("finished")
    def on_finished(data):
        res["text"] = data.get("text", res["text"])
        res["done"] = True
        ev.set()

    @sio.on("error")
    def on_error(data):
        res["error"] = str(data)
        ev.set()

    @sio.on("disconnect")
    def on_disc():
        if not res["done"]:
            res["error"] = "disconnected"
        ev.set()

    try:
        sio.connect(SERVER_URL, transports=['websocket'])
        time.sleep(0.1)
        sio.emit("start", {"sample_rate": SAMPLE_RATE})
        time.sleep(0.1)

        res["start"] = time.time()
        for i in range(n_chunks):
            s = i * chunk_samples
            e = min(s + chunk_samples, len(audio))
            b64 = base64.b64encode(audio[s:e].tobytes()).decode()
            sio.emit("audio", {"audio": b64, "is_final": (i == n_chunks - 1)})
            time.sleep(CHUNK_MS / 1000.0)

        sio.emit("end")
        ev.wait(timeout=120)
        total = time.time() - res["start"]
    except Exception as ex:
        total = time.time() - (res["start"] or time.time())
        res["error"] = str(ex)
    finally:
        try:
            sio.disconnect()
        except:
            pass

    return {
        'idx': idx, 'duration': audio_duration,
        'total_time': round(total, 2),
        'rtf': round(total / audio_duration, 3) if audio_duration > 0 else 0,
        'first_result': round(res['first'], 3) if res['first'] else None,
        'partial_count': res['partial'], 'final_count': res['final'],
        'text_len': len(res['text']), 'error': res['error'],
        'success': res['done'] and res['error'] is None
    }

# 基线
print("\n--- 基线测试 (单路 15s) ---")
baseline_results = []
for i in range(3):
    r = run_streaming_session(i)
    baseline_results.append(r)
    status = f"RTF={r['rtf']}" if r['success'] else f"ERR={r['error']}"
    first = f"{r['first_result']}s" if r['first_result'] else "N/A"
    print(f"  #{i+1}: {r['total_time']}s, first={first}, {status}, partials={r['partial_count']}")

baseline_rtf = sum(r['rtf'] for r in baseline_results if r['success']) / max(1, sum(1 for r in baseline_results if r['success']))

# 并发测试
print(f"\n--- 并发压力测试 (15s 音频, 基线 RTF={baseline_rtf:.3f}) ---")
print(f"{'并发':>4} | {'成功':>4} | {'失败':>4} | {'平均RTF':>8} | {'最大RTF':>8} | {'首包延迟':>8} | {'平均耗时':>8} | {'CPU':>6}")
print("-" * 80)

stream_summary = []
for concurrency in [1, 2, 3, 4, 5, 6, 8, 10, 12, 15]:
    results = []
    barrier = threading.Barrier(concurrency)
    
    def worker(idx, res_list):
        barrier.wait()
        r = run_streaming_session(idx)
        res_list.append(r)
    
    t_start = time.time()
    threads = [threading.Thread(target=worker, args=(i, results)) for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=600)
    total_wall = time.time() - t_start
    
    successes = [r for r in results if r['success']]
    failures = [r for r in results if not r['success']]
    
    cpu_out = subprocess.run(["docker", "stats", "funasr-stream", "--no-stream", "--format", "{{.CPUPerc}}"],
                              capture_output=True, text=True).stdout.strip()
    
    if successes:
        avg_rtf = sum(r['rtf'] for r in successes) / len(successes)
        max_rtf = max(r['rtf'] for r in successes)
        avg_time = sum(r['total_time'] for r in successes) / len(successes)
        first_results = [r['first_result'] for r in successes if r['first_result']]
        avg_first = sum(first_results) / len(first_results) if first_results else 0
        print(f"{concurrency:>4} | {len(successes):>4} | {len(failures):>4} | {avg_rtf:>7.3f} | {max_rtf:>7.3f} | {avg_first:>7.2f}s | {avg_time:>7.2f}s | {cpu_out:>6}")
        stream_summary.append({
            'concurrency': concurrency, 'success': len(successes), 'errors': len(failures),
            'avg_rtf': round(avg_rtf, 3), 'max_rtf': round(max_rtf, 3),
            'avg_first': round(avg_first, 2), 'avg_time': round(avg_time, 2),
            'wall_time': round(total_wall, 2), 'cpu': cpu_out
        })
    else:
        print(f"{concurrency:>4} |    0 | {concurrency:>4} |      N/A |      N/A |      N/A |      N/A | {cpu_out:>6}")
        stream_summary.append({
            'concurrency': concurrency, 'success': 0, 'errors': concurrency,
            'avg_rtf': 0, 'max_rtf': 0, 'avg_first': 0, 'avg_time': 0,
            'wall_time': round(total_wall, 2), 'cpu': cpu_out
        })
    
    if len(successes) == 0 and concurrency >= 3:
        print(f"  全部失败，停止测试")
        break
    if len(failures) > len(successes) and concurrency >= 4:
        print(f"  失败率过高，停止增加并发")
        break
    time.sleep(2)

print("\n--- JSON_SUMMARY ---")
print(json.dumps({
    'baseline_rtf': round(baseline_rtf, 3),
    'audio_duration': round(audio_duration, 1),
    'concurrent_results': stream_summary
}, ensure_ascii=False))
print("--- END_JSON ---")
'''

print("=" * 70)
print("流式 ASR 压力测试 — 15秒音频")
print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

# 关闭 SSL
print("\n1. 临时关闭 SSL...")
out, _ = ssh_exec("docker exec funasr-stream python3 -c \"c = open('/opt/funasr/stream_server.py').read(); c2 = c.replace('ssl_context=ssl_context', 'ssl_context=None'); open('/opt/funasr/stream_server.py','w').write(c2); print('done')\"")
print(f"  {out.strip()}")

# 验证
out, _ = ssh_exec("grep 'ssl_context=' /opt/funasr/stream_server.py | tail -1")
print(f"  验证: {out.strip()}")

# 重启
print("2. 重启流式服务...")
out, _ = ssh_exec("docker restart funasr-stream")
print("  等待模型加载...")
for i in range(12):
    out = ssh_exec("curl -s http://localhost:5002/health")[0].strip()
    if out and 'ok' in out:
        break
    print(f"  等待中... ({(i+1)*10}s)")
    time.sleep(10)
print(f"  Health: {out}")

if 'ok' not in out:
    print("  服务启动失败!")
    sys.exit(1)

# 运行测试
print("\n3. 运行压力测试...")
out, err = upload_and_run(STREAM_SCRIPT, 'stream_15s_test.py', timeout=1800)
print(out)
if err and len(err.strip()) > 0:
    print(f"[STDERR] {err[:500]}")

# 恢复 SSL
print("\n4. 恢复 SSL...")
out, _ = ssh_exec("docker exec funasr-stream python3 -c \"c = open('/opt/funasr/stream_server.py').read(); c2 = c.replace('ssl_context=None', 'ssl_context=ssl_context'); open('/opt/funasr/stream_server.py','w').write(c2); print('done')\"")
print(f"  {out.strip()}")
out, _ = ssh_exec("docker restart funasr-stream")
print("  等待恢复...")
time.sleep(40)
for i in range(8):
    out = ssh_exec("curl -sk https://localhost:5002/health")[0].strip()
    if out and 'ok' in out:
        break
    print(f"  等待中... ({(i+1)*10}s)")
    time.sleep(10)
print(f"  HTTPS Health: {out}")

print(f"\n完成! {time.strftime('%Y-%m-%d %H:%M:%S')}")
