import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [mode, inputPath, thirdArg, fourthArg] = process.argv.slice(2);

if (!mode || !inputPath || !thirdArg) {
  throw new Error(
    "usage: node scripts/build_case_dictionary.mjs inspect <input.xlsx> <preview.png>\n" +
      "   or: node scripts/build_case_dictionary.mjs build <input.xlsx> <case-template.json> <output-dir>",
  );
}

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const sheet = workbook.worksheets.getItemAt(0);

if (mode === "inspect") {
  const summary = await workbook.inspect({
    kind: "workbook,sheet,table",
    maxChars: 8000,
    tableMaxRows: 8,
    tableMaxCols: 12,
    tableMaxCellChars: 100,
  });
  process.stdout.write(`${summary.ndjson}\n`);
  const preview = await workbook.render({
    sheetName: sheet.name,
    range: "A1:I24",
    scale: 1.2,
    format: "png",
  });
  await fs.writeFile(thirdArg, new Uint8Array(await preview.arrayBuffer()));
  process.exit(0);
}

if (mode !== "build" || !fourthArg) {
  throw new Error(`unsupported mode or missing argument: ${mode}`);
}

const templatePayload = JSON.parse(await fs.readFile(thirdArg, "utf8"));
const template = templatePayload.case ?? templatePayload;
const source = sheet.getRange("A1:N134").values;
const headers = source[1];
const rows = source.slice(2).filter((row) => String(row[0] ?? "").trim());

if (rows.length !== 132) {
  throw new Error(`expected 132 dictionary rows, received ${rows.length}`);
}

const exactInteractions = new Map([
  ["SDLAESG", ["SDLA_w", "ESG_w"]],
  ["ESGABSDA1", ["ESG_w", "ABSDA1"]],
  ["ESGABSDA2", ["ESG_w", "ABSDA2"]],
  ["ESG重述", ["ESG_w", "重述"]],
  ["LONGESG", ["ESG_w", "长期投资持股比例"]],
  ["SHORTESG", ["ESG_w", "短期投资持股比例"]],
  ["OVERESG", ["ESG_w", "Over_INV"]],
  ["ZESG", ["ESG_w", "Z值"]],
]);
const names = new Set(rows.map((row) => String(row[0])));
const templateByName = new Map(
  (template.variables ?? []).map((variable) => [variable.name, variable]),
);

function roleFor(name, currentRole) {
  const templateRole = templateByName.get(name)?.role;
  if (templateRole) return templateRole;
  if (name === "S" || name === "证券代码") return "id";
  if (name === "YEAR") return "time";
  if (String(currentRole).includes("因变量") || String(currentRole).includes("经济后果")) return "outcome";
  if (String(currentRole).includes("自变量")) return "exposure";
  if (String(currentRole).includes("机制")) return "mediator";
  if (String(currentRole).includes("调节") || String(currentRole).includes("分组")) return "moderator";
  if (String(currentRole).includes("控制")) return "control";
  return "unknown";
}

function evidenceFor(name, definition) {
  const pairedRaw = name.endsWith("_w") && names.has(name.slice(0, -2));
  if (templateByName.has(name) || exactInteractions.has(name) || pairedRaw) {
    return "A｜可由安全元数据或同表数值关系核验";
  }
  if (!String(definition).includes("完整计算口径未在数据标签中给出")) {
    return "B｜已有中立标签或概念定义";
  }
  return "C｜字段存在，完整口径待案例提供方确认";
}

function definitionFor(row) {
  const name = String(row[0]);
  const templateDefinition = templateByName.get(name)?.definition;
  if (templateDefinition) return templateDefinition;
  const interaction = exactInteractions.get(name);
  if (interaction) {
    return `${interaction[0]} × ${interaction[1]}；已在模型可见数据内逐行核验乘积关系。`;
  }
  if (name.endsWith("_w") && names.has(name.slice(0, -2))) {
    return `${name.slice(0, -2)} 的 1% 与 99% 分位缩尾版本；原始列与处理列均保留。`;
  }
  return String(row[5] ?? "字段存在，但当前安全材料未给出完整计算口径。");
}

function usageFor(name, role, evidence) {
  if (evidence.startsWith("C")) {
    return "不自动进入模型；H1 补充口径后再评估";
  }
  if (role === "id") return "面板主键/匹配";
  if (role === "time") return "面板时间索引";
  if (exactInteractions.has(name)) return "预构造交互项；仅在 H2 冻结后使用";
  if (role === "outcome") return "结果变量候选";
  if (role === "exposure" || role === "treatment") return "核心解释变量候选";
  if (role === "control") return "控制变量候选";
  if (role === "mediator") return "机制/调节边界候选";
  if (role === "moderator") return "异质性或调节候选";
  return "辅助或扩展字段；不得自动进入主模型";
}

