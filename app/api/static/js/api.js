import {saved, save, requestId} from './state.js?v=workspace-4-2';
// Preserve business codes and uncertain writes, so a retry cannot duplicate work.
export async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("连接中断，请保留输入并核对原请求，提交可能已经被接收。");
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
    const error = new Error(message);
    error.code = data.error_code;
    error.status = response.status;
    throw error;
  }
  return data;
}

export function post(path, data, method = 'POST') {
  return api(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data || {}),
  });
}

export const pending = key => saved('intent.' + key);
export async function mutate(key, payload, send) {
  const old = pending(key);
  if (old && JSON.stringify(old.payload) !== JSON.stringify(payload)) throw new Error('上次提交结果尚未确认，请先按原内容核对或重试。');
  const intent = old || {request_id:requestId(), payload};
  save('intent.' + key, intent);
  try {
    const result = await send(intent.request_id);
    save('intent.' + key, undefined);
    return result;
  } catch (error) {
    if (error.status >= 400 && error.status < 500) save('intent.' + key, undefined);
    throw error;
  }
}
export const jsonMutation = (key, path, payload, method = 'POST') =>
  mutate(key, payload, request_id => post(path, {...payload, request_id}, method));
