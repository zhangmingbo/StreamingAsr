#!/usr/bin/env python3
"""构建期 patch：unimrcpserver.xml 注册 mrcpfunasr 引擎 + speechrecog 资源映射，
gateway 地址使用 __GW_HOST__ / __GW_PORT__ 占位符，运行时由容器入口脚本渲染。

对应原 patch_unimrcpserver_xml.py + patch_gateway_param.py 两步手工操作。
"""
p = "/usr/local/unimrcp/conf/unimrcpserver.xml"
s = open(p, encoding="utf-8").read()

# ---- 1. 引擎注册（含 gateway 占位参数）----
old1 = '<engine id="Demo-Recog-1" name="demorecog" enable="true"/>'
new1 = (
    old1 + '\n      <engine id="FunASR-1" name="mrcpfunasr" enable="true">\n'
    '        <param name="gateway-host" value="__GW_HOST__"/>\n'
    '        <param name="gateway-port" value="__GW_PORT__"/>\n'
    '      </engine>'
)
assert old1 in s, "engine anchor not found (Demo-Recog-1)"
s = s.replace(old1, new1, 1)

# ---- 2. speechrecog 资源映射 ----
old2 = '''      <!--
      <resource-engine-map>
        <resource id="speechsynth" engine="Demo-Synth-1"/>
        <resource id="speechrecog" engine="Demo-Recog-1">
          <attrib name="n1" value="v1"/>
          <attrib name="n2" value="v2"/>
        </resource>
      </resource-engine-map>
      -->'''
new2 = '''      <resource-engine-map>
        <resource id="speechrecog" engine="FunASR-1"/>
      </resource-engine-map>'''
assert old2 in s, "map anchor not found (resource-engine-map comment block)"
s = s.replace(old2, new2, 1)

open(p, "w", encoding="utf-8").write(s)
print("unimrcpserver.xml patched OK (engine + resource map + gateway placeholder)")
