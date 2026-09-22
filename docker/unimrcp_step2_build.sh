#!/bin/bash
# UniMRCP 部署 - 第二步：编译 deps（apr/apr-util/sofia-sip）+ unimrcp 1.8.0 + demoverifier
set -e

BUILD_DIR=/opt/unimrcp-build
JOBS=$(nproc)

echo "=== [1/6] 额外依赖 (expat-devel) ==="
dnf install -y expat-devel >/dev/null 2>&1 || yum install -y expat-devel >/dev/null 2>&1 || true

echo "=== [2/6] 编译 apr ==="
cd ${BUILD_DIR}/unimrcp-deps-1.6.0/libs/apr
./configure --prefix=/usr/local/apr >/dev/null
make -j${JOBS} >/dev/null
make install >/dev/null
echo "apr OK: $(/usr/local/apr/bin/apr-1-config --version)"

echo "=== [3/6] 编译 apr-util ==="
cd ${BUILD_DIR}/unimrcp-deps-1.6.0/libs/apr-util
./configure --prefix=/usr/local/apr-util --with-apr=/usr/local/apr >/dev/null
make -j${JOBS} >/dev/null
make install >/dev/null
echo "apr-util OK"

echo "=== [4/6] 编译 sofia-sip ==="
cd ${BUILD_DIR}/unimrcp-deps-1.6.0/libs/sofia-sip
./configure --prefix=/usr/local >/dev/null
make -j${JOBS} >/dev/null
make install >/dev/null
ldconfig
echo "sofia-sip OK: $(pkg-config --modversion sofia-sip 2>/dev/null || /usr/local/bin/sofia-sip-config --version 2>/dev/null || echo done)"

echo "=== [5/6] 编译 unimrcp 1.8.0 ==="
cd ${BUILD_DIR}/UniMRCP-unimrcp-1.8.0
export PATH=/usr/local/apr/bin:${PATH}
./bootstrap >/dev/null
./configure --with-apr=/usr/local/apr --with-apr-util=/usr/local/apr-util --with-sofia-sip=/usr/local >/dev/null
make -j${JOBS} >/dev/null
echo "unimrcp make OK"

echo "=== [6/6] 安装 ==="
make install >/dev/null
ldconfig
echo "=== 全部完成 ==="
ls -la /usr/local/unimrcp/bin/
