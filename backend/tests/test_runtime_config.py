from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch

import httpx
from openai import APIConnectionError

import hypoweaver.api as api_module
from hypoweaver.adapters import (
    HttpResearchExecutor,
    HttpResearchReproducer,
    ModelCallBudget,
    QwenModelGateway,
)
from hypoweaver.engine import PRESET_CASES
from hypoweaver.models import ResearchPackage, ResearchRun
from hypoweaver.runtime_config import (
    FrozenRuntimeConfigStore,
    RuntimeConfigStore,
    RuntimeConfigUpdate,
    RuntimeConnectionTestRequest,
    test_runtime_connection,
)


class RuntimeConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "runtime-config.json"
        self.store = RuntimeConfigStore(self.path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_update_writes_private_file_and_status_never_returns_secrets(self) -> None:
        status = self.store.update(
            RuntimeConfigUpdate(
                qwen_api_key="qwen-secret-value",
                qwen_model="qwen-test",
                qwen_base_url="https://example.test/v1/",
                research_engine_url="http://127.0.0.1:9000/",
                research_engine_token="engine-secret-value",
            )
        )

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertTrue(status.qwen_api_key.configured)
        self.assertTrue(status.research_engine_token.configured)
        serialized_status = status.model_dump_json()
        self.assertNotIn("qwen-secret-value", serialized_status)
        self.assertNotIn("engine-secret-value", serialized_status)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["qwen_api_key"], "qwen-secret-value")
        self.assertEqual(stored["qwen_base_url"], "https://example.test/v1")

    def test_environment_values_take_precedence_over_file(self) -> None:
        self.store.update(
            RuntimeConfigUpdate(
                qwen_api_key="file-key",
                qwen_model="file-model",
                research_engine_url="http://127.0.0.1:9000",
            )
        )
        with patch.dict(
            os.environ,
            {
                "DASHSCOPE_API_KEY": "environment-key",
                "QWEN_MODEL": "environment-model",
                "RESEARCH_ENGINE_URL": "https://engine.example.test",
            },
            clear=True,
        ):
            effective = self.store.resolve()
            status = self.store.status()

        self.assertEqual(effective.qwen_api_key, "environment-key")
        self.assertEqual(effective.qwen_model, "environment-model")
        self.assertEqual(effective.research_engine_url, "https://engine.example.test")
        self.assertEqual(status.qwen_api_key.source, "environment")
        self.assertEqual(status.qwen_model.source, "environment")
        self.assertEqual(status.research_engine_url.source, "environment")

    def test_frozen_runtime_store_keeps_one_in_memory_snapshot(self) -> None:
        self.store.update(
            RuntimeConfigUpdate(
                qwen_api_key="first-key",
                qwen_model="first-model",
                research_engine_url="http://127.0.0.1:9000",
            )
        )
        frozen = FrozenRuntimeConfigStore(self.store.resolve())
        self.store.update(
            RuntimeConfigUpdate(
                qwen_api_key="second-key",
                qwen_model="second-model",
            )
        )

        snapshot = frozen.resolve()

        self.assertEqual(snapshot.qwen_api_key, "first-key")
        self.assertEqual(snapshot.qwen_model, "first-model")

    def test_secret_and_executor_values_require_explicit_clear_flags(self) -> None:
        self.store.update(
            RuntimeConfigUpdate(
                qwen_api_key="file-key",
                research_engine_url="http://127.0.0.1:9000",
                research_engine_token="engine-token",
            )
        )
        status = self.store.update(
            RuntimeConfigUpdate(
                clear_qwen_api_key=True,
                clear_research_engine_url=True,
                clear_research_engine_token=True,
            )
        )

        self.assertFalse(status.qwen_api_key.configured)
        self.assertIsNone(status.research_engine_url.value)
        self.assertFalse(status.research_engine_token.configured)

    def test_changing_service_url_requires_resubmitting_its_secret(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.store.update(
                RuntimeConfigUpdate(
                    qwen_api_key="qwen-key",
                    qwen_base_url="https://qwen-one.example.test/v1",
                    research_engine_url="https://engine-one.example.test",
                    research_engine_token="engine-token",
                )
            )

            with self.assertRaisesRegex(ValueError, "resubmitting the Qwen API Key"):
                self.store.update(
                    RuntimeConfigUpdate(
                        qwen_base_url="https://qwen-two.example.test/v1"
                    )
                )
            qwen_status = self.store.update(
                RuntimeConfigUpdate(
                    qwen_api_key="qwen-key",
                    qwen_base_url="https://qwen-two.example.test/v1",
                )
            )
            self.assertEqual(
                qwen_status.qwen_base_url.value,
                "https://qwen-two.example.test/v1",
            )

            with self.assertRaisesRegex(
                ValueError, "resubmitting its token"
            ):
                self.store.update(
                    RuntimeConfigUpdate(
                        research_engine_url="https://engine-two.example.test"
                    )
                )
            engine_status = self.store.update(
                RuntimeConfigUpdate(
                    research_engine_url="https://engine-two.example.test",
                    research_engine_token="engine-token",
                )
            )
            self.assertEqual(
                engine_status.research_engine_url.value,
                "https://engine-two.example.test",
            )

    def test_page_cannot_redirect_environment_secrets_to_new_urls(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DASHSCOPE_API_KEY": "environment-qwen-key",
                "RESEARCH_ENGINE_TOKEN": "environment-engine-token",
                "RESEARCH_ENGINE_URL": "https://engine-one.example.test",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "QWEN_BASE_URL in the environment"):
                self.store.update(
                    RuntimeConfigUpdate(
                        qwen_api_key="replacement-key",
                        qwen_base_url="https://untrusted-qwen.example.test/v1",
                    )
                )
            with self.assertRaisesRegex(
                ValueError, "RESEARCH_ENGINE_URL in the environment"
            ):
                self.store.update(
                    RuntimeConfigUpdate(
                        research_engine_url="https://untrusted-engine.example.test",
                        research_engine_token="replacement-token",
                    )
                )

    def test_adapters_use_runtime_file_when_environment_values_are_absent(self) -> None:
        self.store.update(
            RuntimeConfigUpdate(
                qwen_api_key="file-key",
                qwen_model="file-model",
                qwen_base_url="https://qwen.example.test/v1",
                research_engine_url="http://127.0.0.1:9000",
                research_engine_token="engine-token",
            )
        )
        with patch.dict(
            os.environ,
            {"HYPOWEAVER_RUNTIME_CONFIG_PATH": str(self.path)},
            clear=True,
        ):
            gateway = QwenModelGateway()
            executor = HttpResearchExecutor()

        self.assertEqual(gateway.model, "file-model")
        self.assertEqual(str(gateway.client.base_url), "https://qwen.example.test/v1/")
        self.assertEqual(executor.url, "http://127.0.0.1:9000")
        self.assertEqual(executor.token, "engine-token")


class ResearchHttpDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_and_reproducer_transport_follow_contract_wall_time(self) -> None:
        contract = SimpleNamespace(
            budget=SimpleNamespace(max_wall_time_seconds=600),
            model_dump=lambda **_kwargs: {"contract_id": "deadline-test"},
        )
        returned_run = ResearchRun(
            research_run_id="deadline-test-run",
            case_id="deadline-test-case",
            contract_hash="deadline-test-plan",
            plan_version=1,
            execution_status="failed",
            scientific_status="invalid",
            fixture_only=False,
        )

        for adapter_type, endpoint in (
            (HttpResearchExecutor, "/v1/runs"),
            (HttpResearchReproducer, "/v1/reproductions"),
        ):
            with self.subTest(adapter=adapter_type.__name__):
                adapter = adapter_type.__new__(adapter_type)
                adapter.url = "http://127.0.0.1:9000"
                adapter.token = None
                response = SimpleNamespace(
                    raise_for_status=MagicMock(),
                    json=MagicMock(
                        return_value=returned_run.model_dump(mode="json")
                    ),
                )
                client = SimpleNamespace(
                    post=AsyncMock(return_value=response),
                )
                client_context = MagicMock()
                client_context.__aenter__ = AsyncMock(return_value=client)
                client_context.__aexit__ = AsyncMock(return_value=None)
                deadline_context = MagicMock()
                deadline_context.__aenter__ = AsyncMock(return_value=None)
                deadline_context.__aexit__ = AsyncMock(return_value=None)

                with (
                    patch(
                        "hypoweaver.adapters.httpx.AsyncClient",
                        return_value=client_context,
                    ) as async_client,
                    patch(
                        "hypoweaver.adapters.asyncio.timeout",
                        return_value=deadline_context,
                    ) as overall_timeout,
                ):
                    result = await adapter.execute(contract)

                self.assertEqual(result.research_run_id, "deadline-test-run")
                overall_timeout.assert_called_once_with(600.0)
                timeout = async_client.call_args.kwargs["timeout"]
                self.assertEqual(timeout.connect, 10.0)
                self.assertEqual(timeout.read, 600.0)
                self.assertEqual(timeout.write, 600.0)
                self.assertEqual(timeout.pool, 600.0)
                self.assertTrue(
                    client.post.await_args.args[0].endswith(endpoint)
                )


class RuntimeConfigApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "runtime-config.json"
        self.store = RuntimeConfigStore(self.path)
        self.store_patch = patch.object(api_module, "runtime_config_store", self.store)
        self.store_patch.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app),
            base_url="http://127.0.0.1",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.store_patch.stop()
        self.tempdir.cleanup()

    async def test_get_and_put_expose_status_without_returning_key(self) -> None:
        update = await self.client.put(
            "/api/v1/runtime-config",
            json={
                "qwen_api_key": "api-secret-value",
                "qwen_model": "qwen-test",
                "research_engine_url": "http://127.0.0.1:9000",
            },
        )
        self.assertEqual(update.status_code, 200)
        self.assertNotIn("api-secret-value", update.text)
        self.assertTrue(update.json()["qwen_api_key"]["configured"])

        status = await self.client.get("/api/v1/runtime-config")
        self.assertEqual(status.status_code, 200)
        self.assertNotIn("api-secret-value", status.text)
        self.assertEqual(status.json()["qwen_model"]["value"], "qwen-test")


class RuntimeConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "runtime-config.json"
        self.store = RuntimeConfigStore(self.path)
        self.store.update(
            RuntimeConfigUpdate(
                qwen_api_key="test-key",
                qwen_model="qwen-test",
                qwen_base_url="https://qwen.example.test/v1",
                research_engine_url="http://engine.example.test",
                research_engine_token="engine-token",
            )
        )

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_qwen_connection_uses_configured_credentials(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            self.assertFalse(json.loads(request.content)["enable_thinking"])
            return httpx.Response(200, json={"choices": []})

        result = await test_runtime_connection(
            RuntimeConnectionTestRequest(target="qwen"),
            self.store,
            transport=httpx.MockTransport(handler),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("test-key", result.model_dump_json())

    async def test_qwen_404_explains_model_id_case_sensitivity(self) -> None:
        result = await test_runtime_connection(
            RuntimeConnectionTestRequest(target="qwen"),
            self.store,
            transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.status_code, 404)
        self.assertIn("模型 ID", result.message)
        self.assertIn("区分大小写", result.message)

    async def test_executor_connection_uses_v1_health_contract(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/health")
            self.assertEqual(request.headers["Authorization"], "Bearer engine-token")
            return httpx.Response(200, json={"status": "ok"})

        result = await test_runtime_connection(
            RuntimeConnectionTestRequest(target="research_engine"),
            self.store,
            transport=httpx.MockTransport(handler),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.status_code, 200)

    async def test_qwen_gateway_turns_connection_drop_into_readable_runtime_error(self) -> None:
        gateway = QwenModelGateway.__new__(QwenModelGateway)
        gateway.model = "qwen-test"
        create_completion = AsyncMock(
            side_effect=APIConnectionError(
                request=httpx.Request("POST", "https://qwen.example.test/v1/chat/completions")
            )
        )
        gateway.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=create_completion
                )
            )
        )

        with self.assertRaisesRegex(RuntimeError, "连接中断或超时"):
            await gateway.generate(
                "intake",
                {"case": PRESET_CASES["green-finance-did"].model_dump(mode="json")},
                ResearchPackage,
            )
        self.assertEqual(create_completion.await_count, 3)
        self.assertEqual(gateway.budget.llm_calls, 3)
        self.assertEqual(len(gateway.budget.technical_failures), 3)
        self.assertEqual(len(gateway.budget.call_receipts), 3)
        self.assertTrue(
            all(
                receipt["provider"] == "qwen"
                and receipt["model"] == "qwen-test"
                and len(receipt["response_sha256"]) == 64
                for receipt in gateway.budget.call_receipts
            )
        )
        self.assertNotIn(
            "qwen.example.test",
            str(gateway.budget.call_receipts),
        )
        self.assertEqual(
            create_completion.await_args.kwargs["extra_body"],
            {"enable_thinking": False},
        )
        self.assertEqual(create_completion.await_args.kwargs["max_tokens"], 8192)

    async def test_qwen_gateway_blocks_before_exceeding_call_budget(self) -> None:
        gateway = QwenModelGateway.__new__(QwenModelGateway)
        gateway.model = "qwen-test"
        gateway.budget = ModelCallBudget(max_calls=1, llm_calls=1)
        create_completion = AsyncMock()
        gateway.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create_completion)
            )
        )

        with self.assertRaisesRegex(RuntimeError, "1/1"):
            await gateway.generate(
                "intake",
                {"case": PRESET_CASES["green-finance-did"].model_dump(mode="json")},
                ResearchPackage,
            )

        create_completion.assert_not_awaited()

    async def test_qwen_gateway_accepts_writer_model_override(self) -> None:
        effective = SimpleNamespace(
            qwen_api_key="test-key",
            qwen_model="qwen3.7-plus",
            qwen_base_url="https://qwen.example.test/v1",
        )
        with (
            patch("hypoweaver.adapters.RuntimeConfigStore") as store_class,
            patch("hypoweaver.adapters.AsyncOpenAI") as client_class,
        ):
            store_class.return_value.resolve.return_value = effective
            gateway = QwenModelGateway(model_override="qwen3.7-max")

        self.assertEqual(gateway.model, "qwen3.7-max")
        self.assertEqual(client_class.call_args.kwargs["api_key"], "test-key")
        self.assertEqual(
            client_class.call_args.kwargs["base_url"],
            "https://qwen.example.test/v1",
        )
        self.assertIs(client_class.call_args.kwargs["http_client"], gateway.http_client)
        self.assertEqual(client_class.call_args.kwargs["max_retries"], 0)

    def test_model_call_budget_records_provider_receipt_without_raw_content(self) -> None:
        # The enterprise-panel workflow reserves room for all nine mandatory
        # first calls. This test exercises receipt recording, not exhaustion.
        budget = ModelCallBudget(max_calls=9)
        response = SimpleNamespace(
            id="provider-response-id",
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"status":"ok"}')
                )
            ],
        )
        budget.reserve()
        budget.record_response(
            response,
            0.25,
            provider="qwen",
            model="qwen-test",
            started_at="2026-07-16T00:00:00+00:00",
            completed_at="2026-07-16T00:00:01+00:00",
        )

        snapshot = budget.snapshot()
        self.assertEqual(snapshot["llm_calls"], 1)
        self.assertEqual(snapshot["input_tokens"], 11)
        self.assertEqual(snapshot["output_tokens"], 7)
        self.assertEqual(len(snapshot["call_receipts"]), 1)
        receipt = snapshot["call_receipts"][0]
        self.assertEqual(receipt["provider"], "qwen")
        self.assertEqual(receipt["model"], "qwen-test")
        self.assertEqual(len(receipt["response_sha256"]), 64)
        self.assertNotIn("status", str(receipt))

    async def test_official_qwen_gateway_bypasses_environment_proxy(self) -> None:
        effective = SimpleNamespace(
            qwen_api_key="test-key",
            qwen_model="qwen3.7-plus",
            qwen_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        with (
            patch("hypoweaver.adapters.RuntimeConfigStore") as store_class,
            patch("hypoweaver.adapters.AsyncOpenAI"),
            patch("hypoweaver.adapters.httpx.AsyncClient") as http_client_class,
        ):
            store_class.return_value.resolve.return_value = effective
            QwenModelGateway()

        http_client_class.assert_called_once_with(trust_env=False)


if __name__ == "__main__":
    unittest.main()
