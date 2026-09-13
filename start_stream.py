"""Upload fixed stream_server.py and start streaming service"""
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
print("1/3 上传修复后的 stream_server.py...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
sftp.put(r'd:\funasr\stream_server.py', '/opt/funasr/stream_server.py')
sftp.close()
client.close()
print("   上传完成")

# Start streaming service
print("\n2/3 启动流式服务...")
out, err = ssh_exec("docker exec -d funasr python3 /opt/funasr/stream_server.py")
print("   已启动，等待模型加载 (15秒)...")
time.sleep(15)

# Check health
print("\n3/3 检查状态...")
out, _ = ssh_exec("curl -s http://localhost:5002/health")
print(f"   流式服务: {out.strip() or '(无响应)'}")

# Check logs
out, _ = ssh_exec("docker logs funasr --tail 10 2>&1")
print(f"\n容器日志 (最后10行):\n{out}")

# Memory
out, _ = ssh_exec("free -h")
print(f"内存:\n{out}")
