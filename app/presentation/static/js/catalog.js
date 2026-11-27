// app/presentation/static/js/catalog.js
// Дерево, серверные фильтры каталога и карточка с историческими наблюдениями.
import {api, element} from './api.js';

const $ = selector => document.querySelector(selector);
const pageSize = 25;
const state = {scope: '', offset: 0, query: '', listToken: 0, cardToken: 0, offerOffset: 0};
const number = new Intl.NumberFormat('ru');

function reportError(error) {
  $('#catalog-error').textContent = error.message;
  $('#catalog-error').hidden = false;
}

function chooseScope(scope, title) {
  state.scope = scope;
  $('#catalog-collection').textContent = title;
  document.querySelectorAll('.catalog-tree__choice').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.scope === scope));
  });
  resetPage();
}

function branchList(children, parent = '') {
  const list = element('ul', undefined, 'catalog-tree');
  for (const category of children.get(parent) || []) {
    const item = element('li');
    const row = element('div', undefined, 'catalog-tree__row');
    if (children.has(category.id)) {
      const toggle = element('button', '▸', 'catalog-tree__toggle');
      toggle.type = 'button';
      toggle.setAttribute('aria-label', `Подкатегории: ${category.name}`);
      toggle.setAttribute('aria-expanded', 'false');
      let nested;
      toggle.addEventListener('click', () => {
        if (!nested) {
          nested = branchList(children, category.id);
          nested.hidden = true;
          item.append(nested);
        }
        nested.hidden = !nested.hidden;
        toggle.textContent = nested.hidden ? '▸' : '▾';
        toggle.setAttribute('aria-expanded', String(!nested.hidden));
      });
      row.append(toggle);
    }
    const button = element('button', undefined, 'catalog-tree__choice');
    button.type = 'button';
    button.dataset.scope = category.id;
    button.setAttribute('aria-pressed', String(state.scope === category.id));
    button.append(element('span', category.name), element('span', number.format(category.subtree_product_count), 'catalog-tree__count'));
    button.addEventListener('click', () => chooseScope(category.id, category.path_names.join(' → ')));
    row.append(button);
    item.append(row);
    list.append(item);
  }
  return list;
}

async function loadTree() {
  const nodes = await api('/categories/tree');
  const children = new Map();
  const available = nodes.filter(node => node.is_active && node.ancestors_active && node.review_status !== 'deprecated');
  available.sort((a,b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name, 'ru'));
  for (const node of available) {
    const parent = node.parent_id || '';
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(node);
  }
  $('#catalog-branches').replaceChildren(branchList(children));
}

async function loadManufacturers() {
  const items = await api('/manufacturers');
  for (const manufacturer of items) {
    $('#catalog-manufacturer').add(new Option(manufacturer.name, manufacturer.id));
  }
}

function listRow(product) {
  const row = element('tr');
  const title = element('td');
  const open = element('button', product.canonical_name);
  open.type = 'button';
  open.dataset.productId = product.id;
  open.setAttribute('aria-controls', 'catalog-card');
  row.setAttribute('aria-selected', String(state.productId === product.id));
  open.addEventListener('click', () => openProduct(product.id, open));
  title.append(open);
  const price = element('td', product.historical_price ? formatPrice(product.historical_price.amount, product.historical_price.currency) : '—');
  if (product.historical_price) price.append(element('small', priceDate(product.historical_price), 'catalog-card__date'));
  row.append(title, element('td', product.mpn || '—'), element('td', product.manufacturer_name || '—'),
    element('td', product.category_name || 'Без категории'), element('td', number.format(product.seller_count)), price);
  return row;
}

async function loadProducts() {
  const token = ++state.listToken;
  $('#catalog-error').hidden = true;
  $('#catalog-total').textContent = 'Загрузка…';
  $('#catalog-prev').disabled = $('#catalog-next').disabled = true;
  const params = new URLSearchParams({q:state.query, offset:state.offset, limit:pageSize, sort:$('#catalog-sort').value});
  if (state.scope === 'uncategorized') params.set('uncategorized', 'true');
  else if (state.scope) params.set('category_id', state.scope);
  const manufacturer = $('#catalog-manufacturer').value;
  if (manufacturer) params.set('manufacturer_id', manufacturer);
  try {
    const page = await api(`/products?${params}`);
    if (token !== state.listToken) return;
    const count = Math.max(1, Math.ceil(page.total / pageSize));
    if (state.offset > 0 && state.offset >= page.total) {
      state.offset = (count - 1) * pageSize;
      return loadProducts();
    }
    if (state.productId && !page.items.some(product => product.id === state.productId)) closeProduct();
    $('#catalog-products').replaceChildren(...page.items.map(listRow));
    $('#catalog-total').textContent = `Товаров: ${number.format(page.total)}`;
    $('#catalog-empty').hidden = page.total !== 0;
    $('#catalog-page').textContent = `${Math.floor(page.offset / pageSize) + 1} / ${count}`;
    $('#catalog-prev').disabled = page.offset === 0;
    $('#catalog-next').disabled = page.offset + page.limit >= page.total;
  } catch (error) {
    if (token !== state.listToken) return;
    $('#catalog-products').replaceChildren();
    $('#catalog-total').textContent = '';
    $('#catalog-page').textContent = '';
    reportError(error);
  }
}

