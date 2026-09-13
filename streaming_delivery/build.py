"""
argosasr-streaming 流式 ASR 黑盒交付版 — 构建脚本
自动复制源码、构建 Docker 镜像、导出为 tar 包
原始源代码完全不受影响
"""
import os, sys, shutil, subprocess, time

# 路径配置
ROOT = os.path.dirname(os.path.abspath(__file__))  # streaming-asr/streaming_delivery/
SRC_ROOT = os.path.dirname(ROOT)  # streaming-asr/
PROJECT_ROOT = os.path.dirname(SRC_ROOT)  # d:\funasr
BUILD_DIR = os.path.join(ROOT, "build_context")
IMAGE_NAME = "argosasr-streaming"
IMAGE_TAG = "v1.0"
OUTPUT_TAR = os.path.join(ROOT, f"{IMAGE_NAME}-{IMAGE_TAG}.tar")

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def run(cmd, cwd=None):
    """执行命令并打印输出"""
    log(f"执行: {cmd}")
    result = subprocess.run(
        cmd, shell=True, cwd=cwd or ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding='utf-8', errors='replace'
    )
    if result.stdout:
        for line in result.stdout.strip().split('\n'):
            if line.strip():
                print(f"  {line}")
    if result.returncode != 0:
        log(f"❌ 命令失败 (exit code={result.returncode})")
        sys.exit(1)
    return result

