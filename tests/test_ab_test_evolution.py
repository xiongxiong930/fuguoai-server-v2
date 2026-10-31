"""A/B 测试单测（无网络）。真进化验证：稳定分桶 + 覆盖注入 + 单例。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils.ab_test import ABTest, ABTestLLMWrapper, get_ab_test


class FakeLLM:
    def __init__(self):
        self.calls = []

    def response(self, session_id, dialogue, **kw):
        self.calls.append(kw)
        yield "hi"

    def response_with_functions(self, session_id, dialogue, functions=None, **kw):
        self.calls.append(kw)
        yield ("hi", [])

    def response_no_stream(self, system_prompt, user_prompt, **kw):
        self.calls.append(kw)
        return ["ok"]


def test_disabled_returns_original():
    cfg = {"ab_test": {"enabled": False, "buckets": {"A": {}, "B": {"overrides": {"temperature": 0.9}}}}}
    ab = ABTest(cfg)
    llm = FakeLLM()
    assert ab.wrap("dev1", llm) is llm


def test_wrap_matches_overrides():
    cfg = {"ab_test": {"enabled": True, "buckets": {"A": {}, "B": {"overrides": {"temperature": 0.95}}}}}
    ab = ABTest(cfg)
    for dev in ["dev1", "dev2", "dev3", "dev4"]:
        name, overrides = ab.resolve(dev)
        llm = FakeLLM()
        w = ab.wrap(dev, llm)
        if overrides:
            assert isinstance(w, ABTestLLMWrapper)
            list(w.response("s", []))
            assert w._wrapped.calls[0].get("temperature") == 0.95
        else:
            assert w is llm


def test_stable_bucket():
    cfg = {"ab_test": {"enabled": True, "buckets": {"A": {}, "B": {"overrides": {}}}}}
    ab = ABTest(cfg)
    assert ab.bucket_for("device-xyz") == ab.bucket_for("device-xyz")
    names = {ab.bucket_for(f"d{i}") for i in range(50)}
    assert names <= {"A", "B"}


def test_get_ab_test_singleton():
    cfg = {"ab_test": {"enabled": True, "buckets": {"A": {}, "B": {}}}}
    assert get_ab_test(cfg) is get_ab_test(cfg)


if __name__ == "__main__":
    test_disabled_returns_original()
    test_wrap_matches_overrides()
    test_stable_bucket()
    test_get_ab_test_singleton()
    print("AB_TEST_TESTS_PASS")
