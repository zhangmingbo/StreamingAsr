"""
ASR 15秒音频并发压力测试
在服务器本地运行，分别测试离线和流式 ASR
"""
import paramiko, time, json, sys, os, threading

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
    stdin, stdout, stderr = client.exec_command(f'cd /tmp && python3 {filename}', timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    client.close()
    return out, err

# ============================================================
# 1. 生成15秒测试音频 (16kHz, mono, 16bit WAV)
# ============================================================
print("=" * 60)
print("步骤1: 在服务器上生成15秒测试音频")
print("=" * 60)

gen_audio_script = r'''
import numpy as np, wave, os

sr = 16000
duration = 15  # 15秒

# 生成模拟语音信号 (正弦波叠加，模拟真实语音频谱)
t = np.linspace(0, duration, sr * duration, endpoint=False)
# 基频 + 谐波
signal = 0.3 * np.sin(2 * np.pi * 200 * t)
signal += 0.2 * np.sin(2 * np.pi * 400 * t)
signal += 0.15 * np.sin(2 * np.pi * 800 * t)
signal += 0.1 * np.sin(2 * np.pi * 1200 * t)
signal += 0.05 * np.sin(2 * np.pi * 2000 * t)
# 添加噪声使信号更真实
signal += 0.02 * np.random.randn(len(t))
# 归一化
signal = signal / np.max(np.abs(signal)) * 0.8
# 转为16bit PCM
pcm = (signal * 32767).astype(np.int16)

path = '/tmp/stress_15s.wav'
with wave.open(path, 'w') as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(sr)
    wf.writeframes(pcm.tobytes())

size = os.path.getsize(path)
print(f"生成完成: {path}, 大小={size}字节, 时长={duration}秒, 采样率={sr}Hz")
'''

out, err = upload_and_run(gen_audio_script, 'gen_15s_audio.py')
print(out)
if err:
    print('错误:', err)

# ============================================================
# 2. 离线 ASR 压力测试 (15秒音频)
# ============================================================
print("\n" + "=" * 60)
print("步骤2: 离线 ASR 15秒音频并发压力测试")
print("=" * 60)

offline_test_script = r'''
import requests, time, json, threading, sys

BASE_URL = "http://localhost:5000"
AUDIO_PATH = "/tmp/stress_15s.wav"
DURATION = 15.0  # 音频时长

results = {}
lock = threading.Lock()

def test_offline(conc_id, req_id):
    """单次离线识别请求"""
    start = time.time()
    try:
        with open(AUDIO_PATH, 'rb') as f:
            audio_data = f.read()
        
        files = {'file': (f'stress_15s_{req_id}.wav', audio_data, 'audio/wav')}
        resp = requests.post(
            f"{BASE_URL}/asr",
            files=files,
            data={'model': 'paraformer-zh'},
            timeout=120,
            stream=True
        )
        
        # 读取SSE响应
        full_text = ""
        first_chunk_time = None
        for line in resp.iter_lines(decode_unicode=True):
            if line and line.startswith('data:'):
                if first_chunk_time is None:
                    first_chunk_time = time.time() - start
                try:
                    data = json.loads(line[5:].strip())
                    if 'text' in data:
                        full_text = data['text']
                    if data.get('status') == 'end':
                        break
                except:
                    pass
        
        elapsed = time.time() - start
        rtf = elapsed / DURATION if DURATION > 0 else 0
        
        with lock:
            results[req_id] = {
                'success': resp.status_code == 200 and len(full_text) > 0,
                'status_code': resp.status_code,
                'elapsed': round(elapsed, 3),
                'rtf': round(rtf, 3),
                'first_chunk': round(first_chunk_time, 3) if first_chunk_time else None,
                'text_len': len(full_text),
                'text_preview': full_text[:80] if full_text else ''
            }
    except Exception as e:
        elapsed = time.time() - start
        with lock:
            results[req_id] = {
                'success': False,
                'error': str(e),
                'elapsed': round(elapsed, 3)
            }

# 测试梯度: 1, 2, 3, 5, 8, 10, 12, 15
levels = [1, 2, 3, 5, 8, 10, 12, 15]
summary = []

for level in levels:
    print(f"\n--- 并发 {level} 路 ---")
    results.clear()
    threads = []
    t0 = time.time()
    
    for i in range(level):
        t = threading.Thread(target=test_offline, args=(level, i))
        threads.append(t)
    
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    
    total_time = time.time() - t0
    
    success_count = sum(1 for r in results.values() if r.get('success'))
    fail_count = len(results) - success_count
    elapsed_list = [r['elapsed'] for r in results.values() if r.get('success')]
    rtf_list = [r['rtf'] for r in results.values() if r.get('success')]
    
    avg_elapsed = sum(elapsed_list) / len(elapsed_list) if elapsed_list else 0
    max_elapsed = max(elapsed_list) if elapsed_list else 0
    avg_rtf = sum(rtf_list) / len(rtf_list) if rtf_list else 0
    max_rtf = max(rtf_list) if rtf_list else 0
    
    status = "PASS" if success_count == level else "FAIL"
    print(f"  成功: {success_count}/{level}, 失败: {fail_count}")
    print(f"  总耗时: {total_time:.1f}s, 平均耗时: {avg_elapsed:.1f}s, 最大耗时: {max_elapsed:.1f}s")
    print(f"  平均RTF: {avg_rtf:.3f}, 最大RTF: {max_rtf:.3f}")
    
    # 打印第一个成功结果的文字预览
    for r in results.values():
        if r.get('success') and r.get('text_preview'):
            print(f"  识别文本预览: {r['text_preview'][:60]}...")
            break
    
    summary.append({
        'level': level,
        'success': success_count,
        'fail': fail_count,
        'total_time': round(total_time, 1),
        'avg_elapsed': round(avg_elapsed, 1),
        'max_elapsed': round(max_elapsed, 1),
        'avg_rtf': round(avg_rtf, 3),
        'max_rtf': round(max_rtf, 3),
        'status': status
    })
    
    if fail_count > 0 and success_count < level * 0.5:
        print(f"  失败率过高，停止测试")
        break

print("\n\n=== 离线 ASR 15秒音频压测汇总 ===")
print(f"{'并发':>4} | {'成功':>4} | {'失败':>4} | {'总耗时':>7} | {'平均耗时':>8} | {'最大耗时':>8} | {'平均RTF':>7} | {'最大RTF':>7} | {'状态':>4}")
print("-" * 85)
for s in summary:
    print(f"{s['level']:>4} | {s['success']:>4} | {s['fail']:>4} | {s['total_time']:>6.1f}s | {s['avg_elapsed']:>7.1f}s | {s['max_elapsed']:>7.1f}s | {s['avg_rtf']:>7.3f} | {s['max_rtf']:>7.3f} | {s['status']:>4}")
'''

out, err = upload_and_run(offline_test_script, 'offline_15s_test.py', timeout=1800)
print(out)
if err and 'WARNING' not in err:
    print('错误:', err[:2000])

# ============================================================
# 3. 流式 ASR 压力测试 (15秒音频)
# ============================================================
print("\n" + "=" * 60)
print("步骤3: 流式 ASR 15秒音频并发压力测试")
print("=" * 60)

stream_test_script = r'''
import socketio, time, json, threading, wave, sys

SERVER_URL = "http://localhost:5002"
AUDIO_PATH = "/tmp/stress_15s.wav"
DURATION = 15.0
CHUNK_SIZE = 3200  # 100ms @ 16kHz = 1600 samples * 2 bytes

results = {}
lock = threading.Lock()

def read_wav_frames(path, chunk_bytes=CHUNK_SIZE):
    """读取WAV文件并按chunk分帧"""
    with wave.open(path, 'rb') as wf:
        data = wf.readframes(wf.getnframes())
    frames = []
    for i in range(0, len(data), chunk_bytes):
        frames.append(data[i:i+chunk_bytes])
    return frames

def test_stream(conc_id, req_id):
    """单次流式识别请求"""
    start = time.time()
    try:
        sio = socketio.Client()
        connected = threading.Event()
        done = threading.Event()
        result_text = ""
        first_result_time = None
        
        @sio.event
        def connect():
            connected.set()
        
        @sio.on('asr_result')
        def on_asr_result(data):
            nonlocal result_text, first_result_time
            if first_result_time is None and data.get('text', '').strip():
                first_result_time = time.time() - start
            if data.get('text'):
                result_text = data['text']
            if data.get('is_final', False) or data.get('status') == 'end':
                done.set()
        
        sio.connect(SERVER_URL, transports=['websocket'])
        if not connected.wait(timeout=10):
            raise Exception("连接超时")
        
        # 发送开始信号
        sio.emit('start_recognition', {'sample_rate': 16000})
        
        # 分帧发送音频
        frames = read_wav_frames(AUDIO_PATH)
        for frame in frames:
            sio.emit('audio_chunk', frame)
            time.sleep(0.05)  # 模拟实时发送节奏（略快于实时）
        
        # 发送结束信号
        sio.emit('end_recognition')
        
        # 等待最终结果
        done.wait(timeout=60)
        elapsed = time.time() - start
        
        rtf = elapsed / DURATION if DURATION > 0 else 0
        
        with lock:
            results[req_id] = {
                'success': len(result_text) > 0,
                'elapsed': round(elapsed, 3),
                'rtf': round(rtf, 3),
                'first_result': round(first_result_time, 3) if first_result_time else None,
                'text_len': len(result_text),
                'text_preview': result_text[:80] if result_text else ''
            }
        
        sio.disconnect()
    except Exception as e:
        elapsed = time.time() - start
        with lock:
            results[req_id] = {
                'success': False,
                'error': str(e),
                'elapsed': round(elapsed, 3)
            }

# 测试梯度
levels = [1, 2, 3, 5, 8, 10, 12, 15]
summary = []

for level in levels:
    print(f"\n--- 并发 {level} 路 ---")
    results.clear()
    threads = []
    t0 = time.time()
    
    for i in range(level):
        t = threading.Thread(target=test_stream, args=(level, i))
        threads.append(t)
    
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    
    total_time = time.time() - t0
    
    success_count = sum(1 for r in results.values() if r.get('success'))
    fail_count = len(results) - success_count
    elapsed_list = [r['elapsed'] for r in results.values() if r.get('success')]
    rtf_list = [r['rtf'] for r in results.values() if r.get('success')]
    
    avg_elapsed = sum(elapsed_list) / len(elapsed_list) if elapsed_list else 0
    max_elapsed = max(elapsed_list) if elapsed_list else 0
    avg_rtf = sum(rtf_list) / len(rtf_list) if rtf_list else 0
    max_rtf = max(rtf_list) if rtf_list else 0
    
    status = "PASS" if success_count == level else "FAIL"
    print(f"  成功: {success_count}/{level}, 失败: {fail_count}")
    print(f"  总耗时: {total_time:.1f}s, 平均耗时: {avg_elapsed:.1f}s, 最大耗时: {max_elapsed:.1f}s")
    print(f"  平均RTF: {avg_rtf:.3f}, 最大RTF: {max_rtf:.3f}")
    
    for r in results.values():
        if r.get('success') and r.get('text_preview'):
            print(f"  识别文本预览: {r['text_preview'][:60]}...")
            break
    
    summary.append({
        'level': level,
        'success': success_count,
        'fail': fail_count,
        'total_time': round(total_time, 1),
        'avg_elapsed': round(avg_elapsed, 1),
        'max_elapsed': round(max_elapsed, 1),
        'avg_rtf': round(avg_rtf, 3),
        'max_rtf': round(max_rtf, 3),
        'status': status
    })
    
    if fail_count > 0 and success_count < level * 0.5:
        print(f"  失败率过高，停止测试")
        break

print("\n\n=== 流式 ASR 15秒音频压测汇总 ===")
print(f"{'并发':>4} | {'成功':>4} | {'失败':>4} | {'总耗时':>7} | {'平均耗时':>8} | {'最大耗时':>8} | {'平均RTF':>7} | {'最大RTF':>7} | {'状态':>4}")
print("-" * 85)
for s in summary:
    print(f"{s['level']:>4} | {s['success']:>4} | {s['fail']:>4} | {s['total_time']:>6.1f}s | {s['avg_elapsed']:>7.1f}s | {s['max_elapsed']:>7.1f}s | {s['avg_rtf']:>7.3f} | {s['max_rtf']:>7.3f} | {s['status']:>4}")
'''

out, err = upload_and_run(stream_test_script, 'stream_15s_test.py', timeout=1800)
print(out)
if err and 'WARNING' not in err:
    print('错误:', err[:2000])

# ============================================================
# 4. 恢复SSL
# ============================================================
print("\n" + "=" * 60)
print("步骤4: 恢复流式服务SSL配置")
print("=" * 60)

out, err = ssh_exec("sed -i 's/ssl_context=None/ssl_context=ssl_context/g' /opt/funasr/stream_server.py")
print('SSL恢复:', out, err)

out, err = ssh_exec("docker restart funasr-stream")
print('重启容器:', out, err)
time.sleep(5)

# 验证
out, err = ssh_exec('docker ps --filter name=funasr-stream --format "{{.Status}}"')
print('流式容器状态:', out.strip())

print("\n" + "=" * 60)
print("全部测试完成!")
print("=" * 60)
