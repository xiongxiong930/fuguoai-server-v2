"""离线语音留言存储（docs/VOICEMAIL_DESIGN.md v0.1 落地）。

存储布局
--------
data/voicemail/<target_device_id>/<msg_id>.fgvm   音频（自描述容器）
data/voicemail/<target_device_id>/index.jsonl     元数据（追加写，读时按 msg_id 合并最后状态）

.fgvm 容器格式（播放端零猜测；不存 seq，播放时现生成）
---------------------------------------------------
[8B  magic "FGVM0001"]
[4B  LE sample_rate][2B LE channels][2B LE frame_ms]
[4B  LE frame_count]
repeat frame_count × { [2B BE frame_len][opus payload] }

配额：每设备 MAX_MSGS 条 / 保留 TTL_DAYS 天 / 单条 MAX_DURATION_MS。
转码：上传音频 → 16k/mono PCM。pcm 直通；mp3/aac/m4a 走纯 PyAV
（经验铁律⑦：禁 ffmpeg 管道）。PCM 按 640B/20ms 切帧 → opuslib_next 编码。
"""

import json
import logging
import os
import struct
import threading
import time
import uuid

TAG = __name__
_logger = logging.getLogger(TAG)

MAGIC = b"FGVM0001"
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 20
FRAME_BYTES = 640          # 16k * 1ch * 2B * 0.02s

MAX_MSGS = 20              # 每设备配额
TTL_DAYS = 7               # 保留天数
MAX_DURATION_MS = 60_000   # 单条上限
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

_BASE_DIR = os.environ.get(
    "FUGUOAI_VOICEMAIL_DIR", "/opt/fuguoai-server/data/voicemail"
)


# ────────────────────────── .fgvm 封装 / 解析 ──────────────────────────

def write_fgvm(path: str, frames: list):
    """把 opus 帧列表写成 .fgvm 文件（原子写：先 .tmp 再 rename）。"""
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<IHH", SAMPLE_RATE, CHANNELS, FRAME_MS))
        f.write(struct.pack("<I", len(frames)))
        for fr in frames:
            f.write(struct.pack(">H", len(fr)))
            f.write(fr)
    os.replace(tmp, path)


def read_fgvm(path: str):
    """解析 .fgvm → (meta dict, [opus帧])。格式坏抛 ValueError。"""
    with open(path, "rb") as f:
        head = f.read(8)
        if head != MAGIC:
            raise ValueError(f"bad magic: {head!r}")
        sr, ch, fms = struct.unpack("<IHH", f.read(8))
        (count,) = struct.unpack("<I", f.read(4))
        frames = []
        for _ in range(count):
            raw = f.read(2)
            if len(raw) < 2:
                raise ValueError("truncated frame header")
            (flen,) = struct.unpack(">H", raw)
            fr = f.read(flen)
            if len(fr) != flen:
                raise ValueError("truncated frame payload")
            frames.append(fr)
    meta = {"sample_rate": sr, "channels": ch, "frame_ms": fms,
            "frame_count": count, "duration_ms": count * fms}
    return meta, frames


# ────────────────────────── 转码（上传 → 20ms opus 帧） ──────────────────────────

