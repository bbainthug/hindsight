"""LLM 提供方：OpenAI 兼容 chat/completions（任务书范围 4）。

- 密钥只从环境变量读取（config 的 ``facts.api_key_env`` 指定变量名，
  默认 ``DEEPSEEK_API_KEY``）；密钥不落盘、不进日志、不进异常消息。
- 使用标准库 urllib，不新增依赖；``response_format=json_object``
  要求模型输出 JSON 对象（DeepSeek JSON 模式）。
- 外部模型失败一律抛 ``LLMError``：由流水线转为该批次失败/跳过，
  绝不影响现有检索路径（设计 §10.4「外部 Provider 失败不影响 History」）。
- 不做无限重试：请求失败直接抛出（预算不以重试消耗，§10.4）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable


class LLMError(Exception):
    """LLM 调用或响应解析失败（不携带密钥）。"""


class LLMConfigError(Exception):
    """配置缺失（如环境变量未设置）。不回显任何密钥内容。"""


@runtime_checkable
class FactLLMClient(Protocol):
    """提炼客户端契约（测试用 Fake 实现同一接口）。"""

    provider_name: str
    model_id: str

    def complete_json(self, system: str, user: str) -> dict:
        """一次对话补全；返回解析后的 JSON 对象。"""
        ...

    @property
    def usage(self) -> tuple[int, int]:
        """(累计 prompt tokens, 累计 completion tokens)。"""
        ...


def resolve_api_key(api_key_env: str) -> str:
    """从环境变量读取密钥；缺失时给出不泄露内容的错误。"""
    value = __import__("os").environ.get(api_key_env)
    if not value:
        raise LLMConfigError(
            f"环境变量 {api_key_env} 未设置：提炼需要把（已遮蔽的）发言发给"
            f"配置的模型；密钥只从环境变量读取，不写入任何文件。"
        )
    return value


class OpenAICompatibleClient:
    """OpenAI 兼容 /chat/completions 客户端（同步、无重试）。"""

    def __init__(
        self,
        *,
        provider_name: str,
        base_url: str,
        model: str,
        api_key: str,
        temperature: float = 0.2,
        timeout_seconds: float = 120.0,
        max_output_tokens: int = 2000,
        extra_body: dict[str, object] | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/")
        self.model_id = model
        self._api_key = api_key
        self._temperature = temperature
        self._timeout = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self.extra_body = dict(extra_body) if extra_body else None
        self._prompt_tokens = 0
        self._completion_tokens = 0

    @property
    def usage(self) -> tuple[int, int]:
        return self._prompt_tokens, self._completion_tokens

    def complete_json(self, system: str, user: str) -> dict:
        payload: dict[str, object] = dict(self.extra_body or {})
        payload |= {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # 密钥只进本次请求头；不打印、不记录。
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 只暴露状态码与截断的响应体；请求头（含密钥）绝不入消息。
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise LLMError(f"LLM HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM 连接失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMError(f"LLM 请求超时（{self._timeout}s）") from exc

        try:
            usage = body.get("usage") or {}
            self._prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self._completion_tokens += int(usage.get("completion_tokens") or 0)
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"LLM 响应结构异常: {body!r:.300}") from exc
        return parse_llm_json(content)


def parse_llm_json(content: str) -> dict:
    """解析模型输出为 JSON 对象；容忍 ```json 围栏。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM 输出不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("LLM 输出不是 JSON 对象")
    return data
