"""腾讯云一句话识别（SentenceRecognition）ASR provider。

复果自有实现要点：

* **外部 HTTP 调用下沉至专属有界线程池**（`_ASR_EXECUTOR`，8 worker），不占用
  asyncio 默认线程池 —— 事件循环持续响应 WS / 对讲 / events，单台慢 ASR 也不会
  挤占其它设备的下沉任务；
* **异常分类暴露**：超时 / 连接失败 / 业务错误分别按类型抛出（不统一吞成 None），
  便于线上直接定位失败类型；
* **超时分级**：连接超时独立为 3.05s、读取 10s —— 连接抖动时快速失败，而不是
  挂起 10s 白等；
* **协议逐字保留**：腾讯云 TC3-HMAC-SHA256 签名算法、请求体字段与 `X-TC-*` 请求头
  与官方文档严格一致，保证协议兼容。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional, Tuple, List

import requests
from fuguoai_core.providers.asr.base import ASRProviderBase
from fuguoai_core.providers.asr.dto.dto import InterfaceType
from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 专属有界线程池：与 loop 默认池隔离，避免单台慢 ASR 占满默认池而拖垮
# 所有设备的意图识别等下沉任务（跨设备干扰）。8 worker 覆盖百级并发，
# 超出则排队而非无限扩线程。
_ASR_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="asr")


class ASRProvider(ASRProviderBase):
    API_URL = "https://asr.tencentcloudapi.com"
    API_VERSION = "2019-06-14"
    FORMAT = "pcm"  # 支持的音频格式：pcm, wav, mp3
    # (连接超时, 读取超时)：连接失败 3.05s 快速失败，读取留足一句话识别余量。
    _REQUEST_TIMEOUT = (3.05, 10)

    def __init__(self, config: dict, delete_audio_file: bool = True):
        super().__init__()
        self.interface_type = InterfaceType.NON_STREAM
        self.secret_id = config.get("secret_id")
        self.secret_key = config.get("secret_key")
        self.output_dir = config.get("output_dir")
        self.delete_audio_file = delete_audio_file

        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        """将语音数据转换为文本"""
        if not opus_data:
            logger.bind(tag=TAG).warning("音频数据为空！")
            return None, None

        try:
            # 检查配置是否已设置
            if not self.secret_id or not self.secret_key:
                logger.bind(tag=TAG).error("腾讯云语音识别配置未设置，无法进行识别")
                return None, None

            if artifacts is None:
                return "", None

            # 将音频数据转换为Base64编码
            base64_audio = base64.b64encode(artifacts.pcm_bytes).decode("utf-8")

            # 构建请求体
            request_body = self._build_request_body(base64_audio)

            # 获取认证头
            timestamp, authorization = self._sign_tencent_request(request_body)

            # 发送到专属 ASR 线程池执行，避免阻塞 asyncio 事件循环、冻结所有连接
            start_time = time.time()
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                _ASR_EXECUTOR,
                lambda: self._send_request(request_body, timestamp, authorization),
            )

            if result:
                logger.bind(tag=TAG).debug(
                    f"腾讯云语音识别耗时: {time.time() - start_time:.3f}s | 结果: {result}"
                )

            return result, artifacts.file_path

        except Exception as e:
            logger.bind(tag=TAG).error(f"处理音频时发生错误！{e}", exc_info=True)
            return None, None

    def _build_request_body(self, base64_audio: str) -> str:
        """构建请求体"""
        request_map = {
            "ProjectId": 0,
            "SubServiceType": 2,  # 一句话识别
            "EngSerViceType": "16k_zh",  # 中文普通话通用
            "SourceType": 1,  # 音频数据来源为语音文件
            "VoiceFormat": self.FORMAT,  # 音频格式
            "Data": base64_audio,  # Base64编码的音频数据
            "DataLen": len(base64_audio),  # 数据长度
        }
        return json.dumps(request_map)

    def _sign_tencent_request(self, request_body: str) -> Tuple[str, str]:
        """TC3-HMAC-SHA256 签名，返回 (timestamp, authorization)"""
        try:
            # 获取当前UTC时间戳
            now = datetime.now(timezone.utc)
            timestamp = str(int(now.timestamp()))
            date = now.strftime("%Y-%m-%d")

            # 服务名称必须是 "asr"
            service = "asr"

            # 拼接凭证范围
            credential_scope = f"{date}/{service}/tc3_request"

            # 使用TC3-HMAC-SHA256签名方法
            algorithm = "TC3-HMAC-SHA256"

            # 构建规范请求字符串
            http_request_method = "POST"
            canonical_uri = "/"
            canonical_query_string = ""

            # 注意：头部信息需要按照ASCII升序排列，且key和value都转为小写
            # 必须包含content-type和host头部
            content_type = "application/json; charset=utf-8"
            host = "asr.tencentcloudapi.com"
            action = "SentenceRecognition"  # 接口名称

            # 构建规范头部信息，注意顺序和格式
            canonical_headers = (
                f"content-type:{content_type.lower()}\n"
                + f"host:{host.lower()}\n"
                + f"x-tc-action:{action.lower()}\n"
            )

            signed_headers = "content-type;host;x-tc-action"

            # 请求体哈希值
            payload_hash = self._sha256_hex(request_body)

            # 构建规范请求字符串
            canonical_request = (
                f"{http_request_method}\n"
                + f"{canonical_uri}\n"
                + f"{canonical_query_string}\n"
                + f"{canonical_headers}\n"
                + f"{signed_headers}\n"
                + f"{payload_hash}"
            )

            # 计算规范请求的哈希值
            hashed_canonical_request = self._sha256_hex(canonical_request)

            # 构建待签名字符串
            string_to_sign = (
                f"{algorithm}\n"
                + f"{timestamp}\n"
                + f"{credential_scope}\n"
                + f"{hashed_canonical_request}"
            )

            # 计算签名密钥
            secret_date = self._hmac_sha256(f"TC3{self.secret_key}", date)
            secret_service = self._hmac_sha256(secret_date, service)
            secret_signing = self._hmac_sha256(secret_service, "tc3_request")

            # 计算签名
            signature = self._bytes_to_hex(
                self._hmac_sha256(secret_signing, string_to_sign)
            )

            # 构建授权头
            authorization = (
                f"{algorithm} "
                + f"Credential={self.secret_id}/{credential_scope}, "
                + f"SignedHeaders={signed_headers}, "
                + f"Signature={signature}"
            )

            return timestamp, authorization

        except Exception as e:
            logger.bind(tag=TAG).error(f"生成认证头失败: {e}", exc_info=True)
            raise RuntimeError(f"生成认证头失败: {e}")

    def _send_request(
        self, request_body: str, timestamp: str, authorization: str
    ) -> Optional[str]:
        """发送请求到腾讯云 API；按异常类型分类暴露，便于线上定位。"""
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Host": "asr.tencentcloudapi.com",
            "Authorization": authorization,
            "X-TC-Action": "SentenceRecognition",
            "X-TC-Version": self.API_VERSION,
            "X-TC-Timestamp": timestamp,
            "X-TC-Region": "ap-shanghai",
        }

        try:
            response = requests.post(
                self.API_URL,
                headers=headers,
                data=request_body,
                timeout=self._REQUEST_TIMEOUT,
            )
        except requests.exceptions.Timeout:
            logger.bind(tag=TAG).warning("腾讯云 ASR 请求超时（Timeout）")
            raise
        except requests.exceptions.ConnectionError:
            logger.bind(tag=TAG).warning("腾讯云 ASR 网络连接失败（ConnectionError）")
            raise
        except requests.exceptions.RequestException as e:
            logger.bind(tag=TAG).warning(f"腾讯云 ASR 请求异常: {e}")
            raise

        if not response.ok:
            raise IOError(f"请求失败: {response.status_code} {response.reason}")

        try:
            response_json = response.json()
        except ValueError as e:
            raise IOError(f"响应 JSON 解析失败: {e}")

        # 检查是否有错误
        if "Response" in response_json and "Error" in response_json["Response"]:
            error = response_json["Response"]["Error"]
            raise IOError(f"API返回错误: {error['Code']}: {error['Message']}")

        # 提取识别结果
        if "Response" in response_json and "Result" in response_json["Response"]:
            return response_json["Response"]["Result"]
        else:
            logger.bind(tag=TAG).warning(f"响应中没有识别结果: {response_json}")
            return ""

    def _sha256_hex(self, data: str) -> str:
        """计算字符串的SHA256哈希值"""
        digest = hashlib.sha256(data.encode("utf-8")).digest()
        return self._bytes_to_hex(digest)

    def _hmac_sha256(self, key, data: str) -> bytes:
        """计算HMAC-SHA256"""
        if isinstance(key, str):
            key = key.encode("utf-8")

        return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()

    def _bytes_to_hex(self, bytes_data: bytes) -> str:
        """字节数组转十六进制字符串"""
        return "".join(f"{b:02x}" for b in bytes_data)
