import paramiko, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)

# Check container status
_, o, _ = c.exec_command('docker ps -a --filter name=funasr-stream --format "{{.Status}}"', timeout=10)
print('Status:', o.read().decode().strip())

# Check logs
_, o, _ = c.exec_command('docker logs funasr-stream --tail 30 2>&1', timeout=10)
print('\nLogs:')
print(o.read().decode('utf-8', errors='replace'))

# Check if stream_server.py has SSL disabled
_, o, _ = c.exec_command('docker exec funasr-stream grep "ssl_context" /opt/funasr/stream_server.py 2>&1', timeout=10)
print('\nSSL config:')
print(o.read().decode('utf-8', errors='replace'))

# Check health
_, o, _ = c.exec_command('curl -s http://localhost:5002/health 2>&1', timeout=10)
print('\nHTTP health:', o.read().decode().strip())

_, o, _ = c.exec_command('curl -sk https://localhost:5002/health 2>&1', timeout=10)
print('HTTPS health:', o.read().decode().strip())

c.close()
