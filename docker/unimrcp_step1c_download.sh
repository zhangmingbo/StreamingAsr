#!/bin/bash
# UniMRCP 部署 - 第一步c：正确 tag + Gitee 镜像下载（修复 tag 名 404）
set -e

UNIMRCP_VER=1.8.0
BUILD_DIR=/opt/unimrcp-build
cd ${BUILD_DIR}

echo "=== [1] 下载 unimrcp-${UNIMRCP_VER} 源码（Gitee 镜像优先） ==="
rm -f unimrcp-${UNIMRCP_VER}.tar.gz
if ! wget -q --timeout=120 -O unimrcp-${UNIMRCP_VER}.tar.gz \
    "https://gitee.com/mirrors/UniMRCP/repository/archive/unimrcp-${UNIMRCP_VER}.tar.gz"; then
    echo "--- gitee 失败，回退 github 直连 ---"
    wget -q --timeout=120 -O unimrcp-${UNIMRCP_VER}.tar.gz \
        "https://github.com/unispeech/unimrcp/archive/refs/tags/unimrcp-${UNIMRCP_VER}.tar.gz"
fi
ls -la unimrcp-${UNIMRCP_VER}.tar.gz
gzip -t unimrcp-${UNIMRCP_VER}.tar.gz && echo "unimrcp gzip OK"

echo "=== [2] 依赖：先试系统包 (apr/apr-util/sofia-sip) ==="
if dnf install -y apr-devel apr-util-devel sofia-sip-devel >/dev/null 2>&1; then
    echo "--- 系统包安装成功 ---"
    pkg-config --modversion apr-1 2>/dev/null || true
    pkg-config --modversion sofia-sip 2>/dev/null || true
else
    echo "--- 系统包缺失，从官网下载 unimrcp-deps-1.6.0 ---"
    rm -f unimrcp-deps-1.6.0.tar.gz
    wget -q --timeout=120 -O unimrcp-deps-1.6.0.tar.gz \
        "https://www.unimrcp.org/project/release-view/unimrcp-deps-1-6-0-tar-gz/download" \
        || wget -q --timeout=120 -O unimrcp-deps-1.6.0.tar.gz \
        "https://www.unimrcp.org/downloads/dependencies/unimrcp-deps-1.6.0.tar.gz"
    ls -la unimrcp-deps-1.6.0.tar.gz
    gzip -t unimrcp-deps-1.6.0.tar.gz && echo "deps gzip OK"
fi

echo "=== [3] 解压 ==="
rm -rf unimrcp-${UNIMRCP_VER}
tar xzf unimrcp-${UNIMRCP_VER}.tar.gz
[ -f unimrcp-deps-1.6.0.tar.gz ] && rm -rf unimrcp-deps-1.6.0 && tar xzf unimrcp-deps-1.6.0.tar.gz
ls -d ${BUILD_DIR}/*/ 2>/dev/null || ls ${BUILD_DIR}
echo "=== 下载完成 ==="
