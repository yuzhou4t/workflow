# 前端 UI 任务书：HypoWeaver 研究控制台

## 目标

把负责人确认的设计参考落实为真正可运行的 React 界面，而不是只交一张设计图。你可以调整
信息层级、布局、视觉系统和交互细节，但不能改变后端工作流、科学状态语义或 benchmark
逻辑。

目标仓库：

```text
https://github.com/yuzhou4t/workflow
```

## 启动前必须拿到

- 负责人指定的干净 base branch 和完整 commit SHA；
- 已确认采用的 `design/` 参考；
- 负责人确认当前是否有另一条 UI 分支正在合并。

不要基于“本地最新文件”猜版本，也不要直接向 `main` 写代码。创建独立分支并通过 PR 交付。

## 必须保留的产品语义

- `#new`、`#runs`、`#settings` 三个入口及现有 API 行为；
- H1/H2/H3/H4 是真实会暂停的人工闸门；
- `execution_status` 与 `scientific_status` 分开显示；
- fixture、外部基线和正式科学结果不能伪装成同一种状态；
- App A 不能读取隐藏参考；
- discovery/aligned、native/common 的 benchmark 边界不能在 UI 中混写；
- 已有案例导入、运行恢复、配置测试、闸门审批和历史记录能力不能丢失。

## 你需要完成

1. 先输出一份短的 UI 映射说明：旧组件 → 新布局 → 保留的交互。
2. 实现统一的颜色、间距、排版、按钮、表单、状态和错误反馈。
3. 把主流程、当前人工决策、运行历史和配置页面做成清晰的响应式界面。
4. 为关键状态补充或更新前端测试。
5. 在 1440、1024 和 768 像素宽度下检查主要页面。
6. 运行 `npm test` 和 `npm run build`。

不要顺便重构后端、改 API Schema、改 benchmark、改提示词或更换前端技术栈。

## 验收材料

- 一个只包含本任务改动的 PR；
- 测试和构建结果；
- 三个宽度下的主要页面截图；
- `ui-result-summary.json`，至少包含：

```json
{
  "base_commit": "<full sha>",
  "head_commit": "<full sha>",
  "changed_surfaces": ["new", "runs", "settings"],
  "contracts_preserved": [
    "H1-H4",
    "execution_status_vs_scientific_status",
    "hidden_reference_isolation"
  ],
  "tests": {
    "npm_test": "passed",
    "npm_build": "passed"
  },
  "known_gaps": [],
  "pr_url": "<url>"
}
```

如果发现现有 API 或状态合同本身阻碍设计，先在 PR 中记录，不要为了让页面好看而静默修改。
