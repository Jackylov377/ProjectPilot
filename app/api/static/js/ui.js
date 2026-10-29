// 页面通用的小工具，不负责请求数据或改变业务状态。
export const $ = (selector) => document.querySelector(selector);
export const $$ = (selector) => [...document.querySelectorAll(selector)];

export const statusTitles = {
  running: "运行中",
  completed: "执行完成",
  failed: "失败",
  cancelled: "已停止",
  interrupted: "已中断",
  passed: "通过",
  review_required: "待复核",
  infrastructure_error: "执行设施错误",
  active: "有效",
  candidate: "候选",
  superseded: "已替代",
  revoked: "已撤回",
  duplicate: "重复",
};

export function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

let noticeTimer;

export function notify(message, error = false) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.className = error ? "error" : "";
  notice.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    notice.hidden = true;
  }, 6500);
}

// 静态控件和动态生成的按钮使用同一条错误提示路径。
export function handleAction(action) {
  return async (event) => {
    try {
      await action(event);
    } catch (error) {
      notify(error.message, true);
    }
  };
}

export function listen(selector, event, action) {
  $(selector).addEventListener(event, handleAction(action));
}

export function badge(status) {
  return node("span", statusTitles[status] || status, "badge " + status);
}

export function empty(container, message) {
  container.replaceChildren(node("p", message, "empty-copy"));
}

export function date(value) {
  return new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function setBusy(busy) {
  for (const selector of [
    "#send",
    "#start-evaluation",
    "#import-repository",
    "#repository-select",
  ]) {
    $(selector).disabled = busy;
  }
  $("#cancel-job").hidden = !busy;
  $("#job-status").textContent = busy ? "任务运行中" : "就绪";
}

const progressTitles = {
  "repository.import.started": "正在连接GitHub获取源码",
  "repository.downloaded": "源码已下载，正在解压",
  "repository.ready": "源码已就绪，可以开始分析",
  "repository.import.failed": "源码导入失败",
  "run.started": "开始执行任务",
  "session.loaded": "已读取会话历史",
  "iteration.started": "正在处理问题",
  "tool.started": "正在使用工具",
  "tool.finished": "工具已返回结果",
  "llm.call": "模型请求已返回",
  "context.summary.saved": "已保存任务摘要",
  "memory.recalled": "已检索项目记忆",
  "knowledge.retrieval_fallback": "检索服务不可用，已切换检索方式",
  "knowledge.retrieval_completed": "资料检索完成",
  "answer.finalizing": "正在根据已有证据整理回答",
  "session.verified": "会话已保存",
  "run.finished": "本次执行已结束",
};

const toolTitles = {
  read_project_note: "读取项目笔记",
  list_files: "查看目录",
  read_file: "读取文件",
  search_code: "查找代码",
  write_report: "保存报告",
  search_knowledge: "检索资料",
};

export function progressLabel(event) {
  const label = progressTitles[event.event] || "正在执行任务";
  // 导入事件没有工具名；聊天中的工具事件则补上具体工具的说明。
  if (event.event.startsWith("repository.")) return label;
  return label + (event.name ? " · " + (toolTitles[event.name] || event.name) : "");
}
