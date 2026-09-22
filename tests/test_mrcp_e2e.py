"""
MRCP 全流程端到端测试（FreeSWITCH → UniMRCP → Gateway → FunASR）

用法：
    python tests/test_mrcp_e2e.py                      # 运行全部场景
    python tests/test_mrcp_e2e.py --scene speech       # 只测正常语音识别
    python tests/test_mrcp_e2e.py --scene noinput      # 只测静音 no-input
    python tests/test_mrcp_e2e.py --scene concurrency  # 只测 2 路并发

场景与判定标准：
    speech       正常语音识别 → 期望 Completion-Cause 000 且识别文本非空
    noinput      静音无输入   → 期望 Completion-Cause 002（约 15 秒触发）
    concurrency  2 路并发     → 期望两个会话均为 Completion-Cause 000

原理：
    每个场景发起 loopback 呼叫到 9196 测试分机（dialplan 中 play_and_detect_speech），
    A 腿播放测试音频，B 腿走 MRCP 识别。呼叫完成后下载 FreeSWITCH 日志，
    按 ASR 会话号递增定位新会话，解析 Completion-Cause 与 <instance> 识别文本。

前置条件（脚本自动检查）：
    - 服务器 FreeSWITCH / UniMRCP(8060) / Gateway 容器运行中
    - /tmp/speech8k_delay10.wav 语音素材存在（缺失时请用 --speech-audio 指定或联系部署方）
    - /tmp/silence40.wav 静音素材（缺失时自动生成）
"""

import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from ops.config import ssh_exec, ssh_connect

FSCLI = '/usr/local/freeswitch/bin/fs_cli'
FS_LOG = '/usr/local/freeswitch/log/freeswitch.log'
DEFAULT_SPEECH_AUDIO = '/tmp/speech8k_delay10.wav'  # 1.5s 静音 + 语音 + 尾静音（约 10 秒）
DEFAULT_SILENCE_AUDIO = '/tmp/silence40.wav'        # 40 秒纯静音
NO_INPUT_WAIT = 20   # 15s 超时 + 事件回传缓冲
SPEECH_WAIT = 16     # 10s 音频 + 识别结果缓冲

_results = []  # (场景名, PASS/FAIL, 说明)


def _ssh(cmd, timeout=120):
    out, err = ssh_exec(cmd, timeout=timeout)
    return out.strip(), err.strip()


def _max_asr_seq():
    """FreeSWITCH 日志中当前最大的 ASR 会话号（新会话号递增，据此定位新呼叫）"""
    out, _ = _ssh(
        f"grep -oE 'ASR-[0-9]+' {FS_LOG} | sed 's/ASR-//' | sort -n | tail -1"
    )
    return int(out) if out.isdigit() else 0


def _download_log():
    """下载 FreeSWITCH 日志到本地临时文件（本地解析，避免远程转义问题）"""
    client = ssh_connect()
    sftp = client.open_sftp()
    local = tempfile.NamedTemporaryFile(suffix='.log', delete=False).name
    sftp.get(FS_LOG, local)
    sftp.close()
    client.close()
    return local


def _call(audio_path):
    """发起 loopback 呼叫：A 腿播放音频，B 腿 9196 分机做 MRCP 识别

    返回呼叫发起时间（服务器时间，用于日志时间戳过滤——
    FreeSWITCH 重启后 ASR 会话号会重新计数，时间戳过滤更可靠）。
    """
    t0, _ = _ssh("date '+%Y-%m-%d %H:%M:%S'")
    out, err = _ssh(
        f'{FSCLI} -x "originate {{ignore_early_media=true}}loopback/9196 &playback({audio_path})&"'
    )
    return t0.strip(), out


