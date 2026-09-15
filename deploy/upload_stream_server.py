import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from ops.config import ssh_connect, sftp_upload, ROOT, CONTAINER_NAME

client = ssh_connect()

sftp = client.open_sftp()
# 上传 stream_server.py
sftp.put(str(ROOT / 'src' / 'stream_server.py'), '/opt/streaming-asr/src/stream_server.py')
print('uploaded: stream_server.py')
# 上传 asr_license_client.py
sftp.put(str(ROOT / 'src' / 'asr_license_client.py'), '/opt/streaming-asr/src/asr_license_client.py')
print('uploaded: asr_license_client.py')
sftp.close()

# 重启流式服务容器
print(f'重启 {CONTAINER_NAME} 容器...')
stdin, stdout, stderr = client.exec_command(f'docker restart {CONTAINER_NAME}', timeout=30)
print(stdout.read().decode('utf-8', errors='replace'))

print('\n完成！等待 30 秒后检查日志...')
client.close()
