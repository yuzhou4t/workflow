from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


DEFAULT_QWEN_MODEL = "qwen-plus"
DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "backend" / "var" / "runtime-config.json"


def _normalized_http_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("must be an http:// or https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are not allowed; use the token field")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL cannot contain a query or fragment")
    return candidate


class StoredRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qwen_api_key: str | None = None
    qwen_model: str | None = None
    qwen_base_url: str | None = None
    research_engine_url: str | None = None
    research_engine_token: str | None = None

    @field_validator("qwen_base_url", "research_engine_url")
    @classmethod
    def validate_urls(cls, value: str | None) -> str | None:
        return _normalized_http_url(value) if value else value


class RuntimeConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qwen_api_key: SecretStr | None = None
    qwen_model: str | None = Field(default=None, min_length=1)
    qwen_base_url: str | None = Field(default=None, min_length=1)
    research_engine_url: str | None = Field(default=None, min_length=1)
    research_engine_token: SecretStr | None = None
    clear_qwen_api_key: bool = False
    clear_research_engine_token: bool = False
    clear_research_engine_url: bool = False

    @field_validator("qwen_base_url", "research_engine_url")
    @classmethod
    def validate_urls(cls, value: str | None) -> str | None:
        return _normalized_http_url(value) if value else value

    @model_validator(mode="after")
    def reject_conflicting_secret_updates(self) -> RuntimeConfigUpdate:
        if self.qwen_api_key is not None and self.clear_qwen_api_key:
            raise ValueError("qwen_api_key cannot be set and cleared together")
        if self.research_engine_token is not None and self.clear_research_engine_token:
            raise ValueError("research_engine_token cannot be set and cleared together")
        if self.research_engine_url is not None and self.clear_research_engine_url:
            raise ValueError("research_engine_url cannot be set and cleared together")
        if self.qwen_api_key is not None and not self.qwen_api_key.get_secret_value():
            raise ValueError("qwen_api_key cannot be empty")
        if (
            self.research_engine_token is not None
            and not self.research_engine_token.get_secret_value()
        ):
            raise ValueError("research_engine_token cannot be empty")
        return self


class EffectiveRuntimeConfig(BaseModel):
    qwen_api_key: str | None
    qwen_model: str
    qwen_base_url: str
    research_engine_url: str | None
    research_engine_token: str | None
    sources: dict[str, Literal["environment", "file", "default", "missing"]]


class SecretStatus(BaseModel):
    configured: bool
    source: Literal["environment", "file", "missing"]


class ValueStatus(BaseModel):
    value: str | None
    source: Literal["environment", "file", "default", "missing"]


class RuntimeConfigStatus(BaseModel):
    config_path: str
    environment_precedence: bool = True
    workflow_api_token_required: bool
    qwen_api_key: SecretStatus
    qwen_model: ValueStatus
    qwen_base_url: ValueStatus
    research_engine_url: ValueStatus
    research_engine_token: SecretStatus


class RuntimeConnectionTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["qwen", "research_engine"]


class RuntimeConnectionTestResult(BaseModel):
    target: Literal["qwen", "research_engine"]
    success: bool
    message: str
    status_code: int | None = None


class RuntimeConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        configured_path = os.getenv("HYPOWEAVER_RUNTIME_CONFIG_PATH")
        self.path = (
            Path(configured_path).expanduser()
            if path is None and configured_path
            else (path or DEFAULT_CONFIG_PATH)
        )
        self._lock = threading.Lock()

    def load(self) -> StoredRuntimeConfig:
        if not self.path.exists():
            return StoredRuntimeConfig()
        os.chmod(self.path, 0o600)
        return StoredRuntimeConfig.model_validate_json(self.path.read_text(encoding="utf-8"))

    def update(self, request: RuntimeConfigUpdate) -> RuntimeConfigStatus:
        with self._lock:
            stored = self.load()
            effective = self.resolve()
            if (
                request.qwen_base_url is not None
                and request.qwen_base_url != effective.qwen_base_url
                and effective.sources["qwen_api_key"] == "environment"
            ):
                raise ValueError(
                    "Qwen API Key comes from the environment; change QWEN_BASE_URL in the environment too"
                )
            if (
                request.qwen_base_url is not None
                and request.qwen_base_url != effective.qwen_base_url
                and effective.qwen_api_key
                and request.qwen_api_key is None
            ):
                raise ValueError(
                    "changing Qwen base URL requires resubmitting the Qwen API Key"
                )
            if (
                request.research_engine_url is not None
                and request.research_engine_url != effective.research_engine_url
                and effective.sources["research_engine_token"] == "environment"
            ):
                raise ValueError(
                    "Research Engine token comes from the environment; change RESEARCH_ENGINE_URL in the environment too"
                )
            if (
                request.research_engine_url is not None
                and request.research_engine_url != effective.research_engine_url
                and effective.research_engine_token
                and request.research_engine_token is None
            ):
                raise ValueError(
                    "changing Research Engine URL requires resubmitting its token"
                )
            updates = {
                field_name: value
                for field_name in (
                    "qwen_model",
                    "qwen_base_url",
                    "research_engine_url",
                )
                if (value := getattr(request, field_name)) is not None
            }
            if request.qwen_api_key is not None:
                updates["qwen_api_key"] = request.qwen_api_key.get_secret_value()
            if request.research_engine_token is not None:
                updates["research_engine_token"] = (
                    request.research_engine_token.get_secret_value()
                )
            if request.clear_qwen_api_key:
                updates["qwen_api_key"] = None
            if request.clear_research_engine_token:
                updates["research_engine_token"] = None
            if request.clear_research_engine_url:
                updates["research_engine_url"] = None
            updated = stored.model_copy(update=updates)
            self._write(updated)
        return self.status()

    def resolve(self) -> EffectiveRuntimeConfig:
        stored = self.load()
        values: dict[str, str | None] = {}
        sources: dict[str, Literal["environment", "file", "default", "missing"]] = {}
        defaults = {
            "qwen_api_key": None,
            "qwen_model": DEFAULT_QWEN_MODEL,
            "qwen_base_url": DEFAULT_QWEN_BASE_URL,
            "research_engine_url": None,
            "research_engine_token": None,
        }
        environment_names = {
            "qwen_api_key": "DASHSCOPE_API_KEY",
            "qwen_model": "QWEN_MODEL",
            "qwen_base_url": "QWEN_BASE_URL",
            "research_engine_url": "RESEARCH_ENGINE_URL",
            "research_engine_token": "RESEARCH_ENGINE_TOKEN",
        }
        for field_name, environment_name in environment_names.items():
            environment_value = os.getenv(environment_name)
            stored_value = getattr(stored, field_name)
            if environment_value:
                values[field_name] = environment_value
                sources[field_name] = "environment"
            elif stored_value:
                values[field_name] = stored_value
                sources[field_name] = "file"
            elif defaults[field_name] is not None:
                values[field_name] = defaults[field_name]
                sources[field_name] = "default"
            else:
                values[field_name] = None
                sources[field_name] = "missing"
        values["qwen_base_url"] = _normalized_http_url(str(values["qwen_base_url"]))
        if values["research_engine_url"]:
            values["research_engine_url"] = _normalized_http_url(
                str(values["research_engine_url"])
            )
        return EffectiveRuntimeConfig(**values, sources=sources)

    def status(self) -> RuntimeConfigStatus:
        effective = self.resolve()
        display_path = (
            "backend/var/runtime-config.json"
            if self.path.resolve() == DEFAULT_CONFIG_PATH.resolve()
            else str(self.path)
        )
        return RuntimeConfigStatus(
            config_path=display_path,
            workflow_api_token_required=bool(os.getenv("HYPOWEAVER_API_TOKEN")),
            qwen_api_key=SecretStatus(
                configured=effective.qwen_api_key is not None,
                source=effective.sources["qwen_api_key"],
            ),
            qwen_model=ValueStatus(
                value=effective.qwen_model,
                source=effective.sources["qwen_model"],
            ),
            qwen_base_url=ValueStatus(
                value=effective.qwen_base_url,
                source=effective.sources["qwen_base_url"],
            ),
            research_engine_url=ValueStatus(
                value=effective.research_engine_url,
                source=effective.sources["research_engine_url"],
            ),
            research_engine_token=SecretStatus(
                configured=effective.research_engine_token is not None,
                source=effective.sources["research_engine_token"],
            ),
        )

    def _write(self, config: StoredRuntimeConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".runtime-config-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump(config.model_dump(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()


async def test_runtime_connection(
    request: RuntimeConnectionTestRequest,
    store: RuntimeConfigStore,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RuntimeConnectionTestResult:
    config = store.resolve()
    if request.target == "qwen":
        if not config.qwen_api_key:
            return RuntimeConnectionTestResult(
                target="qwen",
                success=False,
                message="尚未配置 Qwen API Key。",
            )
        url = f"{config.qwen_base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {config.qwen_api_key}"}
        payload = {
            "model": config.qwen_model,
            "messages": [{"role": "user", "content": "Reply only: OK"}],
            "temperature": 0,
            "max_tokens": 1,
            "enable_thinking": False,
        }
        method = "POST"
    else:
        if not config.research_engine_url:
            return RuntimeConnectionTestResult(
                target="research_engine",
                success=False,
                message="尚未配置 Python Research Engine URL。",
            )
        url = f"{config.research_engine_url}/v1/health"
        headers = (
            {"Authorization": f"Bearer {config.research_engine_token}"}
            if config.research_engine_token
            else {}
        )
        payload = None
        method = "GET"

    try:
        trust_env = urlsplit(url).hostname not in {"127.0.0.1", "localhost", "::1"}
        async with httpx.AsyncClient(
            timeout=15, transport=transport, trust_env=trust_env
        ) as client:
            response = await client.request(method, url, headers=headers, json=payload)
        if response.is_success:
            return RuntimeConnectionTestResult(
                target=request.target,
                success=True,
                message=(
                    "Qwen 连接与模型调用成功。"
                    if request.target == "qwen"
                    else "Python Research Engine 健康检查成功。"
                ),
                status_code=response.status_code,
            )
        if request.target == "qwen" and response.status_code == 404:
            return RuntimeConnectionTestResult(
                target="qwen",
                success=False,
                message=(
                    f"Qwen 返回 HTTP 404：模型 ID {config.qwen_model!r} 或 API 地址不存在。"
                    "模型 ID 区分大小写，例如 qwen3.7-plus。"
                ),
                status_code=404,
            )
        return RuntimeConnectionTestResult(
            target=request.target,
            success=False,
            message=f"连接返回 HTTP {response.status_code}。",
            status_code=response.status_code,
        )
    except httpx.HTTPError:
        return RuntimeConnectionTestResult(
            target=request.target,
            success=False,
            message="连接失败，请检查地址、网络与凭据。",
        )
