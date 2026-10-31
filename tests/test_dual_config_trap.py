# -*- coding: utf-8 -*-
"""双配置陷阱检测（离线）。

验证 detect_dual_config_trap：
- 根配置含真实密钥而运行时未覆盖 → 标记该键；
- 占位/空值不标记；
- 运行时已覆盖则不标记。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuguoai_config.config_loader import detect_dual_config_trap


def test_real_secret_in_base_not_overridden():
    base = {"server": {"auth_key": "abc123real"}, "manager-api": {"secret": "sk-real"}}
    user = {}
    flagged = detect_dual_config_trap(base, user)
    assert "server.auth_key" in flagged
    assert "manager-api.secret" in flagged


def test_placeholder_secret_not_flagged():
    base = {"server": {"auth_key": "你的密钥"}, "manager-api": {"secret": "your_key_here"}}
    user = {}
    assert detect_dual_config_trap(base, user) == []


def test_user_override_not_flagged():
    base = {"server": {"auth_key": "abc123real"}}
    user = {"server": {"auth_key": "abc123real"}}
    assert detect_dual_config_trap(base, user) == []


def test_empty_value_not_flagged():
    base = {"server": {"auth_key": ""}}
    user = {}
    assert detect_dual_config_trap(base, user) == []


def test_max_tokens_not_flagged():
    # 验证 "token" 词尾锚定：max_tokens / max_new_tokens 是普通配置项，不应被误标
    base = {
        "LLM": {"AliLLM": {"max_tokens": 500}},
        "TTS": {"FishSpeech": {"max_new_tokens": 1024}},
    }
    user = {}
    assert detect_dual_config_trap(base, user) == []


def test_placeholder_xxx_not_flagged():
    # 验证模板占位值（含 xxx / 固定本地令牌）不误标
    base = {
        "plugins": {"search_from_ragflow": {"api_key": "ragflow-xxx"}},
        "plugins2": {"web_search": {"api_key": "mk-xxx"}},
        "LLM": {"LMStudioLLM": {"api_key": "lm-studio"}},
    }
    user = {}
    assert detect_dual_config_trap(base, user) == []


if __name__ == "__main__":
    test_real_secret_in_base_not_overridden()
    test_placeholder_secret_not_flagged()
    test_user_override_not_flagged()
    test_empty_value_not_flagged()
    print("test_dual_config_trap: ALL PASS")