def _parse_sessions(local_log, since_ts):
    """解析日志中时间 >= since_ts 的 ASR 会话结果（时间戳过滤，不受会话号重置影响）"""
    sessions = {}
    with open(local_log, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = re.search(r'ASR-(\d+)', line)
            if not m:
                continue
            tm = re.search(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+', line)
            if tm and tm.group(0) < since_ts:
                continue
            n = int(m.group(1))
            sess = sessions.setdefault(n, {'cause': None, 'texts': []})
            cm = re.search(r'Completion-Cause:\s*(\d+)', line)
            if cm:
                sess['cause'] = int(cm.group(1))
            if 'ASR-RESULT' in line:
                sess['result_line'] = line.strip()
            im = re.search(r'<instance>(.*?)</instance>', line)
            if im and im.group(1).strip():
                sess['texts'].append(im.group(1).strip())
    return sessions


def _fmt_sessions(sessions):
    """格式化会话结果（中文用 unicode 转义输出，避免 GBK 终端乱码）"""
    lines = []
    for n in sorted(sessions):
        s = sessions[n]
        cause = s['cause'] if s['cause'] is not None else '?'
        texts = ', '.join(
            t.encode('unicode_escape').decode('ascii') for t in s['texts']
        ) or '(无文本)'
        lines.append(f'  ASR-{n}: Completion-Cause {cause:03d}, 文本: {texts}')
    return '\n'.join(lines)


def _record(name, ok, detail):
    _results.append((name, 'PASS' if ok else 'FAIL', detail))
    print(f'结果: {"PASS" if ok else "FAIL"} — {detail}')
    print()


def precheck(speech_audio, silence_audio):
    """检查测试前置条件，返回警告数"""
    print('=' * 60)
    print('前置检查')
    print('=' * 60)
    warns = 0

    out, _ = _ssh("ps aux | grep 'freeswitch -' | grep -v grep | head -1")
    ok = bool(out)
    if not ok:
        warns += 1
    print(f'  [{"OK" if ok else "WARN"}] FreeSWITCH 进程')

    out, _ = _ssh("ss -tlnp | grep ':8060' | head -1")
    ok = bool(out)
    if not ok:
        warns += 1
    print(f'  [{"OK" if ok else "WARN"}] UniMRCP Server 监听 8060')

    out, _ = _ssh(
        "docker ps --format '{{.Names}} {{.Status}}' | grep argosasr-streaming-delivery"
    )
    ok = 'Up' in out
    if not ok:
        warns += 1
    print(f'  [{"OK" if ok else "WARN"}] Gateway 容器运行: {out or "未找到"}')

    out, _ = _ssh(f'ls -la {speech_audio} 2>/dev/null')
    ok = bool(out)
    if not ok:
        warns += 1
        print(f'  [WARN] 语音素材不存在: {speech_audio}（--speech-audio 指定）')
    else:
        print(f'  [OK] 语音素材: {speech_audio}')

    out, _ = _ssh(f'ls -la {silence_audio} 2>/dev/null')
    if not out:
        # 自动生成 40 秒静音
        out, err = _ssh(
            "python3 -c \"import wave; w=wave.open('/tmp/silence40.wav','wb'); "
            "w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); "
            "w.writeframes(b'\\\\x00\\\\x00'*320000); w.close(); print('ok')\""
        )
        if 'ok' in out:
            print(f'  [OK] 静音素材已自动生成: {silence_audio}')
        else:
            warns += 1
            print(f'  [WARN] 静音素材生成失败: {out} {err[:200]}')
    else:
        print(f'  [OK] 静音素材: {silence_audio}')

    print()
    return warns


def test_speech(seq, audio):
    """场景 1：正常语音识别"""
    print('=' * 60)
    print('场景 1/3: 正常语音识别（期望 Completion-Cause 000 + 文本）')
    print('=' * 60)
    t0, out = _call(audio)
    print(f'发起呼叫: {out[:80]}')
    print(f'等待 {SPEECH_WAIT} 秒...')
    time.sleep(SPEECH_WAIT)

    log = _download_log()
    sessions = _parse_sessions(log, t0)
    print('新会话:')
    print(_fmt_sessions(sessions))

    matched = [s for s in sessions.values() if s['cause'] == 0 and s['texts']]
    if matched:
        _record('speech', True,
                f'识别成功，文本: {matched[0]["texts"][0].encode("unicode_escape").decode("ascii")}')
    elif any(s['cause'] is not None for s in sessions.values()):
        _record('speech', False, '识别完成但文本为空或 Completion-Cause 非 000')
    else:
        _record('speech', False, '未发现新会话，检查 FreeSWITCH/UniMRCP 状态')


def test_noinput(seq, audio):
    """场景 2：静音 no-input"""
    print('=' * 60)
    print('场景 2/3: 静音 no-input（期望 15 秒后 Completion-Cause 002）')
    print('=' * 60)
    t0, out = _call(audio)
    print(f'发起呼叫: {out[:80]}')
    print(f'等待 {NO_INPUT_WAIT} 秒...')
    time.sleep(NO_INPUT_WAIT)

    log = _download_log()
    sessions = _parse_sessions(log, t0)
    print('新会话:')
    print(_fmt_sessions(sessions))

    if any(s['cause'] == 2 for s in sessions.values()):
        _record('noinput', True, 'no-input-timeout 精确触发（Completion-Cause 002）')
    elif sessions:
        causes = {s['cause'] for s in sessions.values()}
        _record('noinput', False, f'Completion-Cause 为 {causes}，期望 002')
    else:
        _record('noinput', False, '未发现新会话，检查 FreeSWITCH/UniMRCP 状态')


def test_concurrency(seq, audio):
    """场景 3：2 路并发"""
    print('=' * 60)
    print('场景 3/3: 2 路并发识别（期望两个会话均 000）')
    print('=' * 60)
    t0, _ = _call(audio)
    _call(audio)
    print('已发起 2 路呼叫')
    print(f'等待 {SPEECH_WAIT + 4} 秒...')
    time.sleep(SPEECH_WAIT + 4)

    log = _download_log()
    sessions = _parse_sessions(log, t0)
    print('新会话:')
    print(_fmt_sessions(sessions))

    if len(sessions) < 2:
        _record('concurrency', False, f'仅发现 {len(sessions)} 个会话，期望 2 个')
    else:
        ok_causes = sum(1 for s in sessions.values() if s['cause'] == 0)
        if ok_causes >= 2:
            _record('concurrency', True, f'{len(sessions)} 路并发全部识别成功')
        else:
            _record('concurrency', False, f'仅 {ok_causes}/{len(sessions)} 路返回 000')


def main():
    import argparse
    parser = argparse.ArgumentParser(description='MRCP 端到端测试')
    parser.add_argument('--scene', choices=['all', 'speech', 'noinput', 'concurrency'],
                        default='all', help='只运行指定场景')
    parser.add_argument('--speech-audio', default=DEFAULT_SPEECH_AUDIO,
                        help=f'语音素材路径（服务器上，默认 {DEFAULT_SPEECH_AUDIO}）')
    args = parser.parse_args()

    speech_audio = args.speech_audio
    silence_audio = DEFAULT_SILENCE_AUDIO

    warns = precheck(speech_audio, silence_audio)
    print()

    scenes = {
        'speech': lambda: test_speech(None, speech_audio),
        'noinput': lambda: test_noinput(None, silence_audio),
        'concurrency': lambda: test_concurrency(None, speech_audio),
    }
    if args.scene == 'all':
        order = ['speech', 'noinput', 'concurrency']
    else:
        order = [args.scene]

    for name in order:
        try:
            scenes[name]()
        except Exception as e:
            _record(name, False, f'异常: {e}')

    print('=' * 60)
    print('测试汇总')
    print('=' * 60)
    passed = sum(1 for _, r, _ in _results if r == 'PASS')
    for name, r, detail in _results:
        print(f'  [{r}] {name}: {detail}')
    print(f'通过 {passed}/{len(_results)}')
    if warns:
        print(f'注意：前置检查有 {warns} 项告警，可能影响测试结果')
    sys.exit(0 if passed == len(_results) and passed > 0 else 1)


if __name__ == '__main__':
    main()