def pcm_to_frames(pcm: bytes) -> list:
    """16k/mono/s16le PCM → opus 帧列表（尾料不足 640B 补零成整帧）。"""
    import opuslib_next
    # aiohttp multipart 可能传入 bytearray/memoryview；opuslib_next encode 只接受 bytes
    pcm = bytes(pcm)
    enc = opuslib_next.Encoder(SAMPLE_RATE, CHANNELS, opuslib_next.APPLICATION_VOIP)
    try:
        enc.bitrate = 24000
    except Exception:
        pass
    if len(pcm) % 2:
        pcm = pcm[:-1]
    frames = []
    for off in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[off:off + FRAME_BYTES]
        if len(chunk) < FRAME_BYTES:
            chunk = chunk + b"\x00" * (FRAME_BYTES - len(chunk))
        frames.append(enc.encode(chunk, FRAME_BYTES // 2))
    return frames


def decode_to_pcm(data: bytes, source_fmt: str) -> bytes:
    """任意上传格式 → 16k/mono/s16le PCM。

    - "pcm"：直通（认定已是 16k/mono/s16le）。
    - 其余（mp3/aac/m4a/wav...）：纯 PyAV 解码 + resample（铁律⑦，禁 ffmpeg 管道）。
    """
    fmt = (source_fmt or "pcm").lower().strip()
    if fmt == "pcm":
        return data
    import io
    import av  # 容器内已有（AacTranscoder 同款依赖）
    out = bytearray()
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(stream):
            for rf in resampler.resample(frame):
                out += bytes(rf.planes[0])[: rf.samples * 2]
        # 排空 resampler 内部缓冲
        for rf in resampler.resample(None):
            out += bytes(rf.planes[0])[: rf.samples * 2]
    return bytes(out)


def transcode_upload(data: bytes, source_fmt: str) -> list:
    """上传字节流 → 20ms opus 帧列表。超长抛 ValueError("too_long")。"""
    pcm = decode_to_pcm(data, source_fmt)
    duration_ms = len(pcm) // 2 * 1000 // SAMPLE_RATE
    if duration_ms > MAX_DURATION_MS:
        raise ValueError("too_long")
    if duration_ms < 200:
        raise ValueError("too_short")
    return pcm_to_frames(pcm)


# ────────────────────────── 留言库 ──────────────────────────

class VoicemailStore:
    def __init__(self, base_dir: str = None):
        self.base = base_dir or _BASE_DIR
        self._lock = threading.Lock()

    # ── 内部工具 ──

    def _dev_dir(self, device_id: str) -> str:
        safe = device_id.replace("/", "_").replace("..", "_")
        return os.path.join(self.base, safe)

    def _index_path(self, device_id: str) -> str:
        return os.path.join(self._dev_dir(device_id), "index.jsonl")

    def _append_index(self, device_id: str, row: dict):
        os.makedirs(self._dev_dir(device_id), exist_ok=True)
        with open(self._index_path(device_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _load_index(self, device_id: str) -> dict:
        """读 index.jsonl，按 msg_id 合并（后行覆盖状态字段）→ {msg_id: meta}。"""
        path = self._index_path(device_id)
        merged = {}
        if not os.path.exists(path):
            return merged
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    mid = row.get("msg_id")
                    if not mid:
                        continue
                    if mid in merged:
                        merged[mid].update(row)
                    else:
                        merged[mid] = row
        except Exception as e:
            _logger.warning(f"[voicemail] index 读取失败 {device_id}: {e}")
        # deleted 的从视图剔除
        return {k: v for k, v in merged.items() if v.get("status") != "deleted"}

    def audio_path(self, device_id: str, msg_id: str) -> str:
        return os.path.join(self._dev_dir(device_id), msg_id + ".fgvm")

    # ── CRUD ──

    def add(self, target_device_id: str, from_id: str, uid: str,
            frames: list, duration_ms: int, source_fmt: str = "pcm") -> dict:
        """新增留言。配额满返回 {"error":"quota_exceeded"}，成功返回元数据。"""
        with self._lock:
            self.cleanup(target_device_id)
            idx = self._load_index(target_device_id)
            if len(idx) >= MAX_MSGS:
                return {"error": "quota_exceeded"}
            msg_id = "vm_" + uuid.uuid4().hex[:12]
            os.makedirs(self._dev_dir(target_device_id), exist_ok=True)
            write_fgvm(self.audio_path(target_device_id, msg_id), frames)
            row = {
                "msg_id": msg_id,
                "from": from_id,
                "to": target_device_id,
                "uid": uid,
                "created_at": int(time.time()),
                "duration_ms": int(duration_ms),
                "status": "new",
                "source_fmt": source_fmt,
            }
            self._append_index(target_device_id, row)
        try:
            from fuguoai_core.utils.event_bus import get_event_bus
            get_event_bus().publish({
                "type": "voicemail.new", "msg_id": msg_id,
                "device_id": target_device_id, "from": from_id,
                "duration_ms": int(duration_ms), "ts": int(time.time()),
            })
        except Exception:
            pass
        return row

    def list(self, device_id: str, status: str = None,
             limit: int = 20, offset: int = 0) -> list:
        with self._lock:
            idx = self._load_index(device_id)
        items = sorted(idx.values(), key=lambda r: r.get("created_at", 0), reverse=True)
        if status:
            items = [r for r in items if r.get("status") == status]
        return items[offset:offset + max(1, min(limit, 100))]

    def get(self, device_id: str, msg_id: str):
        with self._lock:
            return self._load_index(device_id).get(msg_id)

    def set_status(self, device_id: str, msg_id: str, status: str) -> bool:
        """状态迁移（new/played/read/deleted），追加一行状态行。"""
        with self._lock:
            idx = self._load_index(device_id)
            if msg_id not in idx and status != "deleted":
                return False
            self._append_index(device_id, {
                "msg_id": msg_id, "status": status, "status_at": int(time.time()),
            })
        if status == "played":
            try:
                from fuguoai_core.utils.event_bus import get_event_bus
                get_event_bus().publish({
                    "type": "voicemail.played", "msg_id": msg_id,
                    "device_id": device_id, "ts": int(time.time()),
                })
            except Exception:
                pass
        return True

    def delete(self, device_id: str, msg_id: str) -> bool:
        ok = self.set_status(device_id, msg_id, "deleted")
        try:
            os.remove(self.audio_path(device_id, msg_id))
        except FileNotFoundError:
            pass
        except Exception as e:
            _logger.warning(f"[voicemail] 音频删除失败 {msg_id}: {e}")
        return ok

    def pending(self, device_id: str) -> list:
        """待播列表（status=new，按时间正序——先留先播）。"""
        with self._lock:
            idx = self._load_index(device_id)
        items = [r for r in idx.values() if r.get("status") == "new"]
        return sorted(items, key=lambda r: r.get("created_at", 0))

    def devices_with_pending(self) -> list:
        """有待播留言的设备 id 列表（投递 worker 用）。"""
        out = []
        try:
            if not os.path.isdir(self.base):
                return out
            for entry in os.listdir(self.base):
                if os.path.isdir(os.path.join(self.base, entry)) and self.pending(entry):
                    out.append(entry)
        except Exception:
            pass
        return out

    # ── 清扫（TTL + 孤儿文件；调用方持锁或单线程） ──

    def cleanup(self, device_id: str):
        cutoff = time.time() - TTL_DAYS * 86400
        idx = self._load_index(device_id)
        expired = [m for m, r in idx.items() if r.get("created_at", 0) < cutoff]
        for mid in expired:
            self._append_index(device_id, {
                "msg_id": mid, "status": "deleted", "status_at": int(time.time()),
            })
            try:
                os.remove(self.audio_path(device_id, mid))
            except Exception:
                pass
        # index 压缩：删除行数远超有效行时重写（防 jsonl 无限增长）
        try:
            path = self._index_path(device_id)
            if os.path.exists(path) and os.path.getsize(path) > 256 * 1024:
                live = self._load_index(device_id)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    for row in sorted(live.values(), key=lambda r: r.get("created_at", 0)):
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                os.replace(tmp, path)
        except Exception:
            pass


_STORE = None


def get_voicemail_store() -> VoicemailStore:
    global _STORE
    if _STORE is None:
        _STORE = VoicemailStore()
    return _STORE
