from __future__ import annotations

import asyncio
import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
from pydantic import BaseModel

from .adapters import (
    FixtureExecutor,
    FixtureModelGateway,
    HttpResearchExecutor,
    HttpResearchReproducer,
    ModelCallBudget,
    ModelCallBudgetMode,
    V2_LOGICAL_CALL_BUDGET,
    V2_PROVIDER_ATTEMPT_BUDGET,
    V3_LOGICAL_CALL_BUDGET,
    V3_PROVIDER_ATTEMPT_BUDGET,
    ModelGateway,
    QwenModelGateway,
    ResearchExecutor,
    ResearchReproducer,
)
from .definition import DEFINITION_VERSION
from .case_import import CaseImportError, DatasetRegistry
from .claim_gate import (
    ClaimGateError,
    apply_claim_gate,
    code_owned_claim_shells,
    code_owned_claims_for_registry,
    validate_h3_claim_decision,
)
from .models import (
    AnalysisPlan,
    CaseSubmission,
    CandidateDesignSet,
    CandidatePlanBatch,
    ClaimLedger,
    DesignArena,
    DesignCandidate,
    DesignEnvelope,
    DesignReviewerReport,
    CriticIssue,
    CriticReport,
    DataProfile,
    DecisionRecord,
    EvidenceAssessment,
    EvidenceClaimBundle,
    EvidenceRegistry,
    FormalResearchContract,
    FULL_MANUSCRIPT_SECTION_IDS,
    GateDecisionRequest,
    ClaimGateReport,
    ManuscriptPackage,
    ManuscriptSection,
    ManuscriptSectionDraft,
    ManuscriptSectionDraftBatch,
    MethodRoute,
    ModelCallContext,
    ModelSpec,
    PlannedStep,
    PolicyDesignSpec,
    PromptContent,
    ProbeCheck,
    ProbeReport,
    ResearchPackage,
    ResearchRun,
    ReviewerReportBatch,
    ReproductionAudit,
    RevisionRequest,
    RunEvent,
    RunState,
    ScientificAudit,
    StepAttempt,
    TRACEABLE_MANUSCRIPT_SECTION_IDS,
    TestableHypotheses,
    CreateRunRequest,
    utc_now,
)
from .manuscript_ir import (
    ManuscriptIRError,
    allowed_writer_year_literals,
    audit_manuscript_ir,
    build_statement_registry,
    compile_section_draft,
    render_statement,
    reproduction_scope_disclosure,
    reproduction_scope_overclaim,
    required_statements_by_section,
    scrub_writer_numbers as scrub_manuscript_writer_numbers,
    writer_statement_catalog,
)
from .prompts import get_prompt
from .repository import RunRepository, VersionConflictError
from .reproducer import compare_panel_reproduction
from .runtime_config import RuntimeConfigStore
from .seal import canonical_sha256, sign_manifest
from .spatial import SpatialWeights, is_spatial_weights_filename
from .test_dag import (
    ENTERPRISE_PANEL_THREAT_BY_ID,
    ENTERPRISE_PANEL_REGISTRY_VERSION,
    POLICY_DID_REGISTRY_VERSION,
    THREAT_MECHANISM_INTERACTION_BOUNDARY,
    THREAT_POLICY_ENTITY_CLUSTER,
    THREAT_POLICY_INDEPENDENT_REPLICATION,
    build_evidence_registry,
    compile_enterprise_panel_test_dag,
    compile_policy_did_test_dag,
    schedule_test_dag,
    stable_claim_id,
    validate_policy_did_execution_plan,
)
from .visualization import (
    FigureBundle,
    FigureRenderer,
    FigureSource,
    FigureStage,
    LocalFigureRenderer,
    build_figure_requests,
    empty_figure_bundle,
    publication_figure_problems,
    render_figure_requests,
)


def _neutralize_limited_event_study_language(
    value: str,
    *,
    limited_or_mixed: bool,
) -> str:
    if not limited_or_mixed:
        return value
    return re.sub(
        r"事件研究(?:结果)?(?:显示|表明)(?:各期)?动态效应",
        "事件研究报告各事件期组间差异系数",
        value,
    )


class WorkflowTransitionError(RuntimeError):
    pass


