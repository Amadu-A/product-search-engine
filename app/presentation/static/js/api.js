// app/presentation/static/js/api.js
// Единый HTTP-клиент; ошибки показываются оператору, ключ команды уникален.
export async function api(path, options = {}) {
  const headers = {...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (options.method && options.method !== 'GET') headers['Idempotency-Key'] ||= commandKey();
  const response = await fetch(`/api/v1${path}`, {...options, headers});
  const data = await response.json().catch(() => null);
  if (!response.ok || !data) throw responseError(response.status, data);
  return data;
}

function responseError(status, data) {
  const detail = data?.detail;
  const message = Array.isArray(detail) ? detail.map(issue => issue.msg).filter(Boolean).join('. ') : detail;
  return new Error(typeof message === 'string' && message ? message : `Ошибка ответа сервера (HTTP ${status})`);
}

export function uploadFiles(form, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('POST', '/api/v1/import-jobs');
    request.responseType = 'json';
    request.setRequestHeader('Idempotency-Key', commandKey());
    request.upload.addEventListener('progress', event => {
      onProgress(event.lengthComputable ? Math.round(100 * event.loaded / event.total) : null);
    });
    request.upload.addEventListener('load', () => onProgress(100));
    request.addEventListener('load', () => {
      if (request.status === 201 && request.response) resolve(request.response);
      else reject(responseError(request.status, request.response));
    });
    request.addEventListener('error', () => reject(new Error('Не удалось передать файлы на сервер')));
    request.send(form);
  });
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
