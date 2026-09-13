import paramiko
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)

# 检查流式 ASR 启动日志
print('=== funasr-stream 启动日志 ===')
stdin, stdout, stderr = client.exec_command('docker logs funasr-stream 2>&1 | grep -i "license\\|授权" | tail -10', timeout=10)
print(stdout.read().decode('utf-8', errors='replace'))

# 检查容器状态
print('\n=== 容器状态 ===')
stdin, stdout, stderr = client.exec_command('docker ps --filter name=funasr', timeout=10)
print(stdout.read().decode('utf-8', errors='replace'))

client.close()
