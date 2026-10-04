"""facts.extra_body：附加请求字段（如 DeepSeek 关闭默认思考模式）。"""

from __future__ import annotations

import io
import json

import pytest

from personal_brain.facts import llm as llm_mod
from personal_brain.facts.llm import OpenAICompatibleClient
from personal_brain.facts.pipeline import load_facts_config


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    def fake_urlopen(request, timeout):
        sent.append(json.loads(request.data.decode("utf-8")))
        body = {"choices": [{"message": {"content": '{"claims": []}'}}], "usage": {}}
        return _Resp(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    return sent


def _client(extra_body=None) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        provider_name="openai-compatible",
        base_url="https://example.invalid",
        model="m",
        api_key="test-key",
        extra_body=extra_body,
    )


def test_extra_body_merged_into_request(monkeypatch):
    sent = _capture(monkeypatch)
    _client({"thinking": {"type": "disabled"}}).complete_json("s", "u")
    assert sent[0]["thinking"] == {"type": "disabled"}
    assert sent[0]["model"] == "m"
    assert sent[0]["response_format"] == {"type": "json_object"}


def test_no_extra_body_keeps_request_unchanged(monkeypatch):
    sent = _capture(monkeypatch)
    _client().complete_json("s", "u")
    assert set(sent[0]) == {
        "model", "messages", "temperature", "max_tokens", "response_format", "stream",
    }


def test_config_loads_extra_body():
    cfg = load_facts_config({"extra_body": {"thinking": {"type": "disabled"}}})
    assert cfg.extra_body == {"thinking": {"type": "disabled"}}
    assert load_facts_config({}).extra_body is None


@pytest.mark.parametrize("bad", [{"model": "x"}, {"max_tokens": 99999}, ["thinking"]])
def test_config_rejects_bad_extra_body(bad):
    with pytest.raises(ValueError):
        load_facts_config({"extra_body": bad})