async def _gather_llm_batches_to_terminal(*awaitables: Any) -> list[Any]:
    """Wait for every parallel model batch before propagating an error."""

    results = list(
        await asyncio.gather(*awaitables, return_exceptions=True)
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


PRESET_CASES: dict[str, CaseSubmission] = {
    "green-finance-did": CaseSubmission(
        case_id="green-finance-did",
        title="绿色金融试验区政策评估",
        research_question="绿色金融改革创新试验区政策是否促进企业绿色创新？",
        hypotheses=[
            {
                "hypothesis_id": "H1",
                "statement": "绿色金融改革创新试验区政策促进企业绿色创新。",
                "expected_direction": "positive",
                "mechanism": "政策通过缓解绿色项目融资约束并强化创新激励发挥作用。",
            }
        ],
        unit_of_analysis="企业—年度",
        sample_period="政策前后若干年度（由数据组最终确认）",
        data_structure_hint="panel",
        variables=[
            {"name": "firm_id", "label": "企业代码", "role": "id"},
            {"name": "year", "label": "年份", "role": "time"},
            {"name": "green_patent", "label": "绿色专利", "role": "outcome"},
            {"name": "treat_post", "label": "试验区政策处理", "role": "treatment"},
            {"name": "firm_size", "label": "企业规模", "role": "control"},
            {"name": "leverage", "label": "资产负债率", "role": "control"},
        ],
        known_policy_facts=["政策存在明确实施时间和试点地区；具体名单由案例包提供。"],
        constraints=["原论文结论与回归结果不得进入 App A。"],
    ),
    "esg-panel": CaseSubmission(
        case_id="esg-panel",
        title="ESG 与企业融资成本",
        research_question="企业 ESG 表现是否与融资成本下降相关？",
        hypotheses=[
            {
                "hypothesis_id": "H1",
                "statement": "企业 ESG 表现改善与融资成本下降相关。",
                "expected_direction": "negative",
                "mechanism": "更好的 ESG 表现降低信息不对称并改善融资环境。",
            }
        ],
        unit_of_analysis="企业—年度",
        sample_period="由数据组最终确认",
        data_structure_hint="panel",
        variables=[
            {"name": "firm_id", "label": "企业代码", "role": "id"},
            {"name": "year", "label": "年份", "role": "time"},
            {"name": "financing_cost", "label": "融资成本", "role": "outcome"},
            {"name": "esg_score", "label": "ESG 评分", "role": "exposure"},
            {"name": "firm_size", "label": "企业规模", "role": "control"},
            {"name": "roa", "label": "总资产收益率", "role": "control"},
        ],
        constraints=["本设计默认只支持受限关联表述，因果结论需要额外识别策略。"],
    ),
}


MAX_MANUSCRIPT_REPAIR_ROUNDS = 2
LEGACY_OVERBROAD_EXECUTION_WARNING = (
    "稳健性、证伪、机制和异质性步骤尚未执行，因此科学状态标记为 limited。"
)
WRITER_ESCALATION_MODEL = "qwen3.7-max"
REVIEWER_MODEL = "qwen3.7-max"
DESIGN_RETRY_MODEL = "qwen3.7-max"
DESIGN_STRATEGIES: tuple[tuple[str, str], ...] = (
    ("direct_baseline", "以目标估计量和最小可执行模型为优先，不追逐显著性。"),
    ("identification_first", "优先处理识别威胁、竞争解释与证伪条件。"),
    ("measurement_robustness", "优先处理变量口径、缺失样本与测量敏感性。"),
)
DESIGN_STRATEGY_BATCHES: tuple[tuple[str, ...], ...] = (
    ("direct_baseline", "identification_first"),
    ("measurement_robustness",),
)
REVIEWER_DIMENSION_BATCHES: tuple[tuple[str, str], ...] = (
    ("measurement", "reproducibility"),
    ("causal", "statistical"),
)
WRITER_ESCALATION_SECTION_IDS = {
    "introduction",
    "theory_hypotheses",
    "data_variables",
    "research_design",
    "empirical_results",
    "discussion_limitations",
}
WRITER_SECTION_BATCHES: tuple[tuple[str, ...], ...] = (
    (
        "introduction",
        "theory_hypotheses",
        "data_variables",
        "research_design",
    ),
    (
        "empirical_results",
        "discussion_limitations",
        "conclusion",
        "abstract",
    ),
)


MANUSCRIPT_SECTION_SPECS: tuple[dict[str, str], ...] = (
    {
        "section_id": "abstract",
        "title": "摘要",
        "target_characters": "450-650",
        "focus": "概括研究问题、数据与方法、已执行主要证据、识别边界与贡献，不引入新数字。",
        "evidence_keys": "research_context,data_profile,frozen_design,executed_evidence,authorized_claims,writing_requirements",
    },
    {
        "section_id": "introduction",
        "title": "一、引言",
        "target_characters": "650-900",
        "focus": "从研究问题的经济与治理意义切入，说明现实张力、核心问题、分析思路与本稿实际完成的工作；无文献时不宣称文献空白或创新性。",
        "evidence_keys": "research_context,authorized_claims,writing_requirements",
    },
    {
        "section_id": "theory_hypotheses",
        "title": "二、理论分析与研究假设",
        "target_characters": "650-900",
        "focus": "围绕输入中的假设和可用机制形成可证伪推理，同时陈述反向因果和遗漏变量等竞争性解释。",
        "evidence_keys": "research_context,frozen_design,writing_requirements",
    },
    {
        "section_id": "data_variables",
        "title": "三、数据、样本与变量",
        "target_characters": "650-900",
        "focus": "完整交代分析单位、样本时期、筛选规则、变量角色、定义、来源、预处理和已知数据质量情况；每个来源必须忠于输入。",
        "evidence_keys": "research_context,data_profile,frozen_design,executed_evidence,writing_requirements",
    },
    {
        "section_id": "research_design",
        "title": "四、研究设计",
        "target_characters": "650-900",
        "focus": "说明估计对象、模型形式、固定效应、标准误处理、控制变量、识别假设与冻结的后续检验。",
        "evidence_keys": "research_context,frozen_design,executed_evidence,writing_requirements",
    },
    {
        "section_id": "empirical_results",
        "title": "五、实证结果",
        "target_characters": "650-900",
        "focus": "只解释真实执行的基准结果及其范围，不引用不存在的图表，不对统计量作无基准的价值评价；明确区分已执行与尚未执行分析。",
        "evidence_keys": "research_context,executed_evidence,authorized_claims,writing_requirements",
    },
    {
        "section_id": "discussion_limitations",
        "title": "六、讨论、局限与后续检验",
        "target_characters": "650-900",
        "focus": "解释证据可能的理论含义，系统呈现测量、反向因果、时变混杂、稳健性与外部效度边界，并列出可执行的后续检验。",
        "evidence_keys": "research_context,frozen_design,executed_evidence,authorized_claims,writing_requirements",
    },
    {
        "section_id": "conclusion",
        "title": "七、结论",
        "target_characters": "450-650",
        "focus": "回答研究问题，保持 H3 授权的证据强度，概括审慎启示；后续工作优先使用冻结计划，其他方法需新设计审批，不将关联表述升格为因果结论。",
        "evidence_keys": "research_context,executed_evidence,authorized_claims,writing_requirements",
    },
)


DETERMINISTIC_SAFE_SECTION_TEXTS: dict[str, str] = {
    "abstract": (
        "本文围绕一个需要经验材料核验的企业研究问题展开，重点不是预设答案，"
        "而是把问题、可检验命题、分析范围和解释边界放进同一套可追溯结构。"
        "研究叙述先区分概念推演与观察信息，再区分设计约束与正文表达，"
        "从而让每一类陈述承担清楚而有限的功能。摘要不依据常识补写背景，"
        "也不把研究动机当作已经获得核验的事实。关于数据、执行状态和获准主张的"
        "信息均由下方核验语句原样承载，普通叙述不复写其中的方向、量值、强度或状态。"
        "方法层面强调合同约束、步骤闭环、独立复算和语句来源之间的衔接，"
        "这些安排用于说明稿件如何接受审查，而不替代对具体经验信息的判断。"
        "解释层面保持与准入结论一致的力度，区分可报告内容、仍有争议的内容和"
        "不得进入正文的内容。由此形成的贡献主要在于把研究流程中的可证伪性、"
        "可核对性与写作纪律集中呈现，读者可以沿着语句来源理解当前稿件能够回答什么，"
        "又有哪些问题仍留在现有材料之外。全文只讨论冻结案例允许的范围，"
        "不引入外部引文、额外数据或未经审批的分析路径。"
        "章节次序服务于这套核对逻辑，不能被理解为对经验强度的排序或替代准入结论。"
    ),
    "introduction": (
        "企业层面的研究问题往往同时涉及概念界定、数据口径、比较对象与解释尺度。"
        "如果这些部分没有被分别说明，研究动机很容易与经验判断混在一起，"
        "读者也难以辨认一个陈述究竟来自问题设定、分析合同，还是来自已经核验的材料。"
        "本文因此从可检验性出发组织问题：先明确研究对象和变量角色，再把核心命题"
        "写成可能被支持、被削弱或被保留的开放命题。这样的起点不要求预先接受某个方向，"
        "而是要求相反解释也能进入审查范围。研究意义由此落在判断过程的透明度上，"
        "而不是依靠未经提供的制度背景、文献空白或普遍性叙事来强化。"
        "围绕这一问题，稿件采用分层结构。理论部分负责说明命题为何值得检验以及"
        "哪些竞争性解释需要并列考虑；数据部分负责交代分析单位、字段角色和样本口径的"
        "表达边界；设计部分负责说明估计对象、比较方式和推断限制；经验部分则只接纳"
        "已经通过来源核验的语句。各部分之间的分工使概念推理不能冒充观察事实，"
        "也使运行记录不会因为写作需要而被重新解释。本文不补造输入之外的研究惯例，"
        "不引用未进入来源注册表的材料，也不把尚无依据的细节写成案例背景。"
        "最终形成的正文以可追踪性作为共同约束：需要事实支撑的地方交给核验语句，"
        "一般叙述只负责连接研究问题、设计逻辑与适用边界。这样安排既保留完整的"
        "问题意识，也为读者检查每个经验陈述预留明确入口。"
        "问题的现实含义也在这一约束下表达：正文可以说明为什么企业层面的比较值得"
        "被提出，却不借助未经核验的行业趋势或政策故事制造紧迫感。研究对象的边界、"
        "时间范围和变量含义均回到输入材料本身，未提供的内容保持未决并留待逐项核对。"
    ),
    "theory_hypotheses": (
        "理论分析把核心假设视为有待证伪的条件命题，而不是对现实状态的直接断言。"
        "在概念上，一项企业特征可能与另一项企业表现相联系，但这种联系可以对应多种"
        "解释路径。若前者代表组织选择、资源配置或信息环境的一部分，相关变化可能与"
        "企业内部决策同时出现；若两者都回应某些未被充分观察的条件，则表面联系也可能"
        "只是共同背景的投影。理论推演的作用是把这些可能性展开，而不是从推演本身"
        "决定哪一种解释已经成立。"
        "核心命题因此包含两个方向的审查。一方面，若输入假设所描述的条件性路径具有"
        "经验对应，获准信息应当能够在冻结比较中提供与命题相容的材料。另一方面，"
        "时间次序、测量误差、反向选择和时变混杂都可能给出竞争性解释；只要其中任一"
        "可能性未被现有设计充分区分，理论结论就必须保留开放性。这样的表述允许命题"
        "被削弱，也避免把未观察到的过程命名为已经得到确认的渠道。"
        "可证伪性还要求主命题与边界命题分开。主命题关注冻结变量之间的条件性对应，"
        "边界命题关注这种对应是否随预先规定的比较条件而不同。二者不能彼此替代，"
        "更不能用某一局部模式为另一命题提供自动背书。机制层面的讨论只停留在理论可能，"
        "除非存在专门获准的机制语句，否则不把任何路径写成经验事实。"
        "据此，理论部分为后续核验提供的是问题清单：核心联系是否与假设相容，"
        "竞争性解释是否留下可辨认迹象，边界条件是否需要单独判断，以及现有设计允许"
        "多强的表述。回答这些问题必须依赖冻结执行与准入程序，不能由理论文字自行完成。"
        "假设由此保留被反驳和修订的开放空间，理论完整性也不再等同于经验上的确定性或证据充分性。"
    ),
    "data_variables": (
        "数据叙述以分析单位和观察单位的对应关系为起点。企业面板材料需要同时辨认"
        "主体标识与时间标识，并说明同一主体在不同时点如何进入比较；只有这样，"
        "变量角色、样本筛选和模型口径才具有一致的解释基础。本节不把输入总行数、"
        "可用观察和具体估计所使用的观察混为同一概念，任何实际样本信息都由核验语句"
        "单独给出。"
        "变量说明按照研究问题中的功能区分核心解释项、被解释项、控制项以及仅用于"
        "诊断或边界检验的字段。名称、标签与分析角色必须以冻结材料为准，普通正文"
        "不从字段名称推测经济含义，也不为缺少说明的字段补造来源。若执行阶段需要"
        "派生字段，其身份应与原始输入字段区分；本稿不会把运行时构造写成输入数据"
        "原本已经包含的内容。"
        "样本口径的描述还应区分进入案例包的材料、通过主键与缺失检查的观察、"
        "以及特定估计实际采用的观察。重复键、单例、组内变异和字段缺失分别对应不同"
        "的数据风险，它们不能仅用一个总量概括。正文只陈述已由执行来源确认的样本事实，"
        "不自行声称完成清洗、跨库匹配、合并、缩尾或其他数据准备。"
        "测量边界同样保持可核对。若输入提供了口径风险，本节只在原有范围内说明；"
        "若没有相应材料，则不推测评级方法、数据库规则或数据提供方发生过何种变动。"
        "这种克制并不消除测量问题，而是把已知限制与未知事项分开。读者据此可以判断"
        "每个变量名称代表什么、哪些处理有运行记录、哪些事实仍不能从当前材料推出。"
        "对缺失信息的处理遵循同一原则：不以默认值补成案例事实，不以总体描述替代"
        "字段级核验，也不把一个步骤的可用样本自动套用于其他步骤或其他具体分析口径与研究对象。"
    ),
    "research_design": (
        "研究设计围绕一个明确的估计对象展开：在冻结变量、样本和比较结构下，"
        "考察核心解释项与被解释项之间可被模型刻画的条件性对应。设计说明与运行结论"
        "保持分离，本节只解释模型为何这样组织、每一项约束处理哪类威胁，以及最终文字"
        "能够采用何种解释尺度。"
        "面板结构的关键在于比较来源。主体维度的控制用于吸收不随时间变化的主体特征，"
        "时间维度的控制用于处理共同时间背景；两者承担不同功能，不能写成简单删除"
        "某类观察，也不能把主体内变化误述为主体之间差异。控制项的范围由冻结合同"
        "确定，正文不因变量在案例包中出现就声称其已经进入基准设定。"
        "推断设置需要与观察的相关结构相匹配。聚类层级、有限样本修正和可估计条件"
        "都属于执行合同的一部分，其意义在于规定不确定性如何计算，而不是保证某种"
        "判断必然可靠。任何具体运行信息仍由核验语句承担，本节不手写量值、方向或"
        "统计状态。"
        "冻结检验按角色进入有向步骤：基础诊断确认数据与模型能否进入估计，基准步骤"
        "形成主要比较，稳健性与证伪步骤检查结论对替代口径和时间结构是否敏感，"
        "边界步骤只回答其目标主张，复算能核对哪些环节则以结构化审计声明的独立范围和共享组件为准。"
        "这些角色不能事后互换，必做步骤即使失败或不可执行也要保留终态记录。"
        "因此，本设计提供的是受约束的关联性分析框架。反向选择、时变混杂和测量误差"
        "仍可能限制解释，某一检验的通过也不能自动排除全部替代说明。超出冻结范围的"
        "新方法需要另行设计与审批，不能借由正文扩展为本轮已经安排的工作。"
        "设计的充分性最终由全部必做步骤及其终态共同接受审查，而非由单一模型名称或表面完整性决定。"
    ),
    "empirical_results": (
        "本节按照证据角色而不是叙事吸引力组织经验材料。事实性内容只由获准的核验语句"
        "承载，正文不改写其中的量值、方向、统计状态或样本信息。阅读顺序先确认主分析"
        "对应的语句来源，再查看诊断、替代口径、证伪与边界记录是否对同一主张提供一致"
        "约束。不同角色的记录各自回答有限问题，不能因为出现在同一章节就被合并成"
        "更强判断。"
        "对核验语句的解释遵循三个边界。其一，观察材料所允许的力度由准入状态决定，"
        "普通文字不得越过该上限。其二，主分析与证伪记录需要共同阅读；若二者给出的"
        "约束并不完全一致，正文保留这种张力，而不是选择性删除不利分支。其三，"
        "边界或交互记录只对应自己的目标主张，不能自动替代主关联的判断。"
        "执行闭环也属于经验材料的一部分。每个冻结步骤都应有唯一终态，预算不足、"
        "依赖失败或不可执行不会让步骤从记录中消失。已完成的诊断、稳健性和证伪类别"
        "按运行来源理解，正文不会把它们写成待办事项；超出冻结计划的设想也不会被包装"
        "成本轮经验工作。复算只在结构化审计声明的范围内核对相应输出的一致性，不为主张提供"
        "额外强度。"
        "下列核验语句构成本节全部可报告的经验内容。它们由代码从获批主张和成功执行"
        "来源编译，受保护信息保持固定格式，并可沿语句标识回到对应来源。其余段落仅说明"
        "如何阅读这些语句，不补充案例事实，不评价量级是否理想，也不引用未提供的图表。"
        "正文中的经验陈述因此与执行记录逐一对应，同时保留审计所要求的保守边界与完整来源。"
        "当核验语句之间带有不同约束时，排列顺序不代表取舍顺序；相互支持与相互牵制的"
        "记录都留在同一来源链中，读者可以据此复核准入判断。"
    ),
    "discussion_limitations": (
        "讨论从可报告信息的边界出发，而不是从更强结论反推故事。获准核验语句规定"
        "当前材料能够进入正文的内容，普通叙述只分析这些内容在何种条件下可以被理解，"
        "以及哪些不确定性仍然需要保留。理论上的可能路径不等同于经验渠道，局部边界"
        "信息也不等同于主命题的普遍说明。"
        "首要限制来自识别范围。时间次序、反向选择与未充分观察的时变条件可能产生"
        "竞争性解释；现有设计即便完成预设检查，也不能把所有竞争性说明合并为已经排除。"
        "因此，讨论保持关联层面的措辞，只把证伪记录视为对解释范围的约束，不把任何"
        "单一步骤写成对内生性的彻底处理。"
        "其次是测量与样本边界。变量定义只能依照输入材料，缺少来源说明时不能从名称"
        "推测构造过程；样本流需要区分案例包、可用观察与具体估计采用的观察。若某类"
        "主体缺少组内变异、出现单例或在关键字段上缺失，其可比较性可能受到限制，"
        "但正文不在没有来源记录时断言这些情形的现实分布。外部适用性也仅限于冻结案例"
        "所覆盖的对象与时期，不能凭当前稿件延伸到其他场景。"
        "再次是检验角色的边界。替代口径用于检查定义敏感性，时间证伪用于审视时序张力，"
        "交互或机制记录只服务于相应主张，复算仅在审计声明的独立范围内服务于实现一致性。这些步骤可以"
        "共同收紧解释，却不能相互代替，也不能由某个局部状态自动升级主主张。"
        "所有冻结类别的执行状态以运行记录为准；新的分析设想若超出现有合同，需要"
        "新数据、新设计与另行审批。"
        "下方核验语句给出本节可以引用的主张边界。讨论不复写其事实内容，而是据此"
        "保持审慎：把相容性与确定性分开，把尚存风险与已经核验的事项分开，把案例内"
        "运行可靠性与跨案例科学结论分开。这样的限制说明不是结论的附属修辞，而是"
        "当前证据状态的一部分。"
    ),
    "conclusion": (
        "本文以可证伪、可复算和可追溯为共同约束，完成了对冻结企业面板问题的结构化"
        "分析。结论不重新手写经验内容，而由下方获准核验语句承担对研究问题的直接回应。"
        "这种分工确保正文收束与来源记录一致，也避免在摘要式概括中出现强度升级。"
        "从方法边界看，本轮工作把设计合同、步骤终态、独立实现与主张准入连接起来。"
        "这些环节能够说明当前运行是否忠于预设范围，却不能替代对识别限制的审慎判断。"
        "时间次序、反向选择、时变混杂、测量口径与外部适用性仍需按照现有材料保留，"
        "局部检查也只约束其对应主张。"
        "从写作边界看，所有事实性陈述均应保有语句来源，未获准内容不因行文需要进入"
        "结论。本文不把案例内的工程可靠性外推为其他方法或其他任务上的一般结论，"
        "也不把模型盲评等同于人工同行评审。若要形成新的无偏比较，需要使用未见案例、"
        "重新冻结协议并遵守一次性运行约束。"
        "因此，本节提供的是与当前证据强度相匹配的收束：能够确认的内容由核验语句给出，"
        "其余含义保持开放。新的方法、数据或研究问题需要另行设计与审批，不能作为"
        "本轮已经完成的部分写入。"
        "这一边界同时构成后续复核的起点。"
    ),
}


def _deterministic_safe_fallback_quality_problems(
    section_ids: list[str],
) -> list[str]:
    """Validate code-owned fallback prose without counting statement anchors."""

    specs_by_id = {
        spec["section_id"]: spec for spec in MANUSCRIPT_SECTION_SPECS
    }
    normalized: dict[str, str] = {}
    problems: list[str] = []
    for section_id in section_ids:
        text = DETERMINISTIC_SAFE_SECTION_TEXTS.get(section_id)
        spec = specs_by_id.get(section_id)
        if text is None or spec is None:
            problems.append(
                f"{section_id} deterministic_safe_fallback 缺少章别化安全正文"
            )
            continue
        code_owned_text = re.sub(
            r"\[\[STATEMENT:[A-Za-z0-9_.:-]+\]\]",
            "",
            text,
        )
        code_owned_text = "".join(code_owned_text.split())
        normalized[section_id] = code_owned_text
        minimum = int(spec["target_characters"].split("-", 1)[0])
        if len(code_owned_text) < minimum:
            problems.append(
                f"{section_id} deterministic_safe_fallback 代码自有正文"
                f"少于 {minimum} 字"
            )

    normalized_items = list(normalized.items())
    for index, (left_id, left_text) in enumerate(normalized_items):
        for right_id, right_text in normalized_items[index + 1 :]:
            common_length = SequenceMatcher(
                None,
                left_text,
                right_text,
                autojunk=False,
            ).find_longest_match().size
            shorter_length = min(len(left_text), len(right_text))
            repeated_filler = common_length >= 120 or (
                common_length >= 60
                and common_length * 4 >= shorter_length
            )
            if repeated_filler:
                problems.append(
                    "deterministic_safe_fallback 章节存在大段重复填充："
                    f"{left_id}/{right_id} 公共连续文本 {common_length} 字"
                )
    return problems


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _hash(value: Any) -> str:
    return canonical_sha256(_plain(value))


def _is_reviewer_issue_blocking_design(
    issue: CriticIssue,
    method_family: str,
) -> bool:
    """Calibrate Reviewer severity against the code-owned method registry."""

    if issue.status != "open" or issue.severity != "critical":
        return False
    if method_family == "policy_causal":
        # policy-did-v2 freezes its required diagnostics and falsification tests
        # before H2. Repairable Reviewer risks remain visible in the CriticReport
        # and are adjudicated by the Test DAG / Claim Gate after execution; only
        # a genuinely human-owned critical issue is a pre-execution blocker.
        # The independent reproducer re-estimates the frozen analysis from the
        # provided analysis-ready table. Missing upstream raw-data ETL logs are
        # therefore a disclosure boundary, not a reason to block an otherwise
        # executable given-input analysis at H2. Missing or unreadable analysis
        # assets are still caught by the code-owned Probe.
        if issue.threat_id == THREAT_POLICY_INDEPENDENT_REPLICATION:
            return False
        return issue.repair_type == "human_required"
    if method_family not in {"panel_association", "mechanism_boundary"}:
        return True
    return (
        issue.repair_type == "human_required"
        or issue.threat_id in ENTERPRISE_PANEL_THREAT_BY_ID
    )


_SHARED_POLICY_INVARIANT_THREATS = {
    THREAT_POLICY_ENTITY_CLUSTER,
    THREAT_POLICY_INDEPENDENT_REPLICATION,
}


def _policy_shared_invariant_signature(
    plan: AnalysisPlan,
    threat_id: str,
) -> str | None:
    """Fingerprint only the frozen specification implicated by a shared risk."""

    if plan.method_family != "policy_causal" or not plan.baseline_models:
        return None
    baseline = plan.baseline_models[0]
    payload = baseline.model_dump(mode="json")
    for field in ("step_id", "name", "rationale"):
        payload.pop(field, None)
    if threat_id == THREAT_POLICY_ENTITY_CLUSTER:
        policy_design = dict(baseline.parameters.get("policy_design", {}))
        payload = {
            "outcome": baseline.outcome,
            "treatments_or_exposures": baseline.treatments_or_exposures,
            "controls": baseline.controls,
            "fixed_effects": policy_design.get("fixed_effects"),
            "cluster_fields": policy_design.get("cluster_fields"),
            "cluster_composition": policy_design.get("cluster_composition"),
            "group_field": policy_design.get("group_field"),
            "time_field": policy_design.get("time_field"),
        }
    return _hash(payload)


def _propagate_shared_policy_reviewer_issues(
    candidate_set: CandidateDesignSet,
    reports: list[DesignReviewerReport],
) -> tuple[list[DesignReviewerReport], list[str]]:
    """Prevent candidate shopping when plans share the implicated invariant."""

    candidates = {item.candidate_id: item for item in candidate_set.candidates}
    propagated: list[str] = []
    normalized_reports: list[DesignReviewerReport] = []
    for report in reports:
        additions: dict[str, list[CriticIssue]] = {
            review.candidate_id: [] for review in report.candidate_reviews
        }
        existing_threats = {
            review.candidate_id: {
                issue.threat_id for issue in review.issues if issue.status == "open"
            }
            for review in report.candidate_reviews
        }
        for source_review in report.candidate_reviews:
            source = candidates[source_review.candidate_id]
            for issue in source_review.issues:
                if (
                    issue.status != "open"
                    or issue.threat_id not in _SHARED_POLICY_INVARIANT_THREATS
                ):
                    continue
                source_signature = _policy_shared_invariant_signature(
                    source.plan,
                    issue.threat_id,
                )
                if source_signature is None:
                    continue
                for target_review in report.candidate_reviews:
                    target_id = target_review.candidate_id
                    if (
                        target_id == source_review.candidate_id
                        or issue.threat_id in existing_threats[target_id]
                    ):
                        continue
                    target_signature = _policy_shared_invariant_signature(
                        candidates[target_id].plan,
                        issue.threat_id,
                    )
                    if target_signature != source_signature:
                        continue
                    copied = issue.model_copy(
                        update={
                            "issue_id": (
                                f"{issue.issue_id}--shared--{target_id}"
                            ),
                            "evidence": (
                                "该候选与 "
                                f"{source_review.candidate_id} 共享相同冻结规格；"
                                + issue.evidence
                            ),
                        }
                    )
                    additions[target_id].append(copied)
                    existing_threats[target_id].add(issue.threat_id)
                    propagated.append(
                        f"{issue.threat_id}:{source_review.candidate_id}->{target_id}"
                    )
        normalized_reviews = []
        for review in report.candidate_reviews:
            extra = additions[review.candidate_id]
            normalized_reviews.append(
                review.model_copy(
                    update={
                        "verdict": "revise" if extra and review.verdict == "pass" else review.verdict,
                        "issues": [*review.issues, *extra],
                        "required_follow_ups": [
                            *review.required_follow_ups,
                            *[
                                "共享冻结规格问题已传播：" + (item.threat_id or item.issue_id)
                                for item in extra
                            ],
                        ],
                    }
                )
            )
        normalized_reports.append(
            report.model_copy(update={"candidate_reviews": normalized_reviews})
        )
    return normalized_reports, propagated


def _reviewer_issue_disposition(issue: CriticIssue) -> str:
    if issue.status == "resolved":
        disposition = "resolved_before_freeze"
    elif issue.status == "accepted_risk":
        disposition = "accepted_risk"
    elif issue.threat_id:
        disposition = "delegated_to_frozen_test_dag_and_claim_gate"
    else:
        disposition = "open_unmapped_issue"
    return (
        "Reviewer issue ledger: "
        f"issue_id={issue.issue_id}; threat_id={issue.threat_id or 'none'}; "
        f"status={issue.status}; disposition={disposition}; "
        f"required_fix={issue.required_fix}"
    )


def _normalize_external_enterprise_candidate_plan(
    plan: AnalysisPlan,
    profile: DataProfile,
    package: ResearchPackage,
    execution_mode: str,
) -> AnalysisPlan:
    """Keep execution routing and entity clustering under code ownership."""

    executable = (
        execution_mode == "external"
        and plan.method_family in {"panel_association", "mechanism_boundary"}
        and plan.method_family in profile.supported_method_families
        and profile.readiness != "blocked"
        and any(item.role == "main" for item in package.dataset_refs)
    )
    if not executable:
        return plan

    entity = profile.entity_key[0] if len(profile.entity_key) == 1 else None
    baselines: list[ModelSpec] = []
    for baseline in plan.baseline_models:
        normalized = baseline
        if entity and entity in baseline.fixed_effects:
            strategy = re.sub(
                r"[\s_-]+",
                " ",
                str(baseline.standard_error_strategy or "").casefold(),
            ).strip()
            entity_label = re.sub(r"[\s_-]+", " ", entity.casefold()).strip()
            aliases = {
                f"cluster by {entity_label}",
                f"clustered by {entity_label}",
            }
            cluster_variable = baseline.parameters.get("cluster_variable")
            if strategy in aliases and (
                cluster_variable is None
                or cluster_variable == ""
                or cluster_variable == entity
            ):
                normalized = baseline.model_copy(
                    update={
                        "standard_error_strategy": (
                            "cluster_by_entity_finite_sample_correction"
                        ),
                        "parameters": {
                            **baseline.parameters,
                            "cluster_variable": entity,
                        },
                    }
                )
        baselines.append(normalized)
    return plan.model_copy(
        update={
            "design_only": False,
            "baseline_models": baselines,
        }
    )


def _visible_policy_frame(
    package: ResearchPackage,
    dataset_registry: DatasetRegistry | None,
    columns: list[str],
) -> pd.DataFrame | None:
    """Read only visible structural fields needed to freeze a policy design."""

    if dataset_registry is None:
        return None
    main_ref = next(
        (item for item in package.dataset_refs if item.role == "main"),
        None,
    )
    if main_ref is None:
        return None
    try:
        source = dataset_registry.resolve(main_ref)
        _verify_dataset_hash(source, main_ref.sha256)
        frame, _ = _read_profile_csv(source, list(dict.fromkeys(columns)))
    except (CaseImportError, OSError, ValueError):
        return None
    if set(columns) - set(frame.columns):
        return None
    return frame


def _calendar_month_tokens(value: str | None) -> list[tuple[int, int]]:
    if not value:
        return []
    tokens: list[tuple[int, int]] = []
    for year_text, month_text in re.findall(
        r"(?<!\d)(\d{4})[-/.](\d{1,2})(?!\d)",
        value,
    ):
        month = int(month_text)
        if 1 <= month <= 12:
            tokens.append((int(year_text), month))
    return tokens


def _month_ordinal(year: int, month: int) -> int:
    return year * 12 + month - 1


def _calendar_month_for_period(
    sample_period: str | None,
    observed_values: list[int],
    period_value: int,
) -> str | None:
    bounds = _calendar_month_tokens(sample_period)
    if len(bounds) < 2 or not observed_values:
        return None
    start, end = bounds[0], bounds[-1]
    expected_periods = _month_ordinal(*end) - _month_ordinal(*start) + 1
    if (
        expected_periods != len(observed_values)
        or observed_values
        != list(range(observed_values[0], observed_values[-1] + 1))
        or period_value not in observed_values
    ):
        return None
    ordinal = _month_ordinal(*start) + period_value - observed_values[0]
    return f"{ordinal // 12:04d}-{ordinal % 12 + 1:02d}"


def _period_for_calendar_month(
    sample_period: str | None,
    observed_values: list[int],
    calendar_date: str,
) -> int | None:
    tokens = _calendar_month_tokens(calendar_date)
    bounds = _calendar_month_tokens(sample_period)
    if len(tokens) != 1 or len(bounds) < 2 or not observed_values:
        return None
    start, end = bounds[0], bounds[-1]
    target = tokens[0]
    expected_periods = _month_ordinal(*end) - _month_ordinal(*start) + 1
    offset = _month_ordinal(*target) - _month_ordinal(*start)
    if (
        expected_periods != len(observed_values)
        or observed_values
        != list(range(observed_values[0], observed_values[-1] + 1))
        or not 0 <= offset < expected_periods
    ):
        return None
    return observed_values[0] + offset


def _infer_visible_policy_design(
    package: ResearchPackage,
    profile: DataProfile,
    dataset_registry: DatasetRegistry | None,
) -> PolicyDesignSpec | None:
    """Infer one unambiguous group × post contract from visible data fields.

    This audit intentionally does not read the outcome or any hidden reference.
    It only repairs a model-selected ``policy_causal`` route when the visible
    panel contains exactly one entity-invariant binary group, one common
    monotone post indicator, and one field equal to their rowwise product.
    Ambiguous cases remain design-only for model or human resolution.
    """

    if (
        dataset_registry is None
        or len(profile.entity_key) != 1
        or profile.time_key is None
    ):
        return None
    entity = profile.entity_key[0]
    time = profile.time_key
    candidate_fields = [
        item.name
        for item in package.variables
        if item.name not in {entity, time}
        and item.role in {"treatment", "exposure", "unknown"}
    ]
    frame = _visible_policy_frame(
        package,
        dataset_registry,
        [entity, time, *candidate_fields],
    )
    if frame is None:
        return None
    frame[time] = pd.to_numeric(frame[time], errors="coerce")
    frame = frame.dropna(subset=[entity, time]).copy()
    if frame.empty or frame.duplicated([entity, time]).any():
        return None
    numeric: dict[str, pd.Series] = {}
    for field in candidate_fields:
        values = pd.to_numeric(frame[field], errors="coerce")
        if values.isna().any() or set(values.unique()) != {0, 1}:
            continue
        numeric[field] = values.astype(int)

    groups = [
        field
        for field, values in numeric.items()
        if values.groupby(frame[entity], observed=True).nunique().le(1).all()
    ]
    posts: list[tuple[str, int]] = []
    ordered_times = sorted(int(value) for value in frame[time].unique())
    for field, values in numeric.items():
        by_time = values.groupby(frame[time], observed=True)
        if not by_time.nunique().le(1).all():
            continue
        sequence = by_time.first().reindex(ordered_times)
        if (
            sequence.isna().any()
            or int(sequence.iloc[0]) != 0
            or int(sequence.iloc[-1]) != 1
            or (sequence.diff().dropna() < 0).any()
            or int(sequence.diff().fillna(0).eq(1).sum()) != 1
        ):
            continue
        posts.append((field, int(sequence[sequence.eq(1)].index[0])))

    matches: list[tuple[str, str, str, int]] = []
    for group in groups:
        for post, start_value in posts:
            if post == group:
                continue
            product = numeric[group] * numeric[post]
            for exposure, values in numeric.items():
                if exposure in {group, post}:
                    continue
                if values.equals(product):
                    matches.append((group, post, exposure, start_value))
    if len(matches) != 1:
        return None
    group, _post, exposure, start_value = matches[0]
    policy_date = _calendar_month_for_period(
        package.sample_period,
        ordered_times,
        start_value,
    )
    if policy_date is None:
        return None
    return PolicyDesignSpec(
        policy_date=policy_date,
        group_field=group,
        time_field=time,
        policy_start_weight=1.0,
        exposure_name=exposure,
        fixed_effects=[entity, time],
        cluster_fields=[entity],
    )


def _resolve_policy_timeline(
    policy: PolicyDesignSpec,
    package: ResearchPackage,
    dataset_registry: DatasetRegistry | None,
) -> dict[str, Any] | None:
    """Map a calendar policy date to the declared time field without guessing.

    Calendar-year fields retain the existing annual contract. Consecutive
    period indices are accepted only when the visible sample-period bounds map
    one-to-one to their observed values.
    """

    policy_year, policy_month = (int(value) for value in policy.policy_date.split("-"))
    frame = _visible_policy_frame(
        package,
        dataset_registry,
        [policy.time_field],
    )
    observed_values: list[int] = []
    if frame is not None:
        numeric = pd.to_numeric(frame[policy.time_field], errors="coerce").dropna()
        if not numeric.empty and (numeric == numeric.astype(int)).all():
            observed_values = sorted(int(value) for value in numeric.unique())

    if not observed_values or policy_year in observed_values:
        reference = policy.event_reference_year or policy_year - 1
        events = list(
            policy.event_years
            or [
                year
                for year in range(policy_year - 5, policy_year + 7)
                if year != reference
            ]
        )
        return {
            "time_scale": "calendar_year",
            "policy_start_value": policy_year,
            "policy_start_weight": (
                policy.policy_start_weight
                if policy.policy_start_weight is not None
                else (13 - policy_month) / 12
            ),
            "event_reference_value": reference,
            "event_values": events,
            "event_remote_pre_values": list(policy.event_remote_pre_years),
            "placebo_start_value": policy.placebo_start_year or policy_year - 3,
        }

    start_value = _period_for_calendar_month(
        package.sample_period,
        observed_values,
        policy.policy_date,
    )
    if start_value is None:
        return None
    if policy.event_reference_year is not None or policy.event_years:
        return None
    reference = start_value - 1
    explicit_start = max(observed_values[0], start_value - 11)
    explicit_end = min(observed_values[-1], start_value + 11)
    events = [
        value
        for value in range(explicit_start, explicit_end + 1)
        if value != reference
    ]
    remote_pre = [
        value for value in observed_values if value < explicit_start
    ]
    placebo = (
        start_value - 12
        if start_value - 12 in observed_values
        else observed_values[0]
    )
    return {
        "time_scale": "period_index",
        "policy_start_value": start_value,
        "policy_start_weight": (
            policy.policy_start_weight
            if policy.policy_start_weight is not None
            else 1.0
        ),
        "event_reference_value": reference,
        "event_values": events,
        "event_remote_pre_values": remote_pre,
        "placebo_start_value": placebo,
    }


def _normalize_external_policy_candidate_plan(
    plan: AnalysisPlan,
    profile: DataProfile,
    package: ResearchPackage,
    execution_mode: str,
    strategy: str,
    dataset_registry: DatasetRegistry | None = None,
) -> AnalysisPlan:
    """Bind an executable DID contract without consulting outcome values.

    A strict-blind package supplies only observable policy timing and grouping
    facts.  Reproduction-aligned packages may additionally freeze the reference
    study's exposure weight, fixed effects, clustering and event map.  These
    execution-critical fields are code-owned after H1 and cannot be silently
    replaced by model prose.
    """

    policy = package.policy_design or _infer_visible_policy_design(
        package,
        profile,
        dataset_registry,
    )
    if (
        execution_mode != "external"
        or plan.method_family != "policy_causal"
        or policy is None
        or profile.readiness == "blocked"
        or not any(item.role == "main" for item in package.dataset_refs)
        or len(profile.entity_key) != 1
        or profile.time_key is None
    ):
        return plan

    policy_year, policy_month = (int(value) for value in policy.policy_date.split("-"))
    timeline = _resolve_policy_timeline(
        policy,
        package,
        dataset_registry,
    )
    if timeline is None:
        return plan.model_copy(
            update={
                "design_only": True,
                "unsupported_requested_analyses": list(
                    dict.fromkeys(
                        [
                            *plan.unsupported_requested_analyses,
                            "政策日期无法无歧义映射到冻结时间字段。",
                        ]
                    )
                ),
            }
        )
    inferred_partial_weight = float(timeline["policy_start_weight"])
    partial_weight = (
        policy.policy_start_weight
        if policy.policy_start_weight is not None
        else inferred_partial_weight
    )
    entity = profile.entity_key[0]
    time = profile.time_key
    permutation_unit = policy.permutation_unit_field or entity
    fixed_effects = list(policy.fixed_effects or [entity, time])
    cluster_fields = list(policy.cluster_fields or [entity])
    event_reference_year = int(timeline["event_reference_value"])
    event_years = list(timeline["event_values"])
    visible = {item.name for item in package.variables}
    required_contract_fields = {
        policy.group_field,
        policy.time_field,
        *fixed_effects,
        *cluster_fields,
        permutation_unit,
    }
    if not required_contract_fields.issubset(visible):
        return plan.model_copy(
            update={
                "design_only": True,
                "unsupported_requested_analyses": list(
                    dict.fromkeys(
                        [
                            *plan.unsupported_requested_analyses,
                            "政策合约引用了变量字典中不存在的固定效应或聚类字段。",
                        ]
                    )
                ),
            }
        )

    outcomes = [item.name for item in package.variables if item.role == "outcome"]
    controls = [item.name for item in package.variables if item.role == "control"]
    if not outcomes:
        return plan
    drafted_baseline = plan.baseline_models[0] if plan.baseline_models else None
    drafted_outcome = drafted_baseline.outcome if drafted_baseline is not None else None
    outcome = (
        outcomes[0]
        if package.design_envelope.benchmark_track == "reproduction_aligned"
        else drafted_outcome
        if drafted_outcome in outcomes
        else outcomes[0]
    )
    if package.design_envelope.benchmark_track == "reproduction_aligned":
        baseline_controls = controls
    elif strategy == "direct_baseline":
        baseline_controls = []
    else:
        baseline_controls = [
            item
            for item in (drafted_baseline.controls if drafted_baseline else [])
            if item in controls
        ]
    policy_contract = {
        "group_field": policy.group_field,
        "time_field": policy.time_field,
        "time_scale": timeline["time_scale"],
        "policy_start_year": timeline["policy_start_value"],
        "policy_start_month": policy_month,
        "policy_start_weight": partial_weight,
        "post_start_weight": policy.post_start_weight,
        "exposure_name": policy.exposure_name,
        "fixed_effects": fixed_effects,
        "cluster_fields": cluster_fields,
        "cluster_composition": policy.cluster_composition,
        "event_reference_year": event_reference_year,
        "event_years": event_years,
        "event_remote_pre_years": list(timeline["event_remote_pre_values"]),
        "event_term_scaling": policy.event_term_scaling,
        "placebo_start_year": timeline["placebo_start_value"],
        "placebo_repetitions": policy.placebo_repetitions or 500,
        "permutation_scheme": policy.permutation_scheme,
        "permutation_unit_field": permutation_unit,
        "random_seed": policy.random_seed if policy.random_seed is not None else 12345,
        "group_assignment_mode": "observed_time_varying",
    }
    baseline = ModelSpec(
        step_id="model_baseline",
        name="冻结政策暴露 DID 基准模型",
        priority="required",
        rationale="在查看任何结果前由政策日期、分组字段和输入视图约束冻结。",
        required_data_fields=sorted(required_contract_fields | {outcome, *baseline_controls}),
        estimator="absorbing-least-squares policy DID",
        formula=(
            f"{outcome} ~ {policy.exposure_name}"
            + (" + " + " + ".join(baseline_controls) if baseline_controls else "")
        ),
        outcome=outcome,
        treatments_or_exposures=[policy.exposure_name],
        controls=baseline_controls,
        fixed_effects=fixed_effects,
        standard_error_strategy=(
            "cluster_interaction(" + ",".join(cluster_fields) + ")"
        ),
        parameters={
            "policy_design": policy_contract,
            "drop_singletons": False,
        },
    )
    diagnostics = [
        PlannedStep(
            step_id="check-policy-support",
            name="政策组别、时点与样本支持诊断",
            priority="required",
            rationale="确认处理/对照和政策前/后均有支持，并公开分组切换、单期企业与缺失年份。",
            required_data_fields=sorted(
                {entity, time, policy.group_field, *cluster_fields}
            ),
            parameters={"policy_design": policy_contract, "check": "policy_support"},
        )
    ]
    robustness: list[PlannedStep] = [
        PlannedStep(
            step_id="check-policy-group-fixed-pre",
            name="政策前最后观测固定分组敏感性",
            priority="required",
            rationale=(
                "用每个实体最后一个严格政策前年份的组别固定全部时期，"
                "检验时变分组是否驱动结果。"
            ),
            required_data_fields=[entity, time, policy.group_field],
            parameters={
                "group_assignment_mode": "fixed_last_pre_policy",
                "policy_design": {
                    **policy_contract,
                    "group_assignment_mode": "fixed_last_pre_policy",
                },
            },
        ),
        PlannedStep(
            step_id="check-policy-group-stable-only",
            name="剔除组别切换实体敏感性",
            priority="recommended",
            rationale=(
                "剔除观察期内组别发生变化的实体；该结果仅作事后选择敏感性，"
                "不替代政策前固定分组。"
            ),
            required_data_fields=[entity, time, policy.group_field],
            parameters={
                "group_assignment_mode": "stable_entities_only",
                "policy_design": {
                    **policy_contract,
                    "group_assignment_mode": "stable_entities_only",
                },
            },
        ),
    ]
    if cluster_fields != [entity]:
        robustness.append(
            PlannedStep(
                step_id="check-policy-cluster-entity",
                name="实体层级聚类敏感性",
                priority="required",
                rationale=(
                    "基准交互聚类可能产生大量单例或使同一实体跨越多个聚类；"
                    "在冻结的实体层级重新计算聚类协方差。"
                ),
                required_data_fields=[entity],
                parameters={
                    "cluster_fields": [entity],
                    "policy_design": {
                        **policy_contract,
                        "cluster_fields": [entity],
                    },
                },
            )
        )
    alternative_outcome = next(
        (item for item in outcomes if item != outcome),
        None,
    )
    if alternative_outcome is not None:
        robustness.append(
            PlannedStep(
                step_id="check-policy-alternative-outcome",
                name="替代污染强度口径",
                priority="required" if strategy == "measurement_robustness" else "recommended",
                rationale="检验结果是否依赖单一污染强度定义。",
                required_data_fields=[alternative_outcome],
                parameters={
                    "alternative_outcome": alternative_outcome,
                    "policy_design": policy_contract,
                },
            )
        )
    falsification = [
        PlannedStep(
            step_id="check-policy-event-study",
            name="政策前动态与事件期映射",
            priority="required",
            rationale="联合检验政策前动态；不存在的年份不得创建观测或虚构系数。",
            required_data_fields=[policy.group_field, time],
            parameters={
                "policy_event_study": True,
                "policy_design": policy_contract,
            },
        ),
        PlannedStep(
            step_id="check-policy-placebo-time",
            name="伪政策时点检验",
            priority="required" if strategy == "identification_first" else "recommended",
            rationale="用预先冻结的政策前年份检验机械共同趋势。",
            required_data_fields=[policy.group_field, time],
            parameters={
                "policy_placebo": True,
                "policy_design": policy_contract,
            },
        ),
        PlannedStep(
            step_id="check-policy-permutation-placebo",
            name="分配单元标签置换安慰剂",
            priority="required",
            rationale=(
                "先用最后一个政策前年份固定组别，再按冻结分配单元重排"
                "完整组别标签；保持处理单元数量与面板行不变并计算双侧经验 p 值。"
            ),
            required_data_fields=[policy.group_field, time, permutation_unit],
            parameters={
                "policy_permutation_placebo": True,
                "repetitions": policy_contract["placebo_repetitions"],
                "random_seed": policy_contract["random_seed"],
                "scheme": policy_contract["permutation_scheme"],
                "policy_design": {
                    **policy_contract,
                    "group_assignment_mode": "fixed_last_pre_policy",
                },
            },
        ),
    ]
    replication = PlannedStep(
        step_id="check-policy-independent-replication",
        name="独立政策模型复算",
        priority="required",
        rationale="使用不同固定效应与聚类实现复算所有估计型步骤。",
        required_data_fields=baseline.required_data_fields,
        parameters={"implementation": "independent_multiway_within"},
        test_role="replication",
        required_for_admission=True,
    )
    return plan.model_copy(
        update={
            "design_only": False,
            "baseline_models": [baseline],
            "diagnostics": diagnostics,
            "robustness_tests": [*robustness, replication],
            "falsification_tests": falsification,
            "mechanism_tests": [],
            "heterogeneity_tests": [],
            "required_data_fields": sorted(
                {outcome, *baseline_controls, *required_contract_fields}
            ),
            "unsupported_requested_analyses": [],
            "check_registry_version": POLICY_DID_REGISTRY_VERSION,
        }
    )


def _comparable_checkpoint_input(
    value: Any,
    ignored_input_keys: tuple[str, ...],
) -> Any:
    payload = _plain(value)
    if not isinstance(payload, dict) or not ignored_input_keys:
        return payload
    return {
        key: item
        for key, item in payload.items()
        if key not in ignored_input_keys
    }


def _matching_step_inputs(
    state: RunState,
    node_id: str,
    input_value: dict[str, Any],
    ignored_input_keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    expected_input = _comparable_checkpoint_input(
        input_value,
        ignored_input_keys,
    )
    return [
        step.input
        for step in state.steps
        if step.node_id == node_id
        and isinstance(step.input, dict)
        and _comparable_checkpoint_input(step.input, ignored_input_keys)
        == expected_input
    ]


def _load_matching_step_checkpoint(
    state: RunState,
    node_id: str,
    input_value: dict[str, Any],
    output_model: type[BaseModel],
    *,
    ignored_input_keys: tuple[str, ...] = (),
) -> BaseModel | None:
    """Reuse only a successful output for the same run-local slot and input."""

    matching_inputs = _matching_step_inputs(
        state,
        node_id,
        input_value,
        ignored_input_keys,
    )
    for step in reversed(state.steps):
        if (
            step.node_id != node_id
            or step.status != "succeeded"
            or step.input not in matching_inputs
        ):
            continue
        try:
            return output_model.model_validate(step.output)
        except (TypeError, ValueError):
            return None
    return None


def _run_local_logical_call_id(
    state: RunState,
    node_id: str,
    input_value: dict[str, Any],
    prompt_key: str,
    *,
    ignored_input_keys: tuple[str, ...] = (),
) -> str:
    """Keep retries in one Run on one provider-attempt identity."""

    matching_inputs = [
        input_value,
        *_matching_step_inputs(
            state,
            node_id,
            input_value,
            ignored_input_keys,
        ),
    ]
    matching_hashes = {_hash(value) for value in matching_inputs}
    usage_envelope = state.artifacts.get("model_usage") or {}
    usage = usage_envelope.get("payload") or {}
    receipts = usage.get("call_receipts") or []
    node_input_hashes = {
        _hash(step.input)
        for step in state.steps
        if step.node_id == node_id and isinstance(step.input, dict)
    }
    node_prior_ids = {
        str(receipt.get("logical_call_id"))
        for receipt in receipts
        if isinstance(receipt, dict)
        and receipt.get("prompt_key") == prompt_key
        and receipt.get("input_sha256") in node_input_hashes
        and receipt.get("logical_call_id")
    }
    if len(node_prior_ids) > 1:
        raise WorkflowTransitionError(
            f"ambiguous provider-attempt identity for {node_id}"
        )
    prior_ids = {
        str(receipt.get("logical_call_id"))
        for receipt in receipts
        if isinstance(receipt, dict)
        and receipt.get("prompt_key") == prompt_key
        and receipt.get("input_sha256") in matching_hashes
        and receipt.get("logical_call_id")
    }
    if len(prior_ids) > 1:
        raise WorkflowTransitionError(
            f"ambiguous provider-attempt identity for {node_id}"
        )
    if prior_ids:
        return next(iter(prior_ids))
    if node_prior_ids:
        raise WorkflowTransitionError(
            f"checkpoint input changed for provider-attempt slot {node_id}"
        )
    return f"{state.id}:{node_id}"


def _plan_executable_fingerprint(plan: AnalysisPlan) -> str:
    """Ignore prose/IDs and compare only executable scientific choices."""

    def step_payload(step: Any) -> dict[str, Any]:
        payload = {
            "priority": step.priority,
            "required_data_fields": sorted(step.required_data_fields),
            "parameters": step.parameters,
            "threat_id": step.threat_id,
            "target_claim_ids": sorted(step.target_claim_ids),
            "test_role": step.test_role,
            "required_for_admission": step.required_for_admission,
            "is_executable": step.not_executable_reason is None,
        }
        for key in (
            "estimator",
            "formula",
            "outcome",
            "treatments_or_exposures",
            "controls",
            "fixed_effects",
            "standard_error_strategy",
        ):
            if hasattr(step, key):
                payload[key] = getattr(step, key)
        return payload

    return canonical_sha256(
        {
            "method_family": plan.method_family,
            "base_method_family": plan.base_method_family,
            "design_only": plan.design_only,
            "estimands": [step_payload(step) for step in plan.estimands],
            "sample_rules": [step_payload(step) for step in plan.sample_rules],
            "variable_construction": [
                step_payload(step) for step in plan.variable_construction
            ],
            "baseline_models": [
                step_payload(step) for step in plan.baseline_models
            ],
            "diagnostics": [step_payload(step) for step in plan.diagnostics],
            "robustness_tests": [
                step_payload(step) for step in plan.robustness_tests
            ],
            "falsification_tests": [
                step_payload(step) for step in plan.falsification_tests
            ],
            "mechanism_tests": [
                step_payload(step) for step in plan.mechanism_tests
            ],
            "heterogeneity_tests": [
                step_payload(step) for step in plan.heterogeneity_tests
            ],
            "required_data_fields": sorted(plan.required_data_fields),
        }
    )


class WorkflowEngine:
    def __init__(
        self,
        repository: RunRepository,
        dataset_registry: DatasetRegistry | None = None,
        runtime_config_store: RuntimeConfigStore | None = None,
        visualization_renderer: FigureRenderer | None = None,
        *,
        model_call_limit: int = 20,
        model_call_budget_mode: ModelCallBudgetMode = "legacy",
    ) -> None:
        if not 1 <= model_call_limit <= 20:
            raise ValueError("model_call_limit must be between one and twenty")
        if model_call_budget_mode not in {"legacy", "v2", "v3"}:
            raise ValueError("model_call_budget_mode must be legacy, v2, or v3")
        if model_call_budget_mode in {"v2", "v3"} and model_call_limit != 20:
            raise ValueError(
                f"{model_call_budget_mode} model call budget uses a frozen envelope"
            )
        self.repository = repository
        self.dataset_registry = dataset_registry or DatasetRegistry()
        self.runtime_config_store = runtime_config_store
        self.visualization_renderer = visualization_renderer
        self.model_call_limit = model_call_limit
        self.model_call_budget_mode = model_call_budget_mode
        self.model_call_provider_attempt_limit = (
            V2_PROVIDER_ATTEMPT_BUDGET
            if model_call_budget_mode == "v2"
            else (
                V3_PROVIDER_ATTEMPT_BUDGET
                if model_call_budget_mode == "v3"
                else model_call_limit
            )
        )
        self.model_call_logical_limit = (
            V2_LOGICAL_CALL_BUDGET
            if model_call_budget_mode == "v2"
            else (
                V3_LOGICAL_CALL_BUDGET
                if model_call_budget_mode == "v3"
                else None
            )
        )
        self._model_budgets: dict[str, ModelCallBudget] = {}

    def _model_budget(self, state: RunState) -> ModelCallBudget:
        existing = self._model_budgets.get(state.id)
        if existing is not None:
            return existing
        payload = self._artifact_payload(
            state, "model_usage", required=False
        ) or {}
        payload_budget_mode = payload.get(
            "budget_mode", self.model_call_budget_mode
        )
        if payload and payload_budget_mode != self.model_call_budget_mode:
            raise ValueError(
                "persisted model budget mode does not match WorkflowEngine mode"
            )
        budget = ModelCallBudget(
            max_calls=min(
                int(
                    payload.get(
                        "max_calls",
                        self.model_call_provider_attempt_limit,
                    )
                    or self.model_call_provider_attempt_limit
                ),
                self.model_call_provider_attempt_limit,
            ),
            llm_calls=int(payload.get("llm_calls", 0) or 0),
            input_tokens=int(payload.get("input_tokens", 0) or 0),
            output_tokens=int(payload.get("output_tokens", 0) or 0),
            wall_time_seconds=float(payload.get("wall_time_seconds", 0) or 0),
            technical_failures=list(payload.get("technical_failures", [])),
            call_receipts=list(payload.get("call_receipts", [])),
            budget_mode=self.model_call_budget_mode,
        )
        self._model_budgets[state.id] = budget
        return budget

    def _gateway(self, state: RunState) -> ModelGateway:
        return (
            QwenModelGateway(
                budget=self._model_budget(state),
                config_store=self.runtime_config_store,
            )
            if state.model_provider == "qwen"
            else FixtureModelGateway()
        )

    def _reviewer_gateway(self, state: RunState) -> ModelGateway:
        if state.model_provider == "qwen":
            return QwenModelGateway(
                model_override=REVIEWER_MODEL,
                budget=self._model_budget(state),
                config_store=self.runtime_config_store,
            )
        return FixtureModelGateway()

    @staticmethod
    def _event(
        state: RunState,
        event_type: str,
        message: str,
        *,
        node_id: str | None = None,
        status: str | None = None,
    ) -> None:
        state.events.append(
            RunEvent(
                seq=len(state.events) + 1,
                type=event_type,
                message=message,
                node_id=node_id,
                status=status,
            )
        )

    @staticmethod
    def _put_artifact(state: RunState, key: str, value: Any) -> dict[str, Any]:
        payload = _plain(value)
        envelope = {
            "artifact_id": f"{state.id}:{key}",
            "kind": key,
            "sha256": _hash(payload),
            "payload": payload,
        }
        state.artifacts[key] = envelope
        return envelope

    @staticmethod
    def _artifact_envelope(
        state: RunState,
        key: str,
        *,
        required: bool = True,
    ) -> dict[str, Any] | None:
        envelope = state.artifacts.get(key)
        if envelope is None:
            if required:
                raise WorkflowTransitionError(f"required artifact is missing: {key}")
            return None
        if not isinstance(envelope, dict):
            raise WorkflowTransitionError(f"artifact envelope is malformed: {key}")
        if "payload" not in envelope or "sha256" not in envelope:
            raise WorkflowTransitionError(f"artifact envelope is malformed: {key}")
        recorded_sha256 = envelope["sha256"]
        if not isinstance(recorded_sha256, str):
            raise WorkflowTransitionError(f"artifact envelope is malformed: {key}")
        try:
            actual_sha256 = _hash(envelope["payload"])
        except (TypeError, ValueError) as error:
            raise WorkflowTransitionError(
                f"artifact payload cannot be hashed: {key}"
            ) from error
        if recorded_sha256 != actual_sha256:
            raise WorkflowTransitionError(f"artifact sha256 mismatch: {key}")
        return envelope

    @staticmethod
    def _artifact_payload(
        state: RunState,
        key: str,
        *,
        required: bool = True,
    ) -> Any:
        envelope = WorkflowEngine._artifact_envelope(
            state, key, required=required
        )
        return None if envelope is None else envelope["payload"]

    @staticmethod
    def _validate_artifacts(state: RunState) -> RunState:
        for key in state.artifacts:
            WorkflowEngine._artifact_envelope(state, key)
        return state

    @staticmethod
    def _artifact(state: RunState, key: str, model: type[BaseModel]) -> Any:
        payload = WorkflowEngine._artifact_payload(state, key)
        return model.model_validate(payload)

    @staticmethod
    def _attempt_number(state: RunState, node_id: str) -> int:
        return 1 + sum(step.node_id == node_id for step in state.steps)

    def _record_step(
        self,
        state: RunState,
        node_id: str,
        status: str,
        *,
        input_value: Any = None,
        output_value: Any = None,
        prompts: list[dict[str, Any]] | None = None,
        logs: list[str] | None = None,
        error: str | None = None,
    ) -> StepAttempt:
        now = utc_now()
        step = StepAttempt(
            node_id=node_id,
            attempt=self._attempt_number(state, node_id),
            status=status,
            started_at=now,
            ended_at=None if status in ("running", "waiting_human") else now,
            prompts=[PromptContent.model_validate(prompt) for prompt in (prompts or [])],
            input=_plain(input_value),
            output=_plain(output_value),
            logs=logs or [],
            error=error,
        )
        state.steps.append(step)
        state.current_node_id = node_id
        self._event(
            state,
            f"step.{status}",
            logs[-1] if logs else f"{node_id}: {status}",
            node_id=node_id,
            status=status,
        )
        return step

    async def _render_figure_stage(
        self,
        state: RunState,
        run: ResearchRun,
        stage: FigureStage,
        *,
        approved_ledger: ClaimLedger | None = None,
        allowed_estimate_terms: set[str] | None = None,
        allow_dataset_derivation: bool = True,
    ) -> FigureBundle:
        if stage not in {"evidence", "publication"}:
            raise ValueError(f"unknown figure stage: {stage}")
        node_id = f"{stage}_visualization"
        artifact_key = f"{stage}_figure_bundle"

        if run.fixture_only or state.plan_only:
            bundle = empty_figure_bundle(
                stage,
                "Fixture 或未执行 ResearchRun 禁止生成实证图。",
            )
            self._put_artifact(state, artifact_key, bundle)
            self._record_step(
                state,
                node_id,
                "skipped",
                input_value={"research_run_id": run.research_run_id},
                output_value=bundle,
                logs=["Fixture/plan-only 边界生效；未调用内置绘图模块。"],
            )
            return bundle

        renderer = self.visualization_renderer or LocalFigureRenderer()

        envelope = self._artifact_envelope(state, "research_run")
        assert envelope is not None
        source = FigureSource(
            artifact_id=str(envelope["artifact_id"]),
            artifact_key="research_run",
            sha256=str(envelope["sha256"]),
        )
        contract = self._artifact(
            state,
            "formal_research_contract",
            FormalResearchContract,
        )
        dataset_path: Path | None = None
        dataset_source: FigureSource | None = None
        dataset_warnings: list[str] = []
        if stage == "evidence" and contract.dataset_refs and allow_dataset_derivation:
            main_ref = next(
                (item for item in contract.dataset_refs if item.role == "main"),
                contract.dataset_refs[0],
            )
            try:
                dataset_path = self.dataset_registry.resolve(main_ref)
                dataset_source = FigureSource(
                    artifact_id=f"dataset:{main_ref.dataset_id}",
                    artifact_key=main_ref.filename,
                    sha256=main_ref.sha256,
                )
            except CaseImportError as error:
                dataset_warnings.append(
                    f"描述类科研图未生成：主数据资产不可用（{error}）。"
                )
        requests, warnings = build_figure_requests(
            run,
            source,
            stage,
            approved_ledger=approved_ledger,
            allowed_estimate_terms=allowed_estimate_terms,
            contract=contract,
            dataset_path=dataset_path,
            dataset_source=dataset_source,
        )
        warnings = [*dataset_warnings, *warnings]
        if not requests:
            reason = " ".join(warnings) or "没有满足绘图配方的数据。"
            bundle = empty_figure_bundle(stage, reason)
            self._put_artifact(state, artifact_key, bundle)
            self._record_step(
                state,
                node_id,
                "skipped",
                input_value={"research_run_id": run.research_run_id},
                output_value=bundle,
                logs=["没有可验证的配方输入；未调用内置绘图模块。"],
            )
            return bundle

        bundle = await render_figure_requests(
            renderer,
            requests,
            stage,
            initial_warnings=warnings,
        )
        self._put_artifact(state, artifact_key, bundle)
        succeeded = bundle.status == "succeeded"
        self._record_step(
            state,
            node_id,
            "succeeded" if succeeded else "failed",
            input_value=requests,
            output_value=bundle,
            logs=[
                (
                    f"内置绘图模块返回 {len(bundle.figures)} 张可追溯图形。"
                    if succeeded
                    else "内置绘图模块未返回通过契约校验的图形；写作链路继续。"
                )
            ],
            error=None if succeeded else "; ".join(bundle.warnings),
        )
        return bundle

    async def _llm_step(
        self,
        state: RunState,
        node_id: str,
        prompt_key: str,
        payload: dict[str, Any],
        output_model: type[BaseModel],
        gateway: ModelGateway | None = None,
        *,
        call_context: ModelCallContext | None = None,
    ) -> BaseModel:
        prompt = get_prompt(prompt_key)
        rendered = prompt.render(payload)
        selected_gateway = gateway or self._gateway(state)
        try:
            output = await selected_gateway.generate(
                prompt_key,
                payload,
                output_model,
                call_context=call_context,
            )
        except Exception as error:
            self._record_step(
                state,
                node_id,
                "failed",
                input_value=payload,
                prompts=rendered,
                logs=[f"{prompt.title}未通过 Schema 校验或模型调用失败。"],
                error=str(error),
            )
            raise
        finally:
            if isinstance(selected_gateway, QwenModelGateway):
                self._put_artifact(
                    state,
                    "model_usage",
                    selected_gateway.budget.snapshot(),
                )
        self._record_step(
            state,
            node_id,
            "succeeded",
            input_value=payload,
            output_value=output,
            prompts=rendered,
            logs=[f"{prompt.title}完成；Prompt {prompt.version}；输出已通过 {output_model.__name__} 校验。"],
        )
        return output

    def _pause_at_gate(self, state: RunState, gate: str, input_value: Any) -> None:
        node_id = f"{gate.lower()}_gate"
        state.status = "waiting_human"
        state.current_gate = gate
        self._record_step(
            state,
            node_id,
            "waiting_human",
            input_value=input_value,
            logs=[f"{gate} 已暂停，等待服务端记录人工决定。"],
        )
        self._event(state, "gate.waiting", f"{gate} 等待人工决定。", node_id=node_id, status="waiting_human")

    def _profile(self, package: ResearchPackage) -> DataProfile:
        entity_keys = _names(package, "id")
        time_keys = _names(package, "time")
        spatial_keys = _names(package, "spatial_id")
        event_keys = _names(package, "event_date")
        has_refs = bool(package.dataset_refs)
        supported = {
            "panel": ["policy_causal", "panel_association", "mechanism_boundary"],
            "spatial_panel": ["spatial", "panel_association"],
            "event": ["market_event"],
            "time_series": ["structural_macro"],
            "cross_section": ["panel_association", "measurement_efficiency"],
            "unknown": [],
        }[package.data_structure_hint]
        if not has_refs:
            return DataProfile(
                profile_execution_status="not_executed",
                data_structure=package.data_structure_hint,
                unit_of_observation=package.unit_of_analysis,
                entity_key=entity_keys,
                time_key=time_keys[0] if time_keys else None,
                spatial_key=spatial_keys[0] if spatial_keys else None,
                event_date_key=event_keys[0] if event_keys else None,
                confirmed_facts=[
                    f"案例声明的数据结构为 {package.data_structure_hint}。",
                    f"变量字典包含 {len(package.variables)} 个字段。",
                ],
                measurement_risks=["尚未接入可执行数据资产。"],
                supported_method_families=supported,
                readiness="blocked",
                blocking_reasons=["没有可执行数据资产；仅允许形成研究计划。"],
            )

        selected_columns = list(dict.fromkeys(variable.name for variable in package.variables))
        key_columns = [*entity_keys, *time_keys]
        try:
            main_ref = next(
                (item for item in package.dataset_refs if item.role == "main"),
                package.dataset_refs[0],
            )
            source = self.dataset_registry.resolve(main_ref)
            _verify_dataset_hash(source, main_ref.sha256)
            frame, column_count = _read_profile_csv(source, selected_columns)
        except (CaseImportError, OSError, ValueError) as error:
            return DataProfile(
                profile_execution_status="failed",
                data_structure=package.data_structure_hint,
                unit_of_observation=package.unit_of_analysis,
                entity_key=entity_keys,
                time_key=time_keys[0] if time_keys else None,
                spatial_key=spatial_keys[0] if spatial_keys else None,
                event_date_key=event_keys[0] if event_keys else None,
                confirmed_facts=[f"变量字典包含 {len(package.variables)} 个字段。"],
                measurement_risks=[f"数据画像读取失败：{error}"],
                supported_method_families=supported,
                readiness="blocked",
                blocking_reasons=["实际数据无法在 H2 前完成确定性画像。"],
            )

        selected_time_key = time_keys[0] if time_keys else None
        if entity_keys and time_keys:
            unique_time_keys = [
                name
                for name in time_keys
                if name in frame.columns
                and not frame.duplicated(
                    subset=[*entity_keys, name],
                    keep=False,
                ).any()
            ]
            if unique_time_keys:
                selected_time_key = max(
                    unique_time_keys,
                    key=lambda name: int(frame[name].nunique(dropna=True)),
                )
        missing_columns = [name for name in selected_columns if name not in frame.columns]
        key_columns = [
            *entity_keys,
            *([selected_time_key] if selected_time_key else []),
        ]
        missingness = [
            {
                "variable": name,
                "missing_count": int(frame[name].isna().sum()),
                "missing_rate": float(frame[name].isna().mean()) if len(frame) else 0.0,
            }
            for name in selected_columns
            if name in frame.columns
        ]
        duplicate_key_count = (
            int(frame.duplicated(subset=key_columns, keep=False).sum())
            if key_columns and all(name in frame.columns for name in key_columns)
            else None
        )
        risks: list[str] = []
        if missing_columns:
            risks.append("变量字典字段未出现在数据中：" + "、".join(missing_columns))
        if duplicate_key_count:
            risks.append(f"发现 {duplicate_key_count} 行处于重复实体—时间主键中，执行前必须按冻结规则处理。")
        variables_with_missing = [
            item["variable"] for item in missingness if item["missing_count"]
        ]
        if variables_with_missing:
            risks.append("以下建模字段存在缺失值：" + "、".join(variables_with_missing))
        missing_definitions = [
            variable.name for variable in package.variables if not variable.definition
        ]
        if missing_definitions:
            risks.append("以下变量缺少定义：" + "、".join(missing_definitions))

        blocking_reasons: list[str] = []
        if missing_columns:
            blocking_reasons.append("冻结计划所需字段与实际数据表不一致。")
        if package.data_structure_hint in {"panel", "spatial_panel"} and (
            not entity_keys or not time_keys
        ):
            blocking_reasons.append("面板数据缺少实体或时间主键。")

        spatial_facts: list[str] = []
        if package.data_structure_hint == "spatial_panel":
            if not spatial_keys:
                blocking_reasons.append("空间面板缺少 spatial_id 字段。")
            weights_ref = next(
                (
                    item
                    for item in package.dataset_refs
                    if item.role == "supplementary"
                    and is_spatial_weights_filename(item.filename)
                ),
                None,
            )
            if weights_ref is None:
                blocking_reasons.append("空间面板缺少 spatial_weights.csv 权重资产。")
            elif spatial_keys and spatial_keys[0] in frame.columns:
                try:
                    weights_path = self.dataset_registry.resolve(weights_ref)
                    _verify_dataset_hash(weights_path, weights_ref.sha256)
                    weights = SpatialWeights.from_csv(weights_path)
                    weights.aligned(
                        sorted(frame[spatial_keys[0]].dropna().astype(str).unique())
                    )
                    spatial_facts.extend(
                        [
                            f"空间权重矩阵包含 {len(weights.labels)} 个唯一空间单元。",
                            "空间权重矩阵行列标签一致、对角线为 0、行和为 1。",
                            f"空间权重资产 SHA256 已核验：{weights_ref.sha256}。",
                        ]
                    )
                except (CaseImportError, OSError, ValueError) as error:
                    blocking_reasons.append(f"空间权重资产校验失败：{error}")

        return DataProfile(
            profile_execution_status="succeeded",
            data_structure=package.data_structure_hint,
            unit_of_observation=package.unit_of_analysis,
            entity_key=entity_keys,
            time_key=selected_time_key,
            spatial_key=spatial_keys[0] if spatial_keys else None,
            event_date_key=event_keys[0] if event_keys else None,
            row_count=len(frame),
            column_count=column_count,
            duplicate_key_count=duplicate_key_count,
            missingness=missingness,
            confirmed_facts=[
                f"案例声明的数据结构为 {package.data_structure_hint}。",
                f"变量字典包含 {len(package.variables)} 个字段。",
                f"实际 CSV 共 {len(frame)} 行、{column_count} 列。",
                *spatial_facts,
            ],
            measurement_risks=risks,
            merge_risks=[],
            supported_method_families=supported,
            unsupported_method_families=[],
            readiness=(
                "blocked"
                if blocking_reasons
                else ("partially_ready" if risks else "ready")
            ),
            blocking_reasons=blocking_reasons,
        )

    def _normalize_case(self, state: RunState, case: CaseSubmission) -> ResearchPackage:
        package = ResearchPackage(
            **case.model_dump(),
            input_conflicts=[],
            missing_required_information=(
                [] if case.dataset_refs else ["尚未接入可执行数据资产；本次只能形成研究设计。"]
            ),
        )
        self._record_step(
            state,
            "intake_agent",
            "succeeded",
            input_value={"case": case.model_dump(mode="json")},
            output_value=package,
            prompts=[
                {
                    "id": "intake:code",
                    "role": "code",
                    "template": "CaseSubmission → ResearchPackage deterministic normalization",
                    "rendered": "H1 前不调用外部模型；只按严格 Schema 规范化用户可见输入。",
                }
            ],
            logs=["案例规范化由确定性代码完成；H1 前未调用千问。"],
        )
        return package

    async def create_run(self, request: CreateRunRequest) -> RunState:
        if request.preset_case_id:
            try:
                case = PRESET_CASES[request.preset_case_id]
            except KeyError as error:
                raise ValueError(f"unknown preset case: {request.preset_case_id}") from error
        elif request.case:
            case = request.case
        else:
            raise ValueError("preset_case_id or case is required")

        provider = request.model_provider or ("qwen" if request.mode == "research" else "fixture")
        execution_mode = request.execution_mode or ("external" if request.mode == "research" else "fixture")
        state = RunState(
            definition_version=DEFINITION_VERSION,
            case_id=case.case_id,
            case_name=case.title,
            mode=request.mode,
            model_provider=provider,
            execution_mode=execution_mode,
            status="running",
            current_node_id="case_input",
            case_submission=case,
        )
        self._event(state, "run.created", "代码工作流 Run 已创建。", node_id="case_input")
        self._record_step(
            state,
            "case_input",
            "succeeded",
            output_value=case,
            logs=["输入已通过 CaseSubmission 严格 Schema；额外隐藏字段会被拒绝。"],
        )
        package = self._normalize_case(state, case)
        self._put_artifact(state, "research_package", package)
        validation = {
            "valid": not package.input_conflicts,
            "errors": package.input_conflicts,
            "warnings": package.missing_required_information,
            "hidden_reference_access": "denied_by_schema",
        }
        self._record_step(
            state,
            "input_validation",
            "succeeded" if validation["valid"] else "blocked",
            input_value=package,
            output_value=validation,
            prompts=[
                {
                    "id": "input_validation:code",
                    "role": "code",
                    "template": "Pydantic strict schema + deterministic research-boundary rules",
                    "rendered": "No LLM call; hidden reference fields are rejected before persistence.",
                }
            ],
            logs=["确定性输入校验完成；App A 未读取任何隐藏参考材料。"],
        )
        if not validation["valid"]:
            state.status = "blocked"
            state.last_error = "输入存在冲突，需要人工修订。"
        else:
            self._pause_at_gate(state, "H1", package)
        return self.repository.create(state)

    def get_run(self, run_id: str) -> RunState:
        return self._validate_artifacts(self.repository.get(run_id))

    def list_runs(self) -> list[RunState]:
        return [self._validate_artifacts(state) for state in self.repository.list()]

    def delete_run(self, run_id: str) -> None:
        self.repository.delete(run_id)

    async def advance(self, run_id: str) -> RunState:
        state = self.get_run(run_id)
        if state.status == "waiting_human":
            return state
        if (
            state.status == "failed"
            and state.current_node_id == "scientific_writer"
            and "approved_claim_ledger" in state.artifacts
            and "research_run" in state.artifacts
        ):
            return await self.retry_writing(run_id)
        if state.status in ("completed", "stopped"):
            return state
        raise WorkflowTransitionError(
            "该 Run 不能无条件继续；请在当前人工闸门作决定，或修订导致阻塞的输入。"
        )

    async def retry_writing(self, run_id: str) -> RunState:
        state = self.get_run(run_id)
        if state.mode != "research" or state.plan_only:
            raise WorkflowTransitionError("只有已有真实执行结果的研究 Run 可以重试论文写作。")
        if "approved_claim_ledger" not in state.artifacts or "research_run" not in state.artifacts:
            raise WorkflowTransitionError("缺少 H3 授权结论或 ResearchRun，不能重试论文写作。")
        failed_writer = (
            state.status == "failed"
            and state.current_node_id == "scientific_writer"
        )
        completed_draft = (
            state.status == "completed"
            and "manuscript_package" in state.artifacts
        )
        waiting_h4 = state.status == "waiting_human" and state.current_gate == "H4"
        if not failed_writer and not completed_draft and not waiting_h4:
            raise WorkflowTransitionError("当前 Run 不在可重试的论文写作状态。")

        current_version = state.version
        transition_key = f"retry-writer-{uuid4()}"
        self.repository.claim_transition(
            run_id,
            expected_version=current_version,
            idempotency_key=transition_key,
        )
        try:
            human_review_feedback = None
            if (
                state.decisions
                and state.decisions[-1].gate == "H4"
                and state.decisions[-1].action == "revise"
            ):
                h4_decision = state.decisions[-1]
                feedback_already_attempted = any(
                    step.node_id == "scientific_writer"
                    and bool(step.started_at)
                    and step.started_at >= h4_decision.created_at
                    for step in state.steps
                )
                if not feedback_already_attempted:
                    human_review_feedback = h4_decision.comment.strip() or None
            existing_payload = self._artifact_payload(
                state, "manuscript_package", required=False
            )
            previous_version = int(
                (existing_payload or {}).get("version", 0) or 0
            )
            existing_sections: list[ManuscriptSection] | None = None
            if existing_payload:
                existing_sections = ManuscriptPackage.model_validate(
                    existing_payload
                ).manuscript_sections
            latest_generated: dict[str, ManuscriptSection] = {}
            last_completed_step = max(
                (
                    index
                    for index, step in enumerate(state.steps)
                    if step.node_id == "complete" and step.status == "succeeded"
                ),
                default=-1,
            )
            failed_draft_steps = (
                state.steps[last_completed_step + 1:]
                if failed_writer
                else []
            )
            for step in failed_draft_steps:
                if step.node_id != "scientific_writer" or step.status != "succeeded":
                    continue
                try:
                    section = ManuscriptSection.model_validate(step.output)
                except (TypeError, ValueError):
                    continue
                latest_generated[section.section_id] = section
            if latest_generated:
                sections_by_id = {
                    section.section_id: section
                    for section in (existing_sections or [])
                }
                sections_by_id.update(latest_generated)
                existing_sections = [
                    sections_by_id[section_id]
                    for section_id in FULL_MANUSCRIPT_SECTION_IDS
                    if section_id in sections_by_id
                ]
            state.status = "running"
            state.current_node_id = "scientific_writer"
            state.current_gate = None
            state.last_error = None
            package = self._artifact(state, "research_package", ResearchPackage)
            plan = self._artifact(state, "analysis_plan", AnalysisPlan)
            research_run = self._artifact(state, "research_run", ResearchRun)
            ledger = self._artifact(state, "approved_claim_ledger", ClaimLedger)
            approved_claims = [
                claim.model_dump(mode="json")
                for claim in ledger.claims
                if claim.approval_status in ("approved", "downgraded")
            ]
            try:
                await self._finalize_manuscript(
                    state,
                    package,
                    plan,
                    research_run,
                    approved_claims,
                    manuscript_version=previous_version + 1,
                    existing_sections=existing_sections,
                    reuse_existing_if_valid=failed_writer,
                    human_review_feedback=human_review_feedback,
                )
            except Exception as error:
                state.status = "failed"
                state.current_node_id = "scientific_writer"
                state.last_error = str(error)
                self._event(
                    state,
                    "run.failed",
                    f"论文写作失败：{error}",
                    node_id="scientific_writer",
                    status="failed",
                )
            return self.repository.save(state, expected_version=current_version)
        finally:
            self.repository.release_transition(run_id, transition_key)

    async def retry_design(self, run_id: str) -> RunState:
        state = self.get_run(run_id)
        if (
            state.status != "failed"
            or state.current_node_id is None
            or not (
                state.current_node_id.startswith("design_")
                or state.current_node_id.startswith("critic_")
            )
        ):
            raise WorkflowTransitionError(
                "只有候选设计或 Reviewer 失败的 Run 可以重试设计阶段。"
            )
        required_artifacts = (
            "research_package",
            "testable_hypotheses",
            "data_profile",
            "method_route",
            "design_envelope",
        )
        if any(key not in state.artifacts for key in required_artifacts):
            raise WorkflowTransitionError("设计阶段恢复所需 Artifact 不完整。")
        if state.model_provider == "qwen":
            usage = self._artifact_payload(
                state,
                "model_usage",
                required=False,
            ) or {}
            receipts = usage.get("call_receipts") or []
            if int(usage.get("llm_calls", 0) or 0) != len(receipts):
                raise WorkflowTransitionError(
                    "设计阶段恢复被拒绝：模型调用与 receipt 数量不一致。"
                )

        current_version = state.version
        transition_key = f"retry-design-{uuid4()}"
        self.repository.claim_transition(
            run_id,
            expected_version=current_version,
            idempotency_key=transition_key,
        )
        try:
            state.status = "running"
            state.last_error = None
            package = self._artifact(state, "research_package", ResearchPackage)
            hypotheses = self._artifact(
                state, "testable_hypotheses", TestableHypotheses
            )
            profile = self._artifact(state, "data_profile", DataProfile)
            route = self._artifact(state, "method_route", MethodRoute)
            envelope = self._artifact(state, "design_envelope", DesignEnvelope)
            selected_node = f"design_{route.primary_route}"
            try:
                if "candidate_design_set" in state.artifacts:
                    candidate_set = self._artifact(
                        state, "candidate_design_set", CandidateDesignSet
                    )
                else:
                    retry_gateway = (
                        QwenModelGateway(
                            model_override=DESIGN_RETRY_MODEL,
                            budget=self._model_budget(state),
                            config_store=self.runtime_config_store,
                        )
                        if state.model_provider == "qwen"
                        else None
                    )
                    candidate_set = await self._generate_design_candidates(
                        state,
                        selected_node,
                        package,
                        hypotheses,
                        profile,
                        route,
                        envelope,
                        gateway=retry_gateway,
                    )
                    self._put_artifact(
                        state, "candidate_design_set", candidate_set
                    )
                await self._review_design_arena(
                    state,
                    package,
                    profile,
                    route,
                    envelope,
                    candidate_set,
                )
            except Exception as error:
                state.status = "failed"
                state.last_error = str(error)
                self._event(
                    state,
                    "run.failed",
                    f"设计阶段恢复失败：{error}",
                    node_id=state.current_node_id,
                    status="failed",
                )
            return self.repository.save(state, expected_version=current_version)
        finally:
            self.repository.release_transition(run_id, transition_key)

    @staticmethod
    def _has_quality_manuscript(state: RunState) -> bool:
        payload = WorkflowEngine._artifact_payload(
            state, "manuscript_package", required=False
        )
        if not payload:
            return False
        try:
            manuscript = ManuscriptPackage.model_validate(payload)
        except (ValueError, TypeError):
            return False
        return (
            manuscript.mode
            in {"full_manuscript", "identification_failure_report"}
            and manuscript.audit_result == "pass_with_no_critical_issues"
        )

    async def decide_gate(
        self, run_id: str, gate: str, request: GateDecisionRequest
    ) -> RunState:
        state = self.get_run(run_id)
        if request.idempotency_key in state.processed_idempotency_keys:
            return state
        expected = request.expected_run_version
        if expected is not None and expected != state.version:
            raise VersionConflictError(
                f"run {run_id} changed; expected version {expected}, actual {state.version}"
            )
        normalized_gate = gate.upper()
        if normalized_gate not in ("H1", "H2", "H3", "H4"):
            raise WorkflowTransitionError(f"unknown gate: {gate}")
        if state.status != "waiting_human" or state.current_gate != normalized_gate:
            raise WorkflowTransitionError(
                f"run is not waiting at {normalized_gate}; current gate is {state.current_gate}"
            )
        if (
            normalized_gate == "H4"
            and request.action == "revise"
            and not request.comment.strip()
        ):
            raise WorkflowTransitionError("H4 revise requires a concrete review comment")

        current_version = state.version
        self.repository.claim_transition(
            run_id,
            expected_version=current_version,
            idempotency_key=request.idempotency_key,
        )
        try:
            if (
                normalized_gate == "H2"
                and request.action == "approve"
                and self._refresh_h2_test_dag_if_needed(
                    state,
                    request.selected_candidate_id,
                )
            ):
                state.processed_idempotency_keys.append(request.idempotency_key)
                self._event(
                    state,
                    "gate.artifact_refreshed",
                    "H2 待审 Artifact 已升级，需使用新哈希再次确认。",
                    node_id="h2_gate",
                    status="waiting_human",
                )
                return self.repository.save(
                    state,
                    expected_version=current_version,
                )
            if normalized_gate == "H3" and self._refresh_h3_claim_gate_if_needed(state):
                state.processed_idempotency_keys.append(request.idempotency_key)
                self._event(
                    state,
                    "gate.artifact_refreshed",
                    "H3 待审 ClaimLedger 已经确定性 Gate 重建，需核对新哈希后再次提交。",
                    node_id="h3_gate",
                    status="waiting_human",
                )
                return self.repository.save(
                    state,
                    expected_version=current_version,
                )
            reviewed_hashes = self._gate_artifact_hashes(state, normalized_gate)
            if (
                request.reviewed_artifact_hashes
                and request.reviewed_artifact_hashes != reviewed_hashes
            ):
                raise WorkflowTransitionError(
                    "reviewed artifact hashes do not match the current gate artifacts"
                )
            state.processed_idempotency_keys.append(request.idempotency_key)
            decision = DecisionRecord(
                gate=normalized_gate,
                action=request.action,
                actor=request.actor,
                comment=request.comment,
                selected_candidate_id=request.selected_candidate_id,
                reviewed_hashes=reviewed_hashes,
                claim_decisions={item.claim_id: item.decision for item in request.claims},
            )
            state.decisions.append(decision)
            self._record_step(
                state,
                f"{normalized_gate.lower()}_gate",
                "succeeded",
                input_value={"reviewed_artifacts": reviewed_hashes},
                output_value=decision,
                logs=[f"{normalized_gate} 决定已记录：{request.action}。"],
            )
            state.current_gate = None

            if request.action == "reject":
                state.status = "stopped"
                state.last_error = f"{normalized_gate} 被人工拒绝。"
                self._event(state, "run.stopped", state.last_error)
                return self.repository.save(state, expected_version=current_version)
            if request.action == "revise":
                state.status = "failed" if normalized_gate == "H4" else "blocked"
                state.current_node_id = {
                    "H1": "input_validation",
                    "H2": "analysis_plan_merge",
                    "H3": "claim_ledger",
                    "H4": "scientific_writer",
                }[normalized_gate]
                state.last_error = (
                    f"{normalized_gate} 已退回；需要通过 revisions API 提交修订 Artifact。"
                )
                self._event(
                    state,
                    "run.returned",
                    state.last_error,
                    node_id=state.current_node_id,
                    status="blocked",
                )
                return self.repository.save(state, expected_version=current_version)

            try:
                if normalized_gate == "H1":
                    if request.action != "approve":
                        raise WorkflowTransitionError("H1 only accepts approve, revise or reject")
                    await self._after_h1(state)
                elif normalized_gate == "H2":
                    if request.action != "approve":
                        raise WorkflowTransitionError("H2 only accepts approve, revise or reject")
                    await self._after_h2(state, decision)
                elif normalized_gate == "H3":
                    await self._after_h3(state, request)
                else:
                    if request.action != "approve":
                        raise WorkflowTransitionError(
                            "H4 only accepts approve, revise or reject"
                        )
                    self._after_h4(state)
            except WorkflowTransitionError:
                raise
            except Exception as error:
                state.status = "failed"
                state.last_error = str(error)
                self._event(
                    state,
                    "run.failed",
                    f"运行失败：{error}",
                    node_id=state.current_node_id,
                    status="failed",
                )
            return self.repository.save(state, expected_version=current_version)
        finally:
            self.repository.release_transition(run_id, request.idempotency_key)

    async def ingest_external_research_run(
        self,
        run_id: str,
        *,
        selected_candidate_id: str,
        analysis_request: dict[str, Any],
        execution_result_bytes: bytes,
        execution_result_sha256: str,
        expected_run_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> RunState:
        """Approve H2 and resume through H3 with one sealed external result.

        This is the native common-executor stop/resume boundary.  The current
        Run must still be waiting at its genuine H2 gate; no native statistical
        executor is called.  Validation of the shared benchmark contracts is
        delegated to ``common_executor_adapter`` before any transition is
        claimed or persisted.
        """

        from .common_executor_adapter import (
            build_pre_result_binding,
            validate_sealed_common_result,
        )

        if self.model_call_budget_mode != "v3":
            raise WorkflowTransitionError(
                "external ResearchRun ingestion requires the frozen v3 model budget"
            )
        state = self.get_run(run_id)
        transition_key = idempotency_key or f"common-executor-{uuid4()}"
        if transition_key in state.processed_idempotency_keys:
            return state
        if expected_run_version is not None and expected_run_version != state.version:
            raise VersionConflictError(
                f"run {run_id} changed; expected version {expected_run_version}, "
                f"actual {state.version}"
            )
        if state.status != "waiting_human" or state.current_gate != "H2":
            raise WorkflowTransitionError(
                "external ResearchRun ingestion requires a waiting H2 Run"
            )
        _request, request_binding = build_pre_result_binding(
            state,
            analysis_request,
            selected_candidate_id=selected_candidate_id,
        )
        if hashlib.sha256(execution_result_bytes).hexdigest() != execution_result_sha256:
            raise WorkflowTransitionError(
                "external execution result bytes do not match the declared sha256"
            )
        result_model, result_binding = validate_sealed_common_result(
            execution_result_bytes,
            pre_result_binding=request_binding,
        )
        if self._refresh_h2_test_dag_if_needed(state, selected_candidate_id):
            raise WorkflowTransitionError(
                "H2 plan required deterministic migration after AnalysisRequest export; "
                "discard the result and rerun the pre-result stage"
            )

        current_version = state.version
        self.repository.claim_transition(
            run_id,
            expected_version=current_version,
            idempotency_key=transition_key,
        )
        try:
            reviewed_hashes = self._gate_artifact_hashes(state, "H2")
            state.processed_idempotency_keys.append(transition_key)
            decision = DecisionRecord(
                gate="H2",
                action="approve",
                actor="benchmark_common_executor",
                comment=(
                    "Freeze the selected native H2 design and ingest the exact "
                    "benchmark-owned common-executor result."
                ),
                selected_candidate_id=selected_candidate_id,
                reviewed_hashes=reviewed_hashes,
            )
            state.decisions.append(decision)
            self._record_step(
                state,
                "h2_gate",
                "succeeded",
                input_value={"reviewed_artifacts": reviewed_hashes},
                output_value=decision,
                logs=["H2 common-executor decision was recorded before result ingestion."],
            )
            state.current_gate = None
            self._put_artifact(
                state,
                "common_executor_request_binding",
                request_binding,
            )
            self._put_artifact(
                state,
                "common_executor_result_binding",
                result_binding,
            )
            try:
                await self._after_h2(
                    state,
                    decision,
                    common_execution_result=result_model,
                )
            except WorkflowTransitionError:
                raise
            except Exception as error:
                state.status = "failed"
                state.last_error = str(error)
                self._event(
                    state,
                    "run.failed",
                    f"Common-executor H3 resume failed: {error}",
                    node_id=state.current_node_id,
                    status="failed",
                )
            return self.repository.save(state, expected_version=current_version)
        finally:
            self.repository.release_transition(run_id, transition_key)

    @staticmethod
    def _gate_artifact_hashes(state: RunState, gate: str) -> dict[str, str]:
        keys = {
            "H1": ("research_package",),
            "H2": ("design_arena", "analysis_plan", "critic_report"),
            "H3": ("claim_ledger", "research_run", "evidence_figure_bundle"),
            "H4": ("manuscript_package", "publication_figure_bundle"),
        }[gate]
        hashes: dict[str, str] = {}
        for key in keys:
            envelope = WorkflowEngine._artifact_envelope(
                state, key, required=False
            )
            if envelope is not None:
                hashes[key] = envelope["sha256"]
        return hashes

    async def submit_revision(self, run_id: str, request: RevisionRequest) -> RunState:
        state = self.get_run(run_id)
        if request.idempotency_key in state.processed_idempotency_keys:
            return state
        if request.expected_run_version != state.version:
            raise VersionConflictError(
                f"run {run_id} changed; expected version {request.expected_run_version}, actual {state.version}"
            )
        returned_revision = (
            state.status == "blocked"
            and bool(state.decisions)
            and state.decisions[-1].action == "revise"
            and state.decisions[-1].gate == request.gate
        )
        critic_revision = (
            state.status == "blocked"
            and request.gate == "H2"
            and state.current_node_id in {"critic_merge", "design_arena_merge"}
            and "analysis_plan" in state.artifacts
            and "critic_report" in state.artifacts
        )
        if not returned_revision and not critic_revision:
            raise WorkflowTransitionError(
                f"run is not waiting for a {request.gate} revision"
            )

        current_version = state.version
        self.repository.claim_transition(
            run_id,
            expected_version=current_version,
            idempotency_key=request.idempotency_key,
        )
        try:
            state.processed_idempotency_keys.append(request.idempotency_key)
            state.last_error = None
            state.status = "running"
            if request.gate == "H1":
                assert request.case is not None
                case = request.case
                state.case_submission = case
                state.case_id = case.case_id
                state.case_name = case.title
                self._record_step(
                    state,
                    "case_input",
                    "succeeded",
                    output_value=case,
                    logs=["H1 修订案例已提交并通过严格 CaseSubmission Schema。"],
                )
                package = self._normalize_case(state, case)
                self._put_artifact(state, "research_package", package)
                validation = {
                    "valid": not package.input_conflicts,
                    "errors": package.input_conflicts,
                    "warnings": package.missing_required_information,
                    "hidden_reference_access": "denied_by_schema",
                }
                self._record_step(
                    state,
                    "input_validation",
                    "succeeded" if validation["valid"] else "blocked",
                    input_value=package,
                    output_value=validation,
                    logs=["H1 修订输入已重新执行确定性校验。"],
                )
                if validation["valid"]:
                    self._pause_at_gate(state, "H1", package)
                else:
                    state.status = "blocked"
                    state.last_error = "修订输入仍存在冲突。"
            else:
                assert request.analysis_plan is not None
                previous = self._artifact(state, "analysis_plan", AnalysisPlan)
                plan = request.analysis_plan
                if plan.plan_version <= previous.plan_version:
                    raise WorkflowTransitionError(
                        "H2 revision must increment AnalysisPlan.plan_version"
                    )
                if plan.method_family != previous.method_family:
                    raise WorkflowTransitionError(
                        "changing method family requires returning to H1/method routing"
                    )
                self._record_step(
                    state,
                    "plan_revision",
                    "succeeded",
                    input_value={"returned_plan": previous, "human_revision": plan},
                    output_value=plan,
                    logs=["人工修订 AnalysisPlan 已提交；重新进入四类 Critic。"],
                )
                if "design_arena" in state.artifacts:
                    state.artifacts["superseded_design_arena"] = state.artifacts.pop(
                        "design_arena"
                    )
                self._put_artifact(state, "analysis_plan", plan)
                await self._review_plan(
                    state,
                    self._artifact(state, "research_package", ResearchPackage),
                    self._artifact(state, "data_profile", DataProfile),
                    self._artifact(state, "method_route", MethodRoute),
                    plan,
                )
            self._event(
                state,
                "revision.submitted",
                f"{request.gate} 修订 Artifact 已提交。",
                node_id=state.current_node_id,
            )
            return self.repository.save(state, expected_version=current_version)
        finally:
            self.repository.release_transition(run_id, request.idempotency_key)

    async def _after_h1(self, state: RunState) -> None:
        state.status = "running"
        package = self._artifact(state, "research_package", ResearchPackage)
        llm_package = self._llm_research_package(package)
        hypotheses = await self._llm_step(
            state,
            "hypothesis_decomposition",
            "hypothesis_decomposition",
            {"research_package": llm_package},
            TestableHypotheses,
            call_context=ModelCallContext(
                call_group="h1_h2",
                prompt_key="hypothesis_decomposition",
            ),
        )
        self._put_artifact(state, "testable_hypotheses", hypotheses)
        profile = self._profile(package)
        self._record_step(
            state,
            "data_profile",
            "succeeded",
            input_value=package,
            output_value=profile,
            prompts=[
                {
                    "id": "data_profile:code",
                    "role": "code",
                    "template": "Deterministic dataset-reference and variable-role profiling",
                    "rendered": "Server-side code reads only the registered analysis CSV and computes descriptive integrity checks; no model is estimated.",
                }
            ],
            logs=["实际数据画像完成；样本量、字段数、主键重复与缺失率均由确定性代码计算。"],
        )
        self._put_artifact(state, "data_profile", profile)
        route_input = {
            "research_package": llm_package,
            "testable_hypotheses": hypotheses.model_dump(mode="json"),
            "data_profile": profile.model_dump(mode="json"),
        }
        route = MethodRoute.model_validate(FixtureModelGateway._route(route_input))
        self._record_step(
            state,
            "method_route",
            "succeeded",
            input_value=route_input,
            output_value=route,
            prompts=[
                {
                    "id": "method_route:code",
                    "role": "code",
                    "template": "Deterministic method-family routing from research goal and data structure",
                    "rendered": "方法路由由确定性规则完成；不会因模型输出自相矛盾而使 Run 失败。",
                }
            ],
            logs=["确定性方法路由完成；输出已通过 MethodRoute 校验。"],
        )
        self._put_artifact(state, "method_route", route)
        if route.route_status != "routed" or route.primary_route is None:
            state.status = "blocked"
            state.last_error = "方法路由没有满足条件，禁止静默回退。"
            self._event(state, "run.blocked", state.last_error, node_id="method_route", status="blocked")
            return

        selected = f"design_{route.primary_route}"
        for family in (
            "policy_causal",
            "panel_association",
            "mechanism_boundary",
            "market_event",
            "spatial",
            "measurement_efficiency",
            "structural_macro",
        ):
            node_id = f"design_{family}"
            if node_id != selected:
                self._record_step(
                    state,
                    node_id,
                    "skipped",
                    input_value=route,
                    logs=[f"互斥路由未选择 {family}。"],
                )
        envelope = self._derive_design_envelope(package, route)
        self._put_artifact(state, "design_envelope", envelope)
        candidate_set = await self._generate_design_candidates(
            state,
            selected,
            package,
            hypotheses,
            profile,
            route,
            envelope,
        )
        self._put_artifact(state, "candidate_design_set", candidate_set)
        await self._review_design_arena(
            state,
            package,
            profile,
            route,
            envelope,
            candidate_set,
        )

    @staticmethod
    def _derive_design_envelope(
        package: ResearchPackage,
        route: MethodRoute,
    ) -> DesignEnvelope:
        if package.design_envelope is not None:
            return package.design_envelope
        text = " ".join(
            [
                package.research_question,
                *package.known_policy_facts,
                *package.constraints,
            ]
        ).casefold()
        target_estimands = ["主假设对应的核心效应或关联参数"]
        if route.primary_route == "spatial":
            target_estimands = []
            if any(term in text for term in ("本地", "本省", "直接")):
                target_estimands.append("本地直接效应")
            if any(term in text for term in ("跨省", "跨地区", "邻近", "溢出", "间接")):
                target_estimands.append("跨地区间接效应")
            if any(term in text for term in ("总效应", "合计", "总体")):
                target_estimands.append("直接与间接效应合计的总效应")
            if not target_estimands:
                target_estimands.append("空间关联参数")
        allowed_strength = (
            "causal"
            if route.research_goal == "causal"
            else "associational"
            if route.research_goal in ("associational", "mechanism", "mixed")
            else "descriptive"
        )
        return DesignEnvelope(
            benchmark_track="strict_blind",
            research_goal=route.research_goal,
            target_estimands=target_estimands,
            design_constraints=package.constraints,
            required_diagnostics=route.testable_assumptions,
            allowed_claim_strength=allowed_strength,
        )

    @staticmethod
    def _llm_research_package(package: ResearchPackage) -> dict[str, Any]:
        payload = package.model_dump(mode="json")
        payload["variables"] = [
            variable.model_dump(mode="json")
            for variable in package.variables
            if variable.role != "unknown"
        ]
        return payload

    async def _generate_design_candidates(
        self,
        state: RunState,
        selected_node: str,
        package: ResearchPackage,
        hypotheses: TestableHypotheses,
        profile: DataProfile,
        route: MethodRoute,
        envelope: DesignEnvelope,
        *,
        gateway: ModelGateway | None = None,
    ) -> CandidateDesignSet:
        compact_package = {
            "case_id": package.case_id,
            "title": package.title,
            "research_question": package.research_question,
            "hypotheses": [
                item.model_dump(mode="json") for item in package.hypotheses
            ],
            "unit_of_analysis": package.unit_of_analysis,
            "sample_period": package.sample_period,
            "data_structure_hint": package.data_structure_hint,
            "variables": [
                item.model_dump(mode="json")
                for item in package.variables
                if item.role != "unknown"
            ],
            "dataset_refs": [
                item.model_dump(mode="json") for item in package.dataset_refs
            ],
            "policy_design": (
                package.policy_design.model_dump(mode="json")
                if package.policy_design is not None
                else None
            ),
            "known_policy_facts": package.known_policy_facts,
            "constraints": package.constraints,
        }
        compact_hypotheses = {
            "items": [
                {
                    "hypothesis_id": item.hypothesis_id,
                    "theoretical_claim": item.theoretical_claim,
                    "observable_prediction": item.observable_prediction,
                    "boundary_conditions": item.boundary_conditions,
                    "competing_explanations": item.competing_explanations,
                    "falsification_conditions": item.falsification_conditions,
                }
                for item in hypotheses.items
            ]
        }
        compact_profile = {
            "profile_execution_status": profile.profile_execution_status,
            "data_structure": profile.data_structure,
            "unit_of_observation": profile.unit_of_observation,
            "entity_key": profile.entity_key,
            "time_key": profile.time_key,
            "spatial_key": profile.spatial_key,
            "event_date_key": profile.event_date_key,
            "row_count": profile.row_count,
            "column_count": profile.column_count,
            "duplicate_key_count": profile.duplicate_key_count,
            "missingness": [
                item.model_dump(mode="json")
                for item in profile.missingness
                if item.missing_rate
                and any(
                    variable.name == item.variable and variable.role != "unknown"
                    for variable in package.variables
                )
            ],
            "confirmed_facts": profile.confirmed_facts,
            "measurement_risks": profile.measurement_risks,
            "merge_risks": profile.merge_risks,
            "supported_method_families": profile.supported_method_families,
            "readiness": profile.readiness,
            "blocking_reasons": profile.blocking_reasons,
        }

        strategy_rationales = dict(DESIGN_STRATEGIES)
        plans_by_strategy: dict[str, AnalysisPlan] = {}
        for step in reversed(state.steps):
            if step.status != "succeeded" or not isinstance(step.input, dict):
                continue
            if step.node_id == selected_node and step.input.get("candidate_strategy"):
                strategy = str(step.input["candidate_strategy"])
                if strategy in strategy_rationales and strategy not in plans_by_strategy:
                    plans_by_strategy[strategy] = AnalysisPlan.model_validate(step.output)

        async def generate_batch(
            batch_index: int,
            strategies: tuple[str, ...],
        ) -> CandidatePlanBatch | None:
            missing = [
                strategy for strategy in strategies if strategy not in plans_by_strategy
            ]
            if not missing:
                return None
            payload = {
                "candidate_strategies": missing,
                "strategy_rationales": {
                    strategy: strategy_rationales[strategy]
                    for strategy in missing
                },
                "design_envelope": envelope.model_dump(mode="json"),
                "research_package": compact_package,
                "testable_hypotheses": compact_hypotheses,
                "data_profile": compact_profile,
                "method_route": route.model_dump(mode="json"),
                "design_model_policy": {
                    "tier": "balanced_batch",
                    "model": getattr(gateway, "model", state.model_provider),
                },
            }
            node_id = f"{selected_node}_batch_{batch_index}"
            checkpoint = _load_matching_step_checkpoint(
                state,
                node_id,
                payload,
                CandidatePlanBatch,
                ignored_input_keys=("design_model_policy",),
            )
            if checkpoint is not None:
                result = checkpoint
            else:
                result = await self._llm_step(
                    state,
                    node_id,
                    "candidate_plan_batch",
                    payload,
                    CandidatePlanBatch,
                    gateway=gateway,
                    call_context=ModelCallContext(
                        logical_call_id=_run_local_logical_call_id(
                            state,
                            node_id,
                            payload,
                            "candidate_plan_batch",
                            ignored_input_keys=("design_model_policy",),
                        ),
                        call_group="h1_h2",
                        prompt_key="candidate_plan_batch",
                    ),
                )
            assert isinstance(result, CandidatePlanBatch)
            returned = {draft.strategy for draft in result.plans}
            if returned != set(missing):
                raise WorkflowTransitionError(
                    "candidate design batch strategy mismatch: "
                    f"expected {sorted(missing)}, got {sorted(returned)}"
                )
            return result

        batch_results = await _gather_llm_batches_to_terminal(
            *(
                generate_batch(index, strategies)
                for index, strategies in enumerate(DESIGN_STRATEGY_BATCHES, 1)
            )
        )
        for batch in batch_results:
            if batch is None:
                continue
            for draft in batch.plans:
                plans_by_strategy[draft.strategy] = draft.plan

        expected_strategies = {strategy for strategy, _ in DESIGN_STRATEGIES}
        if set(plans_by_strategy) != expected_strategies:
            raise RuntimeError(
                "候选研究设计未完整覆盖三种预注册策略。"
            )

        candidates: list[DesignCandidate] = []
        fingerprints: dict[str, str] = {}
        for strategy, rationale in DESIGN_STRATEGIES:
            plan = plans_by_strategy[strategy].model_copy(
                update={
                    "plan_id": f"plan-{state.case_id}-{strategy}",
                    "plan_version": 1,
                    "revision_round": 0,
                }
            )
            plan = _normalize_external_enterprise_candidate_plan(
                plan,
                profile,
                package,
                state.execution_mode,
            )
            plan = _normalize_external_policy_candidate_plan(
                plan,
                profile,
                package,
                state.execution_mode,
                strategy,
                self.dataset_registry,
            )
            plan = self._bind_spatial_assets(package, plan)
            fingerprint = _plan_executable_fingerprint(plan)
            if fingerprint in fingerprints:
                raise WorkflowTransitionError(
                    "candidate executable plans are duplicates after removing prose: "
                    f"{fingerprints[fingerprint]} and {strategy}"
                )
            fingerprints[fingerprint] = strategy
            candidate_id = f"candidate-{strategy}"
            if not any(
                step.node_id == selected_node
                and step.status == "succeeded"
                and isinstance(step.input, dict)
                and step.input.get("candidate_strategy") == strategy
                for step in state.steps
            ):
                self._record_step(
                    state,
                    selected_node,
                    "succeeded",
                    input_value={
                        "candidate_strategy": strategy,
                        "source_batch": next(
                            (
                                step.node_id
                                for step in reversed(state.steps)
                                if step.node_id.startswith(f"{selected_node}_batch_")
                                and step.status == "succeeded"
                                and isinstance(step.output, dict)
                                and any(
                                    item.get("strategy") == strategy
                                    for item in step.output.get("plans", [])
                                )
                            ),
                            "legacy",
                        ),
                    },
                    output_value=plan,
                    logs=[
                        f"{candidate_id} 已从批量调用拆分；稳定 ID 与执行参数由代码绑定。"
                    ],
                )
            probe = self._probe_candidate(
                state,
                package,
                profile,
                route,
                envelope,
                candidate_id,
                plan,
            )
            candidates.append(
                DesignCandidate(
                    candidate_id=candidate_id,
                    strategy=strategy,
                    rationale=rationale,
                    plan=plan,
                    probe_report=probe,
                )
            )
        candidate_set = CandidateDesignSet(
            candidate_set_id=f"candidate-set-{uuid4()}",
            candidates=candidates,
        )
        self._record_step(
            state,
            "candidate_design_set",
            "succeeded",
            input_value={"selected_method_node": selected_node},
            output_value=candidate_set,
            prompts=[
                {
                    "id": "candidate_design_set:code",
                    "role": "code",
                    "template": "Bounded candidate set with three prespecified design strategies",
                    "rendered": "三种候选策略在查看任何统计结果前生成；不按系数方向或 p 值筛选。",
                }
            ],
            logs=[
                f"{len(candidates)} 个候选研究设计已形成，进入无结果可见的 Probe 与 Reviewer Arena。"
            ],
        )
        self._record_step(
            state,
            "probe_run",
            "succeeded",
            input_value={
                "candidate_ids": [candidate.candidate_id for candidate in candidates],
                "forbidden_inputs": ["estimate", "coefficient", "p_value", "significance"],
            },
            output_value={
                candidate.candidate_id: candidate.probe_report
                for candidate in candidates
            },
            prompts=[
                {
                    "id": "probe_run:code",
                    "role": "code",
                    "template": "Deterministic pre-result feasibility probe",
                    "rendered": "仅检查字段、结构、资产、识别条件与执行器能力；used_outcome_results 固定为 false。",
                }
            ],
            logs=["三个候选均已完成无结果可见 Probe。"],
        )
        return candidate_set

    @staticmethod
    def _spatial_model_type(model: Any) -> str | None:
        declared = str(model.parameters.get("spatial_model", "")).casefold()
        if declared in {"sdm", "sar", "sem"}:
            return declared
        estimator = model.estimator.casefold()
        if any(term in estimator for term in ("sdm", "durbin", "杜宾")):
            return "sdm"
        if any(term in estimator for term in ("sar", "spatial lag", "空间滞后")):
            return "sar"
        if any(term in estimator for term in ("sem", "spatial error", "空间误差")):
            return "sem"
        return None

    def _probe_candidate(
        self,
        state: RunState,
        package: ResearchPackage,
        profile: DataProfile,
        route: MethodRoute,
        envelope: DesignEnvelope,
        candidate_id: str,
        plan: AnalysisPlan,
    ) -> ProbeReport:
        checks: list[ProbeCheck] = []

        def add(
            check_id: str,
            status: str,
            evidence: str,
            required_follow_up: str | None = None,
        ) -> None:
            checks.append(
                ProbeCheck(
                    check_id=check_id,
                    status=status,
                    evidence=evidence,
                    required_follow_up=required_follow_up,
                )
            )

        visible_fields = {variable.name for variable in package.variables}
        missing = sorted(set(plan.required_data_fields) - visible_fields)
        add(
            "required_fields",
            "fail" if missing else "pass",
            "缺少字段：" + "、".join(missing) if missing else "计划所需字段均在安全变量字典中。",
            "删除不可用字段或补充安全可见数据。" if missing else None,
        )
        design_only_probe = plan.design_only or state.execution_mode == "fixture"
        add(
            "data_readiness",
            (
                "warn"
                if profile.readiness == "blocked" and design_only_probe
                else "fail"
                if profile.readiness == "blocked"
                else "warn"
                if profile.readiness == "partially_ready"
                else "pass"
            ),
            f"DataProfile.readiness={profile.readiness}；仅使用结构、主键、缺失与资产信息。",
            (
                "当前仅形成研究计划；接入真实数据后必须重新执行 Probe。"
                if profile.readiness == "blocked" and design_only_probe
                else "先修复 DataProfile 阻塞项。"
                if profile.readiness == "blocked"
                else None
            ),
        )
        add(
            "method_route",
            "fail" if plan.method_family != route.primary_route else "pass",
            f"候选方法家族={plan.method_family}；路由家族={route.primary_route}。",
            "候选方案不得越过 H1 后的确定性方法家族路由。"
            if plan.method_family != route.primary_route
            else None,
        )
        policy_baseline: ModelSpec | None = None
        if state.execution_mode == "external" and plan.method_family == "policy_causal":
            try:
                policy_baseline = validate_policy_did_execution_plan(plan)
            except ValueError as error:
                add(
                    "policy_execution_contract",
                    "fail",
                    f"政策执行合约无效：{error}",
                    "修复唯一基准模型、执行状态、registry 和 policy_design 后重新 Probe。",
                )
            else:
                add(
                    "policy_execution_contract",
                    "pass",
                    "policy-did-v2 执行合约已通过数据读取前校验。",
                )
        if not plan.baseline_models:
            add("baseline_model", "fail", "候选方案没有基准模型。", "补充可执行基准模型。")
        elif (
            state.execution_mode == "external"
            and plan.method_family == "policy_causal"
            and policy_baseline is None
        ):
            pass
        else:
            model = policy_baseline or plan.baseline_models[0]
            add(
                "core_variables",
                "fail" if not model.outcome or not model.treatments_or_exposures else "pass",
                f"outcome={model.outcome or 'missing'}；exposures={model.treatments_or_exposures}。",
                "必须绑定一个结果变量和至少一个处理或暴露变量。"
                if not model.outcome or not model.treatments_or_exposures
                else None,
            )
            if profile.data_structure in ("panel", "spatial_panel"):
                fixed_effects = set(model.fixed_effects)
                entity_effect_recorded = bool(
                    fixed_effects.intersection(profile.entity_key)
                    or (
                        profile.data_structure == "spatial_panel"
                        and profile.spatial_key in fixed_effects
                    )
                )
                missing_fixed_effects = set()
                if profile.entity_key and not entity_effect_recorded:
                    missing_fixed_effects.update(profile.entity_key)
                if profile.time_key and profile.time_key not in fixed_effects:
                    missing_fixed_effects.add(profile.time_key)
                add(
                    "panel_effects",
                    "warn" if missing_fixed_effects else "pass",
                    (
                        "尚未控制面板键对应的固定效应："
                        + "、".join(sorted(missing_fixed_effects))
                        if missing_fixed_effects
                        else "候选模型已显式记录面板层级固定效应。"
                    ),
                    "由 H2 判断是否接受该识别风险。" if missing_fixed_effects else None,
                )

        if policy_baseline is not None and not design_only_probe:
            model = policy_baseline
            policy_contract = model.parameters.get("policy_design")
            if not isinstance(policy_contract, dict):
                add(
                    "policy_contract",
                    "fail",
                    "候选方案没有代码可验证的 policy_design 合约。",
                    "冻结分组、时点、暴露权重、固定效应与聚类字段。",
                )
            else:
                try:
                    group = str(policy_contract["group_field"])
                    time = str(policy_contract["time_field"])
                    start_year = int(policy_contract["policy_start_year"])
                    time_scale = str(
                        policy_contract.get("time_scale", "calendar_year")
                    )
                    start_weight = float(policy_contract["policy_start_weight"])
                    entity = profile.entity_key[0]
                    cluster_fields = [
                        str(value)
                        for value in policy_contract.get("cluster_fields", [])
                    ]
                    probe_fields = list(
                        dict.fromkeys([entity, time, group, *cluster_fields])
                    )
                    main_ref = next(
                        item for item in package.dataset_refs if item.role == "main"
                    )
                    source = self.dataset_registry.resolve(main_ref)
                    _verify_dataset_hash(source, main_ref.sha256)
                    probe_frame, _ = _read_profile_csv(source, probe_fields)
                    if set(probe_fields) - set(probe_frame.columns):
                        raise ValueError("政策 Probe 字段不完整")
                    probe_frame[time] = pd.to_numeric(
                        probe_frame[time], errors="coerce"
                    )
                    probe_frame[group] = pd.to_numeric(
                        probe_frame[group], errors="coerce"
                    )
                    valid = probe_frame.dropna(subset=[entity, time, group]).copy()
                    observed_groups = sorted(valid[group].unique().tolist())
                    observed_years = sorted(
                        int(value) for value in valid[time].unique().tolist()
                    )
                    pre = valid[time] < start_year
                    post = valid[time] >= start_year
                    support = {
                        "treated_pre": int((pre & valid[group].eq(1)).sum()),
                        "control_pre": int((pre & valid[group].eq(0)).sum()),
                        "treated_post": int((post & valid[group].eq(1)).sum()),
                        "control_post": int((post & valid[group].eq(0)).sum()),
                    }
                    support_ok = set(observed_groups) == {0, 1} and all(support.values())
                    add(
                        "policy_group_time_support",
                        "pass" if support_ok else "fail",
                        f"观测组别={observed_groups}；四个组别×政策前后单元观测数={support}。",
                        None if support_ok else "政策 DID 需要处理/对照在政策前后均有观测。",
                    )
                    reference_year = int(
                        policy_contract.get("event_reference_year", start_year - 1)
                    )
                    timing_ok = start_year in observed_years and reference_year in observed_years
                    missing_years = [
                        year
                        for year in range(min(observed_years), max(observed_years) + 1)
                        if year not in observed_years
                    ]
                    add(
                        "policy_timing_map",
                        "pass" if timing_ok else "fail",
                        (
                            f"时间刻度={time_scale}；政策时点值={start_year}；"
                            f"参考时点值={reference_year}；实际时点值={observed_years}；"
                            f"缺失时点值={missing_years}。"
                        ),
                        (
                            None
                            if timing_ok
                            else "政策时点和事件研究参考时点必须真实存在于数据中。"
                        ),
                    )
                    add(
                        "policy_partial_year_weight",
                        "pass" if 0 < start_weight <= 1 else "fail",
                        f"冻结的政策首年暴露权重={start_weight:.6g}；该检查未读取结果变量。",
                        None if 0 < start_weight <= 1 else "首年权重必须位于 (0, 1]。",
                    )
                    group_switchers = int(
                        (valid.groupby(entity, dropna=False)[group].nunique() > 1).sum()
                    )
                    singleton_entities = int(
                        (valid.groupby(entity, dropna=False).size() == 1).sum()
                    )
                    spans = valid.groupby(entity, dropna=False)[time].agg(["min", "max"])
                    both_period_entities = int(
                        ((spans["min"] < start_year) & (spans["max"] >= start_year)).sum()
                    )
                    add(
                        "policy_group_stability",
                        "warn" if group_switchers else "pass",
                        "；".join(
                            [
                                f"企业内分组切换={group_switchers}",
                                f"单期企业={singleton_entities}",
                                f"跨政策前后企业={both_period_entities}",
                            ]
                        ),
                        (
                            "当前分组是企业—年度状态而非永久处理组；H2 必须保留这一识别边界。"
                            if group_switchers
                            else None
                        ),
                    )
                    if cluster_fields:
                        cluster_sizes = valid.groupby(cluster_fields, dropna=False).size()
                        add(
                            "policy_cluster_support",
                            "warn" if int((cluster_sizes == 1).sum()) else "pass",
                            f"交叉聚类单元={len(cluster_sizes)}；单观测聚类单元={int((cluster_sizes == 1).sum())}。",
                            "报告稀疏聚类对推断的影响并保留替代聚类敏感性。"
                            if int((cluster_sizes == 1).sum())
                            else None,
                        )
                except (CaseImportError, KeyError, OSError, TypeError, ValueError) as error:
                    add(
                        "policy_contract",
                        "fail",
                        f"政策合约 Probe 失败：{type(error).__name__}。",
                        "修复结构化政策合约或数据字段后重新 Probe。",
                    )

        executor_ready = state.execution_mode == "fixture"
        if state.execution_mode == "external":
            executor_ready = (
                policy_baseline is not None
                if plan.method_family == "policy_causal"
                else plan.method_family
                in {
                    "panel_association",
                    "mechanism_boundary",
                }
            )
        if plan.method_family == "spatial" and plan.baseline_models:
            model = plan.baseline_models[0]
            spatial_model = self._spatial_model_type(model)
            weights_ref = next(
                (
                    item
                    for item in package.dataset_refs
                    if item.role == "supplementary"
                    and is_spatial_weights_filename(item.filename)
                ),
                None,
            )
            spatial_keys = _names(package, "spatial_id")
            add(
                "spatial_assets",
                "fail" if weights_ref is None or not spatial_keys else "pass",
                "空间权重资产或空间标识缺失。"
                if weights_ref is None or not spatial_keys
                else "空间权重 SHA256 与空间标识均已登记。",
                "补充安全可见的空间权重和对齐标识。"
                if weights_ref is None or not spatial_keys
                else None,
            )
            target_text = " ".join(
                [*envelope.target_estimands, *envelope.design_constraints]
            ).casefold()
            requires_covariate_lags = any(
                term in target_text
                for term in (
                    "解释变量空间滞后",
                    "协变量空间滞后",
                    "spatially lagged covariate",
                    "exposure spillover",
                )
            )
            requires_indirect = any(
                term in target_text
                for term in ("间接", "跨地区", "跨省", "溢出", "indirect")
            )
            spatial_status = "pass"
            spatial_follow_up = None
            if spatial_model is None:
                spatial_status = "fail"
                spatial_follow_up = "明确空间依赖来源和可识别的空间模型。"
            elif spatial_model == "sem" and requires_indirect:
                spatial_status = "fail"
                spatial_follow_up = "该目标需要可分解的跨地区效应，空间误差模型不能单独承担。"
            elif spatial_model != "sdm" and requires_covariate_lags:
                spatial_status = "fail"
                spatial_follow_up = "目标要求区分解释变量的空间滞后项，当前候选未覆盖。"
            elif spatial_model == "sar" and requires_indirect:
                spatial_status = "warn"
                spatial_follow_up = "确认仅由结果变量空间反馈产生的间接效应是否满足目标。"
            add(
                "spatial_estimands",
                spatial_status,
                f"声明的空间模型={spatial_model or 'unknown'}；目标估计量={envelope.target_estimands}。",
                spatial_follow_up,
            )
            executor_ready = state.execution_mode == "fixture" or spatial_model == "sdm"

        add(
            "executor_capability",
            "pass" if executor_ready else "fail",
            (
                "当前执行器能够执行该候选的冻结基准模型。"
                if executor_ready
                else "当前方法库尚无该候选的可审计执行器，不能在本轮静默替换方法。"
            ),
            None if executor_ready else "由 Task2 补充执行器，或选择已有能力覆盖的科学可行候选。",
        )
        verdict = (
            "fail"
            if any(check.status == "fail" for check in checks)
            else "warn"
            if any(check.status == "warn" for check in checks)
            else "pass"
        )
        return ProbeReport(
            report_id=f"probe-{candidate_id}",
            candidate_id=candidate_id,
            verdict=verdict,
            checks=checks,
            executor_ready=executor_ready,
            used_outcome_results=False,
        )

    async def _review_design_arena(
        self,
        state: RunState,
        package: ResearchPackage,
        profile: DataProfile,
        route: MethodRoute,
        envelope: DesignEnvelope,
        candidate_set: CandidateDesignSet,
    ) -> None:
        semaphore = asyncio.Semaphore(1 if state.model_provider == "qwen" else 2)
        compact_package = self._llm_research_package(package)
        compact_profile = profile.model_dump(mode="json")
        visible_names = {
            variable.name for variable in package.variables if variable.role != "unknown"
        }
        compact_profile["missingness"] = [
            item
            for item in compact_profile.get("missingness", [])
            if item.get("variable") in visible_names
        ]

        expected_candidate_ids = {
            candidate.candidate_id for candidate in candidate_set.candidates
        }
        reports_by_dimension: dict[str, DesignReviewerReport] = {}
        for step in reversed(state.steps):
            if (
                step.status == "succeeded"
                and step.node_id.startswith("critic_")
                and isinstance(step.input, dict)
                and step.input.get("dimension")
            ):
                dimension = str(step.input["dimension"])
                if dimension not in reports_by_dimension:
                    try:
                        reports_by_dimension[dimension] = (
                            DesignReviewerReport.model_validate(step.output)
                        )
                    except (TypeError, ValueError):
                        pass

        async def review_batch(
            batch_index: int,
            dimensions: tuple[str, str],
        ) -> ReviewerReportBatch | None:
            missing = [
                dimension
                for dimension in dimensions
                if dimension not in reports_by_dimension
            ]
            if not missing:
                return None
            node_id = f"design_reviewer_batch_{batch_index}"
            payload = {
                "dimensions": missing,
                "reviewer_policy": (
                    f"qwen:{REVIEWER_MODEL}:paired-dimension-batch"
                    if state.model_provider == "qwen"
                    else "fixture:paired-dimension-batch"
                ),
                "research_package": compact_package,
                "design_envelope": envelope.model_dump(mode="json"),
                "data_profile": compact_profile,
                "method_route": route.model_dump(mode="json"),
                "candidates": [
                    candidate.model_dump(mode="json")
                    for candidate in candidate_set.candidates
                ],
            }
            checkpoint = _load_matching_step_checkpoint(
                state,
                node_id,
                payload,
                ReviewerReportBatch,
            )
            if checkpoint is not None:
                result = checkpoint
            else:
                async with semaphore:
                    result = await self._llm_step(
                        state,
                        node_id,
                        "reviewer_report_batch",
                        payload,
                        ReviewerReportBatch,
                        gateway=self._reviewer_gateway(state),
                        call_context=ModelCallContext(
                            logical_call_id=_run_local_logical_call_id(
                                state,
                                node_id,
                                payload,
                                "reviewer_report_batch",
                            ),
                            call_group="h1_h2",
                            prompt_key="reviewer_report_batch",
                        ),
                    )
            assert isinstance(result, ReviewerReportBatch)
            returned = {report.dimension for report in result.reports}
            if returned != set(missing):
                raise WorkflowTransitionError(
                    "reviewer batch dimension mismatch: "
                    f"expected {sorted(missing)}, got {sorted(returned)}"
                )
            for report in result.reports:
                reviewed = {
                    review.candidate_id for review in report.candidate_reviews
                }
                if reviewed != expected_candidate_ids:
                    raise WorkflowTransitionError(
                        f"Reviewer {report.dimension} did not cover every candidate"
                    )
            return result

        if state.model_provider == "qwen":
            batch_results = []
            for index, dimensions in enumerate(REVIEWER_DIMENSION_BATCHES, 1):
                batch_results.append(await review_batch(index, dimensions))
        else:
            batch_results = await _gather_llm_batches_to_terminal(
                *(
                    review_batch(index, dimensions)
                    for index, dimensions in enumerate(
                        REVIEWER_DIMENSION_BATCHES,
                        1,
                    )
                )
            )
        for result in batch_results:
            if result is None:
                continue
            for report in result.reports:
                reports_by_dimension[report.dimension] = report
                self._record_step(
                    state,
                    f"critic_{report.dimension}",
                    "succeeded",
                    input_value={
                        "dimension": report.dimension,
                        "source_batch": result.model_dump(mode="json"),
                    },
                    output_value=report,
                    logs=[
                        f"{report.dimension} Reviewer 报告已从成对调用中独立拆分并通过 Schema。"
                    ],
                )

        canonical_dimensions = (
            "measurement",
            "causal",
            "statistical",
            "reproducibility",
        )
        if set(reports_by_dimension) != set(canonical_dimensions):
            raise WorkflowTransitionError("Reviewer reports do not cover all four dimensions")
        reports = [reports_by_dimension[dimension] for dimension in canonical_dimensions]
        reports, shared_issue_events = _propagate_shared_policy_reviewer_issues(
            candidate_set,
            reports,
        )
        if shared_issue_events:
            self._record_step(
                state,
                "reviewer_shared_issue_propagation",
                "succeeded",
                input_value={
                    "candidate_set_id": candidate_set.candidate_set_id,
                    "report_ids": [report.report_id for report in reports],
                },
                output_value={
                    "reports": [report.model_dump(mode="json") for report in reports],
                    "propagated": shared_issue_events,
                },
                prompts=[
                    {
                        "id": "reviewer_shared_issue_propagation:code",
                        "role": "code",
                        "template": "Propagate issues across identical frozen invariants",
                        "rendered": (
                            "相同政策规格共享同一统计或复算风险；"
                            "不得通过选择未被 Reviewer 点名的候选规避问题。"
                        ),
                    }
                ],
                logs=[f"传播 {len(shared_issue_events)} 个共享规格问题。"],
            )
        compiled_candidates: list[DesignCandidate] = []
        test_dag_changed = False
        for candidate in candidate_set.candidates:
            candidate_issues = [
                issue
                for report in reports
                for review in report.candidate_reviews
                if review.candidate_id == candidate.candidate_id
                for issue in review.issues
            ]
            compiled_plan = self._compile_enterprise_panel_plan(
                candidate.plan,
                package,
                candidate_issues,
            )
            test_dag_changed = test_dag_changed or compiled_plan != candidate.plan
            compiled_candidates.append(
                candidate.model_copy(update={"plan": compiled_plan})
            )
        if test_dag_changed:
            candidate_set = candidate_set.model_copy(
                update={"candidates": compiled_candidates}
            )
            self._record_step(
                state,
                "test_dag_compile",
                "succeeded",
                input_value={
                    "candidate_set_id": candidate_set.candidate_set_id,
                    "registry_version": (
                        POLICY_DID_REGISTRY_VERSION
                        if any(
                            candidate.plan.method_family == "policy_causal"
                            for candidate in candidate_set.candidates
                        )
                        else ENTERPRISE_PANEL_REGISTRY_VERSION
                    ),
                },
                output_value=candidate_set,
                prompts=[
                    {
                        "id": "test_dag_compile:code",
                        "role": "code",
                        "template": "Compile structured Reviewer threats into the frozen Test DAG",
                        "rendered": "代码只使用 threat_id 映射企业面板检查，不解析 required_fix 猜测参数。",
                    }
                ],
                logs=["已在 H2 前绑定方法专属检查注册表与稳定 Claim ID。"],
            )
        candidate_ids = {candidate.candidate_id for candidate in candidate_set.candidates}
        recommended: list[DesignCandidate] = []
        for candidate in candidate_set.candidates:
            reviews = [
                item
                for report in reports
                for item in report.candidate_reviews
                if item.candidate_id == candidate.candidate_id
            ]
            if len(reviews) != len(reports):
                raise WorkflowTransitionError(
                    f"Reviewer did not assess every candidate: {candidate.candidate_id}"
                )
            has_reject = any(review.verdict == "reject" for review in reviews)
            has_human_blocker = any(
                _is_reviewer_issue_blocking_design(
                    issue,
                    candidate.plan.method_family,
                )
                for review in reviews
                for issue in review.issues
            )
            if (
                candidate.probe_report.verdict != "fail"
                and candidate.probe_report.executor_ready
                and not has_reject
                and not has_human_blocker
            ):
                recommended.append(candidate)
        if candidate_ids != {
            item.candidate_id
            for report in reports
            for item in report.candidate_reviews
        }:
            raise WorkflowTransitionError("Reviewer candidate ids do not match the candidate set")
        strategy_order = {
            strategy: index for index, (strategy, _rationale) in enumerate(DESIGN_STRATEGIES)
        }
        recommended.sort(
            key=lambda candidate: (
                candidate.probe_report.verdict != "pass",
                sum(
                    len(item.issues)
                    for report in reports
                    for item in report.candidate_reviews
                    if item.candidate_id == candidate.candidate_id
                ),
                strategy_order[candidate.strategy],
            )
        )
        provisional = recommended[0] if recommended else None
        arena = DesignArena(
            arena_id=f"design-arena-{uuid4()}",
            candidates=candidate_set.candidates,
            reviewer_reports=reports,
            recommended_candidate_ids=[item.candidate_id for item in recommended],
            provisional_candidate_id=(
                provisional.candidate_id if provisional is not None else None
            ),
            selection_rationale=[
                "Probe 只检查字段、结构、资产、识别条件与执行器能力，不读取模型结果。",
                "Reviewer 不投票决定真理；Probe 硬失败、明确 reject，或经方法注册表校准后仍必须人工修复的 critical 问题才会淘汰候选。",
                "可修复的 policy_causal Reviewer 风险由冻结 Test DAG 和 Claim Gate 承接，不在 H2 前预判统计结果。",
                "多个可行候选保留到 H2，由人工选择后冻结。",
            ],
        )
        self._put_artifact(state, "design_arena", arena)
        self._record_step(
            state,
            "design_arena_merge",
            "succeeded" if provisional is not None else "blocked",
            input_value={"candidate_design_set": candidate_set, "reviewer_reports": reports},
            output_value=arena,
            prompts=[
                {
                    "id": "design_arena_merge:code",
                    "role": "code",
                    "template": "Eliminate hard failures and preserve all viable candidates",
                    "rendered": "无总分、无多数投票；依据 Probe 硬约束与结构化 Reviewer 问题形成候选集。",
                }
            ],
            logs=[
                f"Reviewer Arena 完成；保留 {len(recommended)} 个可行候选。"
            ],
        )
        fallback = provisional or candidate_set.candidates[0]
        critic = (
            self._critic_report_for_candidate(arena, fallback.candidate_id)
            if provisional is not None
            else self._critic_report_for_blocked_arena(arena)
        )
        self._put_artifact(state, "analysis_plan", fallback.plan)
        self._put_artifact(state, "critic_report", critic)
        self._record_step(
            state,
            "analysis_plan_merge",
            "succeeded" if provisional is not None else "blocked",
            input_value={"design_arena_id": arena.arena_id},
            output_value=fallback.plan,
            logs=[
                "已形成 H2 暂定计划；人工仍可在可行候选中选择。"
                if provisional is not None
                else "没有候选同时通过 Probe 与 Reviewer，暂定首个方案仅供人工修订。"
            ],
        )
        if provisional is None:
            state.status = "blocked"
            state.current_node_id = "design_arena_merge"
            state.last_error = (
                "候选研究设计均存在 Probe 硬失败、Reviewer reject "
                "或必须人工修复的 critical 问题，H2 未开放。"
            )
            self._event(
                state,
                "run.blocked",
                state.last_error,
                node_id="design_arena_merge",
                status="blocked",
            )
            return
        self._pause_at_gate(
            state,
            "H2",
            {
                "design_arena": arena.model_dump(mode="json"),
                "analysis_plan": provisional.plan.model_dump(mode="json"),
                "critic_report": critic.model_dump(mode="json"),
            },
        )

    @staticmethod
    def _critic_report_for_candidate(
        arena: DesignArena,
        candidate_id: str,
    ) -> CriticReport:
        issues = [
            issue
            for report in arena.reviewer_reports
            for review in report.candidate_reviews
            if review.candidate_id == candidate_id
            for issue in review.issues
        ]
        candidate = next(
            candidate
            for candidate in arena.candidates
            if candidate.candidate_id == candidate_id
        )
        open_issues = [issue for issue in issues if issue.status == "open"]
        verdict = (
            "blocked"
            if any(
                _is_reviewer_issue_blocking_design(
                    issue,
                    candidate.plan.method_family,
                )
                for issue in open_issues
            )
            else "revise"
            if open_issues
            else "pass"
        )
        return CriticReport(
            report_id=f"arena-critic-{candidate_id}",
            review_round=1,
            verdict=verdict,
            issues=issues,
            approved_elements=[
                strength
                for report in arena.reviewer_reports
                for review in report.candidate_reviews
                if review.candidate_id == candidate_id
                for strength in review.strengths
            ],
            remaining_risks=[
                *[
                    risk
                    for report in arena.reviewer_reports
                    for risk in report.remaining_risks
                ],
                *[
                    follow_up
                    for report in arena.reviewer_reports
                    for review in report.candidate_reviews
                    if review.candidate_id == candidate_id
                    for follow_up in review.required_follow_ups
                ],
                *[_reviewer_issue_disposition(issue) for issue in issues],
            ],
        )

    @classmethod
    def _critic_report_for_blocked_arena(
        cls,
        arena: DesignArena,
    ) -> CriticReport:
        """Summarize every eliminated candidate when H2 cannot open.

        ``analysis_plan`` still carries the first draft for manual repair, but its
        candidate-specific critic must not masquerade as the diagnosis for the
        entire blocked arena. Candidate ownership remains canonical in
        ``DesignArena.reviewer_reports`` and is repeated here in plain summaries.
        """

        candidate_reports = {
            candidate.candidate_id: cls._critic_report_for_candidate(
                arena,
                candidate.candidate_id,
            )
            for candidate in arena.candidates
        }
        disposition_summaries: list[str] = [
            "该 CriticReport 汇总全部被淘汰候选；逐项候选归属以 design_arena.reviewer_reports 为准。"
        ]
        for candidate in arena.candidates:
            reviews = [
                review
                for report in arena.reviewer_reports
                for review in report.candidate_reviews
                if review.candidate_id == candidate.candidate_id
            ]
            reasons: list[str] = []
            if candidate.probe_report.verdict == "fail":
                reasons.append("Probe=fail")
            if not candidate.probe_report.executor_ready:
                reasons.append("executor_ready=false")
            rejected_dimensions = [
                report.dimension
                for report in arena.reviewer_reports
                for review in report.candidate_reviews
                if review.candidate_id == candidate.candidate_id
                and review.verdict == "reject"
            ]
            if rejected_dimensions:
                reasons.append("Reviewer reject=" + ",".join(rejected_dimensions))
            blocking_issue_ids = [
                issue.issue_id
                for review in reviews
                for issue in review.issues
                if _is_reviewer_issue_blocking_design(
                    issue,
                    candidate.plan.method_family,
                )
            ]
            if blocking_issue_ids:
                reasons.append("blocking issues=" + ",".join(blocking_issue_ids))
            disposition_summaries.append(
                f"{candidate.candidate_id}: " + ("；".join(reasons) or "未记录淘汰原因")
            )

        return CriticReport(
            report_id="arena-critic-all-candidates",
            review_round=1,
            verdict="blocked",
            issues=[
                issue
                for candidate in arena.candidates
                for issue in candidate_reports[candidate.candidate_id].issues
            ],
            approved_elements=[
                f"{candidate.candidate_id}: {item}"
                for candidate in arena.candidates
                for item in candidate_reports[candidate.candidate_id].approved_elements
            ],
            remaining_risks=[
                *disposition_summaries,
                *[
                    f"{candidate.candidate_id}: {item}"
                    for candidate in arena.candidates
                    for item in candidate_reports[candidate.candidate_id].remaining_risks
                ],
            ],
        )

    @staticmethod
    def _compile_enterprise_panel_plan(
        plan: AnalysisPlan,
        package: ResearchPackage,
        reviewer_issues: list[CriticIssue],
    ) -> AnalysisPlan:
        if plan.method_family == "policy_causal":
            # Legacy/fixture candidates may name a policy route without carrying
            # the code-owned policy-did-v2 execution contract. Keep those plans
            # design-only instead of inventing missing checks.
            if plan.check_registry_version != POLICY_DID_REGISTRY_VERSION:
                return plan
            return compile_policy_did_test_dag(plan, package.hypotheses)
        if plan.method_family not in {"panel_association", "mechanism_boundary"}:
            return plan
        return compile_enterprise_panel_test_dag(
            plan,
            package.hypotheses,
            reviewer_issues,
            mechanism_hypothesis_ids=(
                [
                    item.hypothesis_id
                    for item in package.hypotheses
                    if item.mechanism
                ]
                if plan.method_family == "mechanism_boundary"
                else []
            ),
        )

    def _refresh_h2_test_dag_if_needed(
        self,
        state: RunState,
        selected_candidate_id: str | None,
    ) -> bool:
        package = self._artifact(state, "research_package", ResearchPackage)
        if "design_arena" in state.artifacts:
            arena = self._artifact(state, "design_arena", DesignArena)
            changed = False
            candidates: list[DesignCandidate] = []
            for candidate in arena.candidates:
                critic = self._critic_report_for_candidate(
                    arena,
                    candidate.candidate_id,
                )
                compiled = self._compile_enterprise_panel_plan(
                    candidate.plan,
                    package,
                    critic.issues,
                )
                changed = changed or compiled != candidate.plan
                candidates.append(candidate.model_copy(update={"plan": compiled}))
            if not changed:
                return False
            candidate_ids = {item.candidate_id for item in candidates}
            selected = (
                selected_candidate_id
                if selected_candidate_id in candidate_ids
                else arena.provisional_candidate_id
            )
            refreshed = arena.model_copy(
                update={
                    "candidates": candidates,
                    "provisional_candidate_id": selected,
                }
            )
            assert selected is not None
            selected_candidate = next(
                item for item in candidates if item.candidate_id == selected
            )
            critic = self._critic_report_for_candidate(refreshed, selected)
            self._put_artifact(state, "design_arena", refreshed)
            self._put_artifact(state, "analysis_plan", selected_candidate.plan)
            self._put_artifact(state, "critic_report", critic)
        else:
            plan = self._artifact(state, "analysis_plan", AnalysisPlan)
            critic = self._artifact(state, "critic_report", CriticReport)
            compiled = self._compile_enterprise_panel_plan(
                plan,
                package,
                critic.issues,
            )
            if compiled == plan:
                return False
            self._put_artifact(state, "analysis_plan", compiled)
        self._record_step(
            state,
            "test_dag_compile",
            "succeeded",
            input_value={"migration": "pre-1.4.0-h2-artifact"},
            output_value={"registry_version": ENTERPRISE_PANEL_REGISTRY_VERSION},
            logs=[
                "旧 H2 Artifact 已升级为 enterprise-panel-v1；"
                "本次提交不冻结合同，等待人工重新确认。"
            ],
        )
        return True

    def _refresh_h3_claim_gate_if_needed(self, state: RunState) -> bool:
        if "claim_gate_report" in state.artifacts:
            return False
        package = self._artifact(state, "research_package", ResearchPackage)
        plan = self._artifact(state, "analysis_plan", AnalysisPlan)
        if plan.method_family not in {"panel_association", "mechanism_boundary"}:
            return False
        research_run = self._artifact(state, "research_run", ResearchRun)
        contract = self._artifact(
            state,
            "formal_research_contract",
            FormalResearchContract,
        )
        reproduction_audit = self._artifact(
            state,
            "reproduction_audit",
            ReproductionAudit,
        )
        scientific_audit = self._artifact(
            state,
            "scientific_audit",
            ScientificAudit,
        )
        candidate = self._artifact(state, "claim_ledger", ClaimLedger)
        registry_claims = code_owned_claims_for_registry(
            candidate,
            plan,
            package.hypotheses,
        )
        registry = build_evidence_registry(
            plan,
            research_run,
            registry_claims,
            reproduction_audit=reproduction_audit,
            scientific_audit=scientific_audit,
        )
        gated, report = apply_claim_gate(
            candidate,
            plan,
            research_run,
            registry,
            package.hypotheses,
            contract=contract,
            reproduction_audit=reproduction_audit,
            scientific_audit=scientific_audit,
            research_package=package,
        )
        self._put_artifact(state, "candidate_claim_ledger", candidate)
        self._put_artifact(state, "evidence_registry", registry)
        self._put_artifact(state, "claim_gate_report", report)
        self._put_artifact(state, "claim_ledger", gated)
        state.claims = gated.claims
        self._record_step(
            state,
            "claim_gate",
            "succeeded",
            input_value={"migration": "pre-1.4.0-h3-artifact"},
            output_value=report,
            logs=[
                "旧 H3 Candidate ClaimLedger 已经过确定性 Gate 重建；"
                "本次提交不执行 Claim 决定，等待人工核对新 Artifact。"
            ],
        )
        return True

    @staticmethod
    def _bind_spatial_assets(
        package: ResearchPackage,
        plan: AnalysisPlan,
    ) -> AnalysisPlan:
        if plan.method_family != "spatial" or not plan.baseline_models:
            return plan
        weights_ref = next(
            (
                item
                for item in package.dataset_refs
                if item.role == "supplementary"
                and is_spatial_weights_filename(item.filename)
            ),
            None,
        )
        spatial_keys = _names(package, "spatial_id")
        if weights_ref is None or not spatial_keys:
            return plan

        model = plan.baseline_models[0]
        spatial_model = WorkflowEngine._spatial_model_type(model)
        parameters = {
            **model.parameters,
            "spatial_weights_dataset_id": weights_ref.dataset_id,
            "spatial_weights_sha256": weights_ref.sha256,
            "spatial_id": spatial_keys[0],
        }
        if spatial_model is not None:
            parameters["spatial_model"] = spatial_model
        if spatial_model in {"sdm", "sar"}:
            parameters["effect_decomposition"] = ["direct", "indirect", "total"]
        if spatial_model == "sdm":
            parameters.update(
                {
                    "spatially_lagged_covariates": [
                        *model.treatments_or_exposures,
                        *model.controls,
                    ],
                }
            )
        baseline_models = [
            model.model_copy(update={"parameters": parameters}),
            *plan.baseline_models[1:],
        ]
        return plan.model_copy(
            update={
                "baseline_models": baseline_models,
                "required_data_fields": list(
                    dict.fromkeys([*plan.required_data_fields, spatial_keys[0]])
                ),
            }
        )

    @staticmethod
    def _validate_spatial_plan(
        package: ResearchPackage,
        plan: AnalysisPlan,
    ) -> None:
        if plan.method_family != "spatial":
            return
        problems: list[str] = []
        weights_ref = next(
            (
                item
                for item in package.dataset_refs
                if item.role == "supplementary"
                and is_spatial_weights_filename(item.filename)
            ),
            None,
        )
        spatial_keys = _names(package, "spatial_id")
        if weights_ref is None:
            problems.append("缺少冻结的 spatial_weights.csv")
        if not spatial_keys:
            problems.append("缺少 spatial_id 字段")
        if not plan.baseline_models:
            problems.append("缺少空间基准模型")
        else:
            model = plan.baseline_models[0]
            parameters = model.parameters
            spatial_model = WorkflowEngine._spatial_model_type(model)
            if spatial_model is None:
                problems.append("空间模型没有明确声明为 SDM、SAR 或 SEM")
            if weights_ref is not None and (
                parameters.get("spatial_weights_dataset_id") != weights_ref.dataset_id
                or parameters.get("spatial_weights_sha256") != weights_ref.sha256
            ):
                problems.append("空间权重资产 ID 或 SHA256 未绑定到 H2 合同")
            if spatial_keys and parameters.get("spatial_id") != spatial_keys[0]:
                problems.append("空间标识字段未绑定到权重矩阵")
            if spatial_model in {"sdm", "sar"} and set(
                parameters.get("effect_decomposition", [])
            ) != {"direct", "indirect", "total"}:
                problems.append("可分解空间模型未冻结直接、间接和总效应")
            if spatial_model == "sdm":
                regressors = {
                    *model.treatments_or_exposures,
                    *model.controls,
                }
                if set(parameters.get("spatially_lagged_covariates", [])) != regressors:
                    problems.append("SDM 未冻结全部解释变量的空间滞后项")
        if problems:
            raise WorkflowTransitionError(
                "H2 空间合同不完整：" + "；".join(problems)
            )

    async def _review_plan(
        self,
        state: RunState,
        package: ResearchPackage,
        profile: DataProfile,
        route: MethodRoute,
        initial_plan: AnalysisPlan,
    ) -> None:
        plan = initial_plan
        compact_package = self._llm_research_package(package)
        for round_number in (1, 2):
            gateway = self._gateway(state)

            async def review_dimension(dimension: str) -> CriticReport:
                return await self._llm_step(
                    state,
                    f"critic_{dimension}",
                    "method_critic",
                    {
                        "dimension": dimension,
                        "review_round": round_number,
                        "research_package": compact_package,
                        "data_profile": profile.model_dump(mode="json"),
                        "method_route": route.model_dump(mode="json"),
                        "analysis_plan": plan.model_dump(mode="json"),
                    },
                    CriticReport,
                    gateway=gateway,
                )

            reports = await _gather_llm_batches_to_terminal(
                *(review_dimension(dimension) for dimension in (
                    "measurement",
                    "causal",
                    "statistical",
                    "reproducibility",
                ))
            )
            merged = _merge_critics(reports, round_number)
            self._record_step(
                state,
                "critic_merge",
                "succeeded" if merged.verdict == "pass" else "blocked",
                input_value=reports,
                output_value=merged,
                logs=[f"四类 Critic 已汇合：{merged.verdict}。"],
            )
            self._put_artifact(state, "critic_report", merged)
            open_issues = [issue for issue in merged.issues if issue.status == "open"]
            if not open_issues and merged.verdict == "pass":
                self._put_artifact(state, "analysis_plan", plan)
                self._pause_at_gate(
                    state,
                    "H2",
                    {
                        "analysis_plan": plan.model_dump(mode="json"),
                        "critic_report": merged.model_dump(mode="json"),
                    },
                )
                return
            if any(issue.severity == "critical" and issue.repair_type == "human_required" for issue in open_issues):
                state.status = "blocked"
                state.last_error = "Critic 发现必须由人工处理的 critical 问题，H2 未开放。"
                self._event(state, "run.blocked", state.last_error, node_id="critic_merge", status="blocked")
                return
            if round_number == 2:
                state.status = "blocked"
                state.last_error = "两轮有限修复后仍有未解决问题，H2 未开放。"
                self._event(state, "run.blocked", state.last_error, node_id="critic_merge", status="blocked")
                return
            plan = await self._llm_step(
                state,
                "plan_revision",
                "plan_revision",
                {
                    "analysis_plan": plan.model_dump(mode="json"),
                    "critic_report": merged.model_dump(mode="json"),
                },
                AnalysisPlan,
            )
            self._put_artifact(state, "analysis_plan", plan)

    async def _after_h2(
        self,
        state: RunState,
        decision: DecisionRecord,
        *,
        common_execution_result: dict[str, Any] | None = None,
    ) -> None:
        state.status = "running"
        package = self._artifact(state, "research_package", ResearchPackage)
        if "design_arena" in state.artifacts:
            arena = self._artifact(state, "design_arena", DesignArena)
            selected_candidate_id = (
                decision.selected_candidate_id or arena.provisional_candidate_id
            )
            if selected_candidate_id not in arena.recommended_candidate_ids:
                raise WorkflowTransitionError(
                    "H2 must select one of the Reviewer Arena recommended candidates"
                )
            selected_candidate = next(
                candidate
                for candidate in arena.candidates
                if candidate.candidate_id == selected_candidate_id
            )
            decision.selected_candidate_id = selected_candidate_id
            plan = selected_candidate.plan
            critic = self._critic_report_for_candidate(arena, selected_candidate_id)
            self._put_artifact(state, "analysis_plan", plan)
            self._put_artifact(state, "critic_report", critic)
            self._record_step(
                state,
                "design_selection",
                "succeeded",
                input_value={
                    "design_arena_id": arena.arena_id,
                    "recommended_candidate_ids": arena.recommended_candidate_ids,
                    "human_decision": decision,
                },
                output_value=selected_candidate,
                prompts=[
                    {
                        "id": "design_selection:code",
                        "role": "code",
                        "template": "Human selects one viable candidate before contract freeze",
                        "rendered": "H2 只能选择 Probe 与 Reviewer 均未淘汰的候选；选择结果写入决策记录。",
                    }
                ],
                logs=[f"H2 选择并冻结候选 {selected_candidate_id}。"],
            )
        else:
            plan = self._artifact(state, "analysis_plan", AnalysisPlan)
            critic = self._artifact(state, "critic_report", CriticReport)
        if any(
            _is_reviewer_issue_blocking_design(issue, plan.method_family)
            for issue in critic.issues
        ):
            raise WorkflowTransitionError("H2 cannot freeze a plan with unresolved critical issues")
        if state.execution_mode == "external" and plan.method_family == "policy_causal":
            try:
                validate_policy_did_execution_plan(plan)
            except ValueError as error:
                raise WorkflowTransitionError(
                    "H2 cannot freeze an invalid policy-did-v2 execution plan: "
                    f"{error}"
                ) from error
        self._validate_spatial_plan(package, plan)
        contract = FormalResearchContract(
            contract_id=f"contract-{uuid4()}",
            case_id=state.case_id,
            approved_at=utc_now(),
            approved_by=decision.actor,
            decision_record_id=decision.decision_id,
            research_package_hash=_hash(package),
            data_hashes=[item.sha256 for item in package.dataset_refs],
            dataset_refs=package.dataset_refs,
            approved_plan_hash=_hash(plan),
            approved_plan=plan,
            prohibited_deviations=[
                "因结果不显著而删除样本或更换因变量",
                "不留记录地修改政策时间、变量口径或主模型",
                "隐藏失败、空结果或反向结果",
            ],
            allowed_technical_repairs=["路径、类型、编码和明确的程序错误修复"],
            unresolved_risks=critic.remaining_risks,
        )
        self._record_step(
            state,
            "contract_freeze",
            "succeeded",
            input_value={"analysis_plan": plan, "decision": decision},
            output_value=contract,
            prompts=[
                {
                    "id": "contract_freeze:code",
                    "role": "code",
                    "template": "Canonical JSON SHA-256 + immutable approved plan",
                    "rendered": "The approved AnalysisPlan is embedded in FormalResearchContract.",
                }
            ],
            logs=["研究包与 H2 获批计划已计算哈希并冻结。"],
        )
        self._put_artifact(state, "formal_research_contract", contract)

        # Execution mode is a code-owned boundary.  A model-authored design_only
        # flag must never route a real external case through FixtureExecutor.
        # The common board is the only alternate route: its exact result bytes
        # were validated and persisted before this method was entered.
        common_reproduction_audit: ReproductionAudit | None = None
        if common_execution_result is not None:
            from .common_executor_adapter import build_bound_research_run

            use_fixture = False
            selected_node = "common_executor"
            skipped_nodes = ("fixture_executor", "external_executor")
            research_run, common_reproduction_audit = build_bound_research_run(
                common_execution_result,
                contract,
            )
            executor_name = "benchmark-owned common executor"
            executor = None
        else:
            use_fixture = state.execution_mode == "fixture"
            executor = (
                FixtureExecutor()
                if use_fixture
                else HttpResearchExecutor(self.runtime_config_store)
            )
            selected_node = "fixture_executor" if use_fixture else "external_executor"
            skipped_nodes = (
                ("external_executor",)
                if use_fixture
                else ("fixture_executor",)
            )
            executor_name = executor.executor_name
        self._record_step(
            state,
            "execution_router",
            "succeeded",
            input_value={
                "execution_mode": (
                    "common_executor_reasoning_control"
                    if common_execution_result is not None
                    else state.execution_mode
                ),
                "design_only": plan.design_only,
            },
            output_value={"selected": selected_node},
            logs=[f"执行器路由选择 {selected_node}。"],
        )
        for skipped_node in skipped_nodes:
            self._record_step(
                state,
                skipped_node,
                "skipped",
                input_value=contract,
                logs=["互斥执行器未被选择。"],
            )
        if executor is not None:
            try:
                research_run = await executor.execute(contract)
            except Exception as error:
                self._record_step(
                    state,
                    selected_node,
                    "failed",
                    input_value=contract,
                    logs=["执行器调用失败；没有生成或补造任何统计结果。"],
                    error=str(error),
                )
                raise
        self._validate_research_run_binding(research_run, contract)
        self._record_step(
            state,
            selected_node,
            "succeeded",
            input_value=contract,
            output_value=research_run,
            logs=[f"{executor_name} 返回通过 ResearchRun Schema 的结果。"],
        )
        self._record_step(
            state,
            "research_run_merge",
            "succeeded",
            input_value={selected_node: research_run},
            output_value=research_run,
            logs=["执行状态与科学状态已分别保留。"],
        )
        state.execution_status = research_run.execution_status
        state.scientific_status = research_run.scientific_status
        state.plan_only = research_run.fixture_only or research_run.execution_status in (
            "not_executed",
            "fixture_only",
        )
        self._put_artifact(state, "research_run", research_run)
        reproduction_audit: ReproductionAudit
        if common_reproduction_audit is not None:
            reproduction_audit = common_reproduction_audit
            self._record_step(
                state,
                "replication_executor",
                "skipped",
                input_value=contract,
                output_value=reproduction_audit,
                logs=[
                    "Common-executor control supplied one execution only; "
                    "it was not relabeled as independent replication."
                ],
            )
        elif use_fixture:
            reproduction_audit = ReproductionAudit(
                audit_id=f"reproduction-{uuid4()}",
                primary_run_id=research_run.research_run_id,
                status="not_applicable",
                differences=["Fixture 不含可复现的统计结果。"],
            )
        elif plan.method_family == "spatial":
            try:
                replication_run = await executor.execute(contract)
                self._validate_research_run_binding(replication_run, contract)
                differences = self._research_run_differences(
                    research_run,
                    replication_run,
                )
                reproduction_audit = ReproductionAudit(
                    audit_id=f"reproduction-{uuid4()}",
                    primary_run_id=research_run.research_run_id,
                    replication_run_id=replication_run.research_run_id,
                    status="diverged" if differences else "matched",
                    compared_fields=[
                        "execution_status",
                        "scientific_status",
                        "executions",
                        "deviations",
                        "failed_runs",
                        "warnings",
                    ],
                    differences=differences,
                    mode="same_implementation_rerun",
                )
                self._put_artifact(state, "replication_run", replication_run)
                self._record_step(
                    state,
                    "replication_executor",
                    "succeeded",
                    input_value=contract,
                    output_value=replication_run,
                    logs=[
                        "空间路径仅做同实现重跑；明确不标记为独立统计复算。"
                    ],
                )
            except Exception as error:
                reproduction_audit = ReproductionAudit(
                    audit_id=f"reproduction-{uuid4()}",
                    primary_run_id=research_run.research_run_id,
                    status="failed",
                    differences=[str(error)],
                    mode="same_implementation_rerun",
                )
                self._record_step(
                    state,
                    "replication_executor",
                    "failed",
                    input_value=contract,
                    error=str(error),
                    logs=["同实现重跑失败；未冒充独立复算。"],
                )
        else:
            try:
                reproducer: ResearchReproducer = HttpResearchReproducer(
                    self.runtime_config_store
                )
                replication_run = await reproducer.execute(contract)
                self._validate_research_run_binding(replication_run, contract)
                reproduction_audit = compare_panel_reproduction(
                    research_run,
                    replication_run,
                )
                self._put_artifact(state, "replication_run", replication_run)
                self._record_step(
                    state,
                    "replication_executor",
                    "succeeded",
                    input_value=contract,
                    output_value=replication_run,
                    logs=[
                        (
                            f"{reproducer.reproducer_name} 重新读取冻结分析表；"
                            "政策路径共享分析表准备与事件/安慰剂变量构造，"
                            "仅使用独立 NumPy 估计器与协方差实现复算。"
                            if reproduction_audit.independence_scope
                            == "estimator_only"
                            else f"{reproducer.reproducer_name} 重新读取数据，"
                            "使用独立数据准备、NumPy 双向去均值与手工协方差复算。"
                        )
                    ],
                )
            except Exception as error:
                reproduction_audit = ReproductionAudit(
                    audit_id=f"reproduction-{uuid4()}",
                    primary_run_id=research_run.research_run_id,
                    status="failed",
                    differences=[str(error)],
                    mode="independent_implementation",
                    independence_scope=(
                        "estimator_only"
                        if plan.method_family == "policy_causal"
                        else "data_preparation_and_estimator"
                    ),
                )
                self._record_step(
                    state,
                    "replication_executor",
                    "failed",
                    input_value=contract,
                    error=str(error),
                    logs=[
                        "独立实现复算失败；没有退回同实现重跑。"
                    ],
                )
        self._put_artifact(state, "reproduction_audit", reproduction_audit)
        self._record_step(
            state,
            "reproduction_audit",
            (
                "succeeded"
                if reproduction_audit.status in {"matched", "not_applicable"}
                else "blocked"
            ),
            input_value={
                "primary_run_id": research_run.research_run_id,
                "replication_run_id": reproduction_audit.replication_run_id,
            },
            output_value=reproduction_audit,
            prompts=[
                {
                    "id": "reproduction_audit:code",
                    "role": "code",
                    "template": "Deterministic comparison of two executions of one frozen contract",
                    "rendered": "忽略运行 UUID；比较状态、估计、诊断、警告与偏离，数值容差为 1e-8。",
                }
            ],
            logs=[
                "复算审计结果："
                f"{reproduction_audit.status}；"
                f"independence_scope={reproduction_audit.independence_scope}。"
            ],
        )
        if reproduction_audit.status in {"diverged", "failed"}:
            state.status = "blocked"
            state.current_node_id = "reproduction_audit"
            state.last_error = "独立复现未通过，禁止进入结论生成。"
            self._event(
                state,
                "run.blocked",
                state.last_error,
                node_id="reproduction_audit",
                status="blocked",
            )
            return
        evidence_figure_bundle = await self._render_figure_stage(
            state,
            research_run,
            "evidence",
            allow_dataset_derivation=common_execution_result is None,
        )

        mechanism_claim_ids: set[str] = set()
        if plan.method_family in {
            "policy_causal",
            "panel_association",
            "mechanism_boundary",
        }:
            mechanism_claim_ids = {
                claim_id
                for item in schedule_test_dag(plan)
                if item.step.threat_id
                == THREAT_MECHANISM_INTERACTION_BOUNDARY
                for claim_id in item.step.target_claim_ids
            }
        allowed_claim_specs = [
            {
                "claim_id": stable_claim_id(hypothesis.hypothesis_id),
                "hypothesis_id": hypothesis.hypothesis_id,
                "claim_type": (
                    "mechanism"
                    if stable_claim_id(hypothesis.hypothesis_id)
                    in mechanism_claim_ids
                    else "associational"
                    if plan.method_family
                    in {"panel_association", "mechanism_boundary"}
                    else "causal"
                    if plan.method_family == "policy_causal"
                    else "unspecified"
                ),
            }
            for hypothesis in package.hypotheses
        ]
        evidence_registry = build_evidence_registry(
            plan,
            research_run,
            code_owned_claim_shells(plan, package.hypotheses),
            reproduction_audit=reproduction_audit,
        )
        self._put_artifact(state, "evidence_registry", evidence_registry)
        self._record_step(
            state,
            "evidence_registry",
            "succeeded",
            input_value={
                "research_run_id": research_run.research_run_id,
                "registry_version": evidence_registry.registry_version,
            },
            output_value=evidence_registry,
            logs=[
                "在模型评估前，已将冻结检查、执行终态与独立复算编译为权威 Claim 级证据。"
            ],
        )
        bundle = await self._llm_step(
            state,
            "evidence_claim_bundle",
            "evidence_claim_bundle",
            {
                "research_package": self._llm_research_package(package),
                "research_run": research_run.model_dump(mode="json"),
                "evidence_figure_bundle": evidence_figure_bundle.model_dump(
                    mode="json"
                ),
                "reproduction_audit": reproduction_audit.model_dump(mode="json"),
                "authoritative_evidence_registry": evidence_registry.model_dump(
                    mode="json"
                ),
                "allowed_claim_specs": allowed_claim_specs,
            },
            EvidenceClaimBundle,
            call_context=ModelCallContext(
                call_group="h3",
                prompt_key="evidence_claim_bundle",
            ),
        )
        assert isinstance(bundle, EvidenceClaimBundle)
        assessment = bundle.evidence_assessment
        ledger = bundle.candidate_claim_ledger
        self._put_artifact(state, "evidence_assessment", assessment)
        self._put_artifact(state, "candidate_claim_ledger", ledger)
        self._record_step(
            state,
            "evidence_assessment",
            "succeeded",
            input_value={"source_bundle": "evidence_claim_bundle"},
            output_value=assessment,
            logs=["EvidenceAssessment 已从同一个受 Schema 约束的 H3 Bundle 拆分。"],
        )
        self._record_step(
            state,
            "claim_ledger",
            "succeeded",
            input_value={
                "source_bundle": "evidence_claim_bundle",
                "allowed_claim_specs": allowed_claim_specs,
            },
            output_value=ledger,
            logs=["Candidate ClaimLedger 已原样保留，尚未经确定性 Gate 改写。"],
        )
        audit = await self._llm_step(
            state,
            "scientific_audit",
            "scientific_audit",
            {
                "contract": contract.model_dump(mode="json"),
                "research_run": research_run.model_dump(mode="json"),
                "evidence_figure_bundle": evidence_figure_bundle.model_dump(
                    mode="json"
                ),
                "reproduction_audit": reproduction_audit.model_dump(mode="json"),
                "authoritative_evidence_registry": evidence_registry.model_dump(
                    mode="json"
                ),
                "evidence_assessment": assessment.model_dump(mode="json"),
                "allowed_claim_specs": allowed_claim_specs,
                "audit_independence_policy": (
                    "Do not read or rewrite candidate claim wording; assess evidence and "
                    "contract validity only. This legacy free-text audit is advisory; "
                    "only the code-owned registry, contract, and reproduction audit can "
                    "change Claim admission."
                ),
            },
            ScientificAudit,
            gateway=self._reviewer_gateway(state),
            call_context=ModelCallContext(
                call_group="h3",
                prompt_key="scientific_audit",
            ),
        )
        assert isinstance(audit, ScientificAudit)
        state.scientific_status = research_run.scientific_status
        self._put_artifact(state, "scientific_audit", audit)
        if plan.method_family in {
            "policy_causal",
            "panel_association",
            "mechanism_boundary",
        }:
            ledger, claim_gate_report = apply_claim_gate(
                ledger,
                plan,
                research_run,
                evidence_registry,
                package.hypotheses,
                contract=contract,
                reproduction_audit=reproduction_audit,
                scientific_audit=audit,
                research_package=package,
            )
            self._put_artifact(state, "claim_gate_report", claim_gate_report)
            self._record_step(
                state,
                "claim_gate",
                "succeeded",
                input_value={
                    "candidate_claim_ledger": ledger.ledger_id.removeprefix("gated-"),
                    "evidence_registry": evidence_registry.registry_version,
                },
                output_value=claim_gate_report,
                prompts=[
                    {
                        "id": "claim_gate:code",
                        "role": "code",
                        "template": "Pure deterministic Claim Gate",
                        "rendered": "无 LLM、无随机数、无 I/O；科学审计只能收紧准入上限。",
                    }
                ],
                logs=[f"Claim Gate 已处理 {len(ledger.claims)} 条稳定 Claim。"],
            )
        elif research_run.fixture_only:
            for claim in ledger.claims:
                if claim.evidence_status != "not_tested" or claim.allowed_strength != "prohibited":
                    raise WorkflowTransitionError("Fixture Claim must be not_tested/prohibited")
        state.claims = ledger.claims
        self._put_artifact(state, "claim_ledger", ledger)
        self._pause_at_gate(state, "H3", ledger)

    async def _after_h3(self, state: RunState, request: GateDecisionRequest) -> None:
        ledger = self._artifact(state, "claim_ledger", ClaimLedger)
        run = self._artifact(state, "research_run", ResearchRun)
        package = self._artifact(state, "research_package", ResearchPackage)
        plan = self._artifact(state, "analysis_plan", AnalysisPlan)
        decisions = {item.claim_id: item for item in request.claims}
        expected_ids = {claim.claim_id for claim in ledger.claims}

        if run.fixture_only or state.plan_only:
            if request.action != "generate_plan_only":
                raise WorkflowTransitionError("Fixture/no-execution H3 only allows generate_plan_only")
            if set(decisions) != expected_ids:
                raise WorkflowTransitionError("H3 requires one decision for every Claim")
            if any(item.decision not in ("reject", "hold") for item in decisions.values()):
                raise WorkflowTransitionError("Fixture Claim can only be rejected or held")
        elif request.action not in {
            "approve",
            "generate_identification_failure_report",
        }:
            raise WorkflowTransitionError(
                "Executed research H3 requires manuscript approval or an explicit "
                "identification-failure report"
            )

        approved_claims: list[dict[str, Any]] = []
        for claim in ledger.claims:
            item = decisions.get(claim.claim_id)
            if (
                plan.method_family in {"panel_association", "mechanism_boundary"}
                and item is not None
            ):
                try:
                    validate_h3_claim_decision(
                        claim,
                        item.decision,
                        item.final_text,
                    )
                except ClaimGateError as error:
                    raise WorkflowTransitionError(str(error)) from error
            if run.fixture_only or state.plan_only:
                claim.approval_status = "rejected" if item and item.decision == "reject" else "hold"
            elif item is None:
                raise WorkflowTransitionError(f"missing H3 decision for {claim.claim_id}")
            elif item.decision == "approve":
                if claim.allowed_strength == "prohibited":
                    raise WorkflowTransitionError(
                        f"prohibited Claim cannot be approved: {claim.claim_id}"
                    )
                claim.approval_status = "approved"
                claim.final_text = item.final_text or claim.claim_text
                approved_claims.append(claim.model_dump(mode="json"))
            elif item.decision == "downgrade":
                if claim.allowed_strength == "prohibited":
                    raise WorkflowTransitionError(
                        f"prohibited Claim cannot be downgraded into the manuscript: {claim.claim_id}"
                    )
                claim.approval_status = "downgraded"
                claim.final_text = item.final_text or claim.claim_text
                approved_claims.append(claim.model_dump(mode="json"))
            elif item.decision == "reject":
                claim.approval_status = "rejected"
            else:
                claim.approval_status = "hold"
            claim.human_decision_reason = item.reason if item else request.comment
        if not (run.fixture_only or state.plan_only):
            if approved_claims and request.action != "approve":
                raise WorkflowTransitionError(
                    "An identification-failure report cannot contain an approved Claim"
                )
            if not approved_claims and request.action != "generate_identification_failure_report":
                raise WorkflowTransitionError(
                    "Executed research with no admitted Claim must generate an "
                    "identification-failure report"
                )
        state.claims = ledger.claims
        self._put_artifact(state, "approved_claim_ledger", ledger)
        authorized_figure_terms: set[str] = set()
        if approved_claims:
            writing_evidence = self._writing_evidence_pack(
                state,
                package,
                plan,
                run,
                approved_claims,
            )["writing_evidence_pack"]
            authorized_figure_terms = set(
                writing_evidence.get("writing_requirements", {}).get(
                    "authorized_estimate_terms",
                    [],
                )
            )
        await self._render_figure_stage(
            state,
            run,
            "publication",
            approved_ledger=ledger,
            allowed_estimate_terms=authorized_figure_terms,
        )

        await self._finalize_manuscript(
            state,
            package,
            plan,
            run,
            approved_claims,
        )

    def _build_identification_failure_report(
        self,
        state: RunState,
        package: ResearchPackage,
        plan: AnalysisPlan,
        run: ResearchRun,
    ) -> ManuscriptPackage:
        """Compile a useful negative scientific result without admitting a Claim."""

        registry = self._artifact(state, "evidence_registry", EvidenceRegistry)
        gate = self._artifact(state, "claim_gate_report", ClaimGateReport)
        reproduction_payload = self._artifact_payload(
            state, "reproduction_audit", required=False
        )
        reproduction_audit = (
            ReproductionAudit.model_validate(reproduction_payload)
            if isinstance(reproduction_payload, dict)
            else None
        )
        approved_ledger = self._artifact(
            state, "approved_claim_ledger", ClaimLedger
        )
        approved_claims_by_id = {
            claim.claim_id: claim for claim in approved_ledger.claims
        }

        execution_lines: list[str] = []
        execution_ids: list[str] = []
        for execution in run.executions:
            execution_ids.append(execution.execution_id)
            line = (
                f"- `{execution.plan_step_id}`：`{execution.execution_status}`"
            )
            if execution.error:
                line += f"；原因：{execution.error}"
            execution_lines.append(line)
            for estimate in execution.estimates:
                term = str(estimate.get("term") or "unnamed_term")
                values = [
                    f"coefficient={estimate.get('coefficient')}",
                    f"standard_error={estimate.get('standard_error')}",
                    f"p_value={estimate.get('p_value')}",
                ]
                execution_lines.append(
                    f"  - `{term}`：" + "，".join(values)
                    + "（仅为已执行输出，不构成获准研究主张）"
                )
            for warning in execution.warnings:
                execution_lines.append(f"  - 边界：{warning}")
        if not execution_lines:
            execution_lines.append("- 没有可核验的执行记录。")

        evidence_lines = [
            (
                f"- `{item.check_id}` / "
                f"`{item.status}` / "
                f"`{item.source_kind}`："
                f"{item.reason}"
            )
            for item in registry.evidence
        ] or ["- Evidence Registry 没有生成可用条目。"]

        gate_lines = []
        for result in gate.results:
            reasons = "；".join(result.reasons)
            claim_id = result.claim_id
            approved_claim = approved_claims_by_id.get(claim_id)
            gate_lines.append(
                f"- `{claim_id}`："
                f"admission_status=`{result.admission_status}`；"
                f"allowed_strength=`{approved_claim.allowed_strength if approved_claim else 'unknown'}`；"
                f"max_allowed_strength=`{result.max_allowed_strength}`；"
                f"approval_status=`{approved_claim.approval_status if approved_claim else 'unknown'}`；"
                f"{reasons}"
            )
        if not gate_lines:
            gate_lines.append("- Claim Gate 没有准入任何可写入论文的研究主张。")

        unresolved = [
            "ScientificAudit 为模型生成的第二意见并保留为单独工件；其自由文本未写入本报告，也不覆盖代码拥有的 Evidence Registry。",
        ]
        if reproduction_audit is not None:
            unresolved.append(
                "复算审计状态为 "
                f"{reproduction_audit.status}；mode={reproduction_audit.mode}；"
                f"independence_scope={reproduction_audit.independence_scope}。"
            )
            scope_disclosure = reproduction_scope_disclosure(reproduction_audit)
            if scope_disclosure is not None:
                unresolved.append(scope_disclosure)
        research_plan = self._research_plan_markdown(package, plan)
        sections = [
            ManuscriptSection(
                section_id="research_question_and_contract",
                title="研究问题与冻结合同",
                content_markdown=(
                    f"研究问题：{package.research_question}\n\n"
                    f"本次运行绑定计划 `{plan.plan_id}` v{plan.plan_version}。"
                    "该报告记录已执行但未形成可准入主张的科学终态，"
                    "不是计划书，也不是完整实证论文。"
                ),
                status="generated",
                run_ids=[run.research_run_id],
            ),
            ManuscriptSection(
                section_id="executed_diagnostics",
                title="已执行分析与诊断",
                content_markdown="\n".join(execution_lines),
                status="generated",
                run_ids=[run.research_run_id, *execution_ids],
            ),
            ManuscriptSection(
                section_id="evidence_and_claim_gate",
                title="证据注册表与主张准入结果",
                content_markdown=(
                    "以下状态由代码化证据链生成。模型审计只作为第二意见。\n\n"
                    + "\n".join(evidence_lines)
                    + "\n\n### Claim Gate\n\n"
                    + "\n".join(gate_lines)
                ),
                status="generated",
                run_ids=[run.research_run_id, *execution_ids],
            ),
            ManuscriptSection(
                section_id="limitations_and_next_actions",
                title="限制与下一步",
                content_markdown=(
                    "本次执行不能支持冻结假设的获准表述。后续只能在新合同中"
                    "预先登记额外数据、替代识别策略或敏感性分析；不得在看到"
                    "本次结果后原地修改规则并把重跑计为同一次确认性实验。\n\n"
                    + "\n".join(f"- {item}" for item in unresolved)
                ),
                status="generated",
                run_ids=[run.research_run_id],
            ),
        ]
        return ManuscriptPackage(
            package_id=f"identification-failure-{package.case_id}",
            case_id=package.case_id,
            mode="identification_failure_report",
            status="ready_for_human_review",
            research_plan_markdown=research_plan,
            manuscript_sections=sections,
            empirical_findings_status="executed_not_admissible",
            disclosures=[
                (
                    "本报告由代码拥有的冻结合同、执行记录、Evidence Registry、"
                    "Claim Gate 与已存在的 ReproductionAudit 编译。"
                    if reproduction_audit is not None
                    else "本报告由代码拥有的冻结合同、执行记录、Evidence Registry 与 Claim Gate 编译；本次没有可用的 ReproductionAudit。"
                ),
                "报告中的数值是未获研究主张授权的执行输出，不得解释为因果或关联结论。",
                "模型生成的 ScientificAudit 作为单独第二意见保留，其自由文本不进入确定性报告。",
            ],
            unresolved_issues=list(dict.fromkeys(unresolved)),
        )

    async def _finalize_manuscript(
        self,
        state: RunState,
        package: ResearchPackage,
        plan: AnalysisPlan,
        run: ResearchRun,
        approved_claims: list[dict[str, Any]],
        *,
        manuscript_version: int = 1,
        existing_sections: list[ManuscriptSection] | None = None,
        reuse_existing_if_valid: bool = False,
        human_review_feedback: str | None = None,
    ) -> None:
        if state.plan_only:
            writer_payload = {
                "research_package": self._llm_research_package(package),
                "analysis_plan": plan.model_dump(mode="json"),
                "research_run": run.model_dump(mode="json"),
                "approved_claims": approved_claims,
            }
            manuscript = await self._llm_step(
                state,
                "scientific_writer",
                "scientific_writer",
                writer_payload,
                ManuscriptPackage,
                gateway=FixtureModelGateway(),
            )
        elif not approved_claims:
            writer_payload = {
                "writing_evidence_pack": {
                    "mode": "identification_failure_report",
                }
            }
            manuscript = self._build_identification_failure_report(
                state,
                package,
                plan,
                run,
            )
            self._record_step(
                state,
                "scientific_writer",
                "succeeded",
                input_value={
                    "mode": "identification_failure_report",
                    "research_run_id": run.research_run_id,
                },
                output_value=manuscript,
                prompts=[
                    {
                        "id": "scientific_writer:identification_failure:code",
                        "role": "code",
                        "template": "Compile an identification-failure report from structured artifacts",
                        "rendered": "零获批 Claim 时不调用模型、不生成论文，只封存执行、证据与准入失败原因。",
                    }
                ],
                logs=["零获批 Claim 已编译为识别失败报告；未调用 Writer 模型。"],
            )
        else:
            writer_payload = self._writing_evidence_pack(
                state, package, plan, run, approved_claims
            )
            manuscript = await self._generate_full_manuscript(
                state,
                package,
                plan,
                run,
                approved_claims,
                writer_payload["writing_evidence_pack"],
                existing_sections=existing_sections,
                reuse_existing_if_valid=reuse_existing_if_valid,
                human_review_feedback=human_review_feedback,
            )
        manuscript.version = max(manuscript.version, manuscript_version)
        manuscript.figure_ids = []
        for section in manuscript.manuscript_sections:
            section.figure_ids = []
        publication_payload = self._artifact_payload(
            state,
            "publication_figure_bundle",
            required=False,
        )
        publication_bundle = (
            FigureBundle.model_validate(publication_payload)
            if isinstance(publication_payload, dict)
            else None
        )
        if (
            manuscript.mode == "full_manuscript"
            and publication_bundle is not None
            and publication_bundle.status == "succeeded"
        ):
            manuscript.figure_ids = [
                figure.figure_id for figure in publication_bundle.figures
            ]
            for section in manuscript.manuscript_sections:
                if section.section_id == "empirical_results":
                    section.figure_ids = list(manuscript.figure_ids)

        if manuscript.mode == "full_manuscript":
            self._record_step(
                state,
                "manuscript_ir_compile",
                "succeeded",
                input_value={
                    "ir_version": manuscript.ir_version,
                    "section_templates": {
                        section.section_id: section.content_template
                        for section in manuscript.manuscript_sections
                    },
                },
                output_value={
                    "compiled_sections": [
                        {
                            "section_id": section.section_id,
                            "statement_ids": [
                                statement.statement_id
                                for statement in section.statements
                            ],
                        }
                        for section in manuscript.manuscript_sections
                    ]
                },
                prompts=[
                    {
                        "id": "manuscript_ir_compile:code",
                        "role": "code",
                        "template": "Compile statement anchors from verified sources",
                        "rendered": "Re-read source JSON Pointers and inject code-formatted protected values.",
                    }
                ],
                logs=["Manuscript IR v1 已从来源语句注册表确定性编译。"],
            )

        problems: list[str] = []
        if (run.fixture_only or state.plan_only) and manuscript.mode != "research_plan_only":
            problems.append("无真实执行时成果模式必须为 research_plan_only")
        if (
            not (run.fixture_only or state.plan_only)
            and not approved_claims
            and manuscript.mode != "identification_failure_report"
        ):
            problems.append("真实执行但零获批 Claim 时必须生成识别失败报告")
        if approved_claims and manuscript.mode == "identification_failure_report":
            problems.append("存在获批 Claim 时不得生成识别失败报告")
        if run.fixture_only and manuscript.empirical_findings_status != "prohibited_fixture":
            problems.append("Fixture 必须声明 prohibited_fixture")
        approved_ids = {item["claim_id"] for item in approved_claims}
        used_ids = {
            claim_id
            for section in manuscript.manuscript_sections
            for claim_id in section.claim_ids
        }
        if not used_ids.issubset(approved_ids):
            problems.append("成果包含未经 H3 授权的 Claim")
        if manuscript.mode == "identification_failure_report" and used_ids:
            problems.append("识别失败报告不得绑定任何获批 Claim")
        allowed_run_ids = {
            run.research_run_id,
            *[execution.execution_id for execution in run.executions],
        }
        used_run_ids = {
            run_id
            for section in manuscript.manuscript_sections
            for run_id in section.run_ids
        }
        if not used_run_ids.issubset(allowed_run_ids):
            problems.append("成果引用了不存在的 ResearchRun/Execution")
        if manuscript.mode == "full_manuscript" and not approved_ids:
            problems.append("没有 H3 获批 Claim 时不得生成完整实证论文")
        if publication_bundle is not None:
            problems.extend(
                publication_figure_problems(
                    publication_bundle,
                    approved_claim_ids=approved_ids,
                    allowed_execution_ids={
                        execution.execution_id for execution in run.executions
                    },
                )
            )
            available_figure_ids = {
                figure.figure_id for figure in publication_bundle.figures
            }
            used_figure_ids = {
                *manuscript.figure_ids,
                *[
                    figure_id
                    for section in manuscript.manuscript_sections
                    for figure_id in section.figure_ids
                ],
            }
            if not used_figure_ids.issubset(available_figure_ids):
                problems.append("成果引用了不存在的 Publication Figure")
        for section in manuscript.manuscript_sections:
            if section.status == "not_generated":
                continue
            requires_trace = (
                manuscript.mode == "full_manuscript"
                and section.section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
            )
            if requires_trace and not section.claim_ids:
                problems.append(f"实证章节 {section.section_id} 没有 Claim 追踪信息")
            if requires_trace and not section.run_ids:
                problems.append(f"实证章节 {section.section_id} 没有 Run 追踪信息")
        if manuscript.mode == "full_manuscript":
            approved_ledger = self._artifact(
                state, "approved_claim_ledger", ClaimLedger
            )
            problems.extend(
                f"Manuscript IR: {problem}"
                for problem in audit_manuscript_ir(
                    manuscript,
                    approved_ledger,
                    run,
                    analysis_plan=plan,
                    reproduction_audit=self._artifact(
                        state,
                        "reproduction_audit",
                        ReproductionAudit,
                    ),
                    allowed_estimate_terms=set(
                        writer_payload["writing_evidence_pack"].get(
                            "writing_requirements", {}
                        ).get("authorized_estimate_terms", [])
                    ),
                    allowed_numeric_literals=allowed_writer_year_literals(
                        package,
                        plan,
                    ),
                )
            )
        manuscript.audit_result = "revise" if problems else "pass_with_no_critical_issues"
        if problems:
            manuscript.status = "needs_revision"
            manuscript.unresolved_issues.extend(problems)
        self._record_step(
            state,
            "consistency_audit",
            "blocked" if problems else "succeeded",
            input_value={"manuscript": manuscript, "approved_claim_ids": sorted(approved_ids)},
            output_value={"problems": problems, "audit_result": manuscript.audit_result},
            prompts=[
                {
                    "id": "consistency_audit:code",
                    "role": "code",
                    "template": "Deterministic claim authorization and fixture-boundary checks",
                    "rendered": "Every used claim_id must be H3-approved; fixture output must be plan-only.",
                }
            ],
            logs=["写作一致性确定性审计完成。"],
        )
        if problems:
            state.status = "failed"
            state.current_node_id = "scientific_writer"
            state.last_error = "论文初稿未通过一致性审计；可以调整写作后重试。"
            return
        if manuscript.mode == "full_manuscript":
            statements_by_id = {
                statement.statement_id: statement
                for section in manuscript.manuscript_sections
                for statement in section.statements
            }
            self._put_artifact(
                state,
                "manuscript_statement_registry",
                {
                    "ir_version": 1,
                    "statements": [
                        statement.model_dump(mode="json")
                        for statement in statements_by_id.values()
                    ],
                },
            )
        self._put_artifact(state, "manuscript_package", manuscript)
        self._pause_at_gate(state, "H4", manuscript)

    def _after_h4(self, state: RunState) -> None:
        if not self._has_quality_manuscript(state) and not state.plan_only:
            raise WorkflowTransitionError(
                "H4 cannot approve a manuscript that has not passed the quality audit"
            )
        if not state.plan_only:
            manuscript = self._artifact(
                state, "manuscript_package", ManuscriptPackage
            )
            ledger = self._artifact(
                state, "approved_claim_ledger", ClaimLedger
            )
            run = self._artifact(state, "research_run", ResearchRun)
            package = self._artifact(state, "research_package", ResearchPackage)
            plan = self._artifact(state, "analysis_plan", AnalysisPlan)
            if manuscript.mode == "full_manuscript":
                approved_claims = [
                    claim.model_dump(mode="json")
                    for claim in ledger.claims
                    if claim.approval_status in {"approved", "downgraded"}
                ]
                evidence_pack = self._writing_evidence_pack(
                    state,
                    package,
                    plan,
                    run,
                    approved_claims,
                )["writing_evidence_pack"]
                ir_problems = audit_manuscript_ir(
                    manuscript,
                    ledger,
                    run,
                    analysis_plan=plan,
                    reproduction_audit=self._artifact(
                        state,
                        "reproduction_audit",
                        ReproductionAudit,
                    ),
                    allowed_estimate_terms=set(
                        evidence_pack.get("writing_requirements", {}).get(
                            "authorized_estimate_terms", []
                        )
                    ),
                    allowed_numeric_literals=allowed_writer_year_literals(
                        package,
                        plan,
                    ),
                )
                if ir_problems:
                    raise WorkflowTransitionError(
                        "H4 Manuscript IR re-audit failed: "
                        + "; ".join(ir_problems)
                    )
                publication_payload = self._artifact_payload(
                    state,
                    "publication_figure_bundle",
                    required=False,
                )
                if isinstance(publication_payload, dict):
                    publication_bundle = FigureBundle.model_validate(
                        publication_payload
                    )
                    figure_problems = publication_figure_problems(
                        publication_bundle,
                        approved_claim_ids={
                            claim.claim_id
                            for claim in ledger.claims
                            if claim.approval_status
                            in {"approved", "downgraded"}
                        },
                        allowed_execution_ids={
                            execution.execution_id
                            for execution in run.executions
                        },
                    )
                    available_figure_ids = {
                        figure.figure_id
                        for figure in publication_bundle.figures
                    }
                    if set(manuscript.figure_ids) != available_figure_ids:
                        figure_problems.append(
                            "Manuscript Figure references do not match the sealed Publication FigureBundle"
                        )
                    if figure_problems:
                        raise WorkflowTransitionError(
                            "H4 Publication Figure re-audit failed: "
                            + "; ".join(figure_problems)
                        )
            elif manuscript.mode != "identification_failure_report":
                raise WorkflowTransitionError(
                    "Executed research H4 requires a full manuscript or an "
                    "identification-failure report"
                )
        self._seal_output(state)

    def _seal_output(self, state: RunState) -> None:
        source_keys = (
            "formal_research_contract",
            "analysis_plan",
            "research_run",
            "approved_claim_ledger",
            "manuscript_package",
        )
        source_hashes = {
            key: self._artifact_envelope(state, key)["sha256"]
            for key in source_keys
        }
        sealed = {
            "run_id": state.id,
            "seal_algorithm": "hmac-sha256",
            "contract_sha256": source_hashes["formal_research_contract"],
            "analysis_plan_sha256": source_hashes["analysis_plan"],
            "research_run_sha256": source_hashes["research_run"],
            "claim_ledger_sha256": source_hashes["approved_claim_ledger"],
            "manuscript_sha256": source_hashes["manuscript_package"],
        }
        for artifact_key in (
            "evidence_figure_bundle",
            "publication_figure_bundle",
        ):
            envelope = self._artifact_envelope(
                state,
                artifact_key,
                required=False,
            )
            if envelope is not None:
                sealed[f"{artifact_key}_sha256"] = envelope["sha256"]
        sealed["seal_sha256"] = sign_manifest(sealed)
        self._record_step(
            state,
            "complete",
            "succeeded",
            input_value=sealed,
            output_value=sealed,
            logs=["主 Run 已封存；隐藏 ReferencePackage 不在本进程中。"],
        )
        self._put_artifact(state, "sealed_output", sealed)
        state.status = "completed"
        state.current_gate = None
        state.current_node_id = "complete"
        self._event(state, "run.completed", "代码工作流已完成并封存。", node_id="complete", status="succeeded")

    async def _generate_full_manuscript(
        self,
        state: RunState,
        package: ResearchPackage,
        plan: AnalysisPlan,
        run: ResearchRun,
        approved_claims: list[dict[str, Any]],
        evidence_pack: dict[str, Any],
        *,
        existing_sections: list[ManuscriptSection] | None = None,
        reuse_existing_if_valid: bool = False,
        human_review_feedback: str | None = None,
    ) -> ManuscriptPackage:
        gateway = self._gateway(state)
        escalated_gateway: ModelGateway | None = None
        semaphore = asyncio.Semaphore(4)
        approved_ledger = self._artifact(
            state, "approved_claim_ledger", ClaimLedger
        )
        authorized_estimate_terms = set(
            evidence_pack.get("writing_requirements", {}).get(
                "authorized_estimate_terms", []
            )
        )
        allowed_year_literals = allowed_writer_year_literals(package, plan)
        reproduction_audit = self._artifact(
            state,
            "reproduction_audit",
            ReproductionAudit,
        )
        evidence_registry = self._artifact(
            state,
            "evidence_registry",
            EvidenceRegistry,
        )
        claim_gate_report = self._artifact(
            state,
            "claim_gate_report",
            ClaimGateReport,
        )
        statement_registry = build_statement_registry(
            approved_ledger,
            run,
            analysis_plan=plan,
            reproduction_audit=reproduction_audit,
            allowed_estimate_terms=authorized_estimate_terms,
        )
        statement_requirements = required_statements_by_section(
            statement_registry
        )
        statement_catalog_by_id = {
            item["statement_id"]: item
            for item in writer_statement_catalog(statement_registry)
        }
        code_owned_unresolved_issues = [
            evidence.reason
            for evidence in evidence_registry.evidence
            if evidence.status in {"opposing", "incomplete", "invalid"}
            and evidence.reason
        ]
        code_owned_unresolved_issues.extend(
            reason
            for result in claim_gate_report.results
            if result.admission_status != "admitted"
            for reason in result.reasons
            if reason
        )
        scope_disclosure = reproduction_scope_disclosure(reproduction_audit)
        if scope_disclosure is not None:
            code_owned_unresolved_issues.append(scope_disclosure)
        code_owned_unresolved_issues = list(
            dict.fromkeys(code_owned_unresolved_issues)
        )
        control_variable_names = [
            str(variable.get("name", ""))
            for variable in evidence_pack.get("research_context", {}).get(
                "variables", []
            )
            if variable.get("role") == "control" and variable.get("name")
        ]

        writer_identifier_keys = {
            "case_id",
            "claim_id",
            "claim_ids",
            "hypothesis_id",
            "execution_id",
            "execution_ids",
            "research_run_id",
            "plan_step_id",
            "section_id",
            "statement_id",
            "required_statement_ids",
            "verified_passage_ids",
            "name",
            "role",
            "entity_key",
            "time_key",
            "authorized_estimate_terms",
            "withheld_estimate_terms",
        }

        def scrub_writer_numbers(
            value: Any,
            *,
            allow_visible_years: bool = False,
        ) -> Any:
            return scrub_manuscript_writer_numbers(
                value,
                allowed_numeric_literals=(
                    allowed_year_literals if allow_visible_years else frozenset()
                ),
                identifier_keys=writer_identifier_keys,
            )

        def safe_writer_evidence(evidence_keys: list[str]) -> dict[str, Any]:
            context = evidence_pack.get("research_context", {})
            profile = evidence_pack.get("data_profile", {})
            design = evidence_pack.get("frozen_design", {})
            executed = evidence_pack.get("executed_evidence", {})
            requirements = evidence_pack.get("writing_requirements", {})
            safe_values: dict[str, Any] = {
                "research_context": {
                    key: context.get(key)
                    for key in (
                        "case_id",
                        "title",
                        "research_question",
                        "hypotheses",
                        "unit_of_analysis",
                        "sample_period",
                        "data_structure",
                        "variables",
                        "known_policy_facts",
                        "constraints",
                    )
                },
                "data_profile": {
                    key: profile.get(key)
                    for key in (
                        "profile_execution_status",
                        "entity_key",
                        "time_key",
                        "confirmed_facts",
                        "measurement_risks",
                        "readiness",
                        "panel_balance",
                    )
                },
                "frozen_design": {
                    key: design.get(key)
                    for key in (
                        "method_family",
                        "research_goal",
                        "sample_rules",
                        "variable_construction",
                        "baseline_models",
                        "planned_diagnostics",
                        "planned_robustness",
                        "planned_falsification",
                        "planned_mechanisms",
                        "planned_heterogeneity",
                        "identification_assumptions",
                        "alternative_explanations",
                        "unsupported_analyses",
                    )
                },
                "executed_evidence": {
                    "research_run_id": executed.get("research_run_id"),
                    "execution_status": executed.get("execution_status"),
                    "scientific_status": executed.get("scientific_status"),
                    "executions": [
                        {
                            key: execution.get(key)
                            for key in (
                                "execution_id",
                                "run_type",
                                "plan_step_id",
                                "execution_status",
                                "warnings",
                                "error",
                            )
                        }
                        for execution in executed.get("executions", [])
                    ],
                    "failed_runs": executed.get("failed_runs", []),
                    "warnings": executed.get("warnings", []),
                    "evidence_assessment": {
                        key: executed.get("evidence_assessment", {}).get(key)
                        for key in (
                            "evidence_status",
                            "execution_status",
                            "scientific_status",
                            "limitations",
                        )
                    },
                },
                "authorized_claims": [
                    {
                        key: claim.get(key)
                        for key in (
                            "claim_id",
                            "claim_type",
                            "evidence_status",
                            "allowed_strength",
                            "admission_status",
                            "scope",
                            "robustness_status",
                            "unresolved_risks",
                        )
                    }
                    for claim in evidence_pack.get("authorized_claims", [])
                ],
                "writing_requirements": {
                    key: requirements.get(key)
                    for key in (
                        "language",
                        "required_section_ids",
                        "literature_evidence_provided",
                        "tables_provided",
                        "forbid_unverified_citations",
                        "forbid_unexecuted_results",
                        "authorized_estimate_terms",
                        "withheld_estimate_terms",
                    )
                },
            }
            selected = {
                key: safe_values[key]
                for key in evidence_keys
                if key in safe_values
            }
            return {
                key: scrub_writer_numbers(
                    value,
                    allow_visible_years=key in {"research_context", "frozen_design"},
                )
                for key, value in selected.items()
            }

        def normalize_section_text(value: str) -> str:
            normalized = (
                value.replace("残分布", "残差分布")
                .replace("SDL A", "SDLA")
                .replace("\x08eta", "β")
                .replace("回归元", "回归变量")
                .replace("极易被误判", "可能被误判")
                .replace("极易被模型误判", "可能被模型误判")
                .replace("极度接近", "接近")
                .replace("极为谨慎", "谨慎")
                .replace("极度谨慎", "谨慎")
                .replace("appropriateness", "适用性")
                .replace(
                    "不存在统计上显著的",
                    "未发现达到常用统计显著性阈值的",
                )
                .replace(
                    "在控制变量取值相同且去除个体与时间均值后",
                    "在控制企业特征并吸收企业与年份固定效应后",
                )
            )
            normalized = _neutralize_limited_event_study_language(
                normalized,
                limited_or_mixed=run.scientific_status == "limited" or any(
                    claim.allowed_strength in {"mixed", "insufficient"}
                    for claim in approved_ledger.claims
                ),
            )
            for variable_name in control_variable_names:
                normalized = re.sub(
                    rf"核心解释变量\s*{re.escape(variable_name)}",
                    f"控制变量 {variable_name}",
                    normalized,
                )
            return re.sub(
                r"去除个体均值(?:和|及)时间均值后",
                "去除个体均值后",
                normalized,
            )

        def build_section_request(
            spec: dict[str, str],
            revision_feedback: list[str] | None = None,
        ) -> dict[str, Any]:
            evidence_keys = spec["evidence_keys"].split(",")
            section_spec = {
                key: value
                for key, value in spec.items()
                if key != "evidence_keys"
            }
            section_spec.pop("target_characters", None)
            section_spec["length_guidance"] = (
                "保持简洁但覆盖研究问题、方法、证据边界与结论"
                if spec["section_id"] in {"abstract", "conclusion"}
                else "充分展开本节论证，但不重复其他章节"
            )
            required_statement_ids = statement_requirements.get(
                spec["section_id"], []
            )
            section_spec["required_statement_ids"] = required_statement_ids
            section_spec["statement_catalog"] = [
                statement_catalog_by_id[statement_id]
                for statement_id in required_statement_ids
            ]
            if required_statement_ids:
                statement_anchor_policy = (
                    "本章只能使用 required_statement_ids 中的锚点，且每个锚点"
                    "必须恰好出现一次。所有实证判断，包括方向、显著性、样本、"
                    "模型结果和已完成检验，都只能由这些锚点承担；不得在锚点"
                    "前后改写、概括或补充同义实证判断。"
                )
            else:
                statement_anchor_policy = (
                    "本章 required_statement_ids 和 statement_catalog 均为空；"
                    "禁止输出任何 [[STATEMENT:...]] 锚点，也禁止自行撰写"
                    "方向、显著性、样本、模型结果或已完成检验等实证判断。"
                )
            section_spec["statement_anchor_policy"] = statement_anchor_policy
            section_spec["verified_passage_ids"] = []
            section_spec["formal_citations_allowed"] = False
            if not evidence_pack["writing_requirements"].get(
                "literature_evidence_provided"
            ):
                section_spec["forbidden_phrases"] = (
                    "现有研究、现有文献、参考文献惯例、参照文献、"
                    "弥补空白、鲜有研究、尚缺乏研究"
                )
            if not evidence_pack.get("research_context", {}).get(
                "known_policy_facts"
            ):
                section_spec["unsupported_background_phrases"] = [
                    "普遍存在",
                    "普遍面临",
                    "随着某项制度或关注度变化",
                    "日益成为",
                    "备受关注",
                ]
            if not evidence_pack["writing_requirements"].get("tables_provided"):
                section_spec["unavailable_assets"] = [
                    "table",
                    "figure",
                    "appendix",
                ]
            frozen_design = evidence_pack.get("frozen_design", {})
            frozen_categories = {
                "diagnostics": frozen_design.get("planned_diagnostics", []),
                "robustness": frozen_design.get("planned_robustness", []),
                "falsification": frozen_design.get("planned_falsification", []),
                "mechanisms": frozen_design.get("planned_mechanisms", []),
                "heterogeneity": frozen_design.get("planned_heterogeneity", []),
            }
            section_spec["empty_frozen_plan_categories"] = [
                category
                for category, steps in frozen_categories.items()
                if not steps
            ]
            section_spec["frozen_plan_steps"] = frozen_categories
            if not frozen_categories["mechanisms"]:
                section_spec["mechanism_evidence_status"] = (
                    "未冻结也未执行实证机制检验；可以讨论条件性的理论路径，"
                    "但基准系数方向不能验证机制，也不能把机制检验写成后续计划。"
                )
            frozen_plan_text = json.dumps(
                frozen_categories,
                ensure_ascii=False,
            )
            if "内生性" not in frozen_plan_text:
                section_spec["endogeneity_plan_status"] = (
                    "冻结计划没有单列内生性处理步骤；可以把内生性写成未解决风险，"
                    "但不能声称已有对应的冻结步骤。"
                )
            measurement_risks = evidence_pack.get("data_profile", {}).get(
                "measurement_risks",
                [],
            )
            section_spec["allowed_measurement_risks"] = measurement_risks
            if not measurement_risks:
                section_spec["measurement_risk_policy"] = (
                    "输入没有提供测量口径变迁风险；不得自行推测评级方法、"
                    "数据库口径或数据提供方在年份间发生变化。"
                )
            evidence_assessment = evidence_pack.get("executed_evidence", {}).get(
                "evidence_assessment",
                {},
            )
            section_spec["allowed_unresolved_risks"] = [
                *measurement_risks,
                *frozen_design.get("alternative_explanations", []),
                *evidence_assessment.get("limitations", []),
                *[
                    risk
                    for claim in evidence_pack.get("authorized_claims", [])
                    for risk in claim.get("unresolved_risks", [])
                ],
            ]
            section_spec["executed_run_types"] = [
                execution.get("run_type")
                for execution in evidence_pack.get("executed_evidence", {}).get(
                    "executions", []
                )
                if execution.get("execution_status") == "succeeded"
            ]
            completed_run_counts = {
                run_type: section_spec["executed_run_types"].count(run_type)
                for run_type in (
                    "diagnostic",
                    "robustness",
                    "falsification",
                    "mechanism",
                    "heterogeneity",
                )
            }
            category_run_types = {
                "diagnostics": "diagnostic",
                "robustness": "robustness",
                "falsification": "falsification",
                "mechanisms": "mechanism",
                "heterogeneity": "heterogeneity",
            }
            section_spec["completed_frozen_plan_categories"] = [
                category
                for category, steps in frozen_categories.items()
                if steps
                and completed_run_counts[category_run_types[category]] >= len(steps)
            ]
            section_spec["pending_frozen_plan_categories"] = [
                category
                for category, steps in frozen_categories.items()
                if steps
                and completed_run_counts[category_run_types[category]] < len(steps)
            ]
            if not section_spec["pending_frozen_plan_categories"]:
                section_spec["execution_completion_policy"] = (
                    "所有非空的冻结检验类别均已完成；不得把其中任何步骤写成"
                    "尚待执行、后续执行或未来计划。未来工作只能说明超出冻结计划的"
                    "新分析需要新数据、新识别设计和另行审批。"
                )
            if not any(
                run_type in {"data_preparation", "data_cleaning", "data_merge"}
                for run_type in section_spec["executed_run_types"]
            ):
                section_spec["input_data_status"] = (
                    "输入案例包已提供预处理后的分析数据；"
                    "本系统没有数据清洗、跨库匹配、合并或变量构造的成功执行记录，"
                    "只能把成功的模型运行写成实际完成工作。"
                )
            if reproduction_audit.independence_scope == "estimator_only":
                section_spec["reproduction_scope_policy"] = (
                    "本轮复算的独立性仅覆盖估计器与协方差实现；分析表准备及"
                    "事件研究和安慰剂变量构造与主流程共享。不得称为端到端独立复现，"
                    "范围限制只能由获准的复算范围 statement 锚点承担。"
                )
            payload = {
                "section_spec": scrub_writer_numbers(section_spec),
                "evidence": safe_writer_evidence(evidence_keys),
            }
            if revision_feedback:
                def safe_revision_problem(problem: str) -> str:
                    redacted = re.sub(
                        r"\[\[STATEMENT:[^\]]+\]\]",
                        "[非法锚点已移除]",
                        problem,
                    ).replace("H4", "__H_FOUR_STAGE__")
                    scrubbed = str(scrub_writer_numbers(redacted))
                    return scrubbed.replace("__H_FOUR_STAGE__", "H4")

                safe_problems = [
                    safe_revision_problem(problem)
                    for problem in revision_feedback
                    if "Manuscript IR 编译失败" not in problem
                ]
                safe_problems.append(statement_anchor_policy)
                payload["revision_feedback"] = {
                    "instruction": (
                        "上一版未通过内容或 Manuscript IR 审计。"
                        "请重写本节，不要只做表面替换；所有实证判断"
                        "必须完全由本章获准锚点承担。"
                    ),
                    "problems": list(dict.fromkeys(safe_problems)),
                }
            return payload

        spec_by_id = {
            spec["section_id"]: spec for spec in MANUSCRIPT_SECTION_SPECS
        }

        def compile_writer_draft(
            draft: ManuscriptSectionDraft,
            *,
            section_gateway: ModelGateway,
        ) -> ManuscriptSection:
            spec = spec_by_id.get(draft.section_id)
            if spec is None:
                raise ValueError(f"writer returned unknown section_id={draft.section_id}")
            required_statement_ids = statement_requirements.get(
                draft.section_id, []
            )
            content_template = normalize_section_text(
                draft.content_template
            )
            returned_anchors = re.findall(
                r"\[\[STATEMENT:[A-Za-z0-9_.:-]+\]\]",
                content_template,
            )
            if (
                not returned_anchors
                and required_statement_ids
                and getattr(section_gateway, "provider_name", None) != "qwen"
            ):
                content_template += "\n\n" + "\n".join(
                    f"[[STATEMENT:{statement_id}]]"
                    for statement_id in required_statement_ids
                )
            return compile_section_draft(
                ManuscriptSectionDraft(
                    section_id=draft.section_id,
                    content_template=content_template,
                ),
                statement_registry,
                title=spec["title"],
                required_statement_ids=required_statement_ids,
                research_run_id=run.research_run_id,
                allowed_numeric_literals=allowed_year_literals,
            )

        async def write_batch(
            specs: list[dict[str, str]],
            feedback_by_id: dict[str, list[str]] | None = None,
            *,
            writer_batch_index: int,
            logical_suffix: str,
        ) -> tuple[list[ManuscriptSection], dict[str, list[str]]]:
            nonlocal escalated_gateway
            feedback_by_id = feedback_by_id or {}
            section_gateway = gateway
            if feedback_by_id and getattr(gateway, "provider_name", None) == "qwen":
                if escalated_gateway is None:
                    escalated_gateway = QwenModelGateway(
                        model_override=WRITER_ESCALATION_MODEL,
                        budget=self._model_budget(state),
                        config_store=self.runtime_config_store,
                    )
                section_gateway = escalated_gateway
            requests = [
                build_section_request(
                    spec,
                    feedback_by_id.get(spec["section_id"]),
                )
                for spec in specs
            ]
            if section_gateway is escalated_gateway:
                for request in requests:
                    request["section_spec"]["writer_model_policy"] = {
                        "tier": "escalated_after_quality_failure",
                        "model": WRITER_ESCALATION_MODEL,
                    }
            section_specs = []
            for request in requests:
                section_spec = {
                    **request["section_spec"],
                    "safe_evidence": request["evidence"],
                }
                if "revision_feedback" in request:
                    section_spec["revision_feedback"] = request[
                        "revision_feedback"
                    ]
                section_specs.append(section_spec)
            call_context = ModelCallContext(
                logical_call_id=(
                    f"{state.id}:manuscript_section_draft_batch_"
                    f"{writer_batch_index}"
                ),
                call_group="h4",
                prompt_key="manuscript_section_draft_batch",
                attempt_type=("content_repair" if feedback_by_id else "primary"),
            )
            async with semaphore:
                result = await self._llm_step(
                    state,
                    "scientific_writer",
                    "manuscript_section_draft_batch",
                    {"section_specs": section_specs},
                    ManuscriptSectionDraftBatch,
                    gateway=section_gateway,
                    call_context=call_context,
                )
            assert isinstance(result, ManuscriptSectionDraftBatch)
            expected_ids = {spec["section_id"] for spec in specs}
            returned_ids = {draft.section_id for draft in result.sections}
            if returned_ids != expected_ids:
                raise ValueError(
                    "writer batch section mismatch: "
                    f"expected {sorted(expected_ids)}, got {sorted(returned_ids)}"
                )
            by_id = {draft.section_id: draft for draft in result.sections}
            compiled: list[ManuscriptSection] = []
            compile_problems: dict[str, list[str]] = {}
            for spec in specs:
                section_id = spec["section_id"]
                try:
                    compiled.append(
                        compile_writer_draft(
                            by_id[section_id],
                            section_gateway=section_gateway,
                        )
                    )
                except ManuscriptIRError as error:
                    problem = f"{section_id} Manuscript IR 编译失败：{error}"
                    compile_problems[section_id] = [problem]
                    self._record_step(
                        state,
                        "scientific_writer",
                        "failed",
                        input_value={
                            "source_logical_call_id": call_context.logical_call_id,
                            "logical_suffix": logical_suffix,
                            "section_id": section_id,
                        },
                        logs=["该章节未通过 Manuscript IR 编译，将定向修复。"],
                        error=str(error),
                    )
            for section in compiled:
                self._record_step(
                    state,
                    "scientific_writer",
                    "succeeded",
                    input_value={
                        "source_logical_call_id": call_context.logical_call_id,
                        "logical_suffix": logical_suffix,
                        "section_id": section.section_id,
                    },
                    output_value=section,
                    logs=[
                        f"{section.section_id} 已从 Writer Batch 编译为可追溯 Manuscript IR 章节。"
                    ],
                )
            return compiled, compile_problems

        sections = list(existing_sections or [])
        deterministic_fallback_section_ids: list[str] = []
        if any(section.content_template is None for section in sections):
            try:
                rebuilt_sections: list[ManuscriptSection] = []
                for section in sections:
                    if section.content_template is not None:
                        rebuilt_sections.append(section)
                        continue
                    required_statement_ids = statement_requirements.get(
                        section.section_id, []
                    )
                    content_template = normalize_section_text(
                        section.content_markdown
                    )
                    if required_statement_ids and not re.findall(
                        r"\[\[STATEMENT:[A-Za-z0-9_.:-]+\]\]",
                        content_template,
                    ):
                        content_template += "\n\n" + "\n".join(
                            f"[[STATEMENT:{statement_id}]]"
                            for statement_id in required_statement_ids
                        )
                    rebuilt_sections.append(
                        compile_section_draft(
                            ManuscriptSectionDraft(
                                section_id=section.section_id,
                                content_template=content_template,
                            ),
                            statement_registry,
                            title=section.title,
                            required_statement_ids=required_statement_ids,
                            research_run_id=run.research_run_id,
                            allowed_numeric_literals=allowed_year_literals,
                        )
                    )
                sections = rebuilt_sections
            except ManuscriptIRError:
                # IR0 with hand-written numbers, citations or empirical claims
                # is not migrated in place; regenerate it from the safe catalog.
                sections = []
        if reuse_existing_if_valid and sections:
            try:
                rebuilt_sections = []
                canonical_by_id = {
                    statement.statement_id: statement
                    for statement in statement_registry
                }
                for section in sections:
                    template = normalize_section_text(section.content_markdown)
                    required_statement_ids = statement_requirements.get(
                        section.section_id, []
                    )
                    for statement_id in required_statement_ids:
                        statement = canonical_by_id[statement_id]
                        template = template.replace(
                            render_statement(statement),
                            f"[[STATEMENT:{statement_id}]]",
                        )
                    template = template.replace(
                        "未发现达到常用统计显著性阈值的关联",
                        "相关证据边界由核验语句给出",
                    )
                    if required_statement_ids and not re.findall(
                        r"\[\[STATEMENT:[A-Za-z0-9_.:-]+\]\]",
                        template,
                    ):
                        template += "\n\n" + "\n".join(
                            f"[[STATEMENT:{statement_id}]]"
                            for statement_id in required_statement_ids
                        )
                    rebuilt_sections.append(
                        compile_section_draft(
                            ManuscriptSectionDraft(
                                section_id=section.section_id,
                                content_template=template,
                            ),
                            statement_registry,
                            title=section.title,
                            required_statement_ids=required_statement_ids,
                            research_run_id=run.research_run_id,
                            allowed_numeric_literals=allowed_year_literals,
                        )
                    )
                sections = rebuilt_sections
            except ManuscriptIRError:
                sections = []
        content_problems = (
            self._manuscript_content_problems(sections, evidence_pack)
            if sections
            else []
        )
        present_section_ids = {section.section_id for section in sections}
        content_problems.extend(
            f"{section_id} Manuscript IR 章节在上一次写作中未成功编译"
            for section_id in FULL_MANUSCRIPT_SECTION_IDS
            if section_id not in present_section_ids
        )
        if sections and human_review_feedback:
            content_problems.extend(
                f"{section_id} H4 人工审稿意见：{human_review_feedback}"
                for section_id in self._human_review_target_sections(
                    human_review_feedback
                )
            )
        if not sections or (not content_problems and not reuse_existing_if_valid):
            results = await asyncio.gather(
                *(
                    write_batch(
                        [spec_by_id[section_id] for section_id in section_ids],
                        writer_batch_index=batch_index,
                        logical_suffix=f"initial-{batch_index}",
                    )
                    for batch_index, section_ids in enumerate(
                        WRITER_SECTION_BATCHES, 1
                    )
                ),
                return_exceptions=True,
            )
            failures = [
                result for result in results if isinstance(result, BaseException)
            ]
            if failures:
                raise RuntimeError(
                    "论文分节写作未完成："
                    + "；".join(str(error) for error in failures)
                )
            generated_by_id = {
                section.section_id: section
                for result in results
                if isinstance(result, tuple)
                for section in result[0]
                if isinstance(section, ManuscriptSection)
            }
            compile_problems = {
                section_id: problems
                for result in results
                if isinstance(result, tuple)
                for section_id, problems in result[1].items()
            }
            expected_section_ids = {
                spec["section_id"] for spec in MANUSCRIPT_SECTION_SPECS
            }
            sections = [
                generated_by_id[spec["section_id"]]
                for spec in MANUSCRIPT_SECTION_SPECS
                if spec["section_id"] in generated_by_id
            ]
            missing_without_compile_failure = (
                expected_section_ids - set(generated_by_id) - set(compile_problems)
            )
            if missing_without_compile_failure:
                raise RuntimeError(
                    "writer batches did not return all eight sections: "
                    + ", ".join(sorted(missing_without_compile_failure))
                )
            content_problems = [
                problem
                for problems in compile_problems.values()
                for problem in problems
            ]
            content_problems.extend(
                self._manuscript_content_problems(
                    sections,
                    evidence_pack,
                )
            )
        for repair_round in range(1, MAX_MANUSCRIPT_REPAIR_ROUNDS + 1):
            if not content_problems:
                break
            problem_ids = {
                problem.split(" ", 1)[0]
                for problem in content_problems
            }
            repair_specs = [
                spec
                for spec in MANUSCRIPT_SECTION_SPECS
                if spec["section_id"] in problem_ids
            ]
            feedback_by_id = {
                spec["section_id"]: [
                    problem
                    for problem in content_problems
                    if problem.startswith(spec["section_id"] + " ")
                ]
                for spec in repair_specs
            }
            repair_batches = [
                (
                    batch_index,
                    [
                        spec_by_id[section_id]
                        for section_id in section_ids
                        if section_id in feedback_by_id
                    ],
                )
                for batch_index, section_ids in enumerate(
                    WRITER_SECTION_BATCHES,
                    1,
                )
            ]
            repair_batches = [
                (batch_index, batch)
                for batch_index, batch in repair_batches
                if batch
            ]
            repair_outcomes = list(
                await asyncio.gather(
                    *(
                        write_batch(
                            batch,
                            feedback_by_id,
                            writer_batch_index=batch_index,
                            logical_suffix=f"repair-{repair_round}-{batch_index}",
                        )
                        for batch_index, batch in repair_batches
                    ),
                    return_exceptions=True,
                )
            )
            for outcome in repair_outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome,
                    Exception,
                ):
                    raise outcome
            repair_results = [
                outcome
                for outcome in repair_outcomes
                if not isinstance(outcome, Exception)
            ]
            repair_call_problems = {
                spec["section_id"]: [
                    f'{spec["section_id"]} Writer 第 {repair_round} 轮'
                    "定向修复批次未完成"
                ]
                for (_batch_index, batch), outcome in zip(
                    repair_batches,
                    repair_outcomes,
                    strict=True,
                )
                if isinstance(outcome, Exception)
                for spec in batch
            }
            repairs = [
                section
                for batch, _compile_problems in repair_results
                for section in batch
            ]
            repair_compile_problems = {
                section_id: problems
                for _batch, problems_by_id in repair_results
                for section_id, problems in problems_by_id.items()
            }
            repaired_by_id = {
                section.section_id: section
                for section in repairs
                if isinstance(section, ManuscriptSection)
            }
            sections_by_id = {
                section.section_id: section for section in sections
            }
            sections_by_id.update(repaired_by_id)
            sections = [
                sections_by_id[section_id]
                for section_id in FULL_MANUSCRIPT_SECTION_IDS
                if section_id in sections_by_id
            ]
            content_problems = [
                problem
                for problems in repair_call_problems.values()
                for problem in problems
            ]
            content_problems.extend(
                problem
                for problems in repair_compile_problems.values()
                for problem in problems
            )
            content_problems.extend(
                self._manuscript_content_problems(
                    sections,
                    evidence_pack,
                )
            )
            present_section_ids = {section.section_id for section in sections}
            content_problems.extend(
                f"{section_id} Manuscript IR 章节在定向修复中未成功编译"
                for section_id in FULL_MANUSCRIPT_SECTION_IDS
                if section_id not in present_section_ids
                and section_id not in repair_compile_problems
            )
        if content_problems:
            fallback_section_ids = [
                section_id
                for section_id in FULL_MANUSCRIPT_SECTION_IDS
                if any(
                    problem.startswith(section_id + " ")
                    for problem in content_problems
                )
            ]
            if fallback_section_ids:
                sections_by_id = {
                    section.section_id: section for section in sections
                }
                fallback_compile_problems = (
                    _deterministic_safe_fallback_quality_problems(
                        fallback_section_ids
                    )
                )
                fallback_ids_to_build = (
                    [] if fallback_compile_problems else fallback_section_ids
                )
                for section_id in fallback_ids_to_build:
                    source_problems = [
                        problem
                        for problem in content_problems
                        if problem.startswith(section_id + " ")
                    ]
                    sections_by_id.pop(section_id, None)
                    required_statement_ids = statement_requirements.get(
                        section_id, []
                    )
                    content_template = DETERMINISTIC_SAFE_SECTION_TEXTS[
                        section_id
                    ]
                    if required_statement_ids:
                        content_template += "\n\n" + "\n".join(
                            f"[[STATEMENT:{statement_id}]]"
                            for statement_id in required_statement_ids
                        )
                    fallback_input = {
                        "fallback_type": "deterministic_safe_fallback",
                        "section_id": section_id,
                        "source_problems": source_problems,
                    }
                    try:
                        fallback_section = compile_section_draft(
                            ManuscriptSectionDraft(
                                section_id=section_id,
                                content_template=content_template,
                            ),
                            statement_registry,
                            title=spec_by_id[section_id]["title"],
                            required_statement_ids=required_statement_ids,
                            research_run_id=run.research_run_id,
                            allowed_numeric_literals=allowed_year_literals,
                        )
                    except ManuscriptIRError as error:
                        fallback_compile_problems.append(
                            f"{section_id} deterministic_safe_fallback "
                            f"Manuscript IR 编译失败：{error}"
                        )
                        self._record_step(
                            state,
                            "scientific_writer",
                            "failed",
                            input_value=fallback_input,
                            prompts=[
                                {
                                    "id": (
                                        "scientific_writer:"
                                        f"deterministic_safe_fallback:{section_id}"
                                    ),
                                    "role": "code",
                                    "template": (
                                        "Fixed non-empirical section scaffold plus "
                                        "required statement anchors"
                                    ),
                                    "rendered": content_template,
                                }
                            ],
                            logs=[
                                "deterministic_safe_fallback 仍未通过原始 "
                                "Manuscript IR 编译器。"
                            ],
                            error=str(error),
                        )
                        continue
                    sections_by_id[section_id] = fallback_section
                    deterministic_fallback_section_ids.append(section_id)
                    self._record_step(
                        state,
                        "scientific_writer",
                        "succeeded",
                        input_value=fallback_input,
                        output_value=fallback_section,
                        prompts=[
                            {
                                "id": (
                                    "scientific_writer:"
                                    f"deterministic_safe_fallback:{section_id}"
                                ),
                                "role": "code",
                                "template": (
                                    "Fixed non-empirical section scaffold plus "
                                    "required statement anchors"
                                ),
                                "rendered": content_template,
                            }
                        ],
                        logs=[
                            "deterministic_safe_fallback 仅替换两轮定向修复后"
                            "仍失败的章节；未调用模型，并已重新通过"
                            "原始 Manuscript IR 编译器。"
                        ],
                    )
                sections = [
                    sections_by_id[section_id]
                    for section_id in FULL_MANUSCRIPT_SECTION_IDS
                    if section_id in sections_by_id
                ]
                content_problems = fallback_compile_problems
                content_problems.extend(
                    self._manuscript_content_problems(
                        sections,
                        evidence_pack,
                    )
                )
                present_section_ids = {
                    section.section_id for section in sections
                }
                content_problems.extend(
                    f"{section_id} deterministic_safe_fallback 未生成可编译章节"
                    for section_id in fallback_section_ids
                    if section_id not in present_section_ids
                )
        if content_problems:
            error = ValueError("；".join(content_problems))
            self._record_step(
                state,
                "scientific_writer",
                "failed",
                input_value={
                    "generated_sections": [section.section_id for section in sections]
                },
                logs=["论文章节已生成，但通用内容质量规则未通过。"],
                error=str(error),
            )
            raise error
        try:
            return ManuscriptPackage(
                package_id=f"manuscript-{package.case_id}",
                case_id=package.case_id,
                mode="full_manuscript",
                status="ready_for_human_review",
                research_plan_markdown=self._research_plan_markdown(package, plan),
                manuscript_sections=sections,
                empirical_findings_status="included",
                ir_version=1,
                disclosures=[
                    "文献证据与正式引文待补充；当前初稿未编造参考文献。",
                    "稳健性、证伪、机制与异质性分析如未出现在 ResearchRun 中，均只是待执行计划。",
                    "实证结论仅使用 H3 授权的 Claim，并保留 execution_status 与 scientific_status 的区分。",
                    "模型生成的 ScientificAudit 作为独立第二意见工件保留；"
                    "其 critical_issues 与 unresolved_risks 自由文本不进入 Writer、"
                    "正文或封存 ManuscriptPackage 元数据。",
                    "固定分组换样本、事件窗口、伪时点、置换与复算边界"
                    "由代码拥有的 Manuscript IR statements 编译。",
                    *(
                        [
                            "以下章节在两轮 Writer 修复后仍未通过安全审计，"
                            "已改用代码拥有的确定性安全模板并重新通过 Manuscript IR："
                            + "、".join(deterministic_fallback_section_ids)
                            + "。"
                        ]
                        if deterministic_fallback_section_ids
                        else []
                    ),
                ],
                unresolved_issues=code_owned_unresolved_issues,
            )
        except Exception as error:
            self._record_step(
                state,
                "scientific_writer",
                "failed",
                input_value={
                    "generated_sections": [
                        {
                            "section_id": section.section_id,
                            "character_count": len(section.content_markdown.strip()),
                        }
                        for section in sections
                    ]
                },
                logs=["论文分节均已返回，但整体完整度门槛未通过。"],
                error=str(error),
            )
            raise

    @staticmethod
    def _manuscript_content_problems(
        sections: list[ManuscriptSection],
        evidence_pack: dict[str, Any],
    ) -> list[str]:
        requirements = evidence_pack.get("writing_requirements", {})
        literature_provided = bool(
            requirements.get("literature_evidence_provided")
        )
        tables_provided = bool(requirements.get("tables_provided"))
        background_facts = evidence_pack.get("research_context", {}).get(
            "known_policy_facts", []
        )
        measurement_risks = evidence_pack.get("data_profile", {}).get(
            "measurement_risks", []
        )
        panel_balance = evidence_pack.get("data_profile", {}).get(
            "panel_balance", "unknown"
        )
        frozen_design = evidence_pack.get("frozen_design", {})
        planned_mechanisms = frozen_design.get("planned_mechanisms", [])
        frozen_design_text = str(frozen_design)
        planned_falsification_text = " ".join(
            str(value)
            for value in frozen_design.get("planned_falsification", [])
        ).lower()
        research_goal = frozen_design.get("research_goal")
        scientific_status = evidence_pack.get("executed_evidence", {}).get(
            "scientific_status"
        )
        reproduction_payload = evidence_pack.get("executed_evidence", {}).get(
            "reproduction_audit"
        )
        reproduction_audit = (
            ReproductionAudit.model_validate(reproduction_payload)
            if isinstance(reproduction_payload, dict)
            else None
        )
        executed_records = evidence_pack.get("executed_evidence", {}).get(
            "executions", []
        )
        entity_fixed_effects = any(
            bool(record.get("diagnostic_results", {}).get("entity_fixed_effects"))
            for record in executed_records
            if isinstance(record, dict)
        )
        data_preparation_executed = any(
            record.get("execution_status") == "succeeded"
            and str(record.get("run_type", "")).lower()
            in {"data_preparation", "data_cleaning", "data_merge"}
            for record in executed_records
            if isinstance(record, dict)
        )
        robustness_executed = any(
            record.get("execution_status") == "succeeded"
            and record.get("run_type") == "robustness"
            for record in executed_records
            if isinstance(record, dict)
        )
        baseline_record = next(
            (
                record
                for record in executed_records
                if isinstance(record, dict)
                and record.get("run_type") == "baseline"
                and record.get("execution_status") == "succeeded"
            ),
            {},
        )
        baseline_diagnostics = baseline_record.get("diagnostic_results", {})
        input_row_count = evidence_pack.get("data_profile", {}).get("row_count")
        baseline_rows_used = baseline_diagnostics.get("rows_used")
        baseline_controls = {
            str(control)
            for model in frozen_design.get("baseline_models", [])
            for control in model.get("controls", [])
        }
        declared_control_fields = {
            str(variable.get("name"))
            for variable in evidence_pack.get("research_context", {}).get("variables", [])
            if variable.get("role") == "control" and variable.get("name")
        }
        baseline_control_roots = {
            field[:-2] if field.endswith("_w") else field
            for field in baseline_controls
        }
        unsupported_control_labels = {
            str(variable.get("label"))
            for variable in evidence_pack.get("research_context", {}).get("variables", [])
            if variable.get("role") == "control"
            and variable.get("name")
            and variable.get("label")
            and (
                str(variable.get("name"))[:-2]
                if str(variable.get("name")).endswith("_w")
                else str(variable.get("name"))
            )
            not in baseline_control_roots
        }
        derived_targets = {
            str(step.get("parameters", {}).get("target"))
            for step in frozen_design.get("variable_construction", [])
            if step.get("parameters", {}).get("target")
        }
        executed_run_types = {
            str(record.get("run_type"))
            for record in executed_records
            if isinstance(record, dict)
            and record.get("execution_status") == "succeeded"
        }
        planned_steps_by_run_type = {
            "diagnostic": frozen_design.get("planned_diagnostics", []),
            "robustness": frozen_design.get("planned_robustness", []),
            "falsification": frozen_design.get("planned_falsification", []),
            "mechanism": frozen_design.get("planned_mechanisms", []),
            "heterogeneity": frozen_design.get("planned_heterogeneity", []),
        }
        successful_run_counts = {
            run_type: sum(
                1
                for record in executed_records
                if isinstance(record, dict)
                and record.get("execution_status") == "succeeded"
                and record.get("run_type") == run_type
            )
            for run_type in planned_steps_by_run_type
        }
        all_frozen_steps_executed = all(
            not steps or successful_run_counts[run_type] >= len(steps)
            for run_type, steps in planned_steps_by_run_type.items()
        )
        problems: list[str] = []
        withheld_estimate_terms = requirements.get(
            "withheld_estimate_terms", []
        )
        explicit_plan_absence_markers = (
            "为空",
            "未预设",
            "未纳入",
            "未被纳入",
            "未包含",
            "未列入",
            "未单列",
            "未提供",
            "缺乏",
            "未在",
            "未冻结",
            "没有单列",
            "没有计划",
            "不存在",
            "不预设",
            "不执行",
            "不涉及",
            "不讨论",
            "不包含",
            "不会",
            "不在冻结",
            "不属于冻结",
            "不在本研究计划",
            "超出冻结计划",
            "另行审批",
            "推测性质",
        )
        for section in sections:
            content = section.content_markdown
            if reproduction_scope_overclaim(content, reproduction_audit):
                problems.append(
                    f"{section.section_id} 将受限复算范围写成端到端独立复现"
                )
            if (
                section.section_id == "data_variables"
                and isinstance(input_row_count, int)
                and isinstance(baseline_rows_used, int)
                and input_row_count != baseline_rows_used
            ):
                input_formats = {
                    str(input_row_count),
                    f"{input_row_count:,}",
                }
                sample_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if any(value in sentence for value in input_formats)
                    and re.search(r"(?:最终|进入|用于).{0,18}(?:基准|回归|有效样本)", sentence)
                ]
                if sample_sentences:
                    problems.append(
                        "data_variables 将输入总行数误写为基准模型有效样本量"
                    )
                used_formats = {
                    str(baseline_rows_used),
                    f"{baseline_rows_used:,}",
                }
                if not any(value in content for value in used_formats):
                    problems.append(
                        "data_variables 未报告基准模型删除缺失、重复键和单例后的实际样本量"
                    )
            for target in derived_targets:
                if not target or target not in content:
                    continue
                target_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if target in sentence
                    and re.search(r"输入(?:数据|案例包).{0,20}(?:生成|已有|保留)", sentence)
                ]
                if target_sentences:
                    problems.append(
                        f"{section.section_id} 将执行时构造字段 {target} 错写为输入数据已有"
                    )
            for run_type, marker in (
                ("diagnostic", "诊断"),
                ("robustness", "稳健性"),
                ("falsification", "证伪"),
                ("mechanism", "机制"),
                ("heterogeneity", "异质性"),
            ):
                if run_type not in executed_run_types:
                    continue
                pending_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if marker in sentence
                    and re.search(
                        r"(?:尚待执行|待执行|尚未执行|需完成|后续执行|计划使用|计划执行|后续.{0,18}(?:执行|使用|完成))",
                        sentence,
                    )
                    and not any(
                        negation in sentence
                        for negation in (
                            "没有尚待",
                            "并非尚待",
                            "不再待执行",
                            "暂无待执行",
                            "暂无额外待执行",
                            "无额外待执行",
                            "另行审批",
                            "新设计",
                            "新的",
                        )
                    )
                ]
                if pending_sentences:
                    problems.append(
                        f"{section.section_id} 将已成功执行的{marker}步骤写成待执行"
                    )
            controlled_terms = re.findall(
                r"(?:模型|回归).{0,16}(?:已)?控制\s*([A-Za-z][A-Za-z0-9_]*)",
                content,
            )
            for term in controlled_terms:
                if term not in baseline_controls:
                    problems.append(
                        f"{section.section_id} 声称基准模型控制了冻结计划之外的 {term}"
                    )
            for term in declared_control_fields - baseline_controls:
                escaped_term = rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])"
                unsupported_control_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if re.search(escaped_term, sentence)
                    and re.search(
                        r"(?:模型|回归).{0,30}(?:控制|纳入).{0,50}" + escaped_term,
                        sentence,
                    )
                    and not any(
                        marker in sentence
                        for marker in ("未控制", "没有控制", "未纳入", "不纳入", "移除")
                    )
                ]
                if unsupported_control_sentences:
                    problems.append(
                        f"{section.section_id} 声称基准模型控制了冻结计划之外的 {term}"
                    )
            for label in unsupported_control_labels:
                label_pattern = (
                    rf"(?<![A-Za-z0-9_]){re.escape(label)}(?![A-Za-z0-9_])"
                    if label.isascii()
                    else re.escape(label)
                )
                unsupported_label_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if re.search(label_pattern, sentence)
                    and re.search(r"(?:模型|回归).{0,40}(?:纳入|控制).{0,80}", sentence)
                    and "控制变量" in sentence
                    and not any(
                        marker in sentence
                        for marker in ("未控制", "没有控制", "未纳入", "不纳入", "移除")
                    )
                ]
                if unsupported_label_sentences:
                    problems.append(
                        f"{section.section_id} 声称基准模型控制了冻结计划之外的变量标签 {label}"
                    )
            interaction_main_effect_sentences = [
                sentence
                for sentence in re.split(r"[。！？；\n]", content)
                if "交互边界" in sentence
                and re.search(r"(?:主效应|核心解释变量).{0,24}不显著", sentence)
                and not re.search(r"交互项.{0,24}显著", sentence)
            ]
            if interaction_main_effect_sentences:
                problems.append(
                    f"{section.section_id} 用主效应显著性代替交互项判断调节边界"
                )
            contradictory_interaction_paragraphs = [
                paragraph
                for paragraph in re.split(r"\n\s*\n", content)
                if re.search(r"交互项.{0,50}显著", paragraph)
                and re.search(
                    r"(?:主效应|核心解释变量).{0,40}(?:不显著|失去.{0,8}显著性)",
                    paragraph,
                )
                and re.search(
                    r"(?:无法确认|不足以支持).{0,45}(?:调节|交互).{0,10}边界",
                    paragraph,
                )
            ]
            if contradictory_interaction_paragraphs:
                problems.append(
                    f"{section.section_id} 已承认交互项显著，却因主效应不显著否认调节边界"
                )
            if (
                section.section_id == "discussion_limitations"
                and all_frozen_steps_executed
                and re.search(r"后续可执行的检验(?:步骤)?包括", content)
            ):
                problems.append(
                    "discussion_limitations 将已完成的冻结检验整体列为后续执行"
                )
            if (
                section.section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
                and (research_goal == "associational" or scientific_status == "limited")
                and re.search(r"对应(?:提高|降低|上升|下降|减少)", content)
            ):
                problems.append(
                    f"{section.section_id} 将关联系数写成方向性变化而非对应差异"
                )
            for term in withheld_estimate_terms:
                escaped_term = rf"(?<![A-Za-z0-9_]){re.escape(str(term))}(?![A-Za-z0-9_])"
                result_marker = (
                    r"(?:系数|估计值|标准误|p\s*[=<]|p\s*值|显著|"
                    r"置信区间|直接效应|间接效应|总效应)"
                )
                unauthorized_result_pattern = re.compile(
                    rf"(?:{escaped_term}.{{0,20}}{result_marker}|"
                    rf"{result_marker}.{{0,20}}{escaped_term})",
                    flags=re.IGNORECASE,
                )
                unauthorized_result_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if unauthorized_result_pattern.search(sentence)
                ]
                if unauthorized_result_sentences:
                    problems.append(
                        f"{section.section_id} 写入了 H3 未授权估计项 {term}"
                    )
            certainty_content = re.sub(
                r"(?:不能|无法|未能)(?:彻底|完全)排除",
                "",
                content,
            )
            certainty_content = re.sub(
                r"(?:并非|并不|而非|不是).{0,12}必然(?:结果|关系|影响)?",
                "",
                certainty_content,
            )
            if not tables_provided and re.search(
                r"(?:表|图)\s*[0-9一二三四五六七八九十]+",
                content,
            ):
                problems.append(
                    f"{section.section_id} 引用了未提供的图表"
                )
            if not literature_provided and (
                re.search(
                    r"现有研究.{0,30}(?:多|主要|集中|缺乏|鲜有|尚未|空白)",
                    content,
                )
                or re.search(r"(?:参照|参考).{0,8}文献", content)
                or re.search(
                    r"(?:缺乏|尚无).{0,24}(?:针对|关于).{0,16}(?:直接)?(?:经验|实证|文献)证据",
                    content,
                )
            ):
                problems.append(
                    f"{section.section_id} 声称了未提供证据的文献状况"
                )
            if (
                not literature_provided
                and re.search(
                    r"遵循(?:常规|主流|通行).{0,16}(?:研究|实证).{0,12}做法",
                    content,
                )
            ):
                problems.append(
                    f"{section.section_id} 声称了未提供证据的研究惯例"
                )
            if re.search(
                r"(?:frozen_design|executed_evidence|authorized_claims|scientific_status|ResearchRun|ClaimLedger)",
                content,
                flags=re.IGNORECASE,
            ):
                problems.append(
                    f"{section.section_id} 泄露了工作流内部字段名"
                )
            if (
                section.section_id
                in {"introduction", "theory_hypotheses", "data_variables"}
                and re.search(
                    r"(?:极易|必然|一定会|保证了|有效避免|彻底排除|完全排除|显著(?:增加|降低|提升).{0,12}(?:风险|压力|可能性))",
                    certainty_content,
                )
            ):
                problems.append(
                    f"{section.section_id} 使用了无证据支撑的强确定性表述"
                )
            if re.search(r"(?:具有|达到|呈现).{0,8}较高(?:的)?精度", content):
                problems.append(
                    f"{section.section_id} 对统计精度作了无比较基准的判断"
                )
            invented_measurement_risk_sentences = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if re.search(
                    r"(?:评级体系|评级方法|评分体系|评分方法|数据库口径|统计口径|数据提供方|底层数据).{0,60}(?:调整|变迁|变化|改变|更新频率)|(?:得分|评分).{0,60}权重.{0,12}(?:调整|变化|改变)",
                    sentence,
                )
                and not any(
                    marker in sentence
                    for marker in (
                        "不涉及",
                        "不推断",
                        "不得推断",
                        "未提供",
                        "没有提供",
                    )
                )
            ]
            if not measurement_risks and invented_measurement_risk_sentences:
                problems.append(
                    f"{section.section_id} 增加了输入未提供的数据口径变迁风险"
                )
            if re.search(
                r"(?:企业|个体)和(?:年份|时间)层面的不随时间变化的异质性",
                content,
            ):
                problems.append(
                    f"{section.section_id} 混淆了个体固定效应与年份固定效应的含义"
                )
            if (
                not planned_mechanisms
                and re.search(
                    r"(?:(?:暂不|不再|不对).{0,8}(?:传导路径|作用渠道|理论机制).{0,4}(?:讨论|推测|分析)|(?:暂不|不再|不对).{0,8}(?:讨论|推测|分析).{0,12}(?:传导路径|作用渠道|理论机制)|机制(?:分析|路径)?.{0,30}不在.{0,20}讨论范围)",
                    content,
                )
            ):
                problems.append(
                    f"{section.section_id} 将未执行机制检验误写为不讨论理论机制"
                )
            if (
                not planned_mechanisms
                and re.search(
                    r"(?:显著为正|显著为负|系数.{0,20}(?:为正|为负)).{0,70}(?:支持|验证|证明).{0,28}(?:机制|路径|解释)",
                    content,
                )
            ):
                problems.append(
                    f"{section.section_id} 将系数方向误写为机制得到支持"
                )
            if entity_fixed_effects and re.search(
                r"(?:两家|不同)企业.{0,65}(?:相差|比较|对应)",
                content,
            ):
                problems.append(
                    f"{section.section_id} 将个体固定效应系数误写为企业间比较"
                )
            if (
                entity_fixed_effects
                and section.section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
            ):
                between_entity_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？\n]", content)
                    if re.search(
                        r"(?:得分|评分|表现|水平|取值).{0,10}(?:较高|更高)的(?:企业|个体|地区).{0,50}(?:对应|具有|表现为).{0,20}(?:较低|更低|较高|更高)",
                        sentence,
                    )
                ]
                unqualified_unit_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？\n]", content)
                    if re.search(
                        r"相差.{0,12}(?:单位).{0,35}(?:对应|平均相差)",
                        sentence,
                    )
                    and not any(
                        marker in sentence
                        for marker in (
                            "同一企业内",
                            "同一家",
                            "同一个体",
                            "同一主体",
                            "企业内",
                            "个体内",
                            "随时间",
                            "不同时点",
                        )
                    )
                ]
                if between_entity_sentences or unqualified_unit_sentences:
                    problems.append(
                        f"{section.section_id} 未按个体内随时间变化解释固定效应系数"
                    )
            if section.section_id == "theory_hypotheses" and not literature_provided:
                unconditional_mechanism_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？\n]", content)
                    if re.search(
                        r"(?:是.{0,20}(?:关键|主要).{0,8}(?:路径|机制)|(?:减少了|降低了|提升了|改善了).{0,60}(?:使|从而|进而)|(?:使得|导致).{0,45}(?:增加|降低|减少|提升|改善))",
                        sentence,
                    )
                    and not any(
                        marker in sentence
                        for marker in ("可能", "或许", "若", "如果", "假设", "理论上")
                    )
                ]
                if unconditional_mechanism_sentences:
                    problems.append(
                        "theory_hypotheses 将无文献支持的理论机制写成既定事实"
                    )
            endogeneity_plan_claims = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if (
                    re.search(
                        r"(?:冻结|预设|计划).{0,55}内生性(?:处理|检验)步骤",
                        sentence,
                    )
                    or re.search(
                        r"(?:依据|按照).{0,12}冻结计划.{0,35}(?:处理|解决|缓解).{0,16}内生性",
                        sentence,
                    )
                )
                and not any(
                    marker in sentence
                    for marker in explicit_plan_absence_markers
                )
            ]
            if endogeneity_plan_claims and "内生性" not in frozen_design_text:
                problems.append(
                    f"{section.section_id} 声称冻结计划包含不存在的内生性步骤"
                )
            if "残分布" in content:
                problems.append(
                    f"{section.section_id} 存在残差分布术语缺字"
                )
            if re.search(
                r"(?:R.?\s*平方|R²|R\^2).{0,24}(?:合理|较高|较低|理想)",
                content,
                flags=re.IGNORECASE,
            ):
                problems.append(
                    f"{section.section_id} 对拟合指标作了无比较基准的价值判断"
                )
            unearned_robustness_sentences = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if re.search(
                    r"(?:(?:稳定|稳健|可靠).{0,6}(?:关联|结果|系数|发现|证据)|(?:关联|结果|系数|发现|证据).{0,6}(?:稳定|稳健|可靠)(?!性))",
                    sentence,
                )
                and not any(
                    marker in sentence
                    for marker in (
                        "尚不能",
                        "不能",
                        "无法",
                        "未能",
                        "尚未",
                        "未验证",
                        "待检验",
                        "待执行",
                        "有待",
                        "不得",
                        "不代表",
                    )
                )
            ]
            if not robustness_executed and unearned_robustness_sentences:
                problems.append(
                    f"{section.section_id} 在未执行稳健性检验时声称结果稳定"
                )
            overstated_within_fit_sentences = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if re.search(
                    r"(?:组内\s*R|Within\s+R).{0,80}(?:控制变量与固定效应|固定效应.{0,20}(?:解释|贡献))",
                    sentence,
                    flags=re.IGNORECASE,
                )
                and not any(
                    marker in sentence
                    for marker in ("不代表", "不应归因", "不能归因", "并非")
                )
            ]
            if overstated_within_fit_sentences:
                problems.append(
                    f"{section.section_id} 错误解释了固定效应模型的组内拟合指标"
                )
            if re.search(
                r"(?:组内\s*R|Within\s+R|R²).{0,80}(?:去除|扣除).{0,24}时间趋势",
                content,
                flags=re.IGNORECASE,
            ):
                problems.append(
                    f"{section.section_id} 错误扩大了组内拟合指标的含义"
                )
            if re.search(
                r"(?:组内\s*R|Within\s+R|R²).{0,90}(?:去除|扣除).{0,30}(?:时间均值|年份均值|时间效应)",
                content,
                flags=re.IGNORECASE,
            ):
                problems.append(
                    f"{section.section_id} 将组内拟合指标误写为同时去除时间均值"
                )
            overstated_residual_sentences = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if re.search(
                    r"残差分布.{0,70}(?:验证|确认).{0,24}模型(?:设定|假设).{0,12}(?:合理|有效|正确)",
                    sentence,
                )
                and not any(
                    marker in sentence
                    for marker in (
                        "不能",
                        "无法",
                        "不得",
                        "不应",
                        "未解决",
                        "尚未解决",
                    )
                )
            ]
            if overstated_residual_sentences:
                problems.append(
                    f"{section.section_id} 夸大了残差分布检查的诊断能力"
                )
            if re.search(
                r"(?:确保|保证).{0,24}(?:推断|检验).{0,16}(?:可靠|有效|正确)",
                content,
            ):
                problems.append(
                    f"{section.section_id} 将标准误处理写成保证推断可靠"
                )
            overstated_endogeneity_sentences = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if re.search(
                    r"(?:稳健性|证伪|检验).{0,55}(?:剥离|消除|解决).{0,16}内生性",
                    sentence,
                )
                and not any(
                    marker in sentence
                    for marker in (
                        "不能",
                        "无法",
                        "不得",
                        "不应",
                        "未解决",
                        "尚未解决",
                    )
                )
            ]
            if overstated_endogeneity_sentences:
                problems.append(
                    f"{section.section_id} 夸大了稳健性或证伪检验对内生性的作用"
                )
            if not background_facts and re.search(
                r"(?:评级|评分).{0,28}(?:用作|作为).{0,12}(?:抵押|信用增级)",
                content,
            ):
                problems.append(
                    f"{section.section_id} 增加了输入未提供的融资工具安排"
                )
            if re.search(
                r"(?:剔除|删除|排除).{0,24}(?:个体|企业|年份|时间).{0,12}固定效应",
                content,
            ):
                problems.append(
                    f"{section.section_id} 将控制固定效应错写为剔除固定效应"
                )
            if (
                section.section_id == "introduction"
                and re.search(
                    r"(?:引言|本节)(?:部分)?的(?:核心)?任务(?:在于|是)",
                    content,
                )
            ):
                problems.append(
                    "introduction 泄露了写作任务元叙述"
                )
            execution_claim_content = re.sub(
                r"基于(?:已)?预处理后的",
                "基于输入的",
                content,
            )
            invented_data_preparation_sentences = [
                sentence
                for sentence in re.split(r"[。！？；\n]", execution_claim_content)
                if re.search(
                    r"(?:本稿|本文|本研究|本系统).{0,40}(?:完成|已(?:经)?).{0,70}(?:清理|清洗|匹配|合并|预处理|变量构造|缩尾)",
                    sentence,
                )
            ]
            if not data_preparation_executed and invented_data_preparation_sentences:
                problems.append(
                    f"{section.section_id} 将输入数据准备误写为本系统已执行"
                )
            if not data_preparation_executed and re.search(
                r"本系统.{0,24}(?:验证|核验).{0,45}(?:原始值|处理值|缩尾边界|对应关系)",
                content,
            ):
                problems.append(
                    f"{section.section_id} 声称执行了没有运行记录的数据核验"
                )
            if not planned_mechanisms:
                future_mechanism_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？\n]", content)
                    if "机制" in sentence
                    and any(
                        marker in sentence
                        for marker in (
                            "后续",
                            "计划",
                            "冻结",
                            "待执行",
                            "尚未执行",
                            "优先执行",
                            "将进一步",
                        )
                    )
                    and not any(
                        marker in sentence
                        for marker in explicit_plan_absence_markers
                    )
                ]
                if future_mechanism_sentences:
                    problems.append(
                        f"{section.section_id} 把未冻结的机制分析写成后续计划"
                    )
            unplanned_method_patterns = (
                ("工具变量", r"(?:工具变量|instrumental\s+variables?|\b2SLS\b|\bIV\b)"),
                ("倾向得分", r"(?:倾向得分|\bPSM\b)"),
                ("双重差分", r"(?:双重差分|\bDID\b)"),
                ("广义矩估计", r"(?:广义矩|\bGMM\b)"),
                ("断点回归", r"(?:断点回归|\bRDD\b)"),
                ("合成控制", r"合成控制"),
                ("空间计量", r"(?:空间计量|空间杜宾|\bSDM\b|\bSAR\b)"),
                ("中介检验", r"(?:中介检验|中介效应)"),
                ("门槛模型", r"门槛模型"),
                ("安慰剂检验", r"安慰剂"),
            )
            for method_name, pattern in unplanned_method_patterns:
                if re.search(pattern, frozen_design_text, flags=re.IGNORECASE):
                    continue
                for sentence in re.split(r"[。！？\n]", content):
                    if not re.search(pattern, sentence, flags=re.IGNORECASE):
                        continue
                    if any(
                        marker in sentence
                        for marker in explicit_plan_absence_markers + (
                            "另行审批",
                            "新设计获批",
                            "不报告",
                        )
                    ):
                        continue
                    problems.append(
                        f"{section.section_id} 擅自加入冻结计划之外的{method_name}"
                    )
                    break
            has_planned_lead = (
                "lead" in planned_falsification_text
                or "领先" in planned_falsification_text
                or "超前" in planned_falsification_text
            )
            has_planned_lag = (
                "lag" in planned_falsification_text
                or "滞后" in planned_falsification_text
            )
            if "滞后项" in content and not has_planned_lag:
                problems.append(
                    f"{section.section_id} 擅自把冻结的时间检验扩展为滞后项"
                )
            if (
                ("领先项" in content or "超前项" in content)
                and not has_planned_lead
            ):
                problems.append(
                    f"{section.section_id} 擅自把冻结的时间检验扩展为领先项"
                )
            if (
                section.section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
                and (research_goal == "associational" or scientific_status == "limited")
                and re.search(
                    r"(?:每|当).{0,20}(?:提高|提升|增加|上升|下降|降低).{0,55}(?:提高|提升|增加|上升|下降|降低|减少).{0,24}(?:单位|%|百分点)",
                    content,
                )
            ):
                problems.append(
                    f"{section.section_id} 将关联系数写成了单位变化的因果效果"
                )
            if (
                section.section_id == "introduction"
                and not background_facts
                and re.search(
                    r"(?:普遍存在|普遍面临|日益成为|备受.{0,8}关注|随着.{0,20}(?:强化|发展|提高|增加|提升|完善|推进|演进|普及))",
                    content,
                )
            ):
                problems.append(
                    "introduction 声称了未提供证据的市场或学界趋势"
                )
            if "不随时间变化但随时间演变" in content:
                problems.append(
                    f"{section.section_id} 对时变因素作了自相矛盾的描述"
                )
            if (
                panel_balance == "unbalanced"
                and re.search(r"(?<!非)平衡面板", content)
            ):
                problems.append(
                    f"{section.section_id} 将真实非平衡面板错写为平衡面板"
                )
            if panel_balance == "balanced" and "非平衡面板" in content:
                problems.append(
                    f"{section.section_id} 将真实平衡面板错写为非平衡面板"
                )
        return problems

    @staticmethod
    def _human_review_target_sections(comment: str) -> list[str]:
        normalized = comment.casefold()
        keywords = {
            "abstract": ("摘要", "abstract"),
            "introduction": ("引言", "introduction"),
            "theory_hypotheses": ("理论", "假设", "theory", "hypoth"),
            "data_variables": ("数据", "变量", "data", "variable"),
            "research_design": ("研究设计", "方法", "design", "method"),
            "empirical_results": ("实证", "结果", "result"),
            "discussion_limitations": ("讨论", "局限", "discussion", "limitation"),
            "conclusion": ("结论", "conclusion"),
        }
        targets = [
            section_id
            for section_id, markers in keywords.items()
            if any(marker in normalized for marker in markers)
        ]
        return targets or list(FULL_MANUSCRIPT_SECTION_IDS)

    @staticmethod
    def _research_plan_markdown(
        package: ResearchPackage,
        plan: AnalysisPlan,
    ) -> str:
        def names(items: list[Any]) -> str:
            values = [item.name for item in items]
            return "、".join(values) if values else "本轮未预设"

        return (
            f"# {package.title}：后续研究计划\n\n"
            f"## 研究问题\n{package.research_question}\n\n"
            f"## 冻结基准设计\n方法家族：{plan.method_family}。"
            f"基准模型：{names(plan.baseline_models)}。\n\n"
            f"## 待执行检验\n诊断：{names(plan.diagnostics)}。\n"
            f"稳健性：{names(plan.robustness_tests)}。\n"
            f"证伪：{names(plan.falsification_tests)}。\n"
            f"机制：{names(plan.mechanism_tests)}。\n"
            f"异质性：{names(plan.heterogeneity_tests)}。\n\n"
            "## 执行原则\n保持 H2 冻结的样本、变量和模型定义；"
            "任何偏离都需要记录，不得因显著性改变分析。"
        )

    @staticmethod
    def _writing_evidence_pack(
        state: RunState,
        package: ResearchPackage,
        plan: AnalysisPlan,
        run: ResearchRun,
        approved_claims: list[dict[str, Any]],
    ) -> dict[str, Any]:
        data_profile = WorkflowEngine._artifact_payload(
            state, "data_profile", required=False
        ) or {}
        method_route = WorkflowEngine._artifact_payload(
            state, "method_route", required=False
        ) or {}
        evidence_assessment = WorkflowEngine._artifact_payload(
            state, "evidence_assessment", required=False
        ) or {}
        scientific_audit = WorkflowEngine._artifact_payload(
            state, "scientific_audit", required=False
        ) or {}
        reproduction_audit = WorkflowEngine._artifact_payload(
            state, "reproduction_audit", required=False
        )

        def remove_legacy_warning(values: list[Any]) -> list[Any]:
            return [
                value
                for value in values
                if value != LEGACY_OVERBROAD_EXECUTION_WARNING
            ]

        evidence_assessment = {
            **evidence_assessment,
            "limitations": remove_legacy_warning(
                evidence_assessment.get("limitations", [])
            ),
        }
        scientific_audit = {
            **scientific_audit,
            "unresolved_risks": remove_legacy_warning(
                scientific_audit.get("unresolved_risks", [])
            ),
        }
        approved_claims = [
            {
                **claim,
                "unresolved_risks": remove_legacy_warning(
                    claim.get("unresolved_risks", [])
                ),
            }
            for claim in approved_claims
        ]
        diagnostics = (
            run.executions[0].diagnostic_results
            if run.executions
            else {}
        )
        authorized_claim_text = "\n".join(
            str(claim.get("final_text") or claim.get("claim_text") or "")
            for claim in approved_claims
        )

        variable_labels = {
            variable.name: variable.label
            for variable in package.variables
        }

        def mentions_term(term: str, *, allow_label: bool = True) -> bool:
            exact_match = bool(
                re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                    authorized_claim_text,
                    flags=re.IGNORECASE,
                )
            )
            if exact_match or not allow_label:
                return exact_match
            base_term = term.split(":", 1)[-1]
            label = variable_labels.get(base_term, "").strip()
            return bool(label and label in authorized_claim_text)

        plan_steps = {
            step.step_id: step
            for step in (
                *plan.baseline_models,
                *plan.diagnostics,
                *plan.robustness_tests,
                *plan.falsification_tests,
                *plan.mechanism_tests,
                *plan.heterogeneity_tests,
            )
        }
        baseline = plan.baseline_models[0] if plan.baseline_models else None
        baseline_exposures = set(
            baseline.treatments_or_exposures if baseline is not None else []
        )
        bound_execution_ids = {
            str(run_id)
            for claim in approved_claims
            for run_id in (
                *claim.get("supporting_runs", []),
                *claim.get("opposing_runs", []),
            )
            if run_id
        }
        all_executions_bound = run.research_run_id in bound_execution_ids

        def focal_terms_for_execution(execution: ExecutionRecord) -> set[str]:
            step = plan_steps.get(execution.plan_step_id)
            if step is None:
                return set()
            parameters = step.parameters
            if plan.method_family == "policy_causal":
                estimate_terms = {
                    str(estimate.get("term", ""))
                    for estimate in execution.estimates
                    if estimate.get("term")
                }
                if parameters.get("policy_event_study"):
                    return {
                        term
                        for term in estimate_terms
                        if re.fullmatch(r"event_(?:[0-9]{4}|remote_pre)", term)
                    }
                if parameters.get("policy_placebo"):
                    return {
                        term
                        for term in estimate_terms
                        if re.fullmatch(r"placebo_exposure_[0-9]{4}", term)
                    }
                return set(baseline_exposures)
            alternative_exposure = str(
                parameters.get("alternative_exposure") or ""
            ).strip()
            if alternative_exposure:
                return {alternative_exposure}
            lead_exposure = str(parameters.get("lead_exposure") or "").strip()
            if lead_exposure:
                return {lead_exposure}
            mechanism = str(
                parameters.get("mediator")
                or parameters.get("moderator")
                or parameters.get("mechanism_variable")
                or ""
            ).strip()
            interaction = str(parameters.get("interaction_term") or "").strip()
            if execution.run_type == "mechanism" or mechanism or interaction:
                if not interaction and mechanism and baseline_exposures:
                    interaction = f"{next(iter(baseline_exposures))}_x_{mechanism}"
                return baseline_exposures | ({interaction} if interaction else set())
            return set(baseline_exposures)

        marginal_terms = {
            str(estimate.get("term", ""))
            for execution in run.executions
            for estimate in execution.estimates
            if estimate.get("estimate_type") == "average_marginal_effect"
        }
        effect_markers = {
            "direct": ("直接", "direct"),
            "indirect": ("间接", "indirect", "溢出"),
            "total": ("总效应", "总关联", "direct and indirect", "total"),
        }

        def estimate_is_authorized(
            execution: ExecutionRecord,
            estimate: dict[str, Any],
        ) -> bool:
            term = str(estimate.get("term", ""))
            if not term:
                return False
            if plan.method_family in {
                "panel_association",
                "mechanism_boundary",
                "policy_causal",
            }:
                if (
                    not all_executions_bound
                    and execution.execution_id not in bound_execution_ids
                ):
                    return False
                return term in focal_terms_for_execution(execution)
            if term.casefold() == "rho":
                return True
            if estimate.get("estimate_type") == "average_marginal_effect":
                effect_type = str(estimate.get("effect_type", ""))
                return mentions_term(term) and any(
                    marker.casefold() in authorized_claim_text.casefold()
                    for marker in effect_markers.get(effect_type, (effect_type,))
                    if marker
                )
            if term in marginal_terms:
                return False
            if term.startswith("W:"):
                return mentions_term(term, allow_label=False)
            return mentions_term(term)

        execution_payloads: list[dict[str, Any]] = []
        all_estimate_terms: set[str] = set()
        authorized_estimate_terms: set[str] = set()
        for execution in run.executions:
            payload = execution.model_dump(mode="json")
            estimates = payload.get("estimates", [])
            all_estimate_terms.update(
                str(estimate.get("term", ""))
                for estimate in estimates
                if estimate.get("term")
            )
            payload["estimates"] = [
                estimate
                for estimate in estimates
                if estimate_is_authorized(execution, estimate)
            ]
            authorized_estimate_terms.update(
                str(estimate.get("term", ""))
                for estimate in payload["estimates"]
                if estimate.get("term")
            )
            execution_payloads.append(payload)
        entity_count = diagnostics.get("entity_count")
        time_count = diagnostics.get("time_period_count")
        rows_used = diagnostics.get("rows_used")
        panel_balance = "unknown"
        if all(isinstance(value, int) for value in (entity_count, time_count, rows_used)):
            panel_balance = (
                "balanced"
                if rows_used == entity_count * time_count
                else "unbalanced"
            )
        return {
            "writing_evidence_pack": {
                "research_context": {
                    "case_id": package.case_id,
                    "title": package.title,
                    "research_question": package.research_question,
                    "hypotheses": [item.model_dump(mode="json") for item in package.hypotheses],
                    "unit_of_analysis": package.unit_of_analysis,
                    "sample_period": package.sample_period,
                    "data_structure": package.data_structure_hint,
                    "variables": [
                        variable.model_dump(mode="json")
                        for variable in package.variables
                        if variable.role != "unknown"
                    ],
                    "field_inventory": {
                        "total_fields_registered": len(package.variables),
                        "fields_sent_to_writer": len(
                            [
                                variable
                                for variable in package.variables
                                if variable.role != "unknown"
                            ]
                        ),
                    },
                    "known_policy_facts": package.known_policy_facts,
                    "constraints": package.constraints,
                },
                "data_profile": {
                    **{
                        key: data_profile.get(key)
                        for key in (
                            "profile_execution_status",
                            "row_count",
                            "column_count",
                            "entity_key",
                            "time_key",
                            "duplicate_key_count",
                            "missingness",
                            "confirmed_facts",
                            "measurement_risks",
                            "readiness",
                        )
                    },
                    "panel_balance": panel_balance,
                },
                "frozen_design": {
                    "plan_id": plan.plan_id,
                    "plan_version": plan.plan_version,
                    "method_family": plan.method_family,
                    "research_goal": method_route.get("research_goal"),
                    "sample_rules": [step.model_dump(mode="json") for step in plan.sample_rules],
                    "variable_construction": [
                        step.model_dump(mode="json") for step in plan.variable_construction
                    ],
                    "baseline_models": [
                        model.model_dump(mode="json") for model in plan.baseline_models
                    ],
                    "planned_diagnostics": [step.name for step in plan.diagnostics],
                    "planned_robustness": [step.name for step in plan.robustness_tests],
                    "planned_falsification": [step.name for step in plan.falsification_tests],
                    "planned_mechanisms": [step.name for step in plan.mechanism_tests],
                    "planned_heterogeneity": [step.name for step in plan.heterogeneity_tests],
                    "identification_assumptions": plan.identification_assumptions,
                    "alternative_explanations": plan.alternative_explanations,
                    "unsupported_analyses": plan.unsupported_requested_analyses,
                },
                "executed_evidence": {
                    "research_run_id": run.research_run_id,
                    "execution_status": run.execution_status,
                    "scientific_status": run.scientific_status,
                    "executions": [
                        execution for execution in execution_payloads
                    ],
                    "deviations": run.deviations,
                    "failed_runs": run.failed_runs,
                    "warnings": remove_legacy_warning(run.warnings),
                    "evidence_assessment": evidence_assessment,
                    "scientific_audit": scientific_audit,
                    "reproduction_audit": reproduction_audit,
                },
                "authorized_claims": approved_claims,
                "writing_requirements": {
                    "language": "zh-CN",
                    "required_section_ids": list(FULL_MANUSCRIPT_SECTION_IDS),
                    "target_total_characters": "4000-7000",
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                    "forbid_unverified_citations": True,
                    "forbid_unexecuted_results": True,
                    "authorized_estimate_terms": sorted(
                        authorized_estimate_terms
                    ),
                    "withheld_estimate_terms": sorted(
                        all_estimate_terms - authorized_estimate_terms
                    ),
                },
            }
        }

    @staticmethod
    def _validate_research_run_binding(
        research_run: ResearchRun,
        contract: FormalResearchContract,
    ) -> None:
        mismatches: list[str] = []
        if research_run.case_id != contract.case_id:
            mismatches.append("case_id")
        if research_run.contract_hash != contract.approved_plan_hash:
            mismatches.append("contract_hash")
        if research_run.plan_version != contract.approved_plan.plan_version:
            mismatches.append("plan_version")
        if mismatches:
            raise ValueError(
                "ResearchRun does not match the frozen contract: "
                + ", ".join(mismatches)
            )

    @staticmethod
    def _research_run_differences(
        primary: ResearchRun,
        replication: ResearchRun,
        *,
        tolerance: float = 1e-8,
    ) -> list[str]:
        def comparable(run: ResearchRun) -> dict[str, Any]:
            payload = run.model_dump(mode="json")
            payload.pop("research_run_id", None)
            for execution in payload.get("executions", []):
                execution.pop("execution_id", None)
            return payload

        differences: list[str] = []

        def compare(left: Any, right: Any, path: str) -> None:
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                if abs(float(left) - float(right)) > tolerance:
                    differences.append(f"{path}: {left} != {right}")
                return
            if type(left) is not type(right):
                differences.append(
                    f"{path}: type {type(left).__name__} != {type(right).__name__}"
                )
                return
            if isinstance(left, dict):
                if set(left) != set(right):
                    differences.append(f"{path}: keys differ")
                    return
                for key in sorted(left):
                    compare(left[key], right[key], f"{path}.{key}")
                return
            if isinstance(left, list):
                if len(left) != len(right):
                    differences.append(f"{path}: length {len(left)} != {len(right)}")
                    return
                for index, (left_item, right_item) in enumerate(zip(left, right)):
                    compare(left_item, right_item, f"{path}[{index}]")
                return
            if left != right:
                differences.append(f"{path}: {left!r} != {right!r}")

        compare(comparable(primary), comparable(replication), "research_run")
        return differences


