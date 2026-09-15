"""Upload fixed stream_server.py and restart streaming service"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops.config import ssh_exec, sftp_upload, ROOT, CONTAINER_NAME, SERVICE_PORT

# Upload fixed code
print("1/4 上传修复后的 stream_server.py...")
sftp_upload(str(ROOT / 'src' / 'stream_server.py'), '/opt/streaming-asr/src/stream_server.py')
print("   上传完成")

# Kill old stream_server
print("\n2/4 停止旧流式服务...")
kill_script = '''
import os, signal
for pid_dir in os.listdir('/proc'):
    if not pid_dir.isdigit():
        continue
    try:
        with open(f'/proc/{pid_dir}/cmdline', 'r') as f:
            cmdline = f.read()
            if 'stream_server' in cmdline:
                pid = int(pid_dir)
                print(f"Killing PID {pid}")
                os.kill(pid, signal.SIGKILL)
    except:
        pass
print("Done")
'''
out, _ = ssh_exec(f'docker exec {CONTAINER_NAME} python3 -c "{kill_script}"')
print(f"   {out.strip()}")

time.sleep(2)

# Start streaming service
print("\n3/4 启动流式服务...")
out, err = ssh_exec(f"docker exec -d {CONTAINER_NAME} python3 /opt/streaming-asr/src/stream_server.py")
print("   已启动，等待模型加载 (10秒)...")
time.sleep(10)

# Check health
print("\n4/4 检查状态...")
out, _ = ssh_exec(f"curl -s http://localhost:{SERVICE_PORT}/health")
print(f"   流式服务: {out.strip() or '(无响应)'}")
