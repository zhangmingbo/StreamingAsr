"""恢复原版本 stream_server.py"""
import paramiko

print("Uploading backup...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
sftp.put(r'd:\funasr\stream_server_backup.py', '/tmp/stream_server_backup.py')
sftp.close()
client.close()

def ssh_exec(cmd, timeout=30):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return out, err

# 复制到容器
print("Copying to container...")
out, err = ssh_exec("docker cp /tmp/stream_server_backup.py funasr-stream:/opt/funasr/stream_server.py")
print(f"Copy: {out.strip()} {err.strip()}")

# 重启
print("Restarting...")
ssh_exec("docker restart funasr-stream")

import time
time.sleep(10)

# 检查
print("\nHealth check:")
out, err = ssh_exec("curl -sk https://localhost:5002/health 2>&1 || curl -s http://localhost:5002/health 2>&1")
print(out.strip())

print("\nLogs:")
out, err = ssh_exec("docker logs funasr-stream --tail 10 2>&1")
print(out)
