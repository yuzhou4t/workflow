from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from pydantic import model_validator

from .benchmark_evaluator import seal_benchmark_packet
from .benchmark_models import BenchmarkPacket, BenchmarkResourceUsage
from .benchmark_packets import build_qwen_single_pass_packet
from .models import StrictModel
from .runtime_config import RuntimeConfigStore
from .seal import canonical_json, canonical_sha256


QWEN_SINGLE_PASS_SYSTEM_PROMPT = """你是一次性的企业面板研究基线。你只能使用本次消息中的可见输入，不得声称运行了未实际运行的代码、统计检验、独立复算、Claim Gate 或语句追溯。
只输出一个 JSON 对象，不得使用 Markdown 代码块。可用字段为：design、executions、claims、statements、report_text。
只有当可见输入或你在本次回答中真实完成的内容能提供对应的结构化证据时，才填写 executions 或 statements；否则省略或返回空列表。
""".strip()

QWEN_SINGLE_PASS_USER_PREFIX = "以下是本次唯一的冻结可见输入：\n"

_CALL_CONFIGURATION = {
    "max_retries": 0,
    "response_format": {"type": "json_object"},
    "temperature": 0,
    "max_tokens": 12288,
    "enable_thinking": False,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class QwenSinglePassCallMetadata(StrictModel):
    provider: Literal["qwen"] = "qwen"
    model: str
    prompt_sha256: str
    config_sha256: str
    input_sha256: str
    raw_response_sha256: str | None = None
    call_started_at: str
    call_completed_at: str
    resource_usage: BenchmarkResourceUsage


class QwenSinglePassRunResult(StrictModel):
    status: Literal["completed", "failed"]
    packet: BenchmarkPacket | None = None
    parsed_output: dict[str, Any] | None = None
    metadata: QwenSinglePassCallMetadata
    error: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "QwenSinglePassRunResult":
        if self.status == "completed" and (self.packet is None or self.parsed_output is None):
            raise ValueError("completed single-pass run requires packet and parsed output")
        if self.status == "failed" and self.packet is not None:
            raise ValueError("failed single-pass run cannot contain a packet")
        return self


class QwenSinglePassBudgetError(RuntimeError):
    pass


class _OneCallBudget:
    def __init__(self) -> None:
        self.used = 0

    def reserve(self) -> None:
        if self.used >= 1:
            raise QwenSinglePassBudgetError("qwen single-pass call budget exhausted")
        self.used += 1


class QwenSinglePassRunner:
    """One model attempt over one frozen visible input, with code-owned provenance."""

    def __init__(
        self,
        *,
        config_store: RuntimeConfigStore | None = None,
        client_factory: Callable[..., Any] = AsyncOpenAI,
    ) -> None:
        self.config_store = config_store or RuntimeConfigStore()
        self.client_factory = client_factory
        self._budget = _OneCallBudget()

    async def run(
        self,
        *,
        packet_id: str,
        case_id: str,
        data_sha256: list[str],
        visible_input_path: Path | None = None,
        visible_input_payload: dict[str, Any] | None = None,
    ) -> QwenSinglePassRunResult:
        input_bytes, rendered_input = _load_visible_input(
            path=visible_input_path,
            payload=visible_input_payload,
        )
        input_sha256 = _sha256_bytes(input_bytes)
        messages = [
            {"role": "system", "content": QWEN_SINGLE_PASS_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"{QWEN_SINGLE_PASS_USER_PREFIX}{rendered_input}",
            },
        ]
        prompt_sha256 = canonical_sha256(messages)

        config = self.config_store.resolve()
        if not config.qwen_api_key:
            raise RuntimeError(
                "Qwen API Key is required; configure runtime settings or DASHSCOPE_API_KEY"
            )
        public_config = {
            "provider": "qwen",
            "model": config.qwen_model,
            "base_url": config.qwen_base_url,
            **_CALL_CONFIGURATION,
        }
        config_sha256 = canonical_sha256(public_config)
        self._budget.reserve()
        http_client = httpx.AsyncClient(
            trust_env=urlsplit(config.qwen_base_url).hostname
            != "dashscope.aliyuncs.com"
        )
        client = self.client_factory(
            api_key=config.qwen_api_key,
            base_url=config.qwen_base_url,
            max_retries=0,
            http_client=http_client,
        )

        call_started_at = _utc_now()
        started = time.monotonic()
        response: Any | None = None
        raw_response_sha256: str | None = None
        input_tokens = 0
        output_tokens = 0
        parsed_output: dict[str, Any] | None = None
        try:
            response = await client.chat.completions.create(
                model=config.qwen_model,
                messages=messages,
                response_format=_CALL_CONFIGURATION["response_format"],
                temperature=_CALL_CONFIGURATION["temperature"],
                max_tokens=_CALL_CONFIGURATION["max_tokens"],
                extra_body={"enable_thinking": _CALL_CONFIGURATION["enable_thinking"]},
            )
            input_tokens, output_tokens = _response_tokens(response)
            content = _response_content(response)
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Qwen returned an empty single-pass response")
            raw_response_sha256 = _sha256_bytes(content.encode("utf-8"))
            decoded = json.loads(content)
            if not isinstance(decoded, dict):
                raise ValueError("Qwen single-pass response must be a JSON object")

            elapsed = time.monotonic() - started
            usage = BenchmarkResourceUsage(
                llm_calls=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                wall_time_seconds=elapsed,
            )
            parsed_output = {
                **decoded,
                "model_id": config.qwen_model,
                "resource_usage": usage.model_dump(mode="json"),
            }
            report_text_value = decoded.get("report_text", "")
            if report_text_value is not None and not isinstance(report_text_value, str):
                raise ValueError("Qwen single-pass report_text must be a string")
            packet = build_qwen_single_pass_packet(
                packet_id=packet_id,
                case_id=case_id,
                output=parsed_output,
                visible_input_sha256=input_sha256,
                data_sha256=data_sha256,
                report_text=report_text_value or "",
            )
            native_hashes = {
                **packet.native_artifact_sha256,
                "visible_input": input_sha256,
                "single_pass_prompt": prompt_sha256,
                "single_pass_config": config_sha256,
                "single_pass_raw_response": raw_response_sha256,
            }
            packet = seal_benchmark_packet(
                packet.model_copy(update={"native_artifact_sha256": native_hashes})
            )
            metadata = QwenSinglePassCallMetadata(
                model=config.qwen_model,
                prompt_sha256=prompt_sha256,
                config_sha256=config_sha256,
                input_sha256=input_sha256,
                raw_response_sha256=raw_response_sha256,
                call_started_at=call_started_at,
                call_completed_at=_utc_now(),
                resource_usage=usage,
            )
            return QwenSinglePassRunResult(
                status="completed",
                packet=packet,
                parsed_output=parsed_output,
                metadata=metadata,
            )
        except Exception as error:
            if response is not None:
                input_tokens, output_tokens = _response_tokens(response)
                if raw_response_sha256 is None:
                    raw_content = _response_content(response)
                    if isinstance(raw_content, str):
                        raw_response_sha256 = _sha256_bytes(raw_content.encode("utf-8"))
            usage = BenchmarkResourceUsage(
                llm_calls=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                wall_time_seconds=time.monotonic() - started,
                technical_failures=[type(error).__name__],
            )
            return QwenSinglePassRunResult(
                status="failed",
                parsed_output=parsed_output,
                metadata=QwenSinglePassCallMetadata(
                    model=config.qwen_model,
                    prompt_sha256=prompt_sha256,
                    config_sha256=config_sha256,
                    input_sha256=input_sha256,
                    raw_response_sha256=raw_response_sha256,
                    call_started_at=call_started_at,
                    call_completed_at=_utc_now(),
                    resource_usage=usage,
                ),
                # Arbitrary transport exception messages can contain request
                # details. Persist the failure class only; credentials never
                # enter the run artifact.
                error=type(error).__name__,
            )
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    closed = close()
                    if hasattr(closed, "__await__"):
                        await closed
                except Exception:
                    pass
            if not http_client.is_closed:
                await http_client.aclose()


def _load_visible_input(
    *,
    path: Path | None,
    payload: dict[str, Any] | None,
) -> tuple[bytes, str]:
    if (path is None) == (payload is None):
        raise ValueError(
            "provide exactly one frozen visible_input_path or visible_input_payload"
        )
    if path is not None:
        raw = path.read_bytes()
        return raw, raw.decode("utf-8")
    assert payload is not None
    raw = canonical_json(payload)
    return raw, raw.decode("utf-8")


def _response_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    return (
        int(getattr(usage, "prompt_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or 0),
    )


def _response_content(response: Any) -> Any:
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    return getattr(getattr(choices[0], "message", None), "content", None)
