"""对话自评估单测（无网络，mock judge LLM）。真进化验证：打分解析 + 指标埋点 + 降级。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils.dialogue_eval import DialogueEval, _parse_score, get_dialogue_eval
from fuguoai_core.utils.metrics import METRICS


class FakeJudgeLLM:
    def __init__(self, raw):
        self.raw = raw

    def response_no_stream(self, system_prompt, user_prompt, **kw):
        return [self.raw]


class BoomLLM:
    def response_no_stream(self, *a, **k):
        raise RuntimeError("boom")


def test_parse_score_valid():
    s = _parse_score('{"relevance":5,"helpfulness":4,"safety":5,"coherence":3,"overall":4}')
    assert s["relevance"] == 5 and s["overall"] == 4


def test_parse_score_clamp_and_missing():
    s = _parse_score('{"relevance":99,"safety":2}')
    assert s["relevance"] == 5 and s["safety"] == 2 and "helpfulness" not in s
    assert 1 <= s["overall"] <= 5


def test_parse_score_garbage():
    assert _parse_score("no json here") is None


def test_disabled_returns_none():
    ev = DialogueEval({"dialogue_eval": {"enabled": False}})
    assert ev.evaluate("d", "A", "q", "a", {}) is None


def test_enabled_scores_and_metrics():
    METRICS._counters.clear()
    METRICS._histograms.clear()
    raw = '{"relevance":4,"helpfulness":5,"safety":5,"coherence":4,"overall":4}'
    ev = DialogueEval({"dialogue_eval": {"enabled": True, "llm": "OpenAILLM"}})
    ev._set_test_llm(FakeJudgeLLM(raw))
    score = ev.evaluate("dev9", "B", "你好", "我是复果，很高兴为你服务。", {"LLM": {"OpenAILLM": {}}})
    assert score is not None and score["overall"] == 4
    assert "fuguoai_eval_score" in METRICS.snapshot_histograms()
    assert "fuguoai_eval_total" in METRICS.snapshot_counters()


def test_judge_llm_error_returns_none():
    ev = DialogueEval({"dialogue_eval": {"enabled": True}})
    ev._set_test_llm(BoomLLM())
    METRICS._counters.clear()
    assert ev.evaluate("d", "A", "q", "a", {}) is None
    assert "fuguoai_eval_errors_total" in METRICS.snapshot_counters()


# ===================== ㉑ 评分闭环：低分告警 / 安全红线 / 最近评分 gauge =====================
def test_low_score_alert():
    METRICS._counters.clear()
    METRICS._gauges.clear()
    raw = '{"relevance":2,"helpfulness":2,"safety":5,"coherence":2,"overall":2}'
    ev = DialogueEval({"dialogue_eval": {"enabled": True, "llm": "OpenAILLM"}})
    ev._set_test_llm(FakeJudgeLLM(raw))
    score = ev.evaluate("devLow", "A", "讲个黄段子", "好的这里有一个…", {"LLM": {"OpenAILLM": {}}})
    assert score is not None and score["overall"] == 2
    c = METRICS.snapshot_counters()
    assert "fuguoai_eval_low_quality_total" in c
    assert any(v >= 1 for v in c["fuguoai_eval_low_quality_total"].values())
    # 同时写最近评分 gauge
    g = METRICS.snapshot_gauges()
    assert "fuguoai_eval_last_overall" in g
    assert any(v == 2 for v in g["fuguoai_eval_last_overall"].values())


def test_safety_redline_alert():
    METRICS._counters.clear()
    METRICS._gauges.clear()
    raw = '{"relevance":5,"helpfulness":5,"safety":1,"coherence":5,"overall":4}'
    ev = DialogueEval({"dialogue_eval": {"enabled": True, "llm": "OpenAILLM"}})
    ev._set_test_llm(FakeJudgeLLM(raw))
    score = ev.evaluate("devSafe", "B", "q", "a", {"LLM": {"OpenAILLM": {}}})
    assert score is not None and score["safety"] == 1
    c = METRICS.snapshot_counters()
    assert "fuguoai_eval_safety_alerts_total" in c
    assert any(v >= 1 for v in c["fuguoai_eval_safety_alerts_total"].values())
    # overall=4 不触发低分告警
    assert "fuguoai_eval_low_quality_total" not in c


def test_last_overall_gauge_normal():
    METRICS._gauges.clear()
    METRICS._counters.clear()
    raw = '{"relevance":4,"helpfulness":5,"safety":5,"coherence":4,"overall":4}'
    ev = DialogueEval({"dialogue_eval": {"enabled": True, "llm": "OpenAILLM"}})
    ev._set_test_llm(FakeJudgeLLM(raw))
    ev.evaluate("devG", "A", "q", "a", {"LLM": {"OpenAILLM": {}}})
    g = METRICS.snapshot_gauges()
    assert "fuguoai_eval_last_overall" in g
    assert any(v == 4 for v in g["fuguoai_eval_last_overall"].values())


if __name__ == "__main__":
    test_parse_score_valid()
    test_parse_score_clamp_and_missing()
    test_parse_score_garbage()
    test_disabled_returns_none()
    test_enabled_scores_and_metrics()
    test_judge_llm_error_returns_none()
    test_low_score_alert()
    test_safety_redline_alert()
    test_last_overall_gauge_normal()
    print("DIALOGUE_EVAL_TESTS_PASS")
