"""测试 Paraformer-large streaming (PyTorch) 性能"""
import paramiko
import time

def ssh_exec(cmd, timeout=60):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return out, err

# 测试脚本
test_script = '''
import time
import numpy as np
from funasr import AutoModel

print("Loading Paraformer-large streaming model...")
t0 = time.time()
model = AutoModel(model="paraformer-zh-streaming", disable_update=True)
load_time = time.time() - t0
print(f"Model loaded in {load_time:.2f}s")

# Test inference with dummy audio
print("\\nTesting inference...")
dummy_audio = np.random.randn(16000 * 10).astype(np.float32)  # 10 seconds
chunk_size = [0, 10, 5]  # 600ms
cache = {}

t0 = time.time()
for i in range(0, len(dummy_audio), 9600):  # 600ms chunks
    chunk = dummy_audio[i:i+9600]
    is_final = (i + 9600 >= len(dummy_audio))
    result = model.generate(
        input=chunk,
        cache=cache,
        is_final=is_final,
        chunk_size=chunk_size,
        encoder_chunk_look_back=4,
        decoder_chunk_look_back=1,
    )
inference_time = time.time() - t0
audio_duration = len(dummy_audio) / 16000
rtf = inference_time / audio_duration

print(f"Audio duration: {audio_duration:.1f}s")
print(f"Inference time: {inference_time:.2f}s")
print(f"RTF: {rtf:.3f}")
print("Test completed successfully")
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
with sftp.open('/tmp/test_pytorch_streaming.py', 'w') as f:
    f.write(test_script)
sftp.close()
client.close()

ssh_exec("docker cp /tmp/test_pytorch_streaming.py funasr:/tmp/test_pytorch_streaming.py")
print("Running test (this may take a minute)...")
out, err = ssh_exec("docker exec funasr timeout 60 python3 /tmp/test_pytorch_streaming.py 2>&1", timeout=70)
print(out)
if err.strip():
    print(f"STDERR: {err[:500]}")
