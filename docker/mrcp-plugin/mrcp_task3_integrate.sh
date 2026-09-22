#!/bin/bash
# UniMRCP Task3: 集成 mrcp-funasr 插件到构建体系并编译安装
set -e
SRC=/opt/unimrcp-build/UniMRCP-unimrcp-1.8.0
LOG=/tmp/mrcp_task3.log
exec >> $LOG 2>&1
echo "===== $(date) start ====="

mkdir -p $SRC/plugins/mrcp-funasr/src
cp /tmp/mrcp_funasr_engine.c $SRC/plugins/mrcp-funasr/src/
cp /tmp/mrcp_plugin_Makefile.am $SRC/plugins/mrcp-funasr/Makefile.am
ls -la $SRC/plugins/mrcp-funasr/ $SRC/plugins/mrcp-funasr/src/

# ---- patch configure.ac (2 处) + plugins/Makefile.am (1 处) ----
python3 <<'PYEOF'
p = '/opt/unimrcp-build/UniMRCP-unimrcp-1.8.0/configure.ac'
s = open(p, encoding='utf-8').read()

old1 = 'AM_CONDITIONAL([RECORDER_PLUGIN],[test "${enable_recorder_plugin}" = "yes"])'
new1 = old1 + '''
dnl FunASR recognizer plugin.
UNI_PLUGIN_ENABLED(mrcpfunasr)

AM_CONDITIONAL([FUNASR_PLUGIN],[test "${enable_mrcpfunasr_plugin}" = "yes"])
'''
assert old1 in s, 'patch1 target not found'
s = s.replace(old1, new1, 1)

old2 = '    plugins/demo-verifier/Makefile\n'
new2 = old2 + '    plugins/mrcp-funasr/Makefile\n'
assert old2 in s, 'patch2 target not found'
s = s.replace(old2, new2, 1)
open(p, 'w', encoding='utf-8').write(s)
print('configure.ac patched OK')

p2 = '/opt/unimrcp-build/UniMRCP-unimrcp-1.8.0/plugins/Makefile.am'
s2 = open(p2, encoding='utf-8').read()
old3 = 'if RECORDER_PLUGIN\nSUBDIRS               += mrcp-recorder\nendif'
new3 = old3 + '\n\nif FUNASR_PLUGIN\nSUBDIRS               += mrcp-funasr\nendif'
assert old3 in s2, 'patch3 target not found'
open(p2, 'w', encoding='utf-8').write(s2.replace(old3, new3, 1))
print('plugins/Makefile.am patched OK')
PYEOF

cd $SRC
echo "== autoconf =="
autoconf
echo "== automake =="
automake --no-force
echo "== configure =="
./config.nice
echo "== make plugin =="
make -C plugins/mrcp-funasr -j$(nproc)
echo "== install =="
make -C plugins/mrcp-funasr install
echo "== verify =="
ls -la /usr/local/unimrcp/plugin/ | grep -i funasr || ls -la $SRC/plugins/mrcp-funasr/.libs/
echo "===== $(date) done ====="
