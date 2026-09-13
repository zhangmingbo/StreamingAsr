import paramiko
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)

# 检查流式 ASR 最新日志
print('=== funasr-stream 最新日志 ===')
stdin, stdout, stderr = client.exec_command('docker logs funasr-stream --tail 30 2>&1', timeout=10)
print(stdout.read().decode('utf-8', errors='replace'))

client.close()
