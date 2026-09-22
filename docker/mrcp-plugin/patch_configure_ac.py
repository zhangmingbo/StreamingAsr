#!/usr/bin/env python3
"""构建期 patch：把 mrcp-funasr 插件接入 UniMRCP 构建体系（configure.ac + plugins/Makefile.am）。

对应原 docker/unimrcp_step2b 之后的手工步骤（原 mrcp_task3_integrate.sh 内嵌逻辑），
抽为独立脚本供 Dockerfile 构建阶段调用。源码树路径可用环境变量 UNIMRCP_SRC 覆盖。
"""
import os
import sys

SRC = os.environ.get("UNIMRCP_SRC", "/opt/unimrcp-build/UniMRCP-unimrcp-1.8.0")

# ---- patch 1/2: configure.ac 注册 mrcpfunasr 插件 ----
p = os.path.join(SRC, "configure.ac")
s = open(p, encoding="utf-8").read()

old1 = 'AM_CONDITIONAL([RECORDER_PLUGIN],[test "${enable_recorder_plugin}" = "yes"])'
new1 = old1 + """
dnl FunASR recognizer plugin.
UNI_PLUGIN_ENABLED(mrcpfunasr)

AM_CONDITIONAL([FUNASR_PLUGIN],[test "${enable_mrcpfunasr_plugin}" = "yes"])
"""
assert old1 in s, "patch1 target not found (configure.ac recorder anchor)"
s = s.replace(old1, new1, 1)

old2 = "    plugins/demo-verifier/Makefile\n"
new2 = old2 + "    plugins/mrcp-funasr/Makefile\n"
assert old2 in s, "patch2 target not found (configure.ac demo-verifier anchor)"
s = s.replace(old2, new2, 1)
open(p, "w", encoding="utf-8").write(s)
print("configure.ac patched OK")

# ---- patch 3/3: plugins/Makefile.am 加入 mrcp-funasr 子目录 ----
p2 = os.path.join(SRC, "plugins", "Makefile.am")
s2 = open(p2, encoding="utf-8").read()
old3 = "if RECORDER_PLUGIN\nSUBDIRS               += mrcp-recorder\nendif"
new3 = old3 + "\n\nif FUNASR_PLUGIN\nSUBDIRS               += mrcp-funasr\nendif"
assert old3 in s2, "patch3 target not found (plugins/Makefile.am recorder anchor)"
open(p2, "w", encoding="utf-8").write(s2.replace(old3, new3, 1))
print("plugins/Makefile.am patched OK")
