"""
ASR 综合压力测试 — 离线 + 流式
在服务器本地运行，消除网络延迟影响
"""
import paramiko
import time
import json
import sys
import threading

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SERVER_IP = '8.153.92.96'
SERVER_USER = 'root'
SERVER_PASS = 'myegoo@3466'

# ==================== 工具函数 ====================
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
    """上传脚本到服务器并执行"""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SERVER_IP, username=SERVER_USER, password=SERVER_PASS, timeout=30)
    sftp = client.open_sftp()
    with sftp.open(f'/tmp/{filename}', 'w') as f:
        f.write(script_content)
    sftp.close()
    client.close()
    return ssh_exec(f'python3 /tmp/{filename}', timeout=timeout)

# ==================== 离线 ASR 压力测试脚本 ====================
OFFLINE_STRESS_SCRIPT = r'''
import time, json, threading, requests, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SERVER = "http://127.0.0.1:5000"
AUDIO_FILES = [
    "/opt/funasr/tmp/perf_test_30s.wav",
    "/opt/funasr/tmp/perf_test_60s.wav",
    "/opt/funasr/tmp/perf_test.wav",
]

# 找到可用的测试音频
audio_path = None
for f in AUDIO_FILES:
    try:
        with open(f, 'rb') as fh:
            fh.read(100)
        audio_path = f
        break
    except:
        continue

if not audio_path:
    print("ERROR: 没有可用的测试音频文件")
    sys.exit(1)

import os
file_size = os.path.getsize(audio_path)
print(f"测试音频: {audio_path} ({file_size/1024:.0f} KB)")

# 获取音频时长
import subprocess
try:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", audio_path],
        capture_output=True, text=True, timeout=10
    )
    audio_duration = float(r.stdout.strip())
except:
    audio_duration = 0
print(f"音频时长: {audio_duration:.1f}s")

def single_request(idx, timeout=300):
    t0 = time.time()
    try:
        with open(audio_path, 'rb') as f:
            resp = requests.post(f'{SERVER}/api/transcribe', files={'file': ('test.wav', f, 'audio/wav')}, timeout=timeout)
        elapsed = time.time() - t0
        if resp.status_code == 200:
            text_len = len(resp.text)
            return {'idx': idx, 'status': 200, 'time': round(elapsed, 2), 'text_len': text_len}
        else:
            return {'idx': idx, 'status': resp.status_code, 'time': round(elapsed, 2), 'error': resp.text[:100]}
    except Exception as e:
        elapsed = time.time() - t0
        return {'idx': idx, 'status': -1, 'time': round(elapsed, 2), 'error': str(e)[:100]}

# 基线测试
print("\n--- 基线测试 ---")
baseline = single_request(0)
print(f"  单次请求: {baseline['time']}s, status={baseline['status']}")
baseline_time = baseline['time']

# 并发梯度测试
print("\n--- 并发压力测试 ---")
print(f"{'并发':>4} | {'平均':>7} | {'最慢':>7} | {'最快':>7} | {'吞吐/s':>7} | {'加速比':>6} | {'RTF':>6} | {'成功':>4} | {'失败':>4}")
print("-" * 85)

results_summary = []
for concurrency in [1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20]:
    results = []
    barrier = threading.Barrier(concurrency)
    
    def worker(idx, res_list):
        barrier.wait()  # 所有线程同时启动
        r = single_request(idx)
        res_list.append(r)
    
    t_start = time.time()
    threads = [threading.Thread(target=worker, args=(i, results)) for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=600)
    total_wall = time.time() - t_start
    
    times = []
    errors = 0
    for r in results:
        if r.get('status') == 200:
            times.append(r['time'])
        else:
            errors += 1
    
    if times:
        avg = sum(times) / len(times)
        mx = max(times)
        mn = min(times)
        tput = len(times) / total_wall
        speedup = baseline_time / avg if avg > 0 else 0
        rtf = avg / audio_duration if audio_duration > 0 else 0
        success = len(times)
        print(f"{concurrency:>4} | {avg:>6.2f}s | {mx:>6.2f}s | {mn:>6.2f}s | {tput:>5.3f} | {speedup:>4.2f}x | {rtf:>4.3f} | {success:>4} | {errors:>4}")
        results_summary.append({'concurrency': concurrency, 'avg': avg, 'max': mx, 'min': mn, 
                                'throughput': tput, 'rtf': rtf, 'success': success, 'errors': errors})
    else:
        print(f"{concurrency:>4} |    N/A |    N/A |    N/A |     N/A |    N/A |   N/A |    0 | {concurrency:>4}")
        results_summary.append({'concurrency': concurrency, 'avg': 0, 'max': 0, 'min': 0, 
                                'throughput': 0, 'rtf': 0, 'success': 0, 'errors': concurrency})
    
    # 如果失败率超过一半，停止增加并发
    if errors > concurrency // 2 and concurrency >= 4:
        print(f"  失败率过高，停止增加并发")
        break
    
    # 短暂休息，让服务恢复
    time.sleep(2)

# 最终系统状态
print("\n--- 系统状态 ---")
import subprocess
r = subprocess.run(["free", "-h"], capture_output=True, text=True)
for line in r.stdout.split('\n'):
    if 'Mem' in line:
        print(f"  内存: {line.strip()}")

r = subprocess.run(["docker", "stats", "funasr", "--no-stream", "--format", "CPU: {{.CPUPerc}}, MEM: {{.MemUsage}}"], 
                    capture_output=True, text=True)
print(f"  funasr容器: {r.stdout.strip()}")

# 输出JSON汇总
print("\n--- JSON_SUMMARY ---")
print(json.dumps(results_summary, ensure_ascii=False))
print("--- END_JSON ---")
'''

