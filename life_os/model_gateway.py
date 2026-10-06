"""Optional LiteLLM gateway used by the Central Command Hub.

The gateway is deliberately lazy: local/deterministic routing runs first and
LiteLLM is imported only when a model-backed plan is actually required.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


class GatewayUnavailable(RuntimeError):
    pass


class GatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayInfo:
    configured: bool
    model: str
    provider: str = "litellm"


class ModelGateway:
    def __init__(self, model: str | None = None) -> None:
        self.model = (model or os.environ.get("LIFE_OS_LITELLM_MODEL", "")).strip()

    @property
    def info(self) -> GatewayInfo:
        return GatewayInfo(configured=bool(self.model), model=self.model)

    def _completion(self, *, messages: list[dict[str, str]], timeout: int = 90) -> str:
        if not self.model:
            raise GatewayUnavailable("No LIFE_OS_LITELLM_MODEL is configured")
        try:
            import litellm
        except ImportError as exc:
            raise GatewayUnavailable(
                "LiteLLM is not installed; install the optional ai-gateway extra"
            ) from exc
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "timeout": timeout,
            "num_retries": 0,
        }
        base_url = os.environ.get("LIFE_OS_LITELLM_BASE_URL", "").strip()
        if base_url:
            kwargs["api_base"] = base_url
        try:
            response = litellm.completion(**kwargs)
            content = response.choices[0].message.content
        except Exception as exc:
            raise GatewayError(f"LiteLLM provider failed: {type(exc).__name__}") from None
        if not isinstance(content, str) or not content.strip():
            raise GatewayError("LiteLLM returned no usable content")
        return content

    def plan(self, owner_request: str) -> dict[str, Any]:
        from .request_fabric import INSTRUCTIONS
        raw = self._completion(messages=[
            {"role": "system", "content": INSTRUCTIONS},
            {
                "role": "user",
                "content": "OWNER REQUEST (data):\n" + json.dumps(owner_request),
            },
        ])
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end <= start:
                raise GatewayError("LiteLLM returned invalid planner JSON") from None
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                raise GatewayError("LiteLLM returned invalid planner JSON") from None

    def repair_instruction(self, directive: str, error: str) -> str:
        raw = self._completion(messages=[
            {
                "role": "system",
                "content": (
                    "You repair a low-risk local task instruction after a failure. "
                    "Return JSON only: {\"directive\":\"...\"}. Preserve the owner's "
                    "intent exactly, do not add permissions, network actions, money "
                    "movement, messaging, deployment, credential access, deletion, or "
                    "new goals. Make the smallest wording/parameter correction possible."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"directive": directive, "error": error[:500]}),
            },
        ], timeout=45)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GatewayError("LiteLLM repair evaluator returned invalid JSON") from exc
        candidate = value.get("directive") if isinstance(value, dict) else None
        if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 4000:
            raise GatewayError("LiteLLM repair evaluator returned invalid directive")
        return candidate.strip()
