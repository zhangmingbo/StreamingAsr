#!/bin/bash
# UniMRCP Server 容器入口：
#   1. 用环境变量渲染 gateway 连接参数（占位符 → 实际值）
#   2. 启动 unimrcpserver（stdin 用阻塞管道，防控制台线程 100% CPU 忙循环）
set -e

CONF=/usr/local/unimrcp/conf/unimrcpserver.xml

GW_HOST=${GW_HOST:-127.0.0.1}
GW_PORT=${GW_PORT:-5003}

if [ ! -f "$CONF" ]; then
    echo "ERROR: unimrcpserver.xml not found at $CONF" >&2
    exit 1
fi

# 渲染网关地址（插件内部 WS 连接目标）
sed -i "s|__GW_HOST__|${GW_HOST}|g; s|__GW_PORT__|${GW_PORT}|g" "$CONF"

# 可选：显式指定 MRCP 服务端 IP（<sip-uas> server-ip），未设置保持默认配置
if [ -n "$SERVER_IP" ]; then
    sed -i "s|server-ip=\"[^\"]*\"|server-ip=\"${SERVER_IP}\"|g" "$CONF"
fi

echo "=================================================="
echo " UniMRCP Server 启动"
echo "   GW_HOST   = ${GW_HOST}"
echo "   GW_PORT   = ${GW_PORT}"
[ -n "$SERVER_IP" ] && echo "   SERVER_IP = ${SERVER_IP}"
echo "   配置      = ${CONF}"
echo "   日志      = stdout + /usr/local/unimrcp/log"
echo "=================================================="

# 关键坑：unimrcpserver 的控制台线程逐字符读 stdin，
# stdin 为 /dev/null 时 read 立即返回被当作输入，导致 100% CPU 忙循环。
# 用进程替换提供永不被 EOF 的阻塞管道作为 stdin，同时 exec 保持 PID1 信号转发。
exec /usr/local/unimrcp/bin/unimrcpserver -r /usr/local/unimrcp < <(tail -f /dev/null)
