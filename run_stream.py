"""Run stream_server in foreground to see errors"""
import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)

print("=== 前台运行 stream_server (10秒) ===")
stdin, stdout, stderr = client.exec_command(
    "timeout 10 docker exec funasr python3 /opt/funasr/stream_server.py 2>&1 || true",
    timeout=20
)
output = stdout.read().decode('utf-8', errors='replace')
print(output.encode('gbk', errors='replace').decode('gbk'))

client.close()
