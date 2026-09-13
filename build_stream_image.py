"""构建流式服务 Docker 镜像（包含模型）"""
import paramiko
import time

def ssh_exec(cmd, timeout=600):
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

MODEL_SRC = "/opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en"
BUILD_DIR = "/opt/funasr/stream-build"

# 1. 准备构建目录
print("1/6 准备构建目录...")
out, err = ssh_exec(f"rm -rf {BUILD_DIR} && mkdir -p {BUILD_DIR}/sherpa-model")
print(f"   完成")

# 2. 上传 Dockerfile 和代码
print("\n2/6 上传文件...")
sftp_upload(r'd:\funasr\Dockerfile.stream', f'{BUILD_DIR}/Dockerfile')
sftp_upload(r'd:\funasr\stream_server.py', f'{BUILD_DIR}/stream_server.py')
print(f"   Dockerfile + stream_server.py 已上传")

# 3. 复制模型文件到构建目录
print("\n3/6 复制模型文件到构建目录...")
out, err = ssh_exec(f"cp {MODEL_SRC}/encoder.int8.onnx {MODEL_SRC}/decoder.int8.onnx {MODEL_SRC}/tokens.txt {BUILD_DIR}/sherpa-model/")
if err:
    print(f"   错误: {err}")
else:
    print(f"   模型文件已复制")

# 验证构建目录
out, _ = ssh_exec(f"ls -lh {BUILD_DIR}/ && ls -lh {BUILD_DIR}/sherpa-model/")
print(f"   构建目录:\n{out}")

# 4. 构建镜像
print("\n4/6 构建 Docker 镜像 (funasr-stream-v2)...")
out, err = ssh_exec(f"cd {BUILD_DIR} && docker build -t funasr-stream-v2 -f Dockerfile . 2>&1", timeout=600)
print(out[-2000:] if len(out) > 2000 else out)
if err and "error" in err.lower():
    print(f"   构建错误: {err}")

# 5. 检查镜像
print("\n5/6 检查镜像...")
out, _ = ssh_exec("docker images | grep funasr-stream")
print(f"   {out}")

# 6. 替换容器
print("\n6/6 替换容器...")

# 停止并删除旧容器
out, _ = ssh_exec("docker rm -f funasr-stream")
print(f"   删除旧容器: {out.strip()}")

# 创建新容器 (模型已内置，但仍挂载 results/audio 目录)
cmd = f"""docker run -d --name funasr-stream \\
  --restart always \\
  -p 5002:5002 \\
  -v /opt/funasr/results:/opt/funasr/results \\
  -v /opt/funasr/audio:/opt/funasr/audio \\
  funasr-stream-v2"""
out, err = ssh_exec(cmd)
if err:
    print(f"   创建错误: {err}")
else:
    print(f"   容器已创建: {out.strip()}")

# 等待启动
time.sleep(10)

# 健康检查
print("\n=== 验证 ===")
out, _ = ssh_exec("curl -s http://localhost:5002/health")
print(f"健康检查: {out.strip()}")

out, _ = ssh_exec("docker logs funasr-stream --tail 15 2>&1")
print(f"\n日志:\n{out}")

out, _ = ssh_exec("docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'")
print(f"\n容器列表:\n{out}")

out, _ = ssh_exec("free -h")
print(f"内存:\n{out}")

# 清理构建目录
ssh_exec(f"rm -rf {BUILD_DIR}")
print("构建目录已清理")