function resetPage() {
  state.offset = 0;
  loadProducts();
}

function showFacts(target, pairs) {
  target.replaceChildren();
  for (const [label, value] of pairs) {
    target.append(element('dt', label), element('dd', value));
  }
}

function sourceLink(url, label) {
  const link = element('a', label);
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function candidateName(name, status) {
  if (!name) return 'Не указан';
  return status === 'candidate' ? `${name} (по данным источника)` : name;
}

function showProduct(product) {
  $('#catalog-card-title').textContent = product.canonical_name;
  $('#catalog-category-path').textContent = product.category_path.join(' → ') || 'Без категории';
  const identity = $('#catalog-identity');
  identity.replaceChildren();
  for (const [label, value] of [
    ['Бренд', product.brand_name], ['Артикул', product.mpn],
    ['Серия', product.series], ['Марка', product.mark],
  ]) {
    if (!value) continue;
    const field = element('div');
    field.append(element('dt', label), element('dd', value));
    identity.append(field);
  }
  identity.hidden = !identity.children.length;
  const rows = product.features.filter(item => item.value).map(item => [
    item.name, [item.value, item.unit && !item.value.endsWith(item.unit) ? item.unit : null].filter(Boolean).join(' '),
  ]);
  const main = rows.filter(([name]) => /размер|напряж|мощност|сечен|диаметр|креплен|жил|ток/i.test(name));
  const primary = (main.length ? main : rows).slice(0, 5);
  const features = element('dl', undefined, 'catalog-card__facts');
  showFacts(features, primary);
  $('#catalog-features').replaceChildren(...(primary.length ? [features] : []));
  const extra = [
    ['Производитель', candidateName(product.manufacturer_name, product.manufacturer_status)],
    ['Модель', product.model], ['Тип изделия', product.product_type], ['Единица', product.unit_code],
    ...rows.filter(row => !primary.includes(row)),
  ].filter((entry, index) => entry[1] && (index !== 0 || product.manufacturer_name));
  showFacts($('#catalog-extra-facts'), extra);
  $('#catalog-extra').hidden = !extra.length;
  $('#catalog-extra').open = false;
  const links = element('ul', undefined, 'catalog-card__links');
  for (const source of product.source_links) {
    const row = element('li');
    row.append(sourceLink(source.url, source.source_name));
    links.append(row);
  }
  $('#catalog-links').replaceChildren(links);
  $('#catalog-source-records').hidden = !product.source_links.length;
  $('#catalog-source-records').open = false;
}

function priceDate(point) {
  const value = point.source_reported_date || point.observed_at.slice(0, 10);
  const [year, month, day] = value.split('-');
  return `${point.source_reported_date ? 'Цена от' : 'Наблюдение от'} ${day}.${month}.${year}`;
}

function formatPrice(amount, currency) {
  const price = amount.replace(/(\.\d*?[1-9])0+$|\.0+$/, '$1').replace('.', ',');
  return [price, currency].filter(Boolean).join(' ');
}

function historicalPrice(point) {
  const amount = point.verified_amount ?? point.amount;
  const currency = point.verified_currency ?? point.currency;
  const unit = point.verified_unit ?? point.sale_unit;
  const date = priceDate(point);
  if (amount === null) return `${date} · Некорректная цена: ${point.raw_price || 'значение отсутствует'}`;
  const verification = point.valid_from ? 'подтверждённое наблюдение' : 'наблюдение из источника';
  return `${date} · ${formatPrice(amount, currency)}${unit ? ` / ${unit}` : ''} · ${verification}`;
}

function showOffer(offer, offerOffset) {
  const card = element('article', undefined, 'catalog-card__offer');
  const point = offer.price_history[0];
  const head = element('div', undefined, 'catalog-card__offer-heading');
  head.append(element('h4', offer.seller_name || 'Продавец не указан'));
  const amount = point?.verified_amount ?? point?.amount;
  const currency = point?.verified_currency ?? point?.currency;
  const unit = point?.verified_unit ?? point?.sale_unit;
  if (amount != null) head.append(element('strong', `${formatPrice(amount, currency)}${unit ? ` / ${unit}` : ''}`));
  card.append(head, element('p', offer.source_name, 'muted'));
  if (amount != null && !currency) card.append(element('small', 'Валюта не указана', 'catalog-card__date'));
  if (offer.origin_url) card.append(sourceLink(offer.origin_url, offer.original_name || 'Страница предложения'));
  else if (offer.original_name) card.append(element('p', offer.original_name));
  if (point) card.append(element('small', priceDate(point), 'catalog-card__date'));
  if (!offer.price_history.length) {
    card.append(element('p', 'История цен отсутствует', 'muted'));
    return card;
  }
  const list = element('ul', undefined, 'catalog-card__history');
  list.append(...offer.price_history.map(point => element('li', historicalPrice(point))));
  const details = element('details');
  details.append(element('summary', 'История цен'), list);
  card.append(details);
  if (offer.history_total > offer.price_history.length) {
    const more = element('button', 'Ещё наблюдения');
    more.type = 'button';
    const product = state.productId;
    let offset = offer.price_history.length;
    more.addEventListener('click', async () => {
      more.disabled = true;
      try {
        const page = await api(`/products/${product}/offers?offset=${offerOffset}&limit=1&history_offset=${offset}`);
        const next = page.items[0];
        if (next?.id !== offer.id) throw new Error('Список продавцов изменился. Откройте карточку заново.');
        list.append(...next.price_history.map(point => element('li', historicalPrice(point))));
        offset += next.price_history.length;
        more.hidden = offset >= next.history_total;
      } catch (error) {
        if (product === state.productId) $('#catalog-card-status').textContent = error.message;
      } finally {
        more.disabled = false;
      }
    });
    details.append(more);
  }
  return card;
}

async function loadOffers() {
  const token = state.cardToken;
  const button = $('#catalog-more-offers');
  button.disabled = true;
  try {
    const page = await api(`/products/${state.productId}/offers?offset=${state.offerOffset}&history_limit=100`);
    if (token !== state.cardToken) return;
    $('#catalog-offers').append(...page.items.map((offer, index) => showOffer(offer, state.offerOffset + index)));
    if (page.total === 0) $('#catalog-offers').append(element('p', 'Сохранённых предложений нет', 'muted'));
    $('#catalog-offers-count').textContent = page.total ? number.format(page.total) : '';
    state.offerOffset += page.items.length;
    button.hidden = state.offerOffset >= page.total;
  } catch (error) {
    if (token === state.cardToken) $('#catalog-card-status').textContent = error.message;
  } finally {
    if (token === state.cardToken) button.disabled = false;
  }
}

async function openProduct(id, trigger) {
  const token = ++state.cardToken;
  state.productId = id;
  state.cardTrigger = trigger;
  state.offerOffset = 0;
  document.querySelectorAll('[data-product-id]').forEach(button => {
    button.closest('tr').setAttribute('aria-selected', String(button.dataset.productId === id));
  });
  $('#catalog-card').hidden = false;
  $('#catalog-card').scrollTop = 0;
  $('#catalog-card-body').hidden = true;
  $('#catalog-card-title').textContent = 'Товар';
  $('#catalog-card-status').textContent = 'Загрузка…';
  $('#catalog-offers').replaceChildren();
  $('#catalog-offers-count').textContent = '';
  $('#catalog-more-offers').hidden = true;
  $('#catalog-card-title').focus();
  try {
    const product = await api(`/products/${id}`);
    if (token !== state.cardToken) return;
    showProduct(product);
    $('#catalog-card-body').hidden = false;
    $('#catalog-card-status').textContent = '';
    await loadOffers();
  } catch (error) {
    if (token === state.cardToken) $('#catalog-card-status').textContent = error.message;
  }
}

function closeProduct() {
  ++state.cardToken;
  state.productId = null;
  $('#catalog-card').hidden = true;
  document.querySelector('#catalog-products tr[aria-selected="true"]')?.setAttribute('aria-selected', 'false');
  if (state.cardTrigger?.isConnected) state.cardTrigger.focus();
}

$('#catalog-all').dataset.scope = '';
$('#catalog-uncategorized').dataset.scope = 'uncategorized';
$('#catalog-all').addEventListener('click', () => chooseScope('', 'Все товары'));
$('#catalog-uncategorized').addEventListener('click', () => chooseScope('uncategorized', 'Без категории'));
$('#catalog-filters').addEventListener('submit', event => {
  event.preventDefault();
  state.query = $('#catalog-search').value.trim();
  resetPage();
});
$('#catalog-manufacturer').addEventListener('change', resetPage);
$('#catalog-sort').addEventListener('change', resetPage);
$('#catalog-prev').addEventListener('click', () => { state.offset -= pageSize; loadProducts(); });
$('#catalog-next').addEventListener('click', () => { state.offset += pageSize; loadProducts(); });
$('#catalog-close').addEventListener('click', closeProduct);
$('#catalog-card').addEventListener('keydown', event => { if (event.key === 'Escape') closeProduct(); });
$('#catalog-more-offers').addEventListener('click', loadOffers);
loadProducts();
loadTree().catch(reportError);
loadManufacturers().catch(reportError);
