import paramiko
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)

# 检查容器挂载的启动文件
print('=== funasr-stream 容器启动命令 ===')
stdin, stdout, stderr = client.exec_command('docker inspect funasr-stream --format "{{.Config.Cmd}}"', timeout=10)
print(stdout.read().decode('utf-8', errors='replace'))

# 检查宿主机上的 stream_server.py
print('\n=== 宿主机 stream_server.py 前 20 行 ===')
stdin, stdout, stderr = client.exec_command('head -20 /opt/funasr/stream_server.py', timeout=10)
print(stdout.read().decode('utf-8', errors='replace'))

client.close()
