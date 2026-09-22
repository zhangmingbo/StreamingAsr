#!/usr/bin/env python3
# Task4: 为 FunASR-1 引擎添加 gateway 参数
p = '/usr/local/unimrcp/conf/unimrcpserver.xml'
s = open(p, encoding='utf-8').read()

old = '<engine id="FunASR-1" name="mrcpfunasr" enable="true"/>'
new = ('<engine id="FunASR-1" name="mrcpfunasr" enable="true">\n'
       '        <param name="gateway-host" value="172.17.0.3"/>\n'
       '        <param name="gateway-port" value="5003"/>\n'
       '      </engine>')
assert old in s, 'anchor not found'
s = s.replace(old, new, 1)
open(p, 'w', encoding='utf-8').write(s)
print('xml updated')
