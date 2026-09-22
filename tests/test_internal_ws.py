# -*- coding: utf-8 -*-
"""内部 WS 通道协议验证（MRCP 插件接入点）

验证链路：连接 5003 → start → 发 8kHz PCM → 收事件流 → end → finished
在 Gateway 容器内执行（同容器可访问 Runtime 10095）。
用法: python test_internal_ws.py [ws://localhost:5003]
"""
import asyncio
import json
import math
import struct
import sys
import time

import websockets

WS_URL = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:5003"


def make_pcm(duration_ms=20, sr=8000, freq=440.0, amp=6000.0, silence_frac=0.0):
    """生成一段 8kHz 16bit mono PCM（正弦波 + 可选前导静音）"""
    n = int(sr * duration_ms / 1000)
    samples = []
    silence_n = int(n * silence_frac)
    tone_n = n - silence_n
    for i in range(silence_n):
        samples.append(0)
    for i in range(tone_n):
        samples.append(int(amp * math.sin(2 * math.pi * freq * i / sr)))
    return struct.pack("<%dh" % n, *samples)


async def recv_events(ws, duration, label):
    """一段时间内非阻塞收集事件"""
    end_t = time.time() + duration
    while time.time() < end_t:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=0.1)
            print(f"  [{label}] recv: {msg[:300]}")
        except asyncio.TimeoutError:
            continue
        except websockets.exceptions.ConnectionClosed:
            print(f"  [{label}] connection closed")
            break


async def main():
    print(f"=== 内部 WS 通道验证: {WS_URL} ===")
    async with websockets.connect(WS_URL, max_size=8 * 1024 * 1024) as ws:
        print("[1] connected")

        # start（8kHz 电话流）
        await ws.send(json.dumps({"type": "start", "sample_rate": 8000}))
        print("[2] sent start(sample_rate=8000)")

        # 等待 started 事件
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            print(f"  [started] recv: {msg[:200]}")
        except asyncio.TimeoutError:
            print("  [started] TIMEOUT - no started event")

        # 流式发音频：0.5s 静音 + 2s 语音 + 0.5s 静音（8kHz 20ms/帧）
        silence = make_pcm(duration_ms=20, silence_frac=1.0)
        speech = make_pcm(duration_ms=20, freq=440.0, amp=6000.0)
        print("[3] sending audio frames (500ms silence + 2000ms tone + 500ms silence)")
        for _ in range(25):
            await ws.send(silence)
            await asyncio.sleep(0.02)
        for _ in range(100):
            await ws.send(speech)
            await asyncio.sleep(0.02)
        for _ in range(25):
            await ws.send(silence)
            await asyncio.sleep(0.02)

        # 收事件（speech_start 应已触发）
        print("[4] collecting events after speech")
        await recv_events(ws, 2.0, "post-speech")

        # end
        await ws.send(json.dumps({"type": "end"}))
        print("[5] sent end")

        # 等 finished + 最终结果
        await recv_events(ws, 6.0, "post-end")

    print("=== 验证完成 ===")


if __name__ == "__main__":
    asyncio.run(main())
