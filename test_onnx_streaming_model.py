"""测试 funasr AutoModel 加载 ONNX 流式模型"""
import paramiko

def ssh_exec(cmd, timeout=60):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return out, err

script = '''
from funasr import AutoModel
import numpy as np

model_dir = "/opt/funasr/models/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx"

try:
    print(f"Loading model from {model_dir}...")
    model = AutoModel(model=model_dir, disable_update=True)
    print("SUCCESS: Model loaded")
    
    # Test inference
    print("\\nTesting inference...")
    dummy_audio = np.random.randn(16000).astype(np.float32)  # 1 second
    result = model.generate(input=dummy_audio, cache={}, is_final=False)
    print(f"Result: {result}")
except Exception as e:
    import traceback
    print(f"FAILED: {e}")
    traceback.print_exc()
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
with sftp.open('/tmp/test_onnx_streaming.py', 'w') as f:
    f.write(script)
sftp.close()
client.close()

ssh_exec("docker cp /tmp/test_onnx_streaming.py funasr:/tmp/test_onnx_streaming.py")
out, err = ssh_exec("docker exec funasr timeout 60 python3 /tmp/test_onnx_streaming.py 2>&1")
print(out)
if err.strip():
    print(f"STDERR: {err[:500]}")
