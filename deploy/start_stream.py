"""Upload fixed stream_server.py and start streaming service"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops.config import ssh_exec, sftp_upload, ssh_connect, ROOT, CONTAINER_NAME, SERVICE_PORT

# Upload fixed code
print("1/3 上传修复后的 stream_server.py...")
sftp_upload(str(ROOT / 'src' / 'stream_server.py'), '/opt/streaming-asr/src/stream_server.py')
print("   上传完成")

# Start streaming service
print("\n2/3 启动流式服务...")
out, err = ssh_exec(f"docker exec -d {CONTAINER_NAME} python3 /opt/streaming-asr/src/stream_server.py")
print("   已启动，等待模型加载 (15秒)...")
time.sleep(15)

# Check health
print("\n3/3 检查状态...")
out, _ = ssh_exec(f"curl -s http://localhost:{SERVICE_PORT}/health")
print(f"   流式服务: {out.strip() or '(无响应)'}")

# Check logs
out, _ = ssh_exec(f"docker logs {CONTAINER_NAME} --tail 10 2>&1")
print(f"\n容器日志 (最后10行):\n{out}")

# Memory
out, _ = ssh_exec("free -h")
print(f"内存:\n{out}")
