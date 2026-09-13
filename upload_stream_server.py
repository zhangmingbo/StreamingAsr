import paramiko
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)

sftp = client.open_sftp()
# 上传 stream_server_pytorch.py 为 stream_server.py
sftp.put(r'D:\funasr\stream_server_pytorch.py', '/opt/funasr/stream_server.py')
print('uploaded: stream_server.py (from stream_server_pytorch.py)')
sftp.close()

# 重启流式服务容器
print('重启 funasr-stream 容器...')
stdin, stdout, stderr = client.exec_command('docker restart funasr-stream', timeout=30)
print(stdout.read().decode('utf-8', errors='replace'))

print('\n完成！等待 30 秒后检查日志...')
client.close()
