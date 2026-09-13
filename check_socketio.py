import paramiko, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
_, o, _ = c.exec_command('pip3 show python-socketio 2>/dev/null | grep Version', timeout=10)
print('python-socketio:', o.read().decode().strip())
_, o, _ = c.exec_command('pip3 show websocket-client 2>/dev/null | grep Version', timeout=10)
print('websocket-client:', o.read().decode().strip())
# Test SSL connection methods
_, o, _ = c.exec_command('python3 -c "import socketio; help(socketio.Client.connect)" 2>&1 | head -20', timeout=10)
print('\nconnect() signature:')
print(o.read().decode('utf-8', errors='replace'))
c.close()
