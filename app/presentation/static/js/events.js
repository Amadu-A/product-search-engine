// app/presentation/static/js/events.js
// Replay сохранённых событий с cursor; пользовательский текст выводится только textContent.
import {element} from './api.js';

export function connectEvents(jobId, onUpdate, onError) {
  const list = document.querySelector('#events');
  const filter = document.querySelector('#event-filter');
  const search = document.querySelector('#event-search');
  const records = new Map();
  list.replaceChildren();
  const source = new EventSource(`/api/v1/import-jobs/${jobId}/events`);
  let scheduled = null;
  let renderFrame = null;
  let closed = false;
  const render = () => {
    const atEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 50;
    list.replaceChildren();
    const query = search.value.toLocaleLowerCase('ru');
    [...records.values()].filter(record => (filter.value === 'all' || record.displayLevel === filter.value) && JSON.stringify(record).toLocaleLowerCase('ru').includes(query)).slice(-200).forEach(record => {
      const row = element('li', undefined, `events__${record.displayLevel}`);
      row.append(element('time', new Date(record.occurred_at).toLocaleTimeString('ru')));
      const content = element('span', `${record.message}${record.data.source_name ? ' · ' + record.data.source_name : ''}`);
      if (record.item_id) {
        const link = element('button', 'Подробнее'); link.type = 'button';
        link.addEventListener('click', () => list.dispatchEvent(new CustomEvent('select-item',{detail:record.item_id})));
        content.append(document.createTextNode(' '), link);
      }
      row.append(content); list.append(row);
    });
    if (atEnd) list.scrollTop = list.scrollHeight;
  };
  filter.onchange = render; search.oninput = render;
  source.onmessage = event => {
    const record = JSON.parse(event.data);
    record.displayLevel = /needs_review|taxonomy_gap|invalid_record/.test(record.type) ? 'warning' : /assigned|completed/.test(record.type) ? 'success' : record.level;
    records.set(record.event_id, record);
    if (records.size > 5000) records.delete(records.keys().next().value);
    if (!renderFrame) renderFrame = requestAnimationFrame(() => { renderFrame = null; if (!closed) render(); });
    if (!scheduled) scheduled = setTimeout(() => { scheduled = null; onUpdate().catch(onError); }, 500);
  };
  const close = () => { closed = true; source.close(); if (renderFrame) cancelAnimationFrame(renderFrame); if (scheduled) clearTimeout(scheduled); };
  source.addEventListener('end', () => { render(); close(); onUpdate().catch(onError); });
  return {close};
}
