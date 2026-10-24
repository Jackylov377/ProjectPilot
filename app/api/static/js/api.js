// 统一处理 HTTP 请求；页面模块只关心返回的数据。
export async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("无法连接本地工作台，请检查启动终端中的服务是否仍在运行。");
  }

  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("服务未返回有效结果，请查看启动终端后重试。");
  }

  if (!response.ok) {
    const message = typeof data.detail === "string"
      ? data.detail
      : "提交内容有误，请检查输入。";
    throw new Error(message);
  }
  return data;
}

export function post(path, data) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data || {}),
  });
}
