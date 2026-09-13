"""仅运行流式 ASR 压力测试 (v2 - 直接修改宿主机文件)"""
import paramiko, time, json, sys, threading
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

STREAM_STRESS_SCRIPT = r'''
import time, json, threading, base64, wave, sys
import numpy as np
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

try:
    import socketio
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "python-socketio[client]"], capture_output=True)
    import socketio

SERVER_URL = "http://127.0.0.1:5002"
SAMPLE_RATE = 16000
CHUNK_MS = 600
WAV_FILE = "/opt/funasr/tmp/perf_test_30s.wav"

import os
for f in ["/opt/funasr/tmp/perf_test_30s.wav", "/opt/funasr/tmp/perf_test_60s.wav", "/opt/funasr/tmp/perf_test.wav"]:
    if os.path.exists(f):
        WAV_FILE = f
        break

def read_wav(path):
    with wave.open(path, 'rb') as wf:
        n_ch = wf.getnchannels()
        n_frames = wf.getnframes()
        rate = wf.getframerate()
        pcm = wf.readframes(n_frames)
    audio = np.frombuffer(pcm, dtype=np.int16)
    if n_ch == 2:
        audio = audio.reshape(-1, 2).mean(axis=1).astype(np.int16)
    return audio, rate

audio, rate = read_wav(WAV_FILE)
audio_duration = len(audio) / rate
chunk_samples = int(rate * CHUNK_MS / 1000)
n_chunks = (len(audio) + chunk_samples - 1) // chunk_samples
print(f"测试音频: {WAV_FILE}, 时长: {audio_duration:.1f}s, 采样率: {rate}")

def run_streaming_session(idx, simulate_realtime=True):
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
        time.sleep(0.2)
        sio.emit("start", {"sample_rate": SAMPLE_RATE})
        time.sleep(0.2)

        res["start"] = time.time()
        for i in range(n_chunks):
            s = i * chunk_samples
            e = min(s + chunk_samples, len(audio))
            b64 = base64.b64encode(audio[s:e].tobytes()).decode()
            sio.emit("audio", {"audio": b64, "is_final": (i == n_chunks - 1)})
            if simulate_realtime:
                time.sleep(CHUNK_MS / 1000.0)

        sio.emit("end")
        ev.wait(timeout=120)
        total = time.time() - res["start"]
    except Exception as ex:
        total = time.time() - (res["start"] or time.time())
        res["error"] = str(ex)
    finally:
        try: sio.disconnect()
        except: pass

    return {
        'idx': idx, 'duration': audio_duration,
        'total_time': round(total, 2),
        'rtf': round(total / audio_duration, 3) if audio_duration > 0 else 0,
        'first_result': round(res['first'], 2) if res['first'] else None,
        'partial_count': res['partial'], 'final_count': res['final'],
        'text_len': len(res['text']),
        'error': res['error'],
        'success': res['done'] and res['error'] is None
    }

# 基线
print("\n--- 基线测试 (单路, 3次) ---")
baseline_results = []
for i in range(3):
    r = run_streaming_session(i, simulate_realtime=True)
    baseline_results.append(r)
    status = f"RTF={r['rtf']}" if r['success'] else f"ERR={r['error']}"
    first = f"{r['first_result']}s" if r['first_result'] else "N/A"
    print(f"  #{i+1}: {r['total_time']}s, first={first}, {status}, chars={r['text_len']}")

baseline_rtf = sum(r['rtf'] for r in baseline_results if r['success']) / max(1, sum(1 for r in baseline_results if r['success']))

if baseline_rtf == 0:
    print("\n基线测试全部失败，无法继续!")
    sys.exit(1)

# 并发测试
print(f"\n--- 并发流式压力测试 (基线 RTF={baseline_rtf:.3f}) ---")
print(f"{'并发':>4} | {'成功':>4} | {'失败':>4} | {'平均RTF':>8} | {'最大RTF':>8} | {'首包延迟':>8} | {'平均耗时':>8}")
print("-" * 75)

stream_summary = []
for concurrency in [1, 2, 3, 4, 5, 6, 8, 10]:
    results = []
    barrier = threading.Barrier(concurrency)
    
    def worker(idx, res_list):
        barrier.wait()
        r = run_streaming_session(idx, simulate_realtime=True)
        res_list.append(r)
    
    t_start = time.time()
    threads = [threading.Thread(target=worker, args=(i, results)) for i in range(concurrency)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=600)
    total_wall = time.time() - t_start
    
    successes = [r for r in results if r['success']]
    failures = [r for r in results if not r['success']]
    
    if successes:
        avg_rtf = sum(r['rtf'] for r in successes) / len(successes)
        max_rtf = max(r['rtf'] for r in successes)
        avg_time = sum(r['total_time'] for r in successes) / len(successes)
        first_results = [r['first_result'] for r in successes if r['first_result']]
        avg_first = sum(first_results) / len(first_results) if first_results else 0
        print(f"{concurrency:>4} | {len(successes):>4} | {len(failures):>4} | {avg_rtf:>7.3f} | {max_rtf:>7.3f} | {avg_first:>7.2f}s | {avg_time:>7.2f}s")
        stream_summary.append({
            'concurrency': concurrency, 'success': len(successes), 'errors': len(failures),
            'avg_rtf': round(avg_rtf, 3), 'max_rtf': round(max_rtf, 3),
            'avg_first': round(avg_first, 2), 'avg_time': round(avg_time, 2),
            'wall_time': round(total_wall, 2)
        })
    else:
        print(f"{concurrency:>4} |    0 | {concurrency:>4} |      N/A |      N/A |      N/A |      N/A")
        stream_summary.append({
            'concurrency': concurrency, 'success': 0, 'errors': concurrency,
            'avg_rtf': 0, 'max_rtf': 0, 'avg_first': 0, 'avg_time': 0, 'wall_time': round(total_wall, 2)
        })
    
    if len(successes) == 0 and concurrency >= 3:
        print(f"  全部失败，停止测试")
        break
    if len(failures) > len(successes) and concurrency >= 4:
        print(f"  失败率过高，停止增加并发")
        break
    time.sleep(2)

# 系统状态
print("\n--- 系统状态 ---")
import subprocess
r = subprocess.run(["free", "-h"], capture_output=True, text=True)
for line in r.stdout.split('\n'):
    if 'Mem' in line: print(f"  内存: {line.strip()}")
r = subprocess.run(["docker", "stats", "funasr-stream", "--no-stream", "--format", "CPU: {{.CPUPerc}}, MEM: {{.MemUsage}}"], 
                    capture_output=True, text=True)
print(f"  funasr-stream容器: {r.stdout.strip()}")

print("\n--- JSON_SUMMARY ---")
print(json.dumps({
    'baseline_rtf': round(baseline_rtf, 3),
    'audio_duration': round(audio_duration, 1),
    'concurrent_results': stream_summary
}, ensure_ascii=False))
print("--- END_JSON ---")
'''

