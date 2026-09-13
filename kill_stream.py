import paramiko

def ssh_exec(cmd):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return out, err

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

out, err = ssh_exec(f"docker exec funasr python3 -c \"{kill_script}\"")
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
