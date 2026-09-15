"""生成 3 分钟真实中文语音 PCM 素材（16kHz 16bit mono）"""
import asyncio
import edge_tts
import io
import struct
import numpy as np

# 多段中文文本，模拟真实电话对话场景（约 3 分钟语量）
TEXTS = [
    "您好，欢迎致电某某客服中心。请问有什么可以帮您的吗？",
    "你好，我想咨询一下我的订单状态。我上周三下的单，到现在还没有收到发货通知。",
    "好的，请您提供一下订单号，我帮您查询一下。",
    "我的订单号是二零二五零九一三零零零一。",
    "好的，请稍等，我正在为您查询。根据您的订单号，您的包裹已经在今天上午从仓库发出，预计明天下午可以送达。",
    "好的，那快递是哪家呢？",
    "我们默认使用的是顺丰快递，您可以在顺丰的官方网站或者小程序上输入运单号进行实时追踪。",
    "明白了，那我明天注意查收。另外我还想问一下，你们最近有没有什么优惠活动？",
    "目前我们正在进行秋季促销活动，全场商品满三百减五十，满五百减一百。如果您是会员的话，还可以享受额外的九折优惠。",
    "那太好了。对了，我之前买的那个产品，质量有点问题，想申请退换货。",
    "非常抱歉给您带来不便。请问您能提供一下购买时的订单号吗？我帮您查看一下退换货政策。",
    "好的，那个订单号是二零二五零八二八零零五六。",
    "我查到了，您的订单在十五天无理由退换范围内。您可以在我们的官方应用程序上提交退换货申请，选择上门取件服务，快递员会在四十八小时内上门取件。",
    "好的，那我待会儿就去提交申请。还有一个问题，你们的营业时间是什么时候？",
    "我们的线上客服是全天候二十四小时服务的。线下门店的营业时间是每天早上九点到晚上九点，节假日照常营业。",
    "好的，非常感谢您的帮助。你的服务非常好。",
    "感谢您的来电，祝您生活愉快，再见。",
    "再见。",
    "接下来是一段较长的叙述，用来填充更多的语音时长。在当今数字化转型的大潮中，人工智能技术正在深刻地改变着各行各业的运作方式。特别是在客户服务领域，智能语音识别技术的应用使得自动化的客户服务成为可能。",
    "通过将语音信号实时转换为文字，企业可以更高效地记录和分析客户对话内容，从而提升服务质量和客户满意度。流式语音识别技术更是将这一能力推向了新的高度，它允许在说话的同时就开始识别和处理语音内容，大大降低了响应延迟。",
    "这种技术的应用场景非常广泛，包括但不限于电话客服中心、智能语音助手、会议实时字幕、语音输入法等等。随着模型不断优化和硬件加速技术的发展，流式语音识别的准确率和效率都在持续提升。",
]

async def generate_tts():
    """生成 TTS 音频并拼接"""
    communicate = edge_tts.Communicate(
        text="。".join(TEXTS),
        voice="zh-CN-XiaoxiaoNeural",  # 女声，清晰自然
        rate="+0%",  # 正常语速
    )
    
    mp3_data = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_data.extend(chunk["data"])
    
    print(f"  TTS generated: {len(mp3_data)} bytes (MP3)")
    return bytes(mp3_data)

def mp3_to_pcm(mp3_bytes):
    """MP3 → PCM 16kHz 16bit mono（使用 pydub 或手动解码）"""
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_mp3(io.BytesIO(mp3_bytes))
        audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        pcm = audio.raw_data
        print(f"  PCM converted: {len(pcm)} bytes, duration: {len(pcm)/32000:.1f}s")
        return pcm
    except ImportError:
        print("  pydub not available, trying ffmpeg...")
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
            f.write(mp3_bytes)
            mp3_path = f.name
        pcm_path = mp3_path.replace('.mp3', '.pcm')
        subprocess.run([
            'ffmpeg', '-y', '-i', mp3_path,
            '-ar', '16000', '-ac', '1', '-sample_fmt', 's16',
            '-f', 's16le', pcm_path
        ], capture_output=True, check=True)
        with open(pcm_path, 'rb') as f:
            pcm = f.read()
        duration = len(pcm) / 32000
        print(f"  PCM converted: {len(pcm)} bytes, duration: {duration:.1f}s")
        return pcm

async def main():
    print("[1] Generating Chinese TTS audio...")
    mp3_data = await generate_tts()
    
    print("[2] Converting to PCM 16kHz 16bit mono...")
    pcm_data = mp3_to_pcm(mp3_data)
    
    duration = len(pcm_data) / 32000  # 16kHz * 2 bytes
    print(f"\n  Total duration: {duration:.1f}s ({duration/60:.1f} min)")
    
    # 如果不足 3 分钟，循环拼接
    target_duration = 180  # 3 minutes
    if duration < target_duration:
        loops = int(target_duration / duration) + 1
        pcm_data = (pcm_data * loops)[:target_duration * 32000]
        duration = len(pcm_data) / 32000
        print(f"  Loop-padded to: {duration:.1f}s ({duration/60:.1f} min)")
    
    # 保存
    output_path = r'd:\funasr\streaming-asr\tests\test_speech_3min.pcm'
    with open(output_path, 'wb') as f:
        f.write(pcm_data)
    print(f"\n  Saved to: {output_path}")
    print(f"  File size: {len(pcm_data)} bytes")
    print(f"  Duration: {duration:.1f}s")

asyncio.run(main())
