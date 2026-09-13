"""上传 stream_server.py 到服务器并重启容器"""
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

# 上传文件
print("Uploading stream_server.py...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
sftp.put(r'd:\funasr\stream_server.py', '/tmp/stream_server_new.py')
sftp.close()
client.close()

# 复制到容器
print("Copying to container...")
out, err = ssh_exec("docker cp /tmp/stream_server_new.py funasr-stream:/opt/funasr/stream_server.py")
print(f"Copy result: {out.strip()} {err.strip()}")

# 重启容器
print("Restarting container...")
out, err = ssh_exec("docker restart funasr-stream")
print(f"Restart: {out.strip()}")

# 等待启动
import time
time.sleep(10)

# 检查健康状态
print("\nChecking health...")
out, err = ssh_exec("curl -sk https://localhost:5002/health 2>&1 || curl -s http://localhost:5002/health 2>&1")
print(f"Health: {out.strip()}")

# 查看日志
print("\nContainer logs:")
out, err = ssh_exec("docker logs funasr-stream --tail 20 2>&1")
print(out)
