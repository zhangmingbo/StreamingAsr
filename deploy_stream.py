"""
部署 sherpa-onnx 版流式服务
1. 上传 stream_server.py
2. 安装 sherpa-onnx
3. 下载流式模型
4. 启动服务
"""
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

# 1. 上传代码
print("1/5 上传 stream_server.py...")
sftp_upload(r'd:\funasr\stream_server.py', '/opt/funasr/stream_server.py')
print("    完成")

# 2. 安装 sherpa-onnx
print("\n2/5 安装 sherpa-onnx...")
out, err = ssh_exec("docker exec funasr pip install sherpa-onnx -i https://mirrors.aliyun.com/pypi/simple/ 2>&1 | tail -5")
print(f"    {out.strip()}")

# 3. 下载流式模型
print("\n3/5 下载流式 Paraformer 模型 (中英双语)...")
MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2"
out, err = ssh_exec(f"""
if [ -d /opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en ]; then
    echo '模型已存在，跳过下载'
else
    cd /opt/funasr
    echo '下载中...'
    wget -q --show-progress '{MODEL_URL}' -O model.tar.bz2 2>&1
    echo '解压中...'
    tar xjf model.tar.bz2
    rm model.tar.bz2
    echo '完成'
fi
ls -la /opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en/
""", timeout=600)
print(f"    {out.strip()}")
if err.strip():
    print(f"    [STDERR] {err.strip()}")

# 4. 检查模型文件
print("\n4/5 检查模型文件...")
out, err = ssh_exec("ls -lh /opt/funasr/sherpa-onnx-streaming-paraformer-bilingual-zh-en/")
print(f"    {out.strip()}")

# 5. 启动服务
print("\n5/5 启动流式服务...")
out, err = ssh_exec("docker exec -d funasr python3 /opt/funasr/stream_server.py")
print("    已启动，等待模型加载...")
time.sleep(15)

# 检查状态
out, err = ssh_exec("curl -s http://localhost:5002/health 2>&1")
print(f"\n=== 健康检查 ===\n{out.strip()}")

out, err = ssh_exec("docker stats --no-stream")
print(f"\n=== Docker Stats ===\n{out}")

out, err = ssh_exec("free -h")
print(f"=== Memory ===\n{out}")