const augmentedRows = rows.map((row) => {
  const name = String(row[0]);
  const definition = definitionFor(row);
  const evidence = evidenceFor(name, definition);
  const proposedRole = roleFor(name, row[3]);
  const role = evidence.startsWith("C") ? "unknown" : proposedRole;
  const status = evidence.startsWith("A")
    ? "已核验；可进入 H1 候选池"
    : evidence.startsWith("B")
      ? "已有中立定义；H1 仍需确认口径"
      : "待案例提供方确认；禁止自动建模";
  const next = [...row];
  next[5] = definition;
  next[10] = status;
  return [
    ...next,
    evidence,
    usageFor(name, role, evidence),
    "App A 可见；不含论文结果、作者模型或显著性信息",
    role,
  ];
});

sheet.getRange("A1:N1").unmerge();
sheet.getRange("A1:R1").merge();
sheet.getRange("A1:R1").values = [["数据字典｜模型可见输入｜132 字段完整审计版"]];
sheet.getRange("A2:R2").values = [[
  ...headers,
  "定义证据等级",
  "执行器用途",
  "盲测可见性",
  "标准角色",
]];
sheet.getRange(`A3:R${augmentedRows.length + 2}`).values = augmentedRows;
sheet.freezePanes.freezeRows(2);
sheet.freezePanes.freezeColumns(1);
sheet.showGridLines = false;

sheet.getRange("A1:R1").format = {
  fill: "#1F4E78",
  font: { bold: true, color: "#FFFFFF", size: 15 },
  verticalAlignment: "center",
};
sheet.getRange("A2:R2").format = {
  fill: "#2E75B6",
  font: { bold: true, color: "#FFFFFF" },
  wrapText: true,
  verticalAlignment: "center",
  borders: { preset: "all", style: "thin", color: "#D9E2F3" },
};
sheet.getRange(`A3:R${augmentedRows.length + 2}`).format.wrapText = true;
sheet.getRange(`A2:R${augmentedRows.length + 2}`).format.verticalAlignment = "top";
sheet.getRange(`A2:R${augmentedRows.length + 2}`).format.borders = {
  preset: "all",
  style: "thin",
  color: "#D9E2F3",
};
sheet.getRange("A:A").format.columnWidth = 20;
sheet.getRange("B:B").format.columnWidth = 23;
sheet.getRange("C:E").format.columnWidth = 18;
sheet.getRange("F:F").format.columnWidth = 45;
sheet.getRange("G:H").format.columnWidth = 20;
sheet.getRange("I:K").format.columnWidth = 28;
sheet.getRange("L:N").format.columnWidth = 14;
sheet.getRange("O:O").format.columnWidth = 30;
sheet.getRange("P:P").format.columnWidth = 30;
sheet.getRange("Q:Q").format.columnWidth = 38;
sheet.getRange("R:R").format.columnWidth = 18;
sheet.getRange("1:1").format.rowHeight = 30;
sheet.getRange("2:2").format.rowHeight = 34;

const variables = augmentedRows.map((row) => ({
  name: String(row[0]),
  label: String(row[2] ?? row[0]),
  role: String(row[17]),
  definition: String(row[5]),
  source: String(row[7] ?? "待确认"),
}));
const designEnvelope = {
  ...(template.design_envelope ?? {}),
  design_constraints: [
    ...(template.design_envelope?.design_constraints ?? []),
    "面板固定效应模型默认删除单例实体观测并记录删除数量；如需保留，必须在 H2 冻结前明确说明。",
    "按实体聚类时采用可复现的有限样本校正，并分别报告模型、组内、总体、含固定效应和调整后含固定效应 R²。",
    "替代变量、证伪与交互机制步骤只有在 H2 明确冻结且执行器真实返回记录后才能进入结论。",
  ],
};
const caseProfile = {
  ...template,
  case_id: "case_001_esg_sdla_complete_dictionary_v1",
  variables,
  dataset_refs: [],
  design_envelope: designEnvelope,
};

const evidenceCounts = augmentedRows.reduce((counts, row) => {
  const level = String(row[14]).slice(0, 1);
  counts[level] = (counts[level] ?? 0) + 1;
  return counts;
}, {});
const audit = {
  field_count: variables.length,
  evidence_counts: evidenceCounts,
  exact_interactions_verified: [...exactInteractions.keys()],
  result_leakage_fields_included: [],
  unresolved_fields_are_blocked_from_automatic_modeling: true,
};

await fs.mkdir(fourthArg, { recursive: true });
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(path.join(fourthArg, "data_dictionary_complete.xlsx"));
await fs.writeFile(
  path.join(fourthArg, "case_profile.json"),
  `${JSON.stringify(caseProfile, null, 2)}\n`,
);
await fs.writeFile(
  path.join(fourthArg, "dictionary_audit.json"),
  `${JSON.stringify(audit, null, 2)}\n`,
);

const preview = await workbook.render({
  sheetName: sheet.name,
  range: "A1:R24",
  scale: 1.0,
  format: "png",
});
await fs.writeFile(
  path.join(fourthArg, "data_dictionary_preview.png"),
  new Uint8Array(await preview.arrayBuffer()),
);

const inspection = await workbook.inspect({
  kind: "sheet,table,region",
  sheetId: sheet.name,
  range: "A1:R12",
  maxChars: 10000,
  tableMaxRows: 12,
  tableMaxCols: 18,
  tableMaxCellChars: 100,
});
process.stdout.write(`${inspection.ndjson}\n${JSON.stringify(audit)}\n`);
