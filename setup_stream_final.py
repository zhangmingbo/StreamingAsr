"""用 sleep 保持容器运行，安装 sherpa_onnx"""
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

# 1. 删除旧容器
print("1/5 删除旧容器...")
out, _ = ssh_exec("docker rm -f funasr-stream")
print(f"   {out.strip()}")

# 2. 用 sleep 命令创建容器 (保持运行)
print("\n2/5 创建容器 (sleep 模式)...")
cmd = """docker run -d --name funasr-stream \\
  --restart always \\
  -p 5002:5002 \\
  -v /opt/funasr/stream_server.py:/opt/funasr/stream_server.py \\
  -v /opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en:/opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en \\
  -v /opt/funasr/results:/opt/funasr/results \\
  -v /opt/funasr/audio:/opt/funasr/audio \\
  funasr-onnx-v3 \\
  sleep infinity"""
out, err = ssh_exec(cmd)
if err:
    print(f"   错误: {err.strip()}")
else:
    print(f"   容器 ID: {out.strip()}")

# 3. 安装 sherpa_onnx
print("\n3/5 安装 sherpa_onnx...")
out, err = ssh_exec("docker exec funasr-stream pip install sherpa-onnx -i https://mirrors.aliyun.com/pypi/simple/ 2>&1 | tail -5", timeout=180)
print(f"   {out.strip()}")

# 4. 重启容器 (使用正确命令)
print("\n4/5 重启容器 (流式服务)...")
out, _ = ssh_exec("docker stop funasr-stream")
print(f"   停止: {out.strip()}")

out, _ = ssh_exec("docker rm funasr-stream")
print(f"   删除: {out.strip()}")

cmd = """docker run -d --name funasr-stream \\
  --restart always \\
  -p 5002:5002 \\
  -v /opt/funasr/stream_server.py:/opt/funasr/stream_server.py \\
  -v /opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en:/opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en \\
  -v /opt/funasr/results:/opt/funasr/results \\
  -v /opt/funasr/audio:/opt/funasr/audio \\
  funasr-onnx-v3 \\
  python3 /opt/funasr/stream_server.py"""
out, err = ssh_exec(cmd)
if err:
    print(f"   错误: {err.strip()}")
else:
    print(f"   容器 ID: {out.strip()}")

# 5. 检查状态
print("\n5/5 检查状态...")
time.sleep(15)
out, _ = ssh_exec("curl -s http://localhost:5002/health")
print(f"   健康检查: {out.strip()}")

out, _ = ssh_exec("docker logs funasr-stream --tail 10 2>&1")
print(f"\n日志:\n{out}")

out, _ = ssh_exec("docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'")
print(f"\n容器列表:\n{out}")

out, _ = ssh_exec("free -h")
print(f"内存:\n{out}")

