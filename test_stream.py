"""
FunASR 流式语音识别测试脚本
用法: python test_stream.py <音频文件路径>

流式服务使用 WebSocket 协议，支持实时音频流识别
"""
import socketio
import time
import wave
import sys
import os

SERVER = "http://8.153.92.96:5002"

def test_stream(audio_path):
    if not os.path.exists(audio_path):
        print(f"文件不存在: {audio_path}")
        return

    # 检查文件格式
    if not audio_path.lower().endswith('.wav'):
        print("注意: 流式服务仅支持 16kHz 单声道 WAV 格式")
        print("建议先用离线服务测试其他格式")
    
    print(f"文件: {audio_path}")
    print(f"服务: {SERVER}")
    print("-" * 50)

    # 连接流式服务
    sio = socketio.Client()
    
    results = []
    partial_count = 0
    
    @sio.on('connect')
    def on_connect():
        print("✅ 已连接流式服务")
    
    @sio.on('disconnect')
    def on_disconnect():
        print("❌ 已断开连接")
    
    @sio.on('partial_result')
    def on_partial(data):
        nonlocal partial_count
        partial_count += 1
        text = data.get('text', '')
        if text:
            print(f"\r  [实时] {text}", end='', flush=True)
    
    @sio.on('final_result')
    def on_final(data):
        text = data.get('text', '')
        start = data.get('start', '')
        end = data.get('end', '')
        if text:
            results.append({'text': text, 'start': start, 'end': end})
            print(f"\n  [最终] [{start} --> {end}] {text}")
    
    @sio.on('error')
    def on_error(data):
        print(f"\n  [错误] {data}")
    
    try:
        print("正在连接...")
        sio.connect(SERVER, transports=['websocket'])
        time.sleep(1)
        
        # 读取音频文件
        print("正在发送音频数据...")
        with wave.open(audio_path, 'rb') as wf:
            # 检查音频参数
            if wf.getframerate() != 16000:
                print(f"警告: 音频采样率 {wf.getframerate()}Hz，建议 16000Hz")
            if wf.getnchannels() != 1:
                print(f"警告: 音频声道数 {wf.getnchannels()}，建议单声道")
            
            # 按 600ms 分块发送 (9600 采样点 @16kHz)
            chunk_size = 9600
            total_frames = wf.getnframes()
            sent_frames = 0
            
            while True:
                frames = wf.readframes(chunk_size)
                if not frames:
                    break
                
                # 发送音频数据 (base64 编码)
                import base64
                audio_b64 = base64.b64encode(frames).decode('utf-8')
                sio.emit('audio_chunk', {'audio': audio_b64})
                
                sent_frames += len(frames) // 2  # 16-bit = 2 bytes per sample
                progress = min(100, int(sent_frames / total_frames * 100))
                print(f"\r  发送进度: {progress}%", end='', flush=True)
                
                # 模拟实时流 (600ms 数据间隔 300ms 发送)
                time.sleep(0.3)
        
        print("\n发送完成，等待最终结果...")
        sio.sleep(3)
        
        print("-" * 50)
        print(f"共收到 {len(results)} 条最终结果")
        if results:
            full_text = ''.join([r['text'] for r in results])
            print(f"\n完整文本:\n  {full_text}")
        
    except Exception as e:
        print(f"\n错误: {e}")
    finally:
        sio.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python test_stream.py <音频文件路径>")
        print("示例: python test_stream.py recording.wav")
        print(f"\n先检查服务状态:")
        try:
            import requests
            r = requests.get(f"{SERVER}/health", timeout=5)
            print(f"  服务状态: {r.json()}")
        except Exception as e:
            print(f"  服务不可达: {e}")
        sys.exit(1)
    
    test_stream(sys.argv[1])
