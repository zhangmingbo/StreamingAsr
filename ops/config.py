"""
FunASR 流式语音识别服务 — 公共配置模块

所有脚本统一通过本模块读取配置，禁止在脚本中硬编码服务器地址/密码。
用法：
    import sys; sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ops.config import SERVER_IP, SERVER_USER, SERVER_PASS, ssh_connect

==========================================================================
 目录结构规范
==========================================================================
  streaming-asr/
  ├── src/            运行时代码（stream_server.py, stream_test.html）
  ├── docker/         Dockerfile 集合
  ├── build/          构建脚本（build_stream.py 等）
  ├── deploy/         部署/运维脚本（deploy_*, start_*, restart_* 等）
  ├── ops/            运维检查/监控（check_*, monitor_*）+ 本配置模块
  ├── tests/          测试脚本
  │   ├── perf/       性能测试
  │   └── stress/     压力测试
  └── web/            前端页面

  原则：
  - src/ 只放部署到容器内运行的代码
  - build/ 只放 docker build 相关的脚本
  - deploy/ 只放 SSH 远程操作服务器的脚本
  - ops/ 只放只读的检查/监控脚本
  - tests/ 只放测试用例（含性能/压力子目录）
  - web/ 只放浏览器端 HTML/JS

==========================================================================
 编码规范
==========================================================================
  - 所有 Python 文件：UTF-8 无 BOM
  - 所有脚本文件开头不加 coding 声明（Python 3 默认 UTF-8）
  - PowerShell 中多行脚本使用 ''' 三引号包裹，注意转义 \\n 与 \n
  - 换行符统一使用 LF（.gitattributes 可约束）
  - SSH 远程执行的 Python 脚本内嵌时，注意 \\ 转义

==========================================================================
 路径映射规范
==========================================================================
  服务器项目目录: /opt/streaming-asr/

  宿主机路径                        →  容器内路径
  /opt/funasr/models                →  /opt/funasr/models          （模型，共享 volume）
  /opt/streaming-asr/results        →  /opt/streaming-asr/results  （识别结果）
  /opt/streaming-asr/audio          →  /opt/streaming-asr/audio    （录音文件）
  /opt/streaming-asr/src/stream_server.py       ←  服务代码
  /opt/streaming-asr/src/asr_license_client.py  ←  授权客户端

  全局搜索规则：
  - 修改容器内路径时，必须搜索：HOST_MODEL_DIR, HOST_RESULT_DIR,
    HOST_AUDIO_DIR, CONTAINER_WORKDIR, CONTAINER_SERVER_PATH
  - 修改宿主机 IP 时，只需修改 .env 中的 SERVER_IP
  - Dockerfile 中的路径修改需同步更新对应的 build 脚本
"""

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    print("[config] python-dotenv 未安装，请执行: pip install python-dotenv")
    sys.exit(1)

# ─── 项目根目录 ───
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / '.env'

if not ENV_FILE.exists():
    print(f"[config] .env 文件不存在: {ENV_FILE}")
    print(f"[config] 请复制 .env.example 为 .env 并填写配置")
    sys.exit(1)

load_dotenv(ENV_FILE)

# ─── 云服务器配置 ───
SERVER_IP = os.getenv('SERVER_IP', '8.153.92.96')
SERVER_USER = os.getenv('SERVER_USER', 'root')
SERVER_PASS = os.getenv('SERVER_PASS', '')

# ─── Docker 容器 ───
CONTAINER_NAME = os.getenv('CONTAINER_NAME', 'funasr-stream')
SERVICE_PORT = int(os.getenv('SERVICE_PORT', '5002'))

# ─── 宿主机路径 ───
HOST_MODEL_DIR = os.getenv('HOST_MODEL_DIR', '/opt/funasr/models')
HOST_RESULT_DIR = os.getenv('HOST_RESULT_DIR', '/opt/streaming-asr/results')
HOST_AUDIO_DIR = os.getenv('HOST_AUDIO_DIR', '/opt/streaming-asr/audio')

# ─── 容器内路径 ───
CONTAINER_WORKDIR = os.getenv('CONTAINER_WORKDIR', '/opt/streaming-asr')
CONTAINER_SERVER_PATH = os.getenv('CONTAINER_SERVER_PATH', '/opt/streaming-asr/src/stream_server.py')

# ─── 服务 URL ───
SERVICE_URL = f"http://{SERVER_IP}:{SERVICE_PORT}"


# ─── 公共工具函数 ───

def ssh_exec(cmd, timeout=300):
    """SSH 远程执行命令并返回 (stdout, stderr)

    实现说明：stdout.read() 会阻塞到通道 EOF；若远程有后台进程持有
    通道 fd，read() 会永久挂起。改为轮询读取：
    - 远程命令退出（exit_status 就绪）后再吸 1 秒残余输出即返回；
    - 命令未退出则持续读取，直到 timeout 截止。
    启动后台进程请自行重定向 stdin/stdout/stderr 脱离通道。
    """
    import paramiko
    import time
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SERVER_IP, username=SERVER_USER, password=SERVER_PASS, timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = b''
    err = b''
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if stdout.channel.recv_ready():
                out += stdout.channel.recv(65536)
            if stderr.channel.recv_ready():
                err += stderr.channel.recv(65536)
        except Exception:
            break
        if stdout.channel.exit_status_ready():
            # 进程已退出，再吸 1 秒残余输出
            tail_deadline = time.time() + 1.0
            while time.time() < tail_deadline:
                try:
                    if stdout.channel.recv_ready():
                        out += stdout.channel.recv(65536)
                    if stderr.channel.recv_ready():
                        err += stderr.channel.recv(65536)
                except Exception:
                    break
                time.sleep(0.05)
            break
        time.sleep(0.1)
    client.close()
    return out.decode('utf-8', errors='replace'), err.decode('utf-8', errors='replace')


def ssh_connect():
    """创建并返回一个 SSH 连接（用于 SFTP 等需要保持连接的场景）"""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SERVER_IP, username=SERVER_USER, password=SERVER_PASS, timeout=30)
    return client


def sftp_upload(local_path, remote_path):
    """通过 SFTP 上传本地文件到服务器"""
    client = ssh_connect()
    sftp = client.open_sftp()
    sftp.put(local_path, remote_path)
    sftp.close()
    client.close()


def health_check():
    """检查流式服务健康状态"""
    out, _ = ssh_exec(f"curl -s http://localhost:{SERVICE_PORT}/health 2>&1")
    return out.strip()
