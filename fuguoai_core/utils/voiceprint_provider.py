"""
复果声纹识别提供器（腾讯云「说话人识别」）

对接腾讯云 ASR 产品族下的「说话人识别」接口：
- VoicePrintEnroll    注册（1:1 登记一条声纹，返回 VoicePrintId + SpeakerNick）
- VoicePrintGroupVerify  1:N 验证（单调用，指定 GroupId + TopN，返回打分 TopN 列表）
- VoicePrintDelete    删除（按 VoicePrintId）

复用与腾讯 ASR 完全相同的 TC3-HMAC-SHA256 手写签名与接入点
（asr.tencentcloudapi.com / 2019-06-14 / ap-shanghai / service=asr），
零额外基础设施（你已在用腾讯 ASR）。

设计要点：
- 配置块 `voiceprint: {provider: tencent, secret_id, secret_key, group_id,
  similarity_threshold}`，缺密钥则优雅禁用，不影响主对话链路；
- 1:N 走 VoicePrintGroupVerify（官方接口，非自遍历 Verify）：取 Score 最高且
  >= 阈值者，Score 为 0~100 字符串（官方建议 70 分以上为同一人）；
- 说话人登记（注册）经 admin HTTP 接口触发，registry 落盘
  $FUGUOAI_DATA_DIR/voiceprint_registry.json（group_id -> [{voiceprint_id, name}]）；
- 全程 aiohttp 异步驱动，模块不依赖同步 requests（演进 R2 不变式）；
- 所有网络异常降级为「禁用 / 返回 None / 返回错误」，不冻结事件循环。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, List

import aiohttp
from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 腾讯云接入点（与腾讯 ASR 完全一致）
_VP_API_URL = "https://asr.tencentcloudapi.com"
_VP_API_VERSION = "2019-06-14"
_VP_SERVICE = "asr"
_VP_REGION = "ap-shanghai"
_VP_HOST = "asr.tencentcloudapi.com"

# 注册表落盘位置（与 schedule_mirror_store 等一致使用 FUGUOAI_DATA_DIR）
_DATA_DIR = os.environ.get("FUGUOAI_DATA_DIR", "/opt/fuguoai-server/data")
_REGISTRY_PATH = os.path.join(_DATA_DIR, "voiceprint_registry.json")

# 腾讯云「声音复刻」(VoiceClone) 接入点：TRTC 产品族，独立于说话人识别(asr)。
# 复用同一 secret_id/secret_key 即可调用（你已在用腾讯 ASR/说话人识别）。
_TRTCC_API_URL = "https://trtc.tencentcloudapi.com"
_TRTCC_API_VERSION = "2019-07-22"
_TRTCC_SERVICE = "trtc"
_TRTCC_HOST = "trtc.tencentcloudapi.com"

# 单例缓存：按 (secret_id, secret_key, group_id) 复用 provider 实例
_provider_cache: Dict[tuple, "VoiceprintProvider"] = {}
_provider_cache_lock = threading.Lock()


class VoiceprintProvider:
    """腾讯云说话人识别提供器。"""

    def __init__(self, config: dict):
        self.provider = config.get("provider", "tencent")
        self.secret_id = config.get("secret_id")
        self.secret_key = config.get("secret_key")
        self.group_id = config.get("group_id")
        # Score 为 0~100，官方建议 70 分以上为同一人；可在 config 调低以更宽容
        self.similarity_threshold = float(config.get("similarity_threshold", 70.0))
        self.enabled = bool(self.secret_id and self.secret_key and self.group_id)

        # 音色克隆（声音复刻 VoiceClone）开关与 TRTC 依赖。
        # 复用同一 secret_id/secret_key；额外需要 TRTC SdkAppId。
        # 默认关闭：不影响声纹识别与生产 TTS；开启需先在 TRTC 控制台建应用拿 SdkAppId。
        self.clone_enabled = bool(config.get("clone_enabled")) and bool(
            self.secret_id and self.secret_key and config.get("sdk_app_id")
        )
        self.sdk_app_id = config.get("sdk_app_id")
        self.clone_region = config.get("clone_region", "ap-guangzhou")
        self.clone_model = config.get("clone_model") or "flow_02_turbo"
        self.clone_language = config.get("clone_language", "zh")

        # 注册表（内存镜像 + 文件）；单一 registry 锁，绝不跨 await 持有
        self._registry_lock = threading.Lock()
        self.speakers: List[Dict[str, str]] = []  # [{voiceprint_id, name, created_at}]

        if self.enabled:
            self._load_registry()
            logger.bind(tag=TAG).info(
                f"声纹识别(腾讯云)已启用: group_id={self.group_id}, "
                f"已登记说话人={len(self.speakers)}个, 阈值={self.similarity_threshold}"
            )
        else:
            logger.bind(tag=TAG).warning(
                "声纹识别(腾讯云)未启用：缺少 secret_id/secret_key/group_id 配置"
            )

    # ── TC3-HMAC-SHA256 手写签名（与 tencent.py 同源，纯 CPU 不阻塞） ──
    def _sign(self, action: str, request_body: str,
              service: str = _VP_SERVICE, host: str = _VP_HOST,
              version: str = _VP_API_VERSION, region: str = _VP_REGION):
        now = datetime.now(timezone.utc)
        timestamp = str(int(now.timestamp()))
        date = now.strftime("%Y-%m-%d")
        credential_scope = f"{date}/{service}/tc3_request"
        algorithm = "TC3-HMAC-SHA256"
        content_type = "application/json; charset=utf-8"
        canonical_headers = (
            f"content-type:{content_type.lower()}\n"
            f"host:{host.lower()}\n"
            f"x-tc-action:{action.lower()}\n"
        )
        signed_headers = "content-type;host;x-tc-action"
        payload_hash = self._sha256_hex(request_body)
        canonical_request = (
            f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )
        hashed_cr = self._sha256_hex(canonical_request)
        string_to_sign = f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashed_cr}"
        secret_date = self._hmac(f"TC3{self.secret_key}", date)
        secret_service = self._hmac(secret_date, service)
        secret_signing = self._hmac(secret_service, "tc3_request")
        signature = self._bytes_to_hex(self._hmac(secret_signing, string_to_sign))
        authorization = (
            f"{algorithm} Credential={self.secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return timestamp, authorization

    @staticmethod
    def _sha256_hex(data: str) -> str:
        return VoiceprintProvider._bytes_to_hex(
            hashlib.sha256(data.encode("utf-8")).digest()
        )

    @staticmethod
    def _hmac(key, data: str) -> bytes:
        if isinstance(key, str):
            key = key.encode("utf-8")
        return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()

    @staticmethod
    def _bytes_to_hex(b: bytes) -> str:
        return "".join(f"{x:02x}" for x in b)

    # ── aiohttp 调用（无同步 requests 依赖） ──
    async def _call(self, action: str, params: dict, timeout_s: float = 10.0) -> dict:
        """调用腾讯云说话人识别接口，返回 Response 子字典或抛异常。"""
        request_body = json.dumps(params, ensure_ascii=False)
        timestamp, authorization = self._sign(action, request_body)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Host": _VP_HOST,
            "Authorization": authorization,
            "X-TC-Action": action,
            "X-TC-Version": _VP_API_VERSION,
            "X-TC-Timestamp": timestamp,
            "X-TC-Region": _VP_REGION,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _VP_API_URL,
                    headers=headers,
                    data=request_body,
                    timeout=aiohttp.ClientTimeout(total=timeout_s),
                ) as resp:
                    status = resp.status
                    text = await resp.text()
        except asyncio.TimeoutError:
            raise IOError(f"腾讯云声纹请求超时({timeout_s}s)")
        except aiohttp.ClientError as e:
            raise IOError(f"腾讯云声纹网络异常: {e}")

        if status != 200:
            raise IOError(f"HTTP {status}")
        try:
            j = json.loads(text)
        except ValueError as e:
            raise IOError(f"响应JSON解析失败: {e}")
        resp_obj = j.get("Response", {})
        if "Error" in resp_obj:
            err = resp_obj["Error"]
            raise IOError(f"API错误 {err.get('Code')}: {err.get('Message')}")
        return resp_obj

    # ── 腾讯云「声音复刻」(TRTC VoiceClone)：独立 product/service ──
    async def _call_trtc(self, action: str, params: dict, timeout_s: float = 30.0) -> dict:
        """调用 TRTC 产品族接口（如 VoiceClone）。返回 Response 子字典或抛异常。"""
        request_body = json.dumps(params, ensure_ascii=False)
        timestamp, authorization = self._sign(
            action, request_body,
            service=_TRTCC_SERVICE, host=_TRTCC_HOST,
            version=_TRTCC_API_VERSION, region=self.clone_region,
        )
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Host": _TRTCC_HOST,
            "Authorization": authorization,
            "X-TC-Action": action,
            "X-TC-Version": _TRTCC_API_VERSION,
            "X-TC-Timestamp": timestamp,
            "X-TC-Region": self.clone_region,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _TRTCC_API_URL,
                    headers=headers,
                    data=request_body,
                    timeout=aiohttp.ClientTimeout(total=timeout_s),
                ) as resp:
                    status = resp.status
                    text = await resp.text()
        except asyncio.TimeoutError:
            raise IOError(f"腾讯云声音复刻请求超时({timeout_s}s)")
        except aiohttp.ClientError as e:
            raise IOError(f"腾讯云声音复刻网络异常: {e}")
        if status != 200:
            raise IOError(f"HTTP {status}")
        try:
            j = json.loads(text)
        except ValueError as e:
            raise IOError(f"响应JSON解析失败: {e}")
        resp_obj = j.get("Response", {})
        if "Error" in resp_obj:
            err = resp_obj["Error"]
            raise IOError(f"API错误 {err.get('Code')}: {err.get('Message')}")
        return resp_obj

    @staticmethod
    def _sanitize_clone_name(name: str) -> str:
        """VoiceClone.VoiceName 仅允许数字/字母/下划线，<=36 位。"""
        cleaned = re.sub(r"[^0-9A-Za-z_]", "_", (name or "").strip())
        cleaned = cleaned[:36]
        if not cleaned:
            cleaned = "voice_" + uuid.uuid4().hex[:12]
        return cleaned

    async def clone_voice(self, name: str, wav_data: bytes) -> Optional[str]:
        """一次录音两用：把同一段注册音频复刻为可复用音色，返回 VoiceId。

        best-effort：任何失败（未开启/缺 SdkAppId/时长不符/限频/网络）一律返回 None，
        绝不影响声纹注册主链路。PromptAudio 需 16k 单声道 wav、6–180s；声纹注册
        音频通常 1–8s，不足 6s 时克隆会被服务端拒，属预期（仅告警）。
        """
        if not self.clone_enabled:
            return None
        if not (self.sdk_app_id and self.secret_id and self.secret_key):
            logger.bind(tag=TAG).warning("音色克隆未启用（缺 sdk_app_id 或密钥）")
            return None
        if not wav_data:
            return None
        prompt_b64 = base64.b64encode(wav_data).decode("utf-8")
        params = {
            "SdkAppId": int(self.sdk_app_id),
            "VoiceName": self._sanitize_clone_name(name),
            "PromptAudio": prompt_b64,
            "Model": self.clone_model,
            "Language": self.clone_language,
        }
        try:
            r = await self._call_trtc("VoiceClone", params)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"音色克隆失败(不影响声纹注册): {e}")
            return None
        voice_id = (r.get("Data") or {}).get("VoiceId") or r.get("VoiceId")
        if not voice_id:
            logger.bind(tag=TAG).warning("音色克隆响应缺少 VoiceId(不影响声纹注册)")
            return None
        logger.bind(tag=TAG).info(f"音色克隆成功: {name} -> {voice_id}")
        return voice_id

    def get_cloned_voice_id(self, name: str) -> Optional[str]:
        """按称呼反查已克隆音色 VoiceId（供 FlowTTS 当 VoiceId 使用）。"""
        with self._registry_lock:
            for s in self.speakers:
                if s.get("name") == name:
                    return s.get("voice_id")
        return None

    # ── 注册表持久化 ──
    def _load_registry(self):
        try:
            if not os.path.exists(_REGISTRY_PATH):
                self.speakers = []
                return
            with open(_REGISTRY_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            # 若 group_id 与当前配置不一致（配置变更），以当前配置为准重建为空
            if data.get("group_id") != self.group_id:
                logger.bind(tag=TAG).warning(
                    f"注册表 group_id({data.get('group_id')}) 与当前配置"
                    f"({self.group_id}) 不一致，已忽略旧注册表"
                )
                self.speakers = []
                return
            self.speakers = data.get("speakers", [])
        except Exception as e:
            logger.bind(tag=TAG).error(f"加载声纹注册表失败: {e}")
            self.speakers = []

    def _save_registry(self):
        os.makedirs(_DATA_DIR, exist_ok=True)
        data = {"group_id": self.group_id, "speakers": self.speakers}
        tmp = _REGISTRY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _REGISTRY_PATH)

    # ── 对外能力 ──
    async def identify_speaker(self, wav_data: bytes, session_id: str) -> Optional[str]:
        """1:N 识别说话人；未启用/无登记/异常时返回 None，低于阈值返回『未知说话人』。"""
        if not self.enabled:
            logger.bind(tag=TAG).debug("声纹识别未启用，跳过识别")
            return None
        # 取内存快照（避免持锁跨 await）
        with self._registry_lock:
            candidates = list(self.speakers)
        if not candidates:
            logger.bind(tag=TAG).debug("无已登记说话人，跳过 1:N 识别")
            return None

        b64 = base64.b64encode(wav_data).decode("utf-8")
        params = {
            "VoiceFormat": 1,  # wav
            "SampleRate": 16000,
            "Data": b64,
            "GroupId": self.group_id,
            "TopN": len(candidates),
        }
        t0 = time.monotonic()
        try:
            r = await self._call("VoicePrintGroupVerify", params)
        except Exception as e:
            logger.bind(tag=TAG).error(f"1:N 声纹识别调用失败: {e}")
            return None

        tops = (r.get("Data") or {}).get("VerifyTops") or []
        if not tops:
            logger.bind(tag=TAG).info(f"1:N 无匹配结果(耗时{time.monotonic() - t0:.3f}s)")
            return "未知说话人"

        best = max(tops, key=lambda t: float(t.get("Score", "0")))
        score = float(best.get("Score", "0"))
        if score < self.similarity_threshold:
            logger.bind(tag=TAG).info(
                f"1:N 最高分 {score:.1f} < 阈值 {self.similarity_threshold}，判未知"
                f"(耗时{time.monotonic() - t0:.3f}s)"
            )
            return "未知说话人"

        # 优先用 VoicePrintId 反查 registry 名；缺失则退回 SpeakerId(nick)
        vp_id = best.get("VoicePrintId")
        name = None
        with self._registry_lock:
            for s in self.speakers:
                if s["voiceprint_id"] == vp_id:
                    name = s["name"]
                    break
        if name is None:
            name = best.get("SpeakerId") or "未知说话人"
        logger.bind(tag=TAG).info(
            f"声纹识别成功: {name} (score={score:.1f}, 耗时{time.monotonic() - t0:.3f}s)"
        )
        return name

    async def enroll_speaker(self, name: str, wav_data: bytes) -> Dict:
        """登记一名说话人；成功写入本地注册表并返回 VoicePrintId。"""
        if not self.enabled:
            return {"ok": False, "error": "声纹识别未启用（缺少密钥配置）"}
        name = (name or "").strip()
        if not name or len(name) > 32:
            return {"ok": False, "error": "SpeakerNick 必须 1-32 字符"}
        if not wav_data:
            return {"ok": False, "error": "缺少音频数据"}
        b64 = base64.b64encode(wav_data).decode("utf-8")
        params = {
            "GroupId": self.group_id,
            "SpeakerNick": name,
            "VoiceFormat": 1,
            "SampleRate": 16000,
            "Data": b64,
        }
        try:
            r = await self._call("VoicePrintEnroll", params)
        except Exception as e:
            return {"ok": False, "error": f"注册调用失败: {e}"}
        data = r.get("Data") or r
        vp_id = data.get("VoicePrintId") or r.get("VoicePrintId")
        if not vp_id:
            return {"ok": False, "error": "响应缺少 VoicePrintId"}
        entry = {
            "voiceprint_id": vp_id,
            "name": data.get("SpeakerNick") or name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._registry_lock:
            # 同名去重：先移除旧登记
            self.speakers = [s for s in self.speakers if s["name"] != name]
            self.speakers.append(entry)
            self._save_registry()
        logger.bind(tag=TAG).info(f"声纹登记成功: {entry['name']} -> {vp_id}")

        # 一次录音两用：best-effort 克隆音色（失败不影响注册）
        voice_id = None
        if self.clone_enabled:
            voice_id = await self.clone_voice(name, wav_data)
            if voice_id:
                with self._registry_lock:
                    for s in self.speakers:
                        if s is entry or s.get("voiceprint_id") == vp_id:
                            s["voice_id"] = voice_id
                    self._save_registry()
                entry["voice_id"] = voice_id

        return {
            "ok": True,
            "voiceprint_id": vp_id,
            "name": entry["name"],
            "voice_id": voice_id,
        }

    def list_speakers(self) -> Dict:
        with self._registry_lock:
            sp = list(self.speakers)
        return {
            "enabled": self.enabled,
            "group_id": self.group_id,
            "speakers": sp,
        }

    async def delete_speaker(self, voiceprint_id: str) -> Dict:
        """删除一名说话人：先清本地登记，再尽力调腾讯侧删除。"""
        if not self.enabled:
            return {"ok": False, "error": "声纹识别未启用"}
        if not voiceprint_id:
            return {"ok": False, "error": "缺少 voiceprint_id"}
        params = {"GroupId": self.group_id, "VoicePrintId": voiceprint_id, "DelMod": 0}
        try:
            await self._call("VoicePrintDelete", params)
        except Exception as e:
            # 即便腾讯侧删除失败，本地仍清理登记，避免死登记占用与误匹配
            logger.bind(tag=TAG).warning(f"腾讯侧删除失败(仍清理本地登记): {e}")
        with self._registry_lock:
            before = len(self.speakers)
            self.speakers = [s for s in self.speakers if s["voiceprint_id"] != voiceprint_id]
            after = len(self.speakers)
            self._save_registry()
        return {
            "ok": True,
            "removed_local": before - after > 0,
            "voiceprint_id": voiceprint_id,
        }


def get_voiceprint_provider(config: dict) -> Optional[VoiceprintProvider]:
    """按配置返回（或构造并缓存）腾讯云说话人识别 provider 单例。

    配置缺失 / provider 非 tencent → 返回 None（不影响主链路）。
    """
    vc = (config or {}).get("voiceprint") or {}
    if not vc or vc.get("provider") != "tencent":
        return None
    secret_id = vc.get("secret_id")
    secret_key = vc.get("secret_key")
    group_id = vc.get("group_id")
    if not (secret_id and secret_key and group_id):
        return None
    key = (secret_id, secret_key, group_id)
    with _provider_cache_lock:
        if key in _provider_cache:
            return _provider_cache[key]
        p = VoiceprintProvider(vc)
        _provider_cache[key] = p
        return p
