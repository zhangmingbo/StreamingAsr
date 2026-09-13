"""Upload fixed stream_server.py and restart streaming service"""
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

# Upload fixed code
print("1/4 上传修复后的 stream_server.py...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
sftp.put(r'd:\funasr\stream_server.py', '/opt/funasr/stream_server.py')
sftp.close()
client.close()
print("   上传完成")

# Kill old stream_server
print("\n2/4 停止旧流式服务...")
kill_script = '''
import os, signal
for pid_dir in os.listdir('/proc'):
    if not pid_dir.isdigit():
        continue
    try:
        with open(f'/proc/{pid_dir}/cmdline', 'r') as f:
            cmdline = f.read()
            if 'stream_server' in cmdline:
                pid = int(pid_dir)
                print(f"Killing PID {pid}")
                os.kill(pid, signal.SIGKILL)
    except:
        pass
print("Done")
'''
out, _ = ssh_exec(f'docker exec funasr python3 -c "{kill_script}"')
print(f"   {out.strip()}")

time.sleep(2)

# Start streaming service
print("\n3/4 启动流式服务...")
out, err = ssh_exec("docker exec -d funasr python3 /opt/funasr/stream_server.py")
print("   已启动，等待模型加载 (10秒)...")
time.sleep(10)

# Check health
print("\n4/4 检查状态...")
out, _ = ssh_exec("curl -s http://localhost:5002/health")
print(f"   流式服务: {out.strip() or '(无响应)'}")
