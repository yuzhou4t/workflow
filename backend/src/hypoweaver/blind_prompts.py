from __future__ import annotations

from typing import Any

from .blind_models import BlindSuggestionSet


BLIND_SYSTEM_PROMPT = """你是独立盲测评估者。主研究系统已经封存，你没有权力修改其输出。
论文发表结果只是 reference，不是绝对真值；一致不自动正确，不一致也不自动错误。
固定评价六个维度：method_design、execution_reproducibility、main_result_match、
robustness_falsification、claim_calibration、failure_disclosure。
你只能为每个维度建议分数和提供可核验诊断，不得输出 overall_score。
当 fixture_only=true 时，main_result_match 必须 applicable=false 且 suggested_points=null。
其他维度 suggested_points 不得超过各自权重 20、20、15、15、10；只输出符合 Schema 的 JSON。"""

BLIND_USER_TEMPLATE = """请比较以下已封存主系统产物与隐藏参考材料。
不得改写主系统内容，不得用参考答案反向修正本次 Run。

输入 JSON：
{{input_json}}
"""


def build_app_b_definition() -> dict[str, Any]:
    return {
        "id": "app-b",
        "version": "1.0.0",
        "title": "HypoWeaver 独立盲测评估",
        "description": "独立进程读取封存产物与隐藏参考，LLM 只建议分项，代码校验并计算总分。",
        "steps": [
            {
                "id": "verify_seal",
                "title": "验证封存摘要",
                "kind": "code",
                "prompt_version": None,
                "system_prompt": "",
                "user_template": "",
                "input_schema": {"$ref": "BlindEvaluationRequest"},
                "output_schema": {"type": "object", "required": ["seal_sha256"]},
            },
            {
                "id": "blind_evaluate",
                "title": "独立六维评估",
                "kind": "llm",
                "prompt_version": "1.0.0",
                "system_prompt": BLIND_SYSTEM_PROMPT,
                "user_template": BLIND_USER_TEMPLATE,
                "input_schema": {"$ref": "BlindPromptPayload"},
                "output_schema": BlindSuggestionSet.model_json_schema(),
            },
            {
                "id": "validate_score",
                "title": "确定性评分校验",
                "kind": "code",
                "prompt_version": None,
                "system_prompt": "",
                "user_template": "",
                "input_schema": BlindSuggestionSet.model_json_schema(),
                "output_schema": {"type": "object", "required": ["overall_score"]},
            },
        ],
        "isolation": {
            "database_env": "HYPOWEAVER_BLIND_DB_PATH",
            "can_mutate_app_a": False,
            "hidden_reference_visible_to_app_a": False,
        },
    }
