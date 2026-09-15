import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops.config import ssh_exec, CONTAINER_NAME

# 用 /proc 找 stream_server 进程
kill_script = r'''
import os, signal
for pid_dir in os.listdir('/proc'):
    if not pid_dir.isdigit():
        continue
    try:
        with open(f'/proc/{pid_dir}/cmdline', 'r') as f:
            cmdline = f.read()
            if 'stream_server' in cmdline:
                pid = int(pid_dir)
                print(f"Found stream_server at PID {pid}, killing...")
                os.kill(pid, signal.SIGKILL)
                print(f"Killed PID {pid}")
    except:
        pass
print("Done")
'''

out, err = ssh_exec(f"docker exec {CONTAINER_NAME} python3 -c \"{kill_script}\"")
print("Kill result:", out)
if err:
    print("Error:", err)

# 等待内存释放
import time
time.sleep(3)

# 检查状态
out, err = ssh_exec("docker stats --no-stream; echo '==='; free -h")
print("\n=== Status ===")
print(out)
