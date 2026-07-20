from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hypoweaver.qwen_single_pass_runner import (
    QWEN_SINGLE_PASS_SYSTEM_PROMPT,
    QwenSinglePassBudgetError,
    QwenSinglePassRunner,
)
from hypoweaver.runtime_config import RuntimeConfigStore, RuntimeConfigUpdate
from hypoweaver.seal import canonical_json, canonical_sha256


class _FakeCompletions:
    def __init__(self, *, content: str | None = None, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=17, completion_tokens=23),
        )


class _FakeClient:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class QwenSinglePassRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config_store = RuntimeConfigStore(self.root / "runtime-config.json")
        with patch.dict(os.environ, {}, clear=True):
            self.config_store.update(
                RuntimeConfigUpdate(
                    qwen_api_key="single-pass-secret",
                    qwen_model="qwen-single-test",
                    qwen_base_url="https://qwen.example.test/v1",
                )
            )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_one_call_builds_honest_packet_and_seals_provenance(self) -> None:
        raw_response = json.dumps(
            {
                "design": {
                    "method_family": "panel_association",
                    "outcomes": ["y"],
                    "treatments_or_exposures": ["x"],
                },
                "claims": ["x 与 y 存在关联。"],
                "report_text": "x 与 y 存在关联。",
                "resource_usage": {"llm_calls": 99},
                "model_id": "model-supplied-value",
            },
            ensure_ascii=False,
        )
        completions = _FakeCompletions(content=raw_response)
        client = _FakeClient(completions)
        factory_calls: list[dict[str, object]] = []

        def factory(**kwargs):
            factory_calls.append(kwargs)
            return client

        runner = QwenSinglePassRunner(
            config_store=self.config_store,
            client_factory=factory,
        )
        visible_payload = {"case_id": "case-1", "research_question": "x 是否与 y 相关？"}
        with patch.dict(os.environ, {}, clear=True):
            result = await runner.run(
                packet_id="qwen-packet-1",
                case_id="case-1",
                data_sha256=["d" * 64],
                visible_input_payload=visible_payload,
            )

        self.assertEqual(result.status, "completed")
        self.assertIsNotNone(result.packet)
        packet = result.packet
        assert packet is not None
        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(factory_calls[0]["max_retries"], 0)
        self.assertEqual(factory_calls[0]["api_key"], "single-pass-secret")
        self.assertIn("http_client", factory_calls[0])
        self.assertEqual(len(completions.calls), 1)
        request = completions.calls[0]
        self.assertEqual(request["model"], "qwen-single-test")
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(request["extra_body"], {"enable_thinking": False})
        self.assertEqual(request["messages"][0]["content"], QWEN_SINGLE_PASS_SYSTEM_PROMPT)
        self.assertIn(canonical_json(visible_payload).decode("utf-8"), request["messages"][1]["content"])

        expected_input_sha = hashlib.sha256(canonical_json(visible_payload)).hexdigest()
        expected_response_sha = hashlib.sha256(raw_response.encode("utf-8")).hexdigest()
        self.assertEqual(result.metadata.input_sha256, expected_input_sha)
        self.assertEqual(result.metadata.raw_response_sha256, expected_response_sha)
        self.assertEqual(result.metadata.resource_usage.llm_calls, 1)
        self.assertEqual(result.metadata.resource_usage.input_tokens, 17)
        self.assertEqual(result.metadata.resource_usage.output_tokens, 23)
        self.assertEqual(packet.visible_input_sha256, expected_input_sha)
        self.assertEqual(packet.model_id, "qwen-single-test")
        self.assertEqual(packet.resource_usage.llm_calls, 1)
        self.assertEqual(packet.executions, [])
        self.assertEqual(packet.statements, [])
        self.assertEqual(packet.claims[0].check_ids, [])
        self.assertEqual(packet.native_artifact_sha256["single_pass_raw_response"], expected_response_sha)
        self.assertEqual(packet.native_artifact_sha256["single_pass_prompt"], result.metadata.prompt_sha256)
        self.assertEqual(packet.native_artifact_sha256["single_pass_config"], result.metadata.config_sha256)
        self.assertEqual(
            result.metadata.prompt_sha256,
            canonical_sha256(request["messages"]),
        )
        self.assertNotIn("single-pass-secret", result.model_dump_json())
        self.assertTrue(client.closed)

        with self.assertRaises(QwenSinglePassBudgetError):
            with patch.dict(os.environ, {}, clear=True):
                await runner.run(
                    packet_id="qwen-packet-2",
                    case_id="case-1",
                    data_sha256=["d" * 64],
                    visible_input_payload=visible_payload,
                )
        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(len(completions.calls), 1)

    async def test_artifact_bytes_are_the_frozen_visible_input(self) -> None:
        artifact = self.root / "visible-input.json"
        artifact.write_bytes(b'{"case_id":"case-1"}\n')
        completions = _FakeCompletions(content='{"report_text":"ok"}')
        client = _FakeClient(completions)
        runner = QwenSinglePassRunner(
            config_store=self.config_store,
            client_factory=lambda **kwargs: client,
        )

        with patch.dict(os.environ, {}, clear=True):
            result = await runner.run(
                packet_id="qwen-artifact",
                case_id="case-1",
                data_sha256=["d" * 64],
                visible_input_path=artifact,
            )

        expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.assertEqual(result.metadata.input_sha256, expected)
        self.assertEqual(result.packet.visible_input_sha256, expected)

    async def test_invalid_json_is_a_recorded_one_call_failure(self) -> None:
        raw_response = "not-json"
        completions = _FakeCompletions(content=raw_response)
        client = _FakeClient(completions)
        runner = QwenSinglePassRunner(
            config_store=self.config_store,
            client_factory=lambda **kwargs: client,
        )

        with patch.dict(os.environ, {}, clear=True):
            result = await runner.run(
                packet_id="qwen-invalid",
                case_id="case-1",
                data_sha256=["d" * 64],
                visible_input_payload={"case_id": "case-1"},
            )

        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.packet)
        self.assertEqual(result.metadata.resource_usage.llm_calls, 1)
        self.assertEqual(
            result.metadata.resource_usage.technical_failures,
            ["JSONDecodeError"],
        )
        self.assertEqual(
            result.metadata.raw_response_sha256,
            hashlib.sha256(raw_response.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(len(completions.calls), 1)

    async def test_transport_failure_is_not_retried(self) -> None:
        completions = _FakeCompletions(error=TimeoutError("timeout"))
        runner = QwenSinglePassRunner(
            config_store=self.config_store,
            client_factory=lambda **kwargs: _FakeClient(completions),
        )

        with patch.dict(os.environ, {}, clear=True):
            result = await runner.run(
                packet_id="qwen-timeout",
                case_id="case-1",
                data_sha256=["d" * 64],
                visible_input_payload={"case_id": "case-1"},
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.metadata.resource_usage.llm_calls, 1)
        self.assertEqual(result.metadata.resource_usage.technical_failures, ["TimeoutError"])
        self.assertEqual(len(completions.calls), 1)

    async def test_requires_exactly_one_input_before_creating_client(self) -> None:
        factory_calls = 0

        def factory(**kwargs):
            nonlocal factory_calls
            factory_calls += 1
            return _FakeClient(_FakeCompletions(content="{}"))

        runner = QwenSinglePassRunner(
            config_store=self.config_store,
            client_factory=factory,
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            with patch.dict(os.environ, {}, clear=True):
                await runner.run(
                    packet_id="qwen-no-input",
                    case_id="case-1",
                    data_sha256=["d" * 64],
                )
        self.assertEqual(factory_calls, 0)


if __name__ == "__main__":
    unittest.main()
