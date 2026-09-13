"""在服务器上构建 funasr-stream-funasr 镜像"""
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
sftp.put(r'd:\funasr\Dockerfile.stream.funasr', '/tmp/Dockerfile.stream.funasr')
print("  Uploaded Dockerfile")

# 上传 stream_server_funasr.py
sftp.put(r'd:\funasr\stream_server_funasr.py', '/tmp/stream_server_funasr.py')
print("  Uploaded stream_server_funasr.py")

# 上传测试页面
sftp.put(r'd:\funasr\stream_test.html', '/tmp/stream_test.html')
print("  Uploaded stream_test.html")

sftp.close()
client.close()

# 创建构建目录并复制文件
print("\nPreparing build context...")
out, err = ssh_exec("""
mkdir -p /tmp/funasr-build
cp /tmp/Dockerfile.stream.funasr /tmp/funasr-build/Dockerfile
cp /tmp/stream_server_funasr.py /tmp/funasr-build/stream_server_funasr.py
cp /tmp/stream_test.html /tmp/funasr-build/stream_test.html
echo "Build context ready"
""")
print(out.strip())

# 构建镜像
print("\nBuilding image (this may take a few minutes)...")
out, err = ssh_exec(
    "cd /tmp/funasr-build && docker build -t funasr-stream-funasr . 2>&1",
    timeout=300
)
print(out)
if err.strip():
    print(f"STDERR: {err[:500]}")

# 检查镜像
print("\nChecking built image...")
out, err = ssh_exec("docker images | grep funasr-stream")
print(out)
