"""检查流式容器的 Python 包"""
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
import pkg_resources
packages = [p.project_name for p in pkg_resources.working_set if 'funasr' in p.project_name.lower()]
print("FunASR packages:", packages)

# 尝试导入 funasr_onnx
try:
    import funasr_onnx
    print("funasr_onnx available")
    print("Classes:", [x for x in dir(funasr_onnx) if not x.startswith("_")])
except ImportError as e:
    print(f"funasr_onnx NOT available: {e}")
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
with sftp.open('/tmp/check_stream_packages.py', 'w') as f:
    f.write(script)
sftp.close()
client.close()

ssh_exec("docker cp /tmp/check_stream_packages.py funasr-stream:/tmp/check_stream_packages.py")
out, err = ssh_exec("docker exec funasr-stream python3 /tmp/check_stream_packages.py 2>&1")
print(out)
if err.strip():
    print(f"STDERR: {err}")
