import {checkpoint} from "./state.js?v=workspace-4-2";
import { api, post } from "./api.js?v=workspace-4-2";
import { $, $$, badge, empty, handleAction, node } from "./ui.js?v=workspace-4-2";

const caseTitles = {
  "note-read": "读取项目介绍",
  "note-missing": "资料缺失时说明不足",
  "knowledge-fact": "检索校准间隔",
  "knowledge-unanswerable": "无答案检索",
  "knowledge-update": "资料更新后重新问答",
  "memory-model-change": "跨会话模型决定变更",
  "repository-report": "仓库分析与报告",
  "document-injection": "文档指令干扰",
  "note-stack": "按要求选择技术资料",
  "note-empty": "空资料不编造",
  "note-repair": "资料恢复后继续",
  "note-oversized": "资料超过读取上限",
  "retrieval-two-sources": "两个来源共同作答",
  "retrieval-related-no-answer": "相关资料没有数值答案",
  "retrieval-scope-collision": "训练与推理配置区分",
  "retrieval-deletion": "删除资料后重新问答",
  "retrieval-selected-documents": "未选资料不进入检索",
  "retrieval-historical-value": "历史与现行值区分",
  "memory-hypothetical": "假设不作为决定",
  "memory-negated": "否定不作为决定",
  "memory-language": "跨会话语言偏好",
  "memory-question-not-decision": "提问不作为决定",
  "memory-revert-decision": "模型方案改回旧值",
  "memory-document-not-user": "文档建议不自动记忆",
  "session-history-isolation": "不同会话历史隔离",
  "context-early-constraints": "长历史保留早期约束",
  "context-task-progress": "摘要后继续任务进度",
  "context-source-boundary": "摘要后区分计划与执行",
  "repository-readme-repair": "仓库说明恢复后分析",
  "repository-no-function": "没有实现时不编造",
};

const metricTitles = {
  required_tool_coverage: "必需工具名称覆盖",
  first_search_recall_at_3: "首次检索证据召回@3",
  first_search_mrr_at_3: "首次检索排序@3",
  unanswerable_empty_retrieval: "无答案题空检索比例",
  annotated_memory_recall: "标注记忆召回",
  annotated_context_retention: "实际请求信息保留",
  annotated_response_constraints: "回答约束满足",
  recovery_task_success: "恢复任务严格成功",
};

function metricTable(metrics) {
  const table = node("table", undefined, "metric-table");
  const heading = node("tr");
  heading.append(node("th", "证据指标"), node("th", "结果"));
  table.append(heading);

  for (const [key, metric] of Object.entries(metrics)) {
    const value = metric.value === null
      ? "未测量"
      : (metric.value * 100).toFixed(1) + "%（" + metric.numerator + " / " + metric.denominator + "）";
    const row = node("tr");
    row.append(node("td", metricTitles[key] || key), node("td", value));
    table.append(row);
  }
  return table;
}

// 评测页和运行详情展示同一份统计，避免两处使用不同的计数口径。
export function showGroups(container, groups) {
  for (const group of groups) {
    container.append(node("h3", group.config + " · " + group.planned + " 次任务"));
    const counts = node("div", undefined, "score-grid");
    for (const [key, label] of [
      ["passed", "通过"],
      ["failed", "失败"],
      ["review_required", "待复核"],
    ]) {
      const item = node("div");
      item.append(node("strong", group[key]), node("small", label));
      counts.append(item);
    }
    container.append(counts);
    if (group.metrics) container.append(metricTable(group.metrics));
  }
}

function evaluationCard(report) {
  const card = node("button", undefined, "evaluation-card");
  const mode = report.summary.mode === "offline" ? "本地预设演示" : "真实模型 · 开发集";
  const totals = report.summary.groups.reduce((sum, group) => ({
    passed: sum.passed + group.passed,
    failed: sum.failed + group.failed,
    review: sum.review + group.review_required,
  }), { passed: 0, failed: 0, review: 0 });
  card.append(
    node("span", mode, "badge"),
    node("h3", report.name),
    node("p", "通过 " + totals.passed + " · 失败 " + totals.failed + " · 待复核 " + totals.review),
  );
  card.onclick = handleAction(() => showEvaluation(report.id, report.name));
  return card;
}

async function loadEvaluationCases() {
  const select = $("#eval-cases");
  if (select.options.length) return;
  const cases = await api("/api/evaluation-cases");
  const defaults = ["note-read", "knowledge-fact", "memory-model-change"];
  for (const item of cases) {
    select.append(new Option(caseTitles[item.id] || item.id, item.id, false, defaults.includes(item.id)));
  }
}

let evaluationSequence=0, detailSequence=0;
export async function loadEvaluations() {
  const current=checkpoint(), sequence=++evaluationSequence; ++detailSequence;
  $("#evaluation-detail").hidden=true;
  // 旧实验结果也可能含 summary，但没有评测分组，不能当作标准报告绘制。
  const reports = (await api("/api/evaluations")).filter(
    (report) => Array.isArray(report.summary?.groups),
  );
  if (!current() || sequence !== evaluationSequence) return;
  const container = $("#evaluation-list");
  container.replaceChildren();
  if (!reports.length) empty(container, "还没有评测报告，可以先运行少量开发任务。");
  for (const report of reports) container.append(evaluationCard(report));
  await loadEvaluationCases();
}

async function showEvaluation(id, name) {
  const current=checkpoint(), sequence=++detailSequence;
  const report = await api("/api/evaluations/" + id);
  if (!current() || sequence !== detailSequence) return;
  const container = $("#evaluation-detail");
  container.hidden = false;
  container.replaceChildren(
    node("h3", name),
    node("p", report.summary.interpretation, "helper"),
  );
  showGroups(container, report.summary.groups);

  const issues = (report.tasks || []).filter((task) => task.status !== "passed");
  container.append(node("h3", "失败与待复核项"));
  if (!issues.length) container.append(node("p", "没有需要关注的任务。", "helper"));
  for (const task of issues) {
    const row = node("div", undefined, "list-item");
    const info = node("div");
    info.append(
      node("strong", (caseTitles[task.case_id] || task.case_id) + " · " + task.config),
      node("p", task.failures.join("\n")),
    );
    row.append(info, badge(task.status));
    container.append(row);
  }
  container.scrollIntoView({ behavior: "smooth", block: "start" });
}

export async function submitEvaluation(event) {
  event.preventDefault();
  const cases = [...$("#eval-cases").selectedOptions].map((option) => option.value);
  const configs = $$("input[name=eval-config]:checked").map((input) => input.value);
  if (!cases.length || !configs.length) {
    throw new Error("请至少选择一个任务和一种配置。");
  }
  return post("/api/evaluations/run", {
    cases,
    configs,
    repeats: Number($("#eval-repeats").value),
    mode: $("#eval-mode").value,
  });
}
