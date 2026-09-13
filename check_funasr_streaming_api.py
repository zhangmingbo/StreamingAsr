"""检查 funasr 包的流式 API"""
import paramiko

def ssh_exec(cmd, timeout=30):
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
import inspect

# 检查 AutoModel
print("AutoModel signature:")
sig = inspect.signature(AutoModel.__init__)
for name, param in sig.parameters.items():
    if name != "self":
        default = param.default if param.default != inspect.Parameter.empty else "REQUIRED"
        print(f"  {name}: {default}")

# 检查 generate 方法
if hasattr(AutoModel, 'generate'):
    print("\\nAutoModel.generate signature:")
    sig_gen = inspect.signature(AutoModel.generate)
    for name, param in sig_gen.parameters.items():
        if name != "self":
            default = param.default if param.default != inspect.Parameter.empty else "REQUIRED"
            print(f"  {name}: {default}")
else:
    print("\\nNo generate method")

# 测试加载流式模型
try:
    print("\\nTrying to load streaming model...")
    model = AutoModel(model="paraformer-zh-streaming", disable_update=True)
    print("SUCCESS: Streaming model loaded")
except Exception as e:
    print(f"FAILED: {e}")
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
with sftp.open('/tmp/check_funasr_streaming.py', 'w') as f:
    f.write(script)
sftp.close()
client.close()

ssh_exec("docker cp /tmp/check_funasr_streaming.py funasr:/tmp/check_funasr_streaming.py")
out, err = ssh_exec("docker exec funasr timeout 30 python3 /tmp/check_funasr_streaming.py 2>&1")
print(out)
if err.strip():
    print(f"STDERR: {err[:500]}")
