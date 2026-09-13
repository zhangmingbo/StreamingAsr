"""下载流式 ONNX 模型到服务器"""
import paramiko

def ssh_exec(cmd, timeout=300):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return out, err

# 在 funasr 容器内下载模型（容器已有 modelscope）
download_script = '''
from modelscope import snapshot_download
path = snapshot_download(
    "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx",
    cache_dir="/opt/funasr/models"
)
print("Downloaded to:", path)
'''

# 写入脚本到服务器
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
with sftp.open('/tmp/download_stream_model.py', 'w') as f:
    f.write(download_script)
sftp.close()
client.close()

# 复制到容器并执行
print("Copying script to container...")
out, err = ssh_exec("docker cp /tmp/download_stream_model.py funasr:/tmp/download_stream_model.py")
print(f"Copy: {out.strip()} {err.strip()}")

print("Downloading model (this may take a while)...")
out, err = ssh_exec("docker exec funasr python3 /tmp/download_stream_model.py 2>&1", timeout=300)
print(out)
if err.strip():
    print(f"STDERR: {err[:500]}")
