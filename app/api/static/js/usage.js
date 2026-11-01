import { node } from "./ui.js";

const sources = { reported: "服务返回", estimated: "本地估算", mixed: "包含估算" };
const tokenFields = ["input_tokens", "output_tokens", "total_tokens"];
const numberFormat = new Intl.NumberFormat("zh-CN");

function tokenNumber(value, source) {
  if (!Number.isSafeInteger(value) || value < 0) return "未提供";
  const approximate = source === "estimated" || source === "mixed";
  return (approximate ? "约 " : "") + numberFormat.format(value);
}

function metrics(group = {}, retrieval = false) {
  return ["输入", "输出", "合计"].flatMap((label, index) => {
    const value = group[tokenFields[index]];
    if (retrieval && index !== 2 && value == null) return [];
    return [label + " " + tokenNumber(value, group.source)];
  }).join(" · ");
}

function appendGroup(container, title, group, retrieval = false) {
  const row = node("div", undefined, "usage-group");
  row.append(node("strong", title));
  if (!group || group.status === "unknown") {
    row.append(node("p", "用量未知 · 未提供", "usage-note"));
  } else if (group.status === "not_called") {
    row.append(node("p", "未调用", "usage-note"));
  } else {
    row.append(node("p", metrics(group, retrieval), "usage-metrics"));
    const state = group.status === "complete" ? "统计完整" : "已知用量，统计不完整";
    row.append(node("p", [sources[group.source], state].filter(Boolean).join(" · "), "usage-note"));
    if (group.source === "mixed") {
      row.append(node("p", "合计构成：服务返回 " + tokenNumber(group.reported_tokens)
        + " · 本地估算 " + tokenNumber(group.estimated_tokens, "estimated"), "usage-note"));
    }
  }
  if (retrieval) {
    for (const [index, usage] of (group?.raw_usage || []).entries()) {
      const otherUnits = Object.entries(usage || {}).filter(([key]) => !key.includes("token"));
      if (otherUnits.length) {
        row.append(node("p", "第 " + (index + 1) + " 次服务另报用量："
          + otherUnits.map(([key, value]) => key + " = " + JSON.stringify(value)).join(" · "), "usage-note"));
      }
    }
  }
  container.append(row);
}

export function renderUsage(usage, { mode, interrupted = false } = {}) {
  const block = node("section", undefined, "token-usage");
  block.setAttribute("aria-label", "本轮 Token 用量");
  if (mode === "offline" || usage?.mode === "offline") {
    block.append(node("p", "本地演示，不统计真实模型用量", "usage-note"));
    return block;
  }
  if (!usage) {
    block.append(node("p", interrupted ? "用量未知，统计可能不完整" : "暂无用量记录", "usage-note"));
    return block;
  }

  const total = usage.conversation || {};
  const incomplete = interrupted || usage.status !== "complete" || total.status !== "complete";
  const known = tokenFields.some((field) => Number.isSafeInteger(total[field]) && total[field] >= 0);
  block.dataset.state = incomplete ? "incomplete" : "complete";
  block.append(node("p", "Token 用量：" + metrics(total), "usage-metrics"));
  const state = !known ? "用量未知，统计不完整"
    : interrupted ? "已记录用量，可能不完整"
      : incomplete ? "已知用量，统计不完整" : "统计完整";
  block.append(node("p", [sources[total.source], state].filter(Boolean).join(" · "), "usage-note"));

  const detail = node("details", undefined, "usage-detail");
  const summary = node("summary");
  summary.append(node("span", "展开详情", "usage-expand"), node("span", "收起详情", "usage-collapse"));
  detail.append(summary, node("p", "上述合计包含主模型、记忆提取与上下文摘要。", "usage-note"));
  for (const [key, title] of [["main", "主模型"], ["memory", "记忆提取"], ["summary", "上下文摘要"]]) {
    appendGroup(detail, title, usage.categories?.[key]);
  }
  detail.append(node("p", "检索服务用量单列，不计入上述合计。", "usage-note usage-retrieval-heading"));
  appendGroup(detail, "Embedding（向量化）", usage.retrieval?.embedding, true);
  appendGroup(detail, "重排", usage.retrieval?.rerank, true);
  block.append(detail);
  return block;
}
