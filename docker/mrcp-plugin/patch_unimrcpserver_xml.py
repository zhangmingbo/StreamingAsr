#!/usr/bin/env python3
# Task3: 注册 FunASR 引擎 + speechrecog 资源映射
p = '/usr/local/unimrcp/conf/unimrcpserver.xml'
s = open(p, encoding='utf-8').read()

old1 = '<engine id="Demo-Recog-1" name="demorecog" enable="true"/>'
new1 = old1 + '\n      <engine id="FunASR-1" name="mrcpfunasr" enable="true"/>'
assert old1 in s, 'engine anchor not found'
s = s.replace(old1, new1, 1)

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
assert old2 in s, 'map anchor not found'
s = s.replace(old2, new2, 1)
open(p, 'w', encoding='utf-8').write(s)
print('unimrcpserver.xml patched OK')
