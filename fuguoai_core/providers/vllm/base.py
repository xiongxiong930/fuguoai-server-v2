"""VLLM（视觉推理）提供方的抽象基类。

复果自有的视觉推理抽象层：定义 provider 契约的唯一抽象方法 ``response``，
由 ``fuguoai_core/utils/vllm.py`` 工厂按配置 ``type`` 动态加载具体实现，
再由 ``fuguoai_core/api/vision_handler.py`` 构造并调用。

**为什么响应方法是协程（async）**
视觉推理是一次**阻塞的网络调用**（模型推理耗时可达数百毫秒）。若在事件循环上
同步发起，整个单进程事件循环会被冻结——所有设备的 WebSocket 会话、语音对讲、
事件派发将同时卡死。因此本基类的契约规定 ``response`` 为协程，由具体实现
（如 OpenAI 兼容后端）把阻塞调用交给 ``asyncio.to_thread`` 执行，
使事件循环在推理期间保持响应。

**契约边界**
本基类**只**声明 ``response`` 一个抽象方法，不新增其它抽象方法，
以免破坏各具体实现的实例化能力；实现类必须保持协程签名兼容。
"""

from abc import ABC, abstractmethod
from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class VLLMProviderBase(ABC):
    """视觉推理提供方基类：约定一次「文本 + 图片」推理的调用形态。

    子类必须覆写 :meth:`response`，且覆写实现应为协程（``async def``），
    以便阻塞的模型推理不占用事件循环线程。
    """

    @abstractmethod
    async def response(self, question, base64_image):
        """发起一次视觉推理并返回模型回复文本。

        :param question: 用户的提问文本。
        :param base64_image: 图片的 base64 编码内容（不含 data URI 前缀）。
        :return: 模型生成的回复文本。
        """
        # 抽象方法：子类必须覆写为协程实现
        pass
