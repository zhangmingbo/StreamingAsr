"""测试 PyTorch Paraformer-large 是否能识别真实语音"""
import paramiko

def ssh_exec(cmd, timeout=60):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    client.close()
    return out, err

# 测试脚本 - 用真实音频文件测试
test_script = '''
import numpy as np
from funasr import AutoModel
import soundfile as sf

print("Loading model...")
model = AutoModel(model="paraformer-zh-streaming", disable_update=True)
print("Model loaded")

# 读取一个真实音频文件
try:
    audio, sr = sf.read("/opt/funasr/audio/test.wav")
    if sr != 16000:
        print(f"Resampling from {sr} to 16000")
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
    
    print(f"Audio length: {len(audio)/16000:.2f}s")
    
    # 流式推理
    cache = {}
    chunk_size = [0, 10, 5]
    all_text = ""
    
    for i in range(0, len(audio), 9600):  # 600ms chunks
        chunk = audio[i:i+9600]
        is_final = (i + 9600 >= len(audio))
        
        result = model.generate(
            input=chunk,
            cache=cache,
            is_final=is_final,
            chunk_size=chunk_size,
            encoder_chunk_look_back=4,
            decoder_chunk_look_back=1,
        )
        
        if result and len(result) > 0:
            text = result[0].get("text", "")
            if text:
                print(f"Chunk {i//9600}: '{text}'")
                all_text += text
    
    print(f"\\nFinal result: '{all_text}'")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
'''

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('8.153.92.96', username='root', password='myegoo@3466', timeout=30)
sftp = client.open_sftp()
with sftp.open('/tmp/test_real_audio.py', 'w') as f:
    f.write(test_script)
sftp.close()
client.close()

ssh_exec("docker cp /tmp/test_real_audio.py funasr:/tmp/test_real_audio.py")
print("Running test with real audio...")
out, err = ssh_exec("docker exec funasr timeout 60 python3 /tmp/test_real_audio.py 2>&1", timeout=70)
print(out)
if err.strip():
    print(f"STDERR: {err[:500]}")