def _names(package: ResearchPackage, role: str) -> list[str]:
    return [variable.name for variable in package.variables if variable.role == role]


def _read_profile_csv(path: Any, selected_columns: list[str]) -> tuple[pd.DataFrame, int]:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            header = pd.read_csv(path, encoding=encoding, nrows=0)
            frame = pd.read_csv(
                path,
                encoding=encoding,
                usecols=lambda name: name in selected_columns,
            )
            return frame, len(header.columns)
        except UnicodeDecodeError as error:
            last_error = error
    raise ValueError("CSV 编码必须是 UTF-8 或 GB18030。") from last_error


def _verify_dataset_hash(path: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("数据资产 SHA256 与登记值不一致。")


def _merge_critics(reports: list[CriticReport], round_number: int) -> CriticReport:
    issues: list[CriticIssue] = [issue for report in reports for issue in report.issues]
    open_issues = [issue for issue in issues if issue.status == "open"]
    if any(issue.severity == "critical" for issue in open_issues):
        verdict = "blocked"
    elif open_issues:
        verdict = "revise"
    else:
        verdict = "pass"
    return CriticReport(
        report_id=f"critic-merged-{round_number}",
        review_round=round_number,
        verdict=verdict,
        issues=issues,
        approved_elements=[item for report in reports for item in report.approved_elements],
        remaining_risks=[item for report in reports for item in report.remaining_risks],
    )