# ==================== 流式 ASR 压力测试脚本 ====================
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

# 如果30s文件不存在，尝试其他文件
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
    """运行一个完整的流式识别会话"""
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
        try:
            sio.disconnect()
        except:
            pass

    return {
        'idx': idx,
        'duration': audio_duration,
        'total_time': round(total, 2),
        'rtf': round(total / audio_duration, 3) if audio_duration > 0 else 0,
        'first_result': round(res['first'], 2) if res['first'] else None,
        'partial_count': res['partial'],
        'final_count': res['final'],
        'text_len': len(res['text']),
        'error': res['error'],
        'success': res['done'] and res['error'] is None
    }

# 基线：单路流式识别
print("\n--- 基线测试 (单路) ---")
baseline_results = []
for i in range(3):
    r = run_streaming_session(i, simulate_realtime=True)
    baseline_results.append(r)
    status = f"RTF={r['rtf']}" if r['success'] else f"ERR={r['error']}"
    first = f"{r['first_result']}s" if r['first_result'] else "N/A"
    print(f"  #{i+1}: {r['total_time']}s, first={first}, {status}, chars={r['text_len']}")

baseline_rtf = sum(r['rtf'] for r in baseline_results if r['success']) / max(1, sum(1 for r in baseline_results if r['success']))

# 并发流式测试
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
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=600)
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
    
    # 如果全部失败，停止增加
    if len(successes) == 0 and concurrency >= 3:
        print(f"  全部失败，停止测试")
        break
    
    # 如果失败率超过一半
    if len(failures) > len(successes) and concurrency >= 4:
        print(f"  失败率过高，停止增加并发")
        break
    
    time.sleep(2)

# 系统状态
print("\n--- 系统状态 ---")
import subprocess
r = subprocess.run(["free", "-h"], capture_output=True, text=True)
for line in r.stdout.split('\n'):
    if 'Mem' in line:
        print(f"  内存: {line.strip()}")

r = subprocess.run(["docker", "stats", "funasr-stream", "--no-stream", "--format", "CPU: {{.CPUPerc}}, MEM: {{.MemUsage}}"], 
                    capture_output=True, text=True)
print(f"  funasr-stream容器: {r.stdout.strip()}")

