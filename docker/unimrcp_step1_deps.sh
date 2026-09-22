#!/bin/bash
# UniMRCP Server 部署脚本 - 第一步：系统依赖 + 源码下载
# 服务器: Alibaba Cloud Linux 4 (dnf)
set -e

UNIMRCP_VER=1.8.0
DEPS_VER=1.8.0
BUILD_DIR=/opt/unimrcp-build
PREFIX=/usr/local/unimrcp

echo "=== [1/4] 安装编译依赖 ==="
dnf install -y gcc gcc-c++ make autoconf automake libtool pkgconf wget tar gzip bzip2 openssl-devel \
    || yum install -y gcc gcc-c++ make autoconf automake libtool pkgconfig wget tar gzip bzip2 openssl-devel

echo "=== [2/4] 创建构建目录 ==="
mkdir -p ${BUILD_DIR}
cd ${BUILD_DIR}

echo "=== [3/4] 下载源码 ==="
if [ ! -f unimrcp-${UNIMRCP_VER}.tar.gz ]; then
    echo "--- 下载 unimrcp-${UNIMRCP_VER} ---"
    wget -q --timeout=300 -O unimrcp-${UNIMRCP_VER}.tar.gz \
        https://github.com/unispeech/unimrcp/archive/refs/tags/${UNIMRCP_VER}.tar.gz \
        || wget -q --timeout=300 -O unimrcp-${UNIMRCP_VER}.tar.gz \
        https://ghproxy.com/https://github.com/unispeech/unimrcp/archive/refs/tags/${UNIMRCP_VER}.tar.gz
fi
if [ ! -f unimrcp-deps-${DEPS_VER}.tar.gz ]; then
    echo "--- 下载 unimrcp-deps-${DEPS_VER} ---"
    wget -q --timeout=300 -O unimrcp-deps-${DEPS_VER}.tar.gz \
        https://github.com/unispeech/unimrcp-deps/archive/refs/tags/${DEPS_VER}.tar.gz \
        || wget -q --timeout=300 -O unimrcp-deps-${DEPS_VER}.tar.gz \
        https://ghproxy.com/https://github.com/unispeech/unimrcp-deps/archive/refs/tags/${DEPS_VER}.tar.gz
fi

echo "=== [4/4] 解压 ==="
tar xzf unimrcp-${UNIMRCP_VER}.tar.gz
tar xzf unimrcp-deps-${DEPS_VER}.tar.gz
ls -la ${BUILD_DIR}

echo "=== 下载完成 ==="
echo "源码目录: ${BUILD_DIR}/unimrcp-${UNIMRCP_VER}"
echo "依赖目录: ${BUILD_DIR}/unimrcp-deps-${DEPS_VER}"
