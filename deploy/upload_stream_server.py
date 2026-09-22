import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from ops.config import ssh_connect, ROOT, CONTAINER_NAME

client = ssh_connect()

# 1) 上传 .py 源码到宿主机（过渡路径）
sftp = client.open_sftp()
sftp.put(str(ROOT / 'src' / 'stream_server.py'), '/opt/streaming-asr/src/stream_server.py')
print('uploaded: stream_server.py (host)')
sftp.put(str(ROOT / 'src' / 'asr_license_client.py'), '/opt/streaming-asr/src/asr_license_client.py')
print('uploaded: asr_license_client.py (host)')
sftp.close()


def run(cmd, timeout=180):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    if err.strip():
        print(f'[warn] {err[:300]}')
    return out


# 2) docker cp 进容器 + 容器内编译 .pyc（黑盒模式：启动脚本优先运行 .pyc）
out = run(
    f'docker cp /opt/streaming-asr/src/stream_server.py {CONTAINER_NAME}:/opt/streaming-asr/src/stream_server.py; '
    f'docker cp /opt/streaming-asr/src/asr_license_client.py {CONTAINER_NAME}:/opt/streaming-asr/src/asr_license_client.py; '
    'echo CP_DONE'
)
print(out.strip())

out = run(
    f"docker exec {CONTAINER_NAME} python3 -c "
    f"\"import py_compile; "
    f"py_compile.compile('/opt/streaming-asr/src/stream_server.py', "
    f"cfile='/opt/streaming-asr/src/stream_server.pyc', doraise=True); "
    f"py_compile.compile('/opt/streaming-asr/src/asr_license_client.py', "
    f"cfile='/opt/streaming-asr/src/asr_license_client.pyc', doraise=True)\"; "
    'echo PYC_DONE'
)
print(out.strip())

# 3) 删除容器内 .py（保持黑盒结构，避免与 .pyc 并存混淆）
out = run(
    f'docker exec {CONTAINER_NAME} rm -f '
    f'/opt/streaming-asr/src/stream_server.py /opt/streaming-asr/src/asr_license_client.py; '
    'echo RM_DONE'
)
print(out.strip())

# 4) 重启容器（启动脚本自动选择 .pyc）
out = run(f'docker restart {CONTAINER_NAME}', timeout=60)
print(f'重启 {CONTAINER_NAME} 容器...')
print(out.strip())

print('\n完成！等待约 30 秒后 Gateway 就绪（模型加载）...')
client.close()
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
