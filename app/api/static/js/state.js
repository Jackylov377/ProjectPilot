// 只保存跨页面使用的状态；输入框和选择框的当前值仍从页面读取。
export const state = {
  view: "chat",
  documents: new Set(),
  repositories: [],
  active: null,
  events: null,
  config: null,
};

export const viewTitles = {
  chat: "工作对话",
  knowledge: "知识资料",
  runs: "运行记录",
  evaluations: "评测中心",
  memory: "项目记忆",
  backlog: "待办与缺陷",
};
