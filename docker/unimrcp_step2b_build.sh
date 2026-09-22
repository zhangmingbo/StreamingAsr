#!/bin/bash
# UniMRCP 部署 - 第二步b：重编 unimrcp（deps 已完成，spec 已修复）
set -e
BUILD_DIR=/opt/unimrcp-build
cd ${BUILD_DIR}/UniMRCP-unimrcp-1.8.0
export PATH=/usr/local/apr/bin:${PATH}

echo "=== [1/3] clean + configure ==="
make clean >/dev/null 2>&1 || true
./configure --with-apr=/usr/local/apr --with-apr-util=/usr/local/apr-util --with-sofia-sip=/usr/local >/dev/null
echo "configure OK"

echo "=== [2/3] make ==="
make -j$(nproc)

echo "=== [3/3] install ==="
make install >/dev/null
ldconfig
echo "=== 全部完成 ==="
ls -la /usr/local/unimrcp/bin/
ls /usr/local/unimrcp/plugin/
