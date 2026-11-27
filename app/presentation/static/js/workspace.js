// app/presentation/static/js/workspace.js
// Рабочее место: реальные загрузки, durable progress, ручные решения и SQL-категории.
import {api, element, uploadFiles} from './api.js';
import {connectEvents} from './events.js';

const $ = selector => document.querySelector(selector);
const state = {job: null, preview: null, selected: null, category: null, categories: [], offset: 0, total: 0, stream: null};
state.bulk = new Map();
state.bulkPreview = null;
const terminal = new Set(['completed', 'completed_with_issues', 'failed', 'canceled']);
const completed = new Set(['completed', 'completed_with_issues']);
const labels = {pending:'Ожидает', understanding:'Распознавание', matching:'Сопоставление', classifying:'Классификация', assigned:'Назначено', needs_review:'На проверке', taxonomy_gap:'Нет категории', not_product:'Не товар', invalid_record:'Некорректная строка', error:'Ошибка', queued:'В очереди', running:'Обработка', preview_ready:'Готов preview', completed:'Завершено', completed_with_issues:'Завершено с замечаниями', canceled:'Остановлено', cancel_requested:'Остановка'};

function notice(message, error = false) {
  $('#notice').textContent = message;
  $('#notice').className = error ? 'notice notice--error' : 'notice';
}
function fail(error) { notice(error.message || String(error), true); }
function listen(selector, event, callback) {
  $(selector).addEventListener(event, e => Promise.resolve(callback(e)).catch(fail));
}
function mobileTab(name) {
  if (!['file', 'processing', 'review', 'categories'].includes(name)) return;
  document.body.dataset.mobileTab = name;
  sessionStorage.setItem('pse-mobile-tab', name);
  document.querySelectorAll('.mobile-tabs button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.mobileTab === name)));
}
document.querySelectorAll('.mobile-tabs button').forEach(button => button.addEventListener('click', () => mobileTab(button.dataset.mobileTab)));
mobileTab(sessionStorage.getItem('pse-mobile-tab') || 'file');
function showView(name) {
  $('#import-view').hidden = !['import', 'review'].includes(name);
  $('#history-view').hidden = name !== 'history';
  $('#categories-view').hidden = name !== 'categories';
  $('#products-view').hidden = name !== 'products';
  document.querySelectorAll('[data-view]').forEach(button => {
    if (button.dataset.view === name) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  if (name === 'history') history().catch(fail);
  if (name === 'import') mobileTab('file');
  if (name === 'review') { mobileTab('review'); $('#item-status').value = 'needs_review'; refreshItems().catch(fail); }
  if (name === 'categories') renderAllCategories();
  if (name === 'products') renderProducts().catch(fail);
}
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));

async function categories() {
  state.categories = await api('/categories/tree');
  if (state.category && !state.categories.some(c => c.id === state.category && isAssignable(c))) {
    state.category = null;
    $('#selected-category').textContent = 'Категория не выбрана';
    $('#assign-button').disabled = true;
  }
  renderTree();
  const select = $('#category-parent');
  select.replaceChildren(new Option('Корневая категория', ''));
  state.categories.filter(c => c.is_active && c.review_status !== 'deprecated').sort((a,b) => a.path_names.join(' / ').localeCompare(b.path_names.join(' / '), 'ru')).forEach(category => select.add(new Option(category.path_names.join(' → '), category.id)));
}
function isAssignable(category) {
  return category.is_active && category.is_assignable && category.ancestors_active && category.review_status !== 'deprecated';
}
function renderTree() {
  const query = $('#category-search').value.toLocaleLowerCase('ru');
  const tree = $('#category-tree');
  const visible = state.categories.filter(c => c.is_active && c.ancestors_active && c.review_status !== 'deprecated');
  tree.replaceChildren();
  const choose = category => {
    const button = element('button', category.name);
    button.type = 'button'; button.dataset.categoryId = category.id;
    button.disabled = !isAssignable(category);
    button.setAttribute('aria-pressed', String(state.category === category.id));
    button.append(element('span', `${category.path_names.join(' → ')} · ${category.review_status} · товаров ${category.product_count || 0}`, 'tree__path'));
    button.addEventListener('click', event => { event.preventDefault(); event.stopPropagation(); state.category = category.id; $('#selected-category').textContent = category.path_names.join(' → '); updateReviewActions(); document.querySelectorAll('[data-category-id]').forEach(node => node.setAttribute('aria-pressed', String(node.dataset.categoryId === category.id))); });
    return button;
  };
  if (query) {
    const rows = visible.filter(c => [...c.path_names,...c.aliases].join(' / ').toLocaleLowerCase('ru').includes(query));
    rows.slice(0,150).forEach(category => tree.append(choose(category)));
    if (rows.length > 150) tree.append(element('p', `Ещё ${rows.length - 150}. Уточните поиск.`, 'muted'));
    return;
  }
  const children = new Map();
  visible.forEach(category => { const key = category.parent_id || ''; if (!children.has(key)) children.set(key, []); children.get(key).push(category); });
  const branch = (category, depth) => {
    const nested = children.get(category.id) || [];
    if (!nested.length) return choose(category);
    const details = element('details'); details.open = depth === 0;
    const summary = element('summary'); summary.append(choose(category)); details.append(summary);
    nested.forEach(child => details.append(branch(child, depth + 1)));
    return details;
  };
  (children.get('') || []).forEach(category => tree.append(branch(category,0)));
}
function renderAllCategories() {
  const query = $('#manage-category-search').value.trim().toLocaleLowerCase('ru');
  const rows = state.categories
    .filter(c => c.is_active && c.path_names.join(' → ').toLocaleLowerCase('ru').includes(query))
    .sort((a,b) => a.path_names.join(' / ').localeCompare(b.path_names.join(' / '), 'ru'));
  $('#category-list-count').textContent = rows.length > 100
    ? `Показано 100 из ${rows.length}. Уточните поиск.` : `Найдено категорий: ${rows.length}`;
  $('#all-categories').replaceChildren(...rows.slice(0,100).map(category => {
    const row = element('li');
    const remove = element('button', 'Удалить категорию');
    remove.type = 'button';
    remove.addEventListener('click', () => openCategoryArchive(category));
    row.append(element('span', category.path_names.join(' → ')), remove);
    return row;
  }));
}
let productsOffset = 0;
async function renderProducts() {
  const params = new URLSearchParams({q:$('#product-search').value,offset:productsOffset,limit:50});
  const result = await api(`/products?${params}`);
  $('#product-count').textContent = `Найдено товаров: ${result.total}`;
  $('#products').replaceChildren(...result.items.map(product => {
    const row = element('li');
    const category = state.categories.find(c => c.id === product.category_id);
    const description = element('span', product.canonical_name);
    description.append(element('small', category?.path_names.join(' → ') || 'Без категории', 'muted'));
    const remove = element('button', 'Удалить товар');
    remove.type = 'button';
    remove.addEventListener('click', () => confirmProductArchive(product));
    row.append(description, remove);
    return row;
  }));
  $('#products-previous').disabled = !productsOffset;
  $('#products-next').disabled = productsOffset + 50 >= result.total;
}

function confirmProductArchive(product) {
  confirmArchive(`Удалить «${product.canonical_name}» и скрыть его предложения? Исходные записи и ручные решения останутся в истории.`, async () => {
    const result = await api(`/products/${product.id}/archive`, {method:'POST',body:JSON.stringify({confirm:true})});
    productsOffset = 0;
    await Promise.all([renderProducts(), refresh()]);
    if (state.selected?.product?.id === product.id) await selectItem(state.selected.item.id);
    notice(`Товар удалён из каталога. Скрыто предложений: ${result.offers_archived}.`);
  });
}

function renderPreview(result) {
  state.preview = result;
  $('#preview').hidden = false;
  $('#preview-files').replaceChildren();
  const fields = $('#mapping-fields'); fields.replaceChildren();
  const columns = [...new Set(result.files.flatMap(file => file.preview.columns))];
  const mapping = result.files[0]?.preview.mapping || {};
  result.files.forEach(file => {
    $('#preview-files').append(element('p', `${file.original_filename} · ${file.preview.rows_count} строк · ${file.profile}`));
    const details = element('details'); details.append(element('summary', 'Raw / нормализация (первые 20)'));
    details.append(element('pre', JSON.stringify(file.preview.rows, null, 2), 'upload__preview'));
    $('#preview-files').append(details);
  });
  ['name','source_key','brand','manufacturer','mpn','unit','price','currency','price_date','seller','seller_inn','product_url','category_path'].forEach(key => {
    const label = element('label', key === 'name' ? 'Наименование (обязательно)' : key);
    const select = document.createElement('select'); select.dataset.field = key;
    select.add(new Option('Не сопоставлено', ''));
    columns.forEach(column => select.add(new Option(column, column)));
    select.value = mapping[key] || '';
    label.append(select); fields.append(label);
  });
  $('#mapping-options').open = !result.can_start;
  const sheets = result.files.flatMap(file => file.preview.sheets);
  $('#sheet-label').hidden = !sheets.length;
  $('#sheets').value = sheets.join(', ');
  $('#start-button').disabled = !result.can_start;
  $('#mapping-button').hidden = result.job.status !== 'preview_ready';
}
async function selectJob(jobId) {
  state.job = jobId; state.offset = 0; state.selected = null;
  renderItemDetail();
  state.bulk.clear(); updateBulk();
  localStorage.setItem('pse-import-job', jobId);
  const url = new URL(location); url.searchParams.set('job', jobId); window.history.replaceState({}, '', url);
  state.stream?.close();
  const preview = await api(`/import-jobs/${jobId}/preview`);
  renderPreview(preview);
  state.stream = connectEvents(jobId, refresh, fail);
  await refresh();
}
async function refresh() {
  if (!state.job) return;
  const targetJob = state.job;
  const job = await api(`/import-jobs/${targetJob}`);
  if (targetJob !== state.job) return;
  $('#progress').value = job.total_count ? job.processed_count / job.total_count * 100 : 0;
  $('#progress-label').textContent = `Обработано ${job.processed_count} / ${job.total_count}`;
  $('#job-status').textContent = labels[job.status] || job.status;
  $('#assigned-count').textContent = job.assigned_count;
  $('#products-created-count').textContent = job.products_created_count;
  $('#categorized-products-count').textContent = job.categorized_products_count;
  $('#assigned-without-product-count').textContent = job.assigned_without_product_count;
  $('#incomplete-assignment').hidden = !job.assigned_without_product_count;
  $('#auto-assigned-count').textContent = job.auto_assigned_count;
  $('#manual-assigned-count').textContent = job.manual_assigned_count;
  $('#not-product-count').textContent = job.not_product_count;
  state.notProductCount = job.not_product_count;
  $('#archive-not-products').hidden = !completed.has(job.status) || !job.not_product_count;
  $('#archive-not-products').textContent = `Удалить позиции «Не товар» (${job.not_product_count})`;
  $('#reclassify-button').disabled = !completed.has(job.status) || !job.needs_review_count;
  $('#review-count').textContent = job.needs_review_count;
  $('#gap-count').textContent = job.taxonomy_gap_count;
  $('#error-count').textContent = job.technical_error_count;
  $('#cancel-button').disabled = terminal.has(job.status) || job.status === 'preview_ready';
  $('#resume-button').disabled = !terminal.has(job.status);
  $('#start-button').disabled = job.status !== 'preview_ready' || !state.preview?.can_start;
  for (const format of ['json','csv']) { const link = $(`#report-${format}`); link.hidden = !job.total_count; link.href = `/api/v1/import-jobs/${state.job}/report?format=${format}`; link.target = '_blank'; }
  await refreshItems();
}
async function refreshItems() {
  if (!state.job) return;
  const targetJob = state.job;
  const token = state.itemsToken = (state.itemsToken || 0) + 1;
  const params = new URLSearchParams({offset:state.offset,limit:50,q:$('#item-search').value});
  if ($('#item-status').value) params.set('status', $('#item-status').value);
  const result = await api(`/import-jobs/${targetJob}/items?${params}`);
  if (targetJob !== state.job || token !== state.itemsToken) return;
  state.total = result.total;
  $('#items-total').textContent = `${result.total} позиций`;
  $('#items').replaceChildren();
  result.items.forEach(item => {
    const row = element('tr'); row.setAttribute('aria-selected', String(state.selected?.item.id === item.id));
    const choice = element('td'); const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.setAttribute('aria-label', `Выбрать ${item.name || 'позицию'}`); checkbox.checked = state.bulk.has(item.id);
    checkbox.addEventListener('change', () => { if (checkbox.checked) state.bulk.set(item.id, {id:item.id,expected_version:item.processing_version}); else state.bulk.delete(item.id); updateBulk(); }); choice.append(checkbox); row.append(choice);
    const name = element('td'); const button = element('button', item.name || '(без названия)'); button.type = 'button'; button.dataset.itemId = item.id;
    button.addEventListener('click', () => selectItem(item.id).catch(fail)); name.append(button);
    const status = {auto: 'Автоматически', manual: 'Вручную'}[item.assignment_origin] || labels[item.status] || item.status;
    row.append(name, element('td', status), element('td', item.category_name || '—'));
    $('#items').append(row);
  });
  $('#page-label').textContent = `${Math.floor(state.offset / 50) + 1} / ${Math.max(1, Math.ceil(result.total / 50))}`;
  $('#previous').disabled = !state.offset; $('#next').disabled = state.offset + 50 >= result.total;
}
async function selectItem(itemId) {
  const jobId = state.job;
  const token = state.detailToken = (state.detailToken || 0) + 1;
  const detail = await api(`/import-jobs/${jobId}/items/${itemId}`);
  if (jobId !== state.job || token !== state.detailToken) return;
  state.selected = detail;
  $('#decision-reason').value = '';
  renderItemDetail();
  await refreshItems();
}
function updateReviewActions() {
  const detail = state.selected;
  const canEdit = Boolean(detail && !detail.item.issue_codes.includes('archived_not_product') && detail.product?.status !== 'retired');
  $('#assign-button').disabled = !canEdit || !state.category;
  $('#mark-not-product').disabled = !canEdit || detail.item.status === 'not_product';
  $('#leave-uncategorized').disabled = !canEdit;
}
function renderItemDetail() {
  const detail = state.selected;
  $('#detail-title').textContent = detail ? detail.normalized?.normalized_name || 'Без наименования' : 'Выберите позицию';
  $('#detail-technical').hidden = !detail;
  $('#detail-reason').hidden = true;
  $('#suggestions').hidden = true;
  $('#detail-summary').textContent = '';
  updateReviewActions();
  if (!detail) return;
  const answer = [...detail.understandings]
    .sort((a,b) => b.created_at.localeCompare(a.created_at))
    .find(run => run.answer_json)?.answer_json ||
    [...detail.decisions].sort((a,b) => b.created_at.localeCompare(a.created_at))
      .find(decision => decision.evidence?.answer_1)?.evidence.answer_1;
  $('#detail-summary').textContent = [answer?.product_type, answer?.purpose].filter(Boolean).join(' · ') ||
    (detail.item.status === 'pending' ? 'Ожидает обработки' : 'Тип изделия пока не определён');
  const reason = detail.product?.status === 'retired' ? 'Товар удалён из каталога.' :
    detail.classification_reason || answer?.reason;
  $('#detail-reason').textContent = reason || '';
  $('#detail-reason').hidden = !reason;
  const suggestions = detail.suggested_categories || [];
  $('#suggestions').hidden = !suggestions.length;
  $('#suggested-categories').replaceChildren(...suggestions.map(category => {
    const row = element('li');
    const button = element('button', `Утвердить: ${category.path_names.join(' → ')}`);
    button.type = 'button';
    button.addEventListener('click', () => decision('assign_category', category.category_id).catch(fail));
    row.append(button);
    return row;
  }));
  $('#detail-raw').textContent = JSON.stringify(detail.raw?.payload, null, 2);
  const understanding = detail.understandings.map(({raw_response_text,...answer}) => answer);
  const decisions = detail.decisions.map(decision => ({...decision,evidence:{...decision.evidence,raw_response_text:undefined}}));
  $('#detail-trace').textContent = JSON.stringify(
    {understanding, matching: detail.matches, classification: decisions, observations: detail.observations},
    (key, value) => key === 'raw_response_text' ? undefined : value,
    2
  );
}
async function decision(action, categoryId = state.category) {
  if (!state.selected) return;
  const item = state.selected.item;
  const reason = $('#decision-reason').value.trim();
  if (action !== 'assign_category' && !reason) {
    notice('Укажите комментарий к изменению статуса позиции.', true);
    $('#decision-reason').focus();
    return;
  }
  await api(`/review/items/${item.id}/decisions`, {method:'POST',body:JSON.stringify({action,expected_version:item.processing_version,category_id:categoryId,reason})});
  notice('Решение сохранено в PostgreSQL');
  await selectItem(item.id); await refresh();
}
async function history() {
  const jobs = await api('/import-jobs');
  $('#history').replaceChildren();
  jobs.forEach(job => {
    const row = element('div', undefined, 'history-row');
    const button = element('button', `${new Date(job.created_at).toLocaleString()} · ${labels[job.status] || job.status}`);
    button.addEventListener('click', () => { showView('import'); selectJob(job.id).catch(fail); });
    row.append(button, element('span', `${job.processed_count} / ${job.total_count}`)); $('#history').append(row);
  });
}

const uploadForm = $('#upload-form');
const maxFiles = Number(uploadForm.dataset.maxFiles);
const maxBytes = Number(uploadForm.dataset.maxBytes);
const formats = new Set($('#files').accept.split(','));
let selectedFiles = [];
let uploadBusy = false;
let uploadError = '';

function fileSize(bytes) {
  return bytes < 1024 ? `${bytes} Б` :
    bytes < 1024 ** 2 ? `${(bytes / 1024).toFixed(1)} КиБ` : `${(bytes / 1024 ** 2).toFixed(1)} МиБ`;
}

$('#upload-limits').append(document.createTextNode(` До ${fileSize(maxBytes)} на файл.`));

function fileProblem(file) {
  const dot = file.name.lastIndexOf('.');
  if (dot < 0 || !formats.has(file.name.slice(dot).toLowerCase())) return 'Формат не поддерживается';
  if (!file.size) return 'Файл пуст';
  return file.size > maxBytes ? `Превышен лимит ${fileSize(maxBytes)}` : '';
}

function renderSelectedFiles() {
  const list = $('#selected-files');
  list.replaceChildren();
  selectedFiles.forEach((file, index) => {
    const row = element('li', undefined, 'upload__file');
    const description = element('div');
    const problem = fileProblem(file);
    const status = problem || (uploadBusy ? 'Отправляется' : uploadError ? 'Проверка не завершена' : 'Готов к загрузке');
    description.append(element('strong', file.name), element('p', `${fileSize(file.size)} · ${status}`, problem ? 'issue' : 'muted'));
    const remove = element('button', 'Удалить');
    remove.type = 'button';
    remove.disabled = uploadBusy;
    remove.setAttribute('aria-label', `Удалить ${file.name}`);
    remove.addEventListener('click', () => {
      selectedFiles.splice(index, 1);
      uploadError = '';
      renderSelectedFiles();
    });
    row.append(description, remove);
    list.append(row);
  });
  const tooMany = selectedFiles.length > maxFiles;
  if (!uploadBusy) {
    $('#upload-status').textContent = uploadError ||
      (tooMany ? `Можно выбрать до ${maxFiles} файлов. Удалите лишние.` : '');
  }
  $('#choose-files').disabled = uploadBusy;
  $('#upload-button').disabled = uploadBusy || !selectedFiles.length || tooMany || selectedFiles.some(fileProblem);
  $('#upload-button').textContent = `Загрузить и проверить (${selectedFiles.length})`;
}

function selectFiles(files) {
  if (uploadBusy || !files.length) return;
  selectedFiles.push(...files);
  uploadError = '';
  renderSelectedFiles();
}

listen('#choose-files', 'click', () => $('#files').click());
listen('#files', 'change', event => {
  selectFiles(event.target.files);
  event.target.value = '';
});
for (const name of ['dragover', 'dragleave', 'drop']) {
  $('#drop-zone').addEventListener(name, event => {
    event.preventDefault();
    $('#drop-zone').classList.toggle('upload__drop--active', name === 'dragover' && !uploadBusy);
    if (name === 'drop' && event.dataTransfer) selectFiles(event.dataTransfer.files);
  });
}

listen('#upload-form', 'submit', async event => {
  event.preventDefault();
  if ($('#upload-button').disabled) return;
  uploadBusy = true;
  uploadError = '';
  renderSelectedFiles();
  const progress = $('#upload-progress');
  progress.hidden = false;
  progress.removeAttribute('value');
  $('#upload-status').textContent = 'Передача файлов…';
  let preview;
  try {
    const form = new FormData();
    for (const file of selectedFiles) form.append('files', file);
    const delimiter = $('#delimiter').value;
    form.append('options', JSON.stringify({encoding:$('#encoding').value,header_row:Number($('#header-row').value),...($('#source-name').value ? {source:$('#source-name').value} : {}),...(delimiter ? {delimiter:delimiter === 'tab' ? '\t' : delimiter} : {})}));
    preview = await uploadFiles(form, percent => {
      if (percent === null) progress.removeAttribute('value');
      else progress.value = percent;
      $('#upload-status').textContent = percent === 100 ? 'Сервер проверяет файлы…' :
        percent === null ? 'Передача файлов…' : `Передача файлов: ${percent}%`;
    });
    selectedFiles = [];
  } catch (error) {
    uploadError = error.message;
    return;
  } finally {
    uploadBusy = false;
    progress.hidden = true;
    renderSelectedFiles();
  }
  await selectJob(preview.job.id);
  $('#upload-status').textContent = 'Файлы проверены';
  if (preview.can_start && $('#auto-start').checked) await start();
  else notice('Проверьте preview и подтвердите сопоставление полей');
});
async function start() { await api(`/import-jobs/${state.job}/start`, {method:'POST'}); mobileTab('processing'); await refresh(); notice('Импорт поставлен в очередь'); }
listen('#start-button','click',start);
listen('#mapping-button','click',async () => {
  const fields = {}; document.querySelectorAll('[data-field]').forEach(select => { if (select.value) fields[select.dataset.field] = select.value; });
  const sheets = $('#sheets').value.split(',').map(v => v.trim()).filter(Boolean);
  const delimiter = $('#delimiter').value;
  renderPreview(await api(`/import-jobs/${state.job}/mapping`, {method:'PUT',body:JSON.stringify({fields,encoding:$('#encoding').value,header_row:Number($('#header-row').value),...(delimiter ? {delimiter:delimiter === 'tab' ? '\t' : delimiter} : {}),...(sheets.length ? {sheets} : {}),save_profile:$('#save-profile').checked})}));
  notice('Сопоставление подтверждено. Можно начать обработку');
});
listen('#cancel-button','click',async () => { await api(`/import-jobs/${state.job}/cancel`,{method:'POST'}); await refresh(); });
listen('#resume-button','click',async () => { await api(`/import-jobs/${state.job}/resume`,{method:'POST'}); await selectJob(state.job); });
listen('#reclassify-button','click',async event => {
  const button = event.currentTarget;
  button.disabled = true;
  const jobId = state.job;
  try {
    const result = await api(`/import-jobs/${jobId}/reclassify`, {method:'POST'});
    if (jobId !== state.job) return;
    if (result.queued) await selectJob(jobId);
    notice(result.queued ? `Поставлено на повторную классификацию: ${result.eligible}` :
      'Нет позиций для автоматической повторной классификации');
  } finally {
    await refresh();
  }
});
listen('#category-search','input',renderTree);
listen('#item-status','change',() => { state.offset = 0; return refreshItems(); });
listen('#events','select-item',event => selectItem(event.detail));
listen('#item-search','change',() => { state.offset = 0; return refreshItems(); });
listen('#previous','click',() => { state.offset = Math.max(0,state.offset - 50); return refreshItems(); });
listen('#next','click',() => { state.offset += 50; return refreshItems(); });
listen('#assign-button','click',() => decision('assign_category'));
listen('#mark-not-product','click',() => decision('not_product'));
listen('#leave-uncategorized','click',() => decision('leave_uncategorized'));
function updateBulk() { $('#bulk-preview').disabled = !state.bulk.size; $('#bulk-preview').textContent = `Назначить выбранным (${state.bulk.size})`; }
listen('#bulk-preview','click',async () => {
  const values = {items:[...state.bulk.values()],category_id:state.category,reason:$('#decision-reason').value};
  const preview = await api('/review/bulk-preview', {method:'POST',body:JSON.stringify(values)});
  state.bulkPreview = {...values,preview_sha256:preview.preview_sha256};
  $('#bulk-target').textContent = state.categories.find(c => c.id === state.category)?.path_names.join(' → ');
  $('#bulk-items').replaceChildren(...preview.items.map(item => element('li', item.name)));
  $('#bulk-dialog').showModal();
});
listen('#bulk-cancel','click',() => $('#bulk-dialog').close());
listen('#bulk-confirm','click',async () => {
  const result = await api('/review/bulk-decisions', {method:'POST',body:JSON.stringify(state.bulkPreview)});
  $('#bulk-dialog').close(); state.bulk.clear(); updateBulk(); await refresh();
  notice(`Назначено: ${result.applied.length}. Конфликты версий: ${result.conflicts.length}`, result.conflicts.length > 0);
});
listen('#new-category','click',() => { $('#category-parent').value = state.category || ''; $('#category-dialog').showModal(); });
listen('#close-dialog','click',() => $('#category-dialog').close());
listen('#category-form','submit',async event => {
  event.preventDefault();
  const result = await api('/categories',{method:'POST',body:JSON.stringify({name:$('#category-name').value,parent_id:$('#category-parent').value || null,is_assignable:$('#category-assignable').checked,description:$('#category-description').value || null,purpose:$('#category-purpose').value || null,exclusions:$('#category-exclusions').value || null,reason:$('#category-reason').value})});
  state.category = result.id; $('#selected-category').textContent = result.name; $('#category-dialog').close();
  await categories(); updateReviewActions(); notice('Категория создана. Индекс: sync_pending');
});
listen('#product-search-form','submit',event => {
  event.preventDefault();
  productsOffset = 0;
  return renderProducts();
});
listen('#products-previous','click',() => { productsOffset = Math.max(0,productsOffset - 50); return renderProducts(); });
listen('#products-next','click',() => { productsOffset += 50; return renderProducts(); });

let archiveAction;
function confirmArchive(description, action) {
  archiveAction = action;
  $('#archive-description').textContent = description;
  $('#archive-dialog').showModal();
}
listen('#archive-cancel','click',() => $('#archive-dialog').close());
listen('#archive-confirm','click',async event => {
  event.currentTarget.disabled = true;
  $('#archive-dialog').close();
  try {
    await archiveAction();
  } finally {
    $('#archive-confirm').disabled = false;
  }
});
listen('#archive-not-products','click',() => {
  const jobId = state.job;
  const expectedCount = state.notProductCount;
  confirmArchive(`Удалить ${expectedCount} позиций со статусом «Не товар» из списка задания? Они останутся в исходных данных и отчёте.`, async () => {
    const result = await api(`/import-jobs/${jobId}/archive-not-products`, {
      method:'POST',body:JSON.stringify({expected_count:expectedCount})
    });
    if (jobId === state.job) {
      state.offset = 0; state.selected = null; state.bulk.clear();
      renderItemDetail(); updateBulk();
      await refresh();
    }
    notice(`Скрыто позиций «Не товар»: ${result.archived_count}`);
  });
});

let archivedCategory;
let categoryArchivePlan;
function resetCategoryArchivePlan() {
  categoryArchivePlan = null;
  $('#archive-category-impact').textContent = '';
  $('#archive-category-confirm').hidden = true;
  $('#archive-category-preview').hidden = false;
}
function openCategoryArchive(category) {
  archivedCategory = category;
  $('#archive-category-path').textContent = category.path_names.join(' → ');
  const target = $('#archive-category-target');
  target.replaceChildren(new Option('Без переноса', ''));
  state.categories.filter(c => isAssignable(c) && !c.path_ids.includes(category.id))
    .sort((a,b) => a.path_names.join(' / ').localeCompare(b.path_names.join(' / '), 'ru'))
    .forEach(c => target.add(new Option(c.path_names.join(' → '), c.id)));
  resetCategoryArchivePlan();
  $('#archive-category-dialog').showModal();
}
listen('#manage-category-search','input',renderAllCategories);
listen('#archive-category-cancel','click',() => $('#archive-category-dialog').close());
listen('#archive-category-target','change',resetCategoryArchivePlan);
listen('#archive-category-preview','click',async event => {
  const button = event.currentTarget;
  const target = $('#archive-category-target');
  const category = archivedCategory;
  const targetId = target.value;
  button.disabled = target.disabled = true;
  try {
    const proposal = await api('/taxonomy/change-sets', {method:'POST',body:JSON.stringify({
      action:'deactivate',category_id:category.id,target_category_id:targetId || null,
      reason:'Удаление категории из каталога оператором'
    })});
    if (!$('#archive-category-dialog').open || archivedCategory !== category || target.value !== targetId) return;
    categoryArchivePlan = proposal;
    const plan = categoryArchivePlan.preview;
    const referenceCount = Object.values(plan.references).reduce((sum,count) => sum + count, 0);
    const impact = target.value ? `Связи будут перенаправлены в «${target.selectedOptions[0].text}».` :
      'Исторические связи сохранятся.';
    $('#archive-category-impact').textContent = `Активных товаров: ${plan.active_products}. Связей: ${referenceCount}. ${impact}`;
    $('#archive-category-confirm').hidden = false;
    button.hidden = true;
  } catch (error) {
    if (archivedCategory === category) $('#archive-category-impact').textContent = error.message;
  } finally {
    button.disabled = target.disabled = false;
  }
});
listen('#archive-category-confirm','click',async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const plan = categoryArchivePlan;
    await api(`/taxonomy/change-sets/${plan.id}/apply`, {method:'POST',body:JSON.stringify({expected_sha256:plan.preview_sha256})});
    $('#archive-category-dialog').close();
    await categories();
    renderAllCategories();
    await refresh();
    notice('Категория удалена из каталога. Индекс синхронизируется.');
  } catch (error) {
    resetCategoryArchivePlan();
    $('#archive-category-impact').textContent = error.message;
    fail(error);
  } finally {
    button.disabled = false;
  }
});
listen('#reindex-button','click',async () => { await api('/taxonomy/reindex',{method:'POST'}); notice('Запрошена синхронизация индекса'); });
async function indexStatus() { const result = await api('/taxonomy/index-status'); $('#index-status').textContent = `Qdrant: ${result.status} · очередь ${result.pending_events}`; }
await categories().catch(fail);
await indexStatus().catch(fail);
setInterval(() => indexStatus().catch(fail), 5000);
const jobId = new URL(location).searchParams.get('job') || localStorage.getItem('pse-import-job');
if (jobId) await selectJob(jobId).catch(fail);
const section = {'/admin/review':'review', '/admin/categories':'categories', '/admin/history':'history'}[location.pathname];
if (section) showView(section);
