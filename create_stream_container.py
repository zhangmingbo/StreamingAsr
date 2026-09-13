"""清理并创建流式服务容器"""
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

# 1. 杀掉 funasr 容器内的 stream_server 进程
print("1/4 杀掉 funasr 容器内的流式服务进程...")
kill_script = '''
import os, signal
for p in os.listdir("/proc"):
    if not p.isdigit():
        continue
    try:
        cmd = open(f"/proc/{p}/cmdline", "rb").read().decode("utf-8", "ignore")
        if "stream_server" in cmd:
            pid = int(p)
            os.kill(pid, signal.SIGKILL)
            print(f"Killed {pid}")
    except:
        pass
'''
sftp_script = '''
import paramiko
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
with sftp.open('/tmp/kill_stream.py', 'w') as f:
    f.write("""import os, signal
for p in os.listdir('/proc'):
    if not p.isdigit(): continue
    try:
        cmd = open(f'/proc/{p}/cmdline', 'rb').read().decode('utf-8', 'ignore')
        if 'stream_server' in cmd:
            os.kill(int(p), signal.SIGKILL)
            print(f'Killed {p}')
    except: pass
""")
sftp.close()
client.close()
'''
exec(sftp_script)
out, _ = ssh_exec("docker exec funasr python3 /tmp/kill_stream.py")
print(f"   {out.strip()}")

# 2. 清理失败容器
print("\n2/4 清理失败容器...")
out, _ = ssh_exec("docker rm -f funasr-stream confident_lamport 2>/dev/null; echo 'Done'")
print(f"   {out.strip()}")

time.sleep(2)

# 3. 创建新容器
print("\n3/4 创建流式服务容器...")
cmd = """docker run -d --name funasr-stream \\
  --restart always \\
  -p 5002:5002 \\
  -v /opt/funasr/stream_server.py:/opt/funasr/stream_server.py \\
  -v /opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en:/opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en \\
  -v /opt/funasr/results:/opt/funasr/results \\
  -v /opt/funasr/audio:/opt/funasr/audio \\
  funasr-onnx-v3 \\
  python3 /opt/funasr/stream_server.py"""
out, err = ssh_exec(cmd)
if err and "already allocated" not in err:
    print(f"   错误: {err.strip()}")
else:
    print(f"   容器 ID: {out.strip()}")

# 4. 检查状态
print("\n4/4 检查状态...")
time.sleep(10)
out, _ = ssh_exec("curl -s http://localhost:5002/health")
print(f"   健康检查: {out.strip()}")

out, _ = ssh_exec("docker logs funasr-stream --tail 10 2>&1")
print(f"\n容器日志:\n{out}")

out, _ = ssh_exec("docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'")
print(f"容器列表:\n{out}")
