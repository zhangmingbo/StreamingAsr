"""停止容器，安装 sherpa_onnx，再启动"""
import paramiko
import time

def ssh_exec(cmd, timeout=300):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return out, err

# 1. 停止容器
print("1/4 停止容器...")
out, _ = ssh_exec("docker stop funasr-stream")
print(f"   {out.strip()}")

# 2. 启动容器 (临时，为了安装)
print("\n2/4 临时启动容器...")
out, _ = ssh_exec("docker start funasr-stream")
print(f"   {out.strip()}")
time.sleep(3)

# 3. 安装 sherpa_onnx
print("\n3/4 安装 sherpa_onnx...")
out, err = ssh_exec("docker exec funasr-stream pip install sherpa-onnx -i https://mirrors.aliyun.com/pypi/simple/ 2>&1 | tail -5", timeout=180)
print(f"   {out.strip()}")

# 4. 重启容器
print("\n4/4 重启容器...")
out, _ = ssh_exec("docker restart funasr-stream")
print(f"   {out.strip()}")

time.sleep(15)

# 检查
out, _ = ssh_exec("curl -s http://localhost:5002/health")
print(f"\n健康检查: {out.strip()}")

out, _ = ssh_exec("docker logs funasr-stream --tail 10 2>&1")
print(f"\n日志:\n{out}")

out, _ = ssh_exec("docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'")
print(f"\n容器列表:\n{out}")
