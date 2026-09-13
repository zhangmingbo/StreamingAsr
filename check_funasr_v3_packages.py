"""检查 funasr-onnx-v3 容器的 Python 包"""
import paramiko

def ssh_exec(cmd, timeout=30):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return out, err

script = '''
import pkg_resources
print("All installed packages:")
for p in sorted(pkg_resources.working_set, key=lambda x: x.project_name.lower()):
    print(f"  {p.project_name}=={p.version}")
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
with sftp.open('/tmp/check_packages.py', 'w') as f:
    f.write(script)
sftp.close()
client.close()

ssh_exec("docker cp /tmp/check_packages.py funasr:/tmp/check_packages.py")
out, err = ssh_exec("docker exec funasr python3 /tmp/check_packages.py 2>&1 | grep -i funasr")
print("FunASR related packages:")
print(out)
if err.strip():
    print(f"STDERR: {err[:300]}")
