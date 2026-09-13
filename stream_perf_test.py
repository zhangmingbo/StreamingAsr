"""临时关闭HTTPS -> 测试流式性能 -> 恢复HTTPS"""
import paramiko
import time

def ssh_exec(cmd, timeout=300):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return out, err

test_script = '''
import time, json, base64, wave, numpy as np
import socketio, threading

WAV_FILE = "/tmp/test_200s.wav"
SERVER_URL = "http://127.0.0.1:5002"
SAMPLE_RATE = 16000
CHUNK_MS = 600

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

def run_test(simulate_realtime=True):
    audio, rate = read_wav(WAV_FILE)
    duration = len(audio) / rate
    chunk_samples = int(rate * CHUNK_MS / 1000)
    n_chunks = (len(audio) + chunk_samples - 1) // chunk_samples

    sio = socketio.Client()
    res = {"first": None, "partial": 0, "final": 0, "text": "", "done": False, "start": None}
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
        print(f"  [ERR] {data}")
        ev.set()

    @sio.on("disconnect")
    def on_disc():
        ev.set()

    sio.connect(SERVER_URL, transports=['websocket'])
    time.sleep(0.3)
    sio.emit("start", {"sample_rate": SAMPLE_RATE})
    time.sleep(0.3)

    res["start"] = time.time()
    for i in range(n_chunks):
        s = i * chunk_samples
        e = min(s + chunk_samples, len(audio))
        b64 = base64.b64encode(audio[s:e].tobytes()).decode()
        sio.emit("audio", {"audio": b64, "is_final": (i == n_chunks - 1)})
        if simulate_realtime:
            time.sleep(CHUNK_MS / 1000.0)

    sio.emit("end")
    ev.wait(timeout=60)
    total = time.time() - res["start"]
    try: sio.disconnect()
    except: pass

    return {"dur": duration, "total": total, "rtf": total/duration,
            "first": res["first"], "partial": res["partial"],
            "final": res["final"], "chars": len(res["text"]), "text": res["text"]}

audio, rate = read_wav(WAV_FILE)
dur = len(audio) / rate
print(f"audio={dur:.0f}s rate={rate}Hz")

# Test 1: realtime (2 runs)
print("\\n=== Real-time streaming (600ms/chunk) ===")
rt = []
for i in range(2):
    r = run_test(True)
    rt.append(r)
    f = f"{r['first']:.2f}s" if r['first'] else "N/A"
    print(f"  #{i+1}: total={r['total']:.1f}s first={f} partial={r['partial']} final={r['final']} chars={r['chars']}")

# Test 2: fast (2 runs)
print("\\n=== Fast streaming (no delay) ===")
fa = []
for i in range(2):
    r = run_test(False)
    fa.append(r)
    f = f"{r['first']:.2f}s" if r['first'] else "N/A"
    print(f"  #{i+1}: total={r['total']:.2f}s RTF={r['rtf']:.3f} first={f} partial={r['partial']} final={r['final']} chars={r['chars']}")

# Summary
print("\\n=== Summary ===")
if rt:
    at = sum(r['total'] for r in rt)/len(rt)
    af = sum((r['first'] for r in rt if r['first']), default=0) / max(1, sum(1 for r in rt if r['first']))
    print(f"Realtime: avg_total={at:.1f}s avg_first={af:.2f}s avg_chars={sum(r['chars'] for r in rt)//len(rt)}")
if fa:
    at = sum(r['total'] for r in fa)/len(fa)
    ar = sum(r['rtf'] for r in fa)/len(fa)
    af = sum((r['first'] for r in fa if r['first']), default=0) / max(1, sum(1 for r in fa if r['first']))
    print(f"Fast: avg_total={at:.2f}s RTF={ar:.3f}({1/ar:.0f}x realtime) avg_first={af:.2f}s avg_chars={sum(r['chars'] for r in fa)//len(fa)}")
print(f"\\nText sample: {fa[-1]['text'][:200] if fa else ''}")
'''

# 1. 临时关闭 SSL，重启
print("1. 临时关闭 SSL...")
cmd = """docker exec funasr-stream python3 -c "
c = open('/opt/funasr/stream_server.py').read()
c2 = c.replace('ssl_context=ssl_ctx', 'ssl_context=None')
open('/opt/funasr/stream_server.py','w').write(c2)
print('OK')
"
"""
out, _ = ssh_exec(cmd)
print(f"   {out.strip()}")
out, _ = ssh_exec("docker restart funasr-stream")
time.sleep(8)
out, _ = ssh_exec("curl -s http://localhost:5002/health")
print(f"   HTTP: {out.strip()}")

# 2. 上传并运行测试
print("\n2. 运行性能测试...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
with sftp.open('/tmp/stream_perf_test.py', 'w') as f:
    f.write(test_script)
sftp.close()
client.close()

ssh_exec("docker cp /tmp/stream_perf_test.py funasr-stream:/tmp/stream_perf_test.py")
out, err = ssh_exec("docker exec funasr-stream python3 /tmp/stream_perf_test.py 2>&1", timeout=900)
print(out)

# 3. 恢复 SSL
print("\n3. 恢复 HTTPS...")
cmd = """docker exec funasr-stream python3 -c "
c = open('/opt/funasr/stream_server.py').read()
c2 = c.replace('ssl_context=None', 'ssl_context=ssl_ctx')
open('/opt/funasr/stream_server.py','w').write(c2)
print('OK')
"
"""
out, _ = ssh_exec(cmd)
print(f"   {out.strip()}")
out, _ = ssh_exec("docker restart funasr-stream")
time.sleep(8)
out, _ = ssh_exec("curl -sk https://localhost:5002/health")
print(f"   HTTPS: {out.strip()}")