def main():
    log("=" * 60)
    log("argosasr-streaming 流式 ASR 黑盒交付版 - 构建开始")
    log("=" * 60)
    
    # 1. 清理旧的构建目录
    if os.path.exists(BUILD_DIR):
        log("清理旧构建目录...")
        shutil.rmtree(BUILD_DIR)
    os.makedirs(BUILD_DIR)
    
    # 2. 复制必要文件到构建上下文（不动原始代码）
    log("复制源码到构建上下文...")
    
    # 复制 stream_server.py（位于 docker/ 子目录）
    src_server = os.path.join(SRC_ROOT, "docker", "stream_server.py")
    dst_server = os.path.join(BUILD_DIR, "stream_server.py")
    if not os.path.exists(src_server):
        log(f"❌ 找不到源文件: {src_server}")
        sys.exit(1)
    shutil.copy2(src_server, dst_server)
    log(f"  ✓ stream_server.py ({os.path.getsize(src_server)} bytes)")
    
    # 复制授权客户端（位于 license-service/docker/ 子目录）
    src_license_client = os.path.join(PROJECT_ROOT, "license-service", "docker", "asr_license_client.py")
    dst_license_client = os.path.join(BUILD_DIR, "asr_license_client.py")
    if os.path.exists(src_license_client):
        shutil.copy2(src_license_client, dst_license_client)
        log(f"  ✓ asr_license_client.py ({os.path.getsize(src_license_client)} bytes)")
    else:
        log(f"⚠ 警告: 找不到授权客户端: {src_license_client}")
    
    # 复制 Dockerfile（位于 docker/ 子目录）
    src_dockerfile = os.path.join(SRC_ROOT, "docker", "Dockerfile.stream")
    dst_dockerfile = os.path.join(BUILD_DIR, "Dockerfile")
    shutil.copy2(src_dockerfile, dst_dockerfile)
    log(f"  ✓ Dockerfile")
    
    # 检查并复制模型文件
    # 优先从本地 models/ 目录复制，其次检查云服务器路径
    models_dst = os.path.join(BUILD_DIR, "models")
    local_models = os.path.join(ROOT, "models")  # streaming_delivery/models/
    cloud_models = "/opt/funasr/models"  # 云服务器上的模型路径
    
    models_src = None
    if os.path.exists(local_models) and os.listdir(local_models):
        models_src = local_models
        log(f"使用本地模型目录: {local_models}")
    elif os.path.exists(cloud_models):
        models_src = cloud_models
        log(f"使用云服务器模型目录: {cloud_models}")
    
    if models_src:
        # 直接复制模型目录（不嵌套 iic/）
        for item in os.listdir(models_src):
            src_path = os.path.join(models_src, item)
            dst_path = os.path.join(models_dst, item)
            if os.path.isdir(src_path):
                shutil.copytree(src_path, dst_path)
                log(f"  ✓ {item}")
            elif item.endswith(('.onnx', '.yaml', '.json', '.tok')):
                os.makedirs(models_dst, exist_ok=True)
                shutil.copy2(src_path, dst_path)
                log(f"  ✓ {item}")
        model_count = len([d for d in os.listdir(models_dst) if os.path.isdir(os.path.join(models_dst, d))])
        log(f"  共 {model_count} 个模型目录")
    else:
        log("⚠ 警告: 未找到模型文件，构建镜像时不包含预置模型")
        log("  请将模型文件放到 streaming_delivery/models/ 目录")
        os.makedirs(models_dst, exist_ok=True)
    
    log(f"\n构建上下文准备完成: {BUILD_DIR}")
    
    # 3. 构建 Docker 镜像
    log("\n" + "=" * 60)
    log("构建 Docker 镜像...")
    log("=" * 60)
    image_full = f"{IMAGE_NAME}:{IMAGE_TAG}"
    run(f"docker build -t {image_full} .", cwd=BUILD_DIR)
    
    # 4. 验证镜像
    log("\n验证镜像内容...")
    result = run(f"docker run --rm {image_full} ls -la /opt/argosasr-streaming/stream_server.pyc 2>&1")
    
    # 检查是否有 .py 残留
    result2 = run(f"docker run --rm {image_full} find /opt/argosasr-streaming -name '*.py' 2>/dev/null || echo 'No .py files found'")
    if "No .py files found" in result2.stdout or result2.stdout.strip() == "":
        log("✓ 黑盒验证通过：镜像内无 .py 源文件")
    else:
        log("⚠ 警告：镜像内发现 .py 文件残留")
        print(result2.stdout)
    
    # 检查模型是否存在
    result3 = run(f"docker run --rm {image_full} ls -la /opt/funasr/models/ 2>/dev/null || echo 'Models not found'")
    if "Models not found" not in result3.stdout:
        log("✓ 模型文件已集成到镜像中")
    else:
        log("⚠ 模型文件未找到（可能未在构建上下文中）")
    
    # 5. 导出为 tar 包
    log("\n" + "=" * 60)
    log("导出 Docker 镜像为 tar 包...")
    log("=" * 60)
    if os.path.exists(OUTPUT_TAR):
        os.remove(OUTPUT_TAR)
    run(f"docker save -o {OUTPUT_TAR} {image_full}")
    tar_size = os.path.getsize(OUTPUT_TAR) / (1024**3)
    log(f"✓ 导出完成: {OUTPUT_TAR} ({tar_size:.2f} GB)")
    
    # 6. 清理构建上下文
    log("\n清理临时构建目录...")
    shutil.rmtree(BUILD_DIR)
    log(f"✓ 已删除: {BUILD_DIR}")
    
    # 7. 总结
    log("\n" + "=" * 60)
    log("✅ 构建完成！")
    log("=" * 60)
    log(f"镜像名称: {image_full}")
    log(f"交付文件: {OUTPUT_TAR}")
    log(f"文件大小: {tar_size:.2f} GB")
    log("")
    log("部署命令:")
    log(f"  docker load -i {os.path.basename(OUTPUT_TAR)}")
    log(f"  docker run -d --name argosasr-streaming -p 5002:5002 -e LICENSE_SERVER=http://host.docker.internal:9800 {image_full}")
    log("")
    log("注意: 原始源代码未被修改或删除")

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log("\n\n❌ 用户中断")
        sys.exit(1)
    except Exception as e:
        log(f"\n\n❌ 构建失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