# JSON 汇总
print("\n--- JSON_SUMMARY ---")
print(json.dumps({
    'baseline_rtf': round(baseline_rtf, 3),
    'audio_duration': round(audio_duration, 1),
    'concurrent_results': stream_summary
}, ensure_ascii=False))
print("--- END_JSON ---")
'''

# ==================== 主流程 ====================
print("=" * 70)
print("ASR 综合压力测试")
print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

# 0. 检查服务器连通性
print("\n0. 检查服务器连通性...")
out, _ = ssh_exec("echo OK")
if "OK" not in out:
    print("  无法连接服务器!")
    sys.exit(1)
print("  服务器连接正常")

# 检查容器状态
out, _ = ssh_exec("docker ps --filter name=funasr --format 'table {{.Names}}\t{{.Status}}'")
print(f"  容器状态:\n{out}")

# 1. 准备测试音频
print("1. 准备测试音频...")
ssh_exec("mkdir -p /opt/funasr/tmp")

# 检查是否已有测试音频
out, _ = ssh_exec("ls -la /opt/funasr/tmp/perf_test*.wav 2>/dev/null")
if "perf_test" not in out:
    # 上传测试音频
    print("  上传测试音频...")
    local_audio = r"D:\funasr\480001011453538.wav"
    if os.path.exists(local_audio):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(SERVER_IP, username=SERVER_USER, password=SERVER_PASS, timeout=30)
        sftp = client.open_sftp()
        sftp.put(local_audio, '/opt/funasr/tmp/perf_test.wav')
        sftp.close()
        client.close()
        print("  上传完成")
    else:
        print(f"  本地音频文件不存在: {local_audio}")
        # 尝试在服务器上生成测试音频
        ssh_exec("python3 -c \"import numpy as np, wave; data=(np.random.randn(16000*60)*1000).astype(np.int16); w=wave.open('/opt/funasr/tmp/perf_test.wav','wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(data.tobytes()); w.close()\"")
        print("  已生成随机测试音频(60s)")
else:
    print(f"  已有测试音频:\n{out}")

# 截取 30s 和 60s 版本
ssh_exec("ffmpeg -y -i /opt/funasr/tmp/perf_test.wav -t 30 -ar 16000 -ac 1 -acodec pcm_s16le /opt/funasr/tmp/perf_test_30s.wav 2>/dev/null")
ssh_exec("ffmpeg -y -i /opt/funasr/tmp/perf_test.wav -t 60 -ar 16000 -ac 1 -acodec pcm_s16le /opt/funasr/tmp/perf_test_60s.wav 2>/dev/null")

out, _ = ssh_exec("ls -lh /opt/funasr/tmp/perf_test*.wav")
print(f"  音频文件:\n{out}")

# 2. 离线 ASR 压力测试
print("\n" + "=" * 70)
print("第一部分：离线 ASR 压力测试")
print("=" * 70)
out, err = upload_and_run(OFFLINE_STRESS_SCRIPT, 'offline_stress_test.py', timeout=1800)
print(out)
if err:
    print(f"[STDERR] {err[:500]}")

# 3. 流式 ASR 压力测试
# 临时关闭 SSL（python-socketio 客户端不支持 ssl_verify 参数）
print("\n" + "=" * 70)
print("第二部分：流式 ASR 压力测试")
print("=" * 70)

print("临时关闭流式服务 SSL...")
disable_ssl_cmd = '''docker exec funasr-stream python3 -c "
c = open('/opt/funasr/stream_server.py').read()
c2 = c.replace('ssl_context=ssl_ctx', 'ssl_context=None')
open('/opt/funasr/stream_server.py','w').write(c2)
print('SSL disabled')
"
'''
out, _ = ssh_exec(disable_ssl_cmd)
print(f"  {out.strip()}")
out, _ = ssh_exec("docker restart funasr-stream")
print("  等待流式服务重启和模型加载 (约40秒)...")
time.sleep(40)
# 循环检查 health 直到成功
for _check in range(6):
    out = ssh_exec("curl -s http://localhost:5002/health")[0].strip()
    if out and 'ok' in out:
        break
    print(f"  等待中... ({(_check+1)*10}s)")
    time.sleep(10)
print(f"  HTTP health: {out}")

out, err = upload_and_run(STREAM_STRESS_SCRIPT, 'stream_stress_test.py', timeout=1800)
print(out)
if err:
    print(f"[STDERR] {err[:500]}")

# 恢复 SSL
print("恢复流式服务 SSL...")
enable_ssl_cmd = '''docker exec funasr-stream python3 -c "
c = open('/opt/funasr/stream_server.py').read()
c2 = c.replace('ssl_context=None', 'ssl_context=ssl_ctx')
open('/opt/funasr/stream_server.py','w').write(c2)
print('SSL restored')
"
'''
out, _ = ssh_exec(enable_ssl_cmd)
print(f"  {out.strip()}")
out, _ = ssh_exec("docker restart funasr-stream")
print("  等待流式服务恢复 (约40秒)...")
time.sleep(40)
for _check in range(6):
    out = ssh_exec("curl -sk https://localhost:5002/health")[0].strip()
    if out and 'ok' in out:
        break
    print(f"  等待中... ({(_check+1)*10}s)")
    time.sleep(10)
print(f"  HTTPS health: {out}")

# 4. 最终系统状态
print("\n" + "=" * 70)
print("测试后系统状态")
print("=" * 70)
out, _ = ssh_exec("free -h | grep Mem")
print(f"内存: {out.strip()}")

out, _ = ssh_exec("docker stats funasr funasr-stream --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'")
print(f"容器资源:\n{out}")

out, _ = ssh_exec("uptime")
print(f"服务器运行时间: {out.strip()}")

print("\n" + "=" * 70)
print(f"压力测试完成! {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)
