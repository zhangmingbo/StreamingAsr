"""在服务器上构建 PyTorch Paraformer-large 流式服务镜像"""
import paramiko

def ssh_exec(cmd, timeout=300):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return out, err

# 上传 Dockerfile 和代码
print("Uploading files to server...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()

# 上传 Dockerfile
sftp.put(r'd:\funasr\Dockerfile.stream.pytorch', '/tmp/Dockerfile.stream.pytorch')
print("  Uploaded Dockerfile")

# 上传 stream_server_pytorch.py
sftp.put(r'd:\funasr\stream_server_pytorch.py', '/tmp/stream_server_pytorch.py')
print("  Uploaded stream_server_pytorch.py")

# 上传测试页面
sftp.put(r'd:\funasr\stream_test.html', '/tmp/stream_test.html')
print("  Uploaded stream_test.html")

sftp.close()
client.close()

# 创建构建目录并复制文件
print("\nPreparing build context...")
out, err = ssh_exec("""
mkdir -p /tmp/funasr-build-pytorch
cp /tmp/Dockerfile.stream.pytorch /tmp/funasr-build-pytorch/Dockerfile
cp /tmp/stream_server_pytorch.py /tmp/funasr-build-pytorch/stream_server_pytorch.py
cp /tmp/stream_test.html /tmp/funasr-build-pytorch/stream_test.html
echo "Build context ready"
""")
print(out.strip())

# 停止并删除旧容器
print("\nStopping old container...")
out, err = ssh_exec("docker stop funasr-stream 2>/dev/null; docker rm funasr-stream 2>/dev/null; echo 'Old container removed'")
print(out.strip())

# 构建镜像
print("\nBuilding image (this may take a few minutes)...")
out, err = ssh_exec(
    "cd /tmp/funasr-build-pytorch && docker build -t funasr-stream-pytorch . 2>&1",
    timeout=300
)
print(out)
if err.strip():
    print(f"STDERR: {err[:500]}")

# 检查镜像
print("\nChecking built image...")
out, err = ssh_exec("docker images | grep funasr-stream-pytorch")
print(out)

# 启动新容器
print("\nStarting new container...")
out, err = ssh_exec("""
docker run -d --name funasr-stream \
  -p 5002:5002 \
  -v /opt/funasr/results:/opt/funasr/results \
  -v /opt/funasr/audio:/opt/funasr/audio \
  -v /opt/funasr/models:/opt/funasr/models \
  -v /opt/funasr/server.crt:/opt/funasr/server.crt \
  -v /opt/funasr/server.key:/opt/funasr/server.key \
  funasr-stream-pytorch 2>&1
""")
print(f"Container ID: {out.strip()}")

# 等待服务启动
print("\nWaiting for service to start...")
ssh_exec("sleep 10")

# 检查容器状态
print("\nChecking container status...")
out, err = ssh_exec("docker ps | grep funasr-stream")
print(out)

# 健康检查
print("\nHealth check...")
out, err = ssh_exec("curl -sk https://localhost:5002/health 2>&1 || curl -s http://localhost:5002/health 2>&1")
print(out)

# 查看日志
print("\nRecent logs...")
out, err = ssh_exec("docker logs funasr-stream --tail 20 2>&1")
print(out)