print("=" * 70)
print("流式 ASR 压力测试 (v2)")
print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

# 1. 直接修改宿主机上的 stream_server.py (通过 sed)
print("\n1. 临时关闭 SSL...")
# 使用 sed 直接修改宿主机文件（容器挂载点）
out, _ = ssh_exec("sed -i 's/ssl_context=ssl_context/ssl_context=None/g' /opt/funasr/stream_server.py")
out, _ = ssh_exec("grep 'ssl_context' /opt/funasr/stream_server.py | tail -3")
print(f"  当前 SSL 配置: {out.strip()}")

# 2. 重启容器
print("\n2. 重启流式服务...")
out, _ = ssh_exec("docker restart funasr-stream")
print(f"  {out.strip()}")

# 3. 等待 health 成功（循环检查，最多等 90 秒）
print("\n3. 等待服务启动...")
health_ok = False
for i in range(18):
    time.sleep(5)
    out, _ = ssh_exec("curl -s http://localhost:5002/health 2>/dev/null")
    out = out.strip()
    if out and 'ok' in out:
        print(f"  Health OK ({(i+1)*5}s): {out}")
        health_ok = True
        break
    # 也检查容器是否在运行
    if i % 4 == 3:
        status, _ = ssh_exec("docker ps --filter name=funasr-stream --format '{{.Status}}'")
        print(f"  容器状态: {status.strip()}")
    else:
        print(f"  等待中... ({(i+1)*5}s)")

if not health_ok:
    print("  ERROR: 启动超时! 恢复 SSL 并退出")
    ssh_exec("sed -i 's/ssl_context=None/ssl_context=ssl_context/g' /opt/funasr/stream_server.py")
    ssh_exec("docker restart funasr-stream")
    sys.exit(1)

# 4. 运行测试
print("\n4. 运行流式压力测试...")
out, err = upload_and_run(STREAM_STRESS_SCRIPT, 'stream_stress_test.py', timeout=1800)
print(out)
if err:
    print(f"[STDERR] {err[:500]}")

# 5. 恢复 SSL
print("\n5. 恢复 SSL...")
out, _ = ssh_exec("sed -i 's/ssl_context=None/ssl_context=ssl_context/g' /opt/funasr/stream_server.py")
out, _ = ssh_exec("grep 'ssl_context' /opt/funasr/stream_server.py | tail -1")
print(f"  恢复后: {out.strip()}")

out, _ = ssh_exec("docker restart funasr-stream")
print(f"  重启: {out.strip()}")
print("  等待恢复...")
for i in range(18):
    time.sleep(5)
    out, _ = ssh_exec("curl -sk https://localhost:5002/health 2>/dev/null")
    if out.strip() and 'ok' in out.strip():
        print(f"  HTTPS 恢复 ({(i+1)*5}s)")
        break

print("\n" + "=" * 70)
print(f"流式压力测试完成! {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)
