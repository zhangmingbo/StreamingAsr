"""测试 funasr AutoModel 加载流式模型（用模型名）"""
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

try:
    print("Loading ParaformerStreaming model...")
    model = AutoModel(model="ParaformerStreaming", disable_update=True)
    print("SUCCESS: Model loaded")
    
    # Check if it has streaming methods
    print(f"\\nModel type: {type(model)}")
    print(f"Has generate: {hasattr(model, 'generate')}")
    
    # Test inference with cache
    print("\\nTesting streaming inference...")
    dummy_audio = np.random.randn(9600).astype(np.float32)  # 600ms @ 16kHz
    cache = {}
    result = model.generate(input=dummy_audio, cache=cache, is_final=False, chunk_size=[0, 10, 5])
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
with sftp.open('/tmp/test_streaming_model.py', 'w') as f:
    f.write(script)
sftp.close()
client.close()

ssh_exec("docker cp /tmp/test_streaming_model.py funasr:/tmp/test_streaming_model.py")
out, err = ssh_exec("docker exec funasr timeout 60 python3 /tmp/test_streaming_model.py 2>&1")
print(out)
if err.strip():
    print(f"STDERR: {err[:500]}")
