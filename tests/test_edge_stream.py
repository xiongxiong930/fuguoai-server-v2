"""EdgeTTS 流式边到边(text_to_stream)离线回归测试。

不触网：用 unittest.mock 替换 edge_tts.Communicate，使其产出预建的真实 MP3 块
(本地 av 编码正弦生成)，验证
1) text_to_stream 把这些 MP3 增量解成 16-bit PCM 并逐块回调 pcm_callback；
2) 回调的 PCM 经真实 OpusEncoderUtils(24k) 产出 opus 帧(pcm→opus 数据通路)；
3) 非 24k 采样率(conn.sample_rate!=24000) 抛 ValueError，由 base.py 回落整段合成；
4) av 不可用时抛 RuntimeError 回落(无 av 环境该用例优雅 skip)。

注：流式解码依赖 PyAV(av)。无 av 的本地 venv 中用例1/4 skip，真实验证在已装
av 的 159 容器侧进行(部署后跑本文件)。
"""
import asyncio
import io
import os
import sys
from unittest.mock import patch, Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuguoai_core.providers.tts.edge import TTSProvider
from fuguoai_core.utils.opus_encoder_utils import OpusEncoderUtils

try:
    import av as _AV
    import numpy as _NP
    _HAVE_AV = True
except Exception:  # pragma: no cover
    _HAVE_AV = False


def _make_provider(conn_sr=24000):
    cfg = {"voice": "zh-CN-XiaoxiaoNeural", "output_dir": "tmp/"}
    p = TTSProvider(cfg, delete_audio_file=True)
    p.opus_encoder = OpusEncoderUtils(24000, 1, 60)  # 模拟 open_audio_channels 建编码器

    class _Conn:
        sample_rate = conn_sr

    p.conn = _Conn()
    return p


def _build_mp3_chunks(sr=24000, seconds=0.5, chunk_bytes=700):
    """用 av 把正弦编码成 MP3 并切块，模拟 Edge 网络流式输出。"""
    n = int(sr * seconds)
    pcm = (_NP.sin(2 * _NP.pi * 440 * _NP.arange(n) / sr) * (2 ** 14)).astype(_NP.int16)
    buf = io.BytesIO()
    with _AV.open(buf, "w", format="mp3") as container:
        stream = container.add_stream("mp3", rate=sr)
        stream.layout = "mono"
        frame = _AV.AudioFrame.from_ndarray(pcm.reshape(1, n), format="s16", layout="mono")
        frame.sample_rate = sr
        for pkt in stream.encode(frame):
            container.mux(pkt)
        for pkt in stream.encode(None):
            container.mux(pkt)
    mp3 = buf.getvalue()
    return [mp3[i : i + chunk_bytes] for i in range(0, len(mp3), chunk_bytes)]


class _FakeCommunicate:
    def __init__(self, chunks):
        self._chunks = chunks

    async def stream(self):
        for c in self._chunks:
            yield {"type": "audio", "data": c}

    async def close(self):
        pass


def test_stream_decodes_mp3_to_pcm_and_opus():
    if not _HAVE_AV:
        print("SKIP test_stream_decodes_mp3_to_pcm_and_opus (av/numpy 不可用，容器内验证)")
        return
    p = _make_provider(24000)
    chunks = _build_mp3_chunks()
    received_pcm = []
    opus_frames = []

    def cb(pcm):
        received_pcm.append(pcm)
        p.opus_encoder.encode_pcm_to_opus_stream(
            pcm, False, lambda f: opus_frames.append(f) if f else None
        )

    with patch(
        "fuguoai_core.providers.tts.edge.edge_tts.Communicate",
        new=Mock(return_value=_FakeCommunicate(chunks)),
    ):
        asyncio.run(p.text_to_stream("你好，流式边到边测试。", cb))
    # 冲刷末帧(零填充)
    p.opus_encoder.encode_pcm_to_opus_stream(
        b"", True, lambda f: opus_frames.append(f) if f else None
    )

    assert received_pcm, "应回调出非空 PCM"
    total_pcm = sum(len(c) for c in received_pcm)
    assert total_pcm > 1000, f"PCM 总量过小: {total_pcm}"
    assert len(opus_frames) >= 3, f"应产出多个 opus 帧，实际 {len(opus_frames)}"
    print(
        f"[edge stream] pcm块数={len(received_pcm)}, 总PCM字节={total_pcm}, "
        f"opus帧={len(opus_frames)}"
    )


def test_non_24k_raises_to_fallback():
    """非 24k 设备(conn.sample_rate=16000) 应抛 ValueError 回落整段合成。"""
    p = _make_provider(16000)
    raised = False
    try:
        asyncio.run(p.text_to_stream("测试", lambda x: None))
    except ValueError:
        raised = True
    assert raised, "非 24k 应抛 ValueError 由 base.py 回落整段合成"


def test_no_av_raises_runtimeerror():
    """av 不可用时抛 RuntimeError 回落整段合成(无 av 环境无法构造，skip)。"""
    if not _HAVE_AV:
        print("SKIP test_no_av_raises_runtimeerror (本环境无 av，无法构造缺 av 场景)")
        return
    import fuguoai_core.providers.tts.edge as edgemod

    real_av = edgemod._AV
    edgemod._AV = None
    try:
        p = _make_provider(24000)
        raised = False
        try:
            asyncio.run(p.text_to_stream("测试", lambda x: None))
        except RuntimeError:
            raised = True
        assert raised, "av 不可用时抛 RuntimeError 回落整段合成"
    finally:
        edgemod._AV = real_av


if __name__ == "__main__":
    test_stream_decodes_mp3_to_pcm_and_opus()
    test_non_24k_raises_to_fallback()
    test_no_av_raises_runtimeerror()
    print("ALL EDGE STREAM TESTS PASSED")
