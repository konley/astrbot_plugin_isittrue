import inspect
from unittest.mock import MagicMock

from main import IsItTrue


def _plugin() -> IsItTrue:
    return IsItTrue(MagicMock(), {})


def test_now_label_has_date():
    label = IsItTrue._now_label()
    assert len(label) >= 16
    assert label[0:4].isdigit()
    assert "-" in label


def test_user_prompt_injects_now_and_search_priority():
    plugin = _plugin()
    prompt = plugin._build_user_prompt(
        source="引用消息",
        text="所以今天算梁文峰还是梁文谷",
        images=[],
        supplement="",
        search_block="## Tavily 搜索结果\n### DeepSeek峰谷定价",
        notes=[],
        claim="所以今天算梁文峰还是梁文谷",
    )
    assert "【当前时间】" in prompt
    assert "伪造日期" in prompt
    assert "过时训练知识" in prompt
    assert "DeepSeek峰谷定价" in prompt


def test_default_prompts_reject_stale_future_date_heuristic():
    plugin = _plugin()
    assert "早于或等于当前时间的日期不是「伪造的未来日期」" in plugin.system_prompt
    assert "当前时间" in plugin.system_prompt
    assert "整句玩笑原样当搜索词" in plugin.plan_prompt
    src = inspect.getsource(IsItTrue._plan_verification)
    assert "短文本直通" not in src
