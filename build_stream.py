"""构建并部署流式服务 Docker 镜像"""
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

def sftp_upload(local_path, remote_path):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    sftp = client.open_sftp()
    sftp.put(local_path, remote_path)
    sftp.close()
    client.close()

# 1. 上传 Dockerfile 和代码
print("1/6 上传文件...")
sftp_upload(r'd:\funasr\Dockerfile.stream', '/opt/funasr/Dockerfile.stream')
sftp_upload(r'd:\funasr\stream_server.py', '/opt/funasr/stream_server.py')
print("   完成")

# 2. 构建 Docker 镜像
print("\n2/6 构建 Docker 镜像 (funasr-stream)...")
out, err = ssh_exec("cd /opt/funasr && docker build -f Dockerfile.stream -t funasr-stream . 2>&1", timeout=300)
print(out[-500:] if len(out) > 500 else out)
if err:
    print(f"STDERR: {err[-200:]}")

# 3. 停止并删除旧容器
print("\n3/6 清理旧容器...")
out, _ = ssh_exec("docker stop funasr-stream 2>/dev/null; docker rm funasr-stream 2>/dev/null; echo 'Done'")
print(f"   {out.strip()}")

# 4. 启动新容器
print("\n4/6 启动流式服务容器...")
cmd = """docker run -d --name funasr-stream \\
  --restart always \\
  -p 5002:5002 \\
  -v /opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en:/opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en \\
  -v /opt/funasr/results:/opt/funasr/results \\
  -v /opt/funasr/audio:/opt/funasr/audio \\
  funasr-stream"""
out, err = ssh_exec(cmd)
print(f"   容器 ID: {out.strip()}")
if err:
    print(f"   错误: {err.strip()}")

# 5. 等待服务启动
print("\n5/6 等待服务启动 (15秒)...")
time.sleep(15)

# 6. 检查状态
print("\n6/6 检查状态...")
out, _ = ssh_exec("curl -s http://localhost:5002/health")
print(f"   健康检查: {out.strip()}")

out, _ = ssh_exec("docker logs funasr-stream --tail 20 2>&1")
print(f"\n容器日志:\n{out}")

out, _ = ssh_exec("docker stats --no-stream")
print(f"Docker Stats:\n{out}")

print("\n✅ 流式服务部署完成！")
print("   查看日志: docker logs -f funasr-stream")
print("   重启服务: docker restart funasr-stream")
