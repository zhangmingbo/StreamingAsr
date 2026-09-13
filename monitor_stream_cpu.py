"""实时监测流式 ASR 服务的 CPU 和内存使用率"""
import paramiko
import time
from datetime import datetime

def ssh_exec(cmd, timeout=10):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return out, err

# 监测脚本（在服务器上运行）
monitor_script = '''
import psutil
import time
import sys

container_name = "funasr-stream"

# 获取容器 PID
def get_container_pid():
    import subprocess
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Pid}}", container_name],
        capture_output=True, text=True
    )
    pid = result.stdout.strip()
    return int(pid) if pid.isdigit() else None

pid = get_container_pid()
if not pid:
    print("Error: Container not found")
    sys.exit(1)

print(f"Container PID: {pid}")
print("="*80)
print(f"{'时间':<20} {'CPU%':<10} {'内存MB':<12} {'会话数':<10}")
print("="*80)

try:
    while True:
        try:
            process = psutil.Process(pid)
            cpu_percent = process.cpu_percent(interval=1)
            mem_info = process.memory_info()
            mem_mb = mem_info.rss / 1024 / 1024
            
            # 获取当前时间
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            
            print(f"{now:<20} {cpu_percent:<10.1f} {mem_mb:<12.1f} N/A")
            sys.stdout.flush()
            
            time.sleep(2)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            print("Process ended or access denied")
            break
except KeyboardInterrupt:
    print("\\nMonitoring stopped")
'''

# 上传监测脚本
print("Uploading monitor script...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
with sftp.open('/tmp/monitor_stream_service.py', 'w') as f:
    f.write(monitor_script)
sftp.close()
client.close()

print("\nStarting real-time monitoring...")
print("Press Ctrl+C to stop\n")

# 执行监测
try:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    
    # 先安装 psutil（如果还没有）
    stdin, stdout, stderr = client.exec_command("pip install psutil -q 2>&1 | tail -1")
    stdout.read()
    
    # 启动监测
    stdin, stdout, stderr = client.exec_command("python3 /tmp/monitor_stream_service.py", timeout=3600)
    
    # 实时输出
    while True:
        line = stdout.readline()
        if not line:
            break
        print(line, end='')
        
except KeyboardInterrupt:
    print("\n\nMonitoring stopped by user")
finally:
    client.close()
