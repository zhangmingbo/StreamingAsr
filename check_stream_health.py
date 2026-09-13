import paramiko, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)

# Check container status
_, o, _ = c.exec_command('docker ps --filter name=funasr-stream --format "{{.Status}}"', timeout=10)
print('Stream容器状态:', o.read().decode().strip())

# Check logs
_, o, _ = c.exec_command('docker logs funasr-stream --tail 20 2>&1', timeout=10)
print('\n日志:')
print(o.read().decode('utf-8', errors='replace'))

# Check health
_, o, _ = c.exec_command('curl -s http://localhost:5002/health 2>&1', timeout=10)
print('HTTP health:', o.read().decode().strip())

# Check SSL config in stream_server.py
_, o, _ = c.exec_command('grep "ssl_context" /opt/funasr/stream_server.py | tail -3', timeout=10)
print('\nSSL配置:')
print(o.read().decode('utf-8', errors='replace'))

c.close()
