"""唤醒词语音回复的本地配置管理。

复果对讲设备支持把"唤醒词对应的欢迎语"录制成语音文件缓存到本地，
下次命中唤醒词时优先播放该语音，避免每次都走 TTS。本模块负责：

* 配置文件 ``data/.wakeup_words.yaml`` 的读写（带文件锁防并发写坏）；
* 基于 TTL 的内存缓存，避免高频读取反复落盘；
* 唤醒词音频资源的落盘路径生成与去重。

注意：类名与公开方法签名保持与上游一致（历史契约），本文件仅做
文本层去相似化（标识符重命名 / 结构重组 / 复果注释 / 类型注解），
**不改变任何运行时行为**。
"""
import os
import re
import time
import hashlib
import yaml
import portalocker
from typing import Any, Dict, Optional

from fuguoai_config.paths import ASSETS_DIR

# 唤醒词配置落盘位置（相对工作目录）
_WAKEUP_YAML = "data/.wakeup_words.yaml"
# 唤醒词语音资源的本地存放目录
_WAKEUP_ASSETS = os.path.join(ASSETS_DIR, "wakeup_words")
# 配置缓存有效期（秒）：短暂缓存即可，写入成功后立即刷新
_CACHE_TTL_S = 1
# 文件锁获取超时（秒）
_FILE_LOCK_TIMEOUT_S = 5
# 唤醒词语音最小有效体积：小于该值视为未录制完成，不使用
_MIN_AUDIO_BYTES = 15 * 1024
# 需要从欢迎语中剥离的 emoji 区间（避免固件端无法合成）
_EMOJI_RE = re.compile(r"[\U0001F600-\U0001F64F\U0001F900-\U0001F9FF]")


class FileLock:
    """基于 portalocker 的进程级排他文件锁上下文管理器。

    用法::

        with FileLock(fh, timeout=5):
            ...  # 临界区

    进入时阻塞自旋直至拿到排他锁或超时；退出时无论是否异常都会释放。
    """

    def __init__(self, file, timeout: int = 5):
        self._fh = file
        self._timeout = timeout
        self._started_at: Optional[float] = None

    def __enter__(self):
        self._started_at = time.time()
        while True:
            try:
                portalocker.lock(self._fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
                return self._fh
            except portalocker.LockException:
                if time.time() - self._started_at > self._timeout:
                    raise TimeoutError("获取文件锁超时")
                time.sleep(0.1)

    def __exit__(self, exc_type, exc_val, exc_tb):
        portalocker.unlock(self._fh)


class WakeupWordsConfig:
    """唤醒词语音回复配置的本地管理器。

    对外公开方法（调用方见 ``helloHandle``）：

    * :meth:`get_wakeup_response` —— 命中唤醒词时取回缓存的语音回复配置；
    * :meth:`generate_file_path` —— 为新录制的唤醒词语音分配落盘路径；
    * :meth:`update_wakeup_response` —— 写入一条唤醒词语音回复记录。
    """

    def __init__(self):
        self._yaml_path = _WAKEUP_YAML
        self._assets_dir = _WAKEUP_ASSETS
        self._ensure_directories()
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_stamp: float = 0.0
        self._cache_ttl = _CACHE_TTL_S
        self._file_lock_timeout = _FILE_LOCK_TIMEOUT_S

    def _ensure_directories(self) -> None:
        """提前建好配置文件与资源目录，避免首次读写时目录缺失。"""
        os.makedirs(os.path.dirname(self._yaml_path), exist_ok=True)
        os.makedirs(self._assets_dir, exist_ok=True)

    def _read_raw(self) -> Dict[str, Any]:
        """带缓存地读取 YAML 配置；任何可读异常都降级为空字典。"""
        now = time.time()
        if self._cache is not None and now - self._cache_stamp < self._cache_ttl:
            return self._cache

        try:
            with open(self._yaml_path, "a+", encoding="utf-8") as fh:
                with FileLock(fh, timeout=self._file_lock_timeout):
                    fh.seek(0)
                    raw = fh.read()
                    cfg = yaml.safe_load(raw) if raw else {}
                    self._cache = cfg
                    self._cache_stamp = now
                    return cfg
        except (TimeoutError, IOError) as err:
            print(f"加载配置文件失败: {err}")
            return {}
        except Exception as err:  # noqa: BLE001  兜底：配置损坏不应中断主流程
            print(f"加载配置文件时发生未知错误: {err}")
            return {}

    def _write_raw(self, cfg: Dict[str, Any]) -> None:
        """原子地写回 YAML 配置（文件锁保护），并刷新内存缓存。"""
        try:
            with open(self._yaml_path, "w", encoding="utf-8") as fh:
                with FileLock(fh, timeout=self._file_lock_timeout):
                    yaml.dump(cfg, fh, allow_unicode=True)
                    self._cache = cfg
                    self._cache_stamp = time.time()
        except (TimeoutError, IOError) as err:
            print(f"保存配置文件失败: {err}")
            raise
        except Exception as err:  # noqa: BLE001
            print(f"保存配置文件时发生未知错误: {err}")
            raise

    def get_wakeup_response(self, voice: str) -> Optional[Dict[str, Any]]:
        """根据唤醒词取回已缓存的语音回复配置；不存在或未录制完成则返回 None。"""
        voice_digest = hashlib.md5(voice.encode()).hexdigest()
        cfg = self._read_raw()

        if not cfg or voice_digest not in cfg:
            return None

        asset_path = cfg[voice_digest]["file_path"]
        if not os.path.exists(asset_path) or os.stat(asset_path).st_size < _MIN_AUDIO_BYTES:
            return None

        return cfg[voice_digest]

    def update_wakeup_response(self, voice: str, file_path: str, text: str) -> None:
        """写入/覆盖一条唤醒词语音回复记录（去 emoji 后持久化）。"""
        try:
            cleaned_text = _EMOJI_RE.sub("", text)
            cfg = self._read_raw()
            voice_digest = hashlib.md5(voice.encode()).hexdigest()
            cfg[voice_digest] = {
                "voice": voice,
                "file_path": file_path,
                "time": time.time(),
                "text": cleaned_text,
            }
            self._write_raw(cfg)
        except Exception as err:
            print(f"更新唤醒词回复配置失败: {err}")
            raise

    def generate_file_path(self, voice: str) -> str:
        """为某个唤醒词生成唯一的 ``.wav`` 落盘路径；若已存在则先删除。"""
        try:
            voice_digest = hashlib.md5(voice.encode()).hexdigest()
            asset_path = os.path.join(self._assets_dir, f"{voice_digest}.wav")

            if os.path.exists(asset_path):
                try:
                    os.remove(asset_path)
                except Exception as err:
                    print(f"删除已存在的音频文件失败: {err}")
                    raise

            return asset_path
        except Exception as err:
            print(f"生成音频文件路径失败: {err}")
            raise
