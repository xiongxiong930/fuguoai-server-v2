"""离线测试：维度单例 config=None 回退到 config_holder 全局配置。

回归背景：persona_routes.py 曾用 get_persona_store() 无参调用，在 connection
带参初始化之前抢先锁死进程单例为 disabled（PersonaStore(None)->enabled=False），
导致生产环境 persona 注入与计数器永亮不了（07-31 探针因隔离进程带参调用而假阳性通过）。
修复后两工厂在 config=None 时回退到 config_holder.get_config()（运行进程由
app.py:54 注入含 persona 的全局 config）。

测试验证：注入含 persona/dialogue_eval 的全局 config 后，无参调用单例工厂应得到
enabled=True 且 render 产出非空块；关闭时得到 enabled=False。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils.config_holder import set_config
from fuguoai_core.utils import persona as P
from fuguoai_core.utils import dialogue_eval as D
from fuguoai_core.utils.persona import get_persona_store
from fuguoai_core.utils.dialogue_eval import get_dialogue_eval


def _reset():
    # 隔离单例，避免测试间互相锁死
    P._instance = None
    D._eval_instance = None


def test_persona_fallback_enabled():
    set_config({
        "persona": {"enabled": True, "default": {"name": "复果", "style": "温暖"}},
        "dialogue_eval": {"enabled": True, "llm": "OpenAILLM"},
        "LLM": {"OpenAILLM": {"type": "openai"}},
    })
    _reset()
    ps = get_persona_store()  # 无参调用
    assert ps is not None, "单例应被创建"
    assert ps.enabled is True, "无参回退到全局 config 应得到 enabled=True"
    block = ps.render("test_dev")
    assert "复果" in block, "render 应产出含默认画像的块"


def test_dialogue_eval_fallback_enabled():
    set_config({
        "persona": {"enabled": True, "default": {"name": "复果"}},
        "dialogue_eval": {"enabled": True, "llm": "OpenAILLM"},
        "LLM": {"OpenAILLM": {"type": "openai"}},
    })
    _reset()
    de = get_dialogue_eval()  # 无参调用
    assert de.enabled is True, "无参回退到全局 config 应得到 enabled=True"


def test_persona_fallback_disabled_when_off():
    set_config({"persona": {"enabled": False}})
    _reset()
    ps = get_persona_store()
    assert ps.enabled is False, "关闭时应得到 enabled=False"


if __name__ == "__main__":
    test_persona_fallback_enabled()
    test_dialogue_eval_fallback_enabled()
    test_persona_fallback_disabled_when_off()
    print("ALL PASS")
