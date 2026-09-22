#!/bin/bash
# UniMRCP 部署 - 第一步b：镜像探测 + 下载源码（替代失效的 ghproxy）
# 服务器: Alibaba Cloud Linux 4
set -e

UNIMRCP_VER=1.8.0
DEPS_VER=1.8.0
BUILD_DIR=/opt/unimrcp-build
cd ${BUILD_DIR}

echo "=== [0] 清理卡住的 wget 和 0 字节文件 ==="
pkill -f 'wget -q' 2>/dev/null || true
rm -f unimrcp-${UNIMRCP_VER}.tar.gz unimrcp-deps-${DEPS_VER}.tar.gz

MIRRORS=(
    "https://gh-proxy.com/"
    "https://ghfast.top/"
    "https://mirror.ghproxy.com/"
    "https://github.moeyy.xyz/"
    ""
)

echo "=== [1] 探测可用镜像（curl HEAD，15s 超时） ==="
GOOD=""
for m in "${MIRRORS[@]}"; do
    url="${m}https://github.com/unispeech/unimrcp/archive/refs/tags/${UNIMRCP_VER}.tar.gz"
    echo "--- 测试: ${m:-直连} ---"
    if curl -sIL --max-time 15 "$url" -o /dev/null -w "%{http_code}\n" 2>/dev/null | tail -1 | grep -qE '^(200|302)$'; then
        echo "    可用!"
        GOOD="${m}"
        break
    else
        echo "    不可用"
    fi
done

if [ -z "${GOOD}" ]; then
    echo "FATAL: 所有镜像均不可用"
    exit 1
fi

echo "=== [2] 用镜像 ${GOOD:-直连} 下载 ==="
wget -q --timeout=120 -O unimrcp-${UNIMRCP_VER}.tar.gz \
    "${GOOD}https://github.com/unispeech/unimrcp/archive/refs/tags/${UNIMRCP_VER}.tar.gz"
ls -la unimrcp-${UNIMRCP_VER}.tar.gz

wget -q --timeout=120 -O unimrcp-deps-${DEPS_VER}.tar.gz \
    "${GOOD}https://github.com/unispeech/unimrcp-deps/archive/refs/tags/${DEPS_VER}.tar.gz"
ls -la unimrcp-deps-${DEPS_VER}.tar.gz

echo "=== [3] 校验（非 0 字节 + gzip 完整性） ==="
gzip -t unimrcp-${UNIMRCP_VER}.tar.gz && echo "unimrcp OK"
gzip -t unimrcp-deps-${DEPS_VER}.tar.gz && echo "unimrcp-deps OK"

echo "=== [4] 解压 ==="
tar xzf unimrcp-${UNIMRCP_VER}.tar.gz
tar xzf unimrcp-deps-${DEPS_VER}.tar.gz
ls -d ${BUILD_DIR}/unimrcp-${UNIMRCP_VER} ${BUILD_DIR}/unimrcp-deps-${DEPS_VER}
echo "=== 下载完成，使用镜像: ${GOOD:-直连} ==="
