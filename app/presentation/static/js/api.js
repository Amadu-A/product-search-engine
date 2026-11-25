// app/presentation/static/js/api.js
// Единый HTTP-клиент; ошибки показываются оператору, ключ команды уникален.
export async function api(path, options = {}) {
  const headers = {...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (options.method && options.method !== 'GET') headers['Idempotency-Key'] ||= commandKey();
  const response = await fetch(`/api/v1${path}`, {...options, headers});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}

export function commandKey() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return `cmd_${Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')}`;
}

export function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
