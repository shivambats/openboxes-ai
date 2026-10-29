const $ = (id) => document.getElementById(id);
let current = null;
let graphCatalog = [];
let loading = false;
let toastTimer;
let pendingMessage = null;
let activeNode = null;
let renderedModelCalls = null;
let executionDetail = null;
let selectedExecutionId = null;
let seenExecutionIds = null;
let chatMode = 'driver';
let consoleId = localStorage.getItem('openboxes-console-operator-id');
if (!consoleId) {
  consoleId = window.crypto.randomUUID();
  localStorage.setItem('openboxes-console-operator-id', consoleId);
}
let operatorMessages = [];
let operatorPending = null;
let operatorBusy = false;
let operatorFocusing = false;
let operatorProgress = '';
let operatorProgressSocket = null;
let lastDriverAssistantId = null;
let lastDriverPendingId = null;
let lastOperatorAssistantId = null;
let lastOperatorDividerId = null;
let lastOperatorPendingId = null;
let operatorRecallNotices = [];
let toolCatalog = null;
// Run order per user message: saved traces by message id, plus 'pending' while running.
let traces = new Map();
let tracedIds = new Set();
let activeTraceId = null;
let selectedCard = null;
let toolRun = {status: 'idle', running: null, live: [], steps: [], decisions: [],
  answeredBy: null, errors: [], reader: null, ran: new Set(), failed: new Set()};
const STAGE_IDS = ['extract_shipment_entity', 'choose_next_capability', 'answer_operator'];
const BLOCKED_OUTCOMES = new Set(['explicit_request_required', 'permission_required',
  'repeat_blocked']);
const READ_ONLY_CARD_ACTIONS = new Set(['focus', 'inspection_status', 'load_transfer_status',
  'check_quotes']);

async function request(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

function toast(message) {
  const box = $('toast');
  box.textContent = message;
  box.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove('visible'), 4000);
}

function setBusy(value) {
  loading = value;
  $('send-message').disabled = value;
  $('chat-input').disabled = value;
  $('chat-image').disabled = value;
  $('new-session').disabled = value;
  $('sample-session').disabled = value;
  $('send-message').textContent = value ? 'Running graph…' : 'Send update ↑';
}

function putText(parent, tag, className, value) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = value;
  parent.append(element);
  return element;
}

function putMarkdown(parent, value, className = 'bubble') {
  const element = document.createElement('div');
  element.className = className;
  element.innerHTML = window.ChatMarkdown.renderChatMarkdown(value);
  parent.append(element);
  return element;
}

function focusMessageStart(log, message) {
  if (!message) return;
  log.scrollTop += message.getBoundingClientRect().top
    - log.getBoundingClientRect().top - 8;
}

function renderChat(state) {
  const log = $('chat-log');
  const oldScrollTop = log.scrollTop;
  log.replaceChildren();
  const messages = (state.messages || []).filter((message) =>
    message.visibility !== 'internal');
  if (pendingMessage && !messages.some((message) => message.id === pendingMessage.id)) {
    messages.push(pendingMessage);
  }
  if (!messages.length) {
    lastDriverAssistantId = null;
    lastDriverPendingId = null;
    putText(log, 'div', 'empty-chat', 'The driver conversation will appear here.');
    return;
  }
  let newestAssistant = null;
  const newestAssistantMessage = [...messages].reverse().find((item) => item.role !== 'driver');
  for (const message of messages) {
    const wrap = document.createElement('article');
    wrap.className = `chat-message ${message.role === 'driver' ? 'driver' : 'assistant'}`;
    const time = message.timestamp ? new Date(message.timestamp).toLocaleString() : '';
    putText(wrap, 'div', 'message-meta', `${message.role === 'driver' ? 'Driver' : 'Sidecar'} · ${time}${message.delivery ? ` · ${message.delivery}` : ''}`);
    putMarkdown(wrap, (message.text || '') +
      (message.image ? `\n\n📎 Scene photo · ${message.image.mime_type}` : ''));
    log.append(wrap);
    if (message === newestAssistantMessage) newestAssistant = wrap;
  }
  if (loading) {
    const working = document.createElement('article');
    working.className = 'chat-message assistant working-message';
    putText(working, 'div', 'message-meta', 'Sidecar · running graph');
    putText(working, 'div', 'bubble', activeNode ? `Working on ${activeNode.node.replaceAll('_', ' ')}…` : 'Starting workflow…');
    log.append(working);
  }
  log.scrollTop = oldScrollTop;
  if (newestAssistantMessage && newestAssistantMessage.id !== lastDriverAssistantId) {
    focusMessageStart(log, newestAssistant);
  } else if (pendingMessage && pendingMessage.id !== lastDriverPendingId) {
    log.scrollTop = log.scrollHeight;
  }
  lastDriverAssistantId = newestAssistantMessage?.id || null;
  lastDriverPendingId = pendingMessage?.id || null;
}

function setChatMode(mode) {
  chatMode = mode;
  $('driver-mode').classList.toggle('chat-mode-active', mode === 'driver');
  $('operator-mode').classList.toggle('chat-mode-active', mode === 'operator');
  for (const id of ['case-context', 'chat-log', 'chat-form']) {
    $(id).hidden = mode !== 'driver';
  }
  for (const id of ['operator-case-context', 'operator-chat-log', 'operator-chat-form']) {
    $(id).hidden = mode !== 'operator';
  }
  $('chat-title').textContent = mode === 'driver' ? 'Driver & dispatcher' : 'Operator chat';
  if (mode === 'operator') {
    renderOperatorChat();
    $('operator-chat-input').focus();
  }
  showModelEvidence();
  localStorage.setItem('openboxes-workflow-chat-mode', mode);
}

function renderOperatorBubble(parent, message) {
  putMarkdown(parent, message.text, 'bubble operator-bubble');
}

// Interactive components: built by the sidecar from tool data, chosen by Jev.
const COMPONENT_ICONS = {timeline: '◷', email_thread: '✉', table: '▦', bar_chart: '▮',
  stat_cards: '#', detail: '≡'};

function formatCell(type, value) {
  if (value === null || value === undefined || value === '') return '—';
  if (type === 'date') return utcSourceLabel(value) || String(value);
  if (type === 'number' && !Number.isNaN(Number(value))) return Number(value).toLocaleString();
  return String(value).replaceAll('_', ' ');
}

function componentShell(card) {
  const shell = document.createElement('section');
  shell.className = `chat-component component-${card.component}`;
  const head = document.createElement('header');
  putText(head, 'span', 'chat-component-icon', COMPONENT_ICONS[card.component] || '•')
    .setAttribute('aria-hidden', 'true');
  putText(head, 'strong', '', card.title);
  putText(head, 'span', 'chat-component-kind', card.component.replace('_', ' '));
  shell.append(head);
  return shell;
}

function renderTimeline(shell, card) {
  const tags = [...new Set(card.items.map((item) => item.tag).filter(Boolean))];
  let active = null;
  const filters = document.createElement('div');
  filters.className = 'chat-component-filters';
  const list = document.createElement('ol');
  list.className = 'chat-timeline';
  const draw = () => {
    list.replaceChildren();
    card.items.filter((item) => !active || item.tag === active).forEach((item) => {
      const row = document.createElement('li');
      row.className = `chat-timeline-item tag-${(item.tag || 'other').replaceAll(' ', '-')}`;
      putText(row, 'time', 'chat-timeline-at', formatCell('date', item.at));
      putText(row, 'div', 'chat-timeline-title', item.title);
      if (item.tag) putText(row, 'span', 'chat-timeline-tag', item.tag);
      if (item.detail) {
        const quote = putText(row, 'blockquote', 'chat-timeline-detail', `“${item.detail}”`);
        quote.hidden = true;
        const toggle = putText(row, 'button', 'chat-link-button', 'Show evidence');
        toggle.type = 'button';
        toggle.addEventListener('click', () => {
          quote.hidden = !quote.hidden;
          toggle.textContent = quote.hidden ? 'Show evidence' : 'Hide evidence';
        });
      }
      list.append(row);
    });
  };
  if (tags.length > 1) {
    ['All', ...tags].forEach((tag) => {
      const chip = putText(filters, 'button', 'chat-filter-chip', tag);
      chip.type = 'button';
      chip.addEventListener('click', () => {
        active = tag === 'All' ? null : tag;
        filters.querySelectorAll('button').forEach((button) =>
          button.classList.toggle('active', button === chip));
        draw();
      });
      if (tag === 'All') chip.classList.add('active');
    });
    shell.append(filters);
  }
  draw();
  shell.append(list);
}

function renderEmailThread(shell, card) {
  const list = document.createElement('div');
  list.className = 'chat-mail';
  putText(shell, 'div', 'chat-mail-subject', card.messages.find((item) => item.subject)?.subject ||
    `${card.messages.length} message${card.messages.length === 1 ? '' : 's'}`);
  card.messages.forEach((message, index) => {
    const item = document.createElement('article');
    item.className = `chat-mail-item ${message.direction === 'out' ? 'outbound' : 'inbound'}`;
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'chat-mail-row';
    const sender = message.direction === 'out' ? 'Warehouse team (us)'
      : String(message.sender || 'Contact').replaceAll('_', ' ');
    putText(row, 'span', 'chat-mail-avatar', sender.charAt(0).toUpperCase()).setAttribute('aria-hidden', 'true');
    const middle = document.createElement('span');
    middle.className = 'chat-mail-middle';
    putText(middle, 'span', 'chat-mail-sender', sender);
    putText(middle, 'span', 'chat-mail-snippet', message.body.replace(/\s+/g, ' ').slice(0, 90));
    row.append(middle);
    putText(row, 'time', 'chat-mail-at', formatCell('date', message.at));
    const body = putText(item, 'div', 'chat-mail-body', message.body || '(no text)');
    item.prepend(row);
    body.hidden = index !== card.messages.length - 1;
    row.setAttribute('aria-expanded', String(!body.hidden));
    row.addEventListener('click', () => {
      body.hidden = !body.hidden;
      row.setAttribute('aria-expanded', String(!body.hidden));
    });
    list.append(item);
  });
  shell.append(list);
}

function renderTable(shell, card) {
  const pageSize = 10;
  let sortAt = null;
  let descending = false;
  let query = '';
  let page = 0;
  const tools = document.createElement('div');
  tools.className = 'chat-table-tools';
  const search = document.createElement('input');
  search.type = 'search';
  search.placeholder = 'Filter rows';
  search.setAttribute('aria-label', `Filter ${card.title}`);
  tools.append(search);
  const copy = putText(tools, 'button', 'chat-link-button', 'Copy CSV');
  copy.type = 'button';
  const wrap = document.createElement('div');
  wrap.className = 'chat-table-wrap';
  const table = document.createElement('table');
  wrap.append(table);
  const pager = document.createElement('div');
  pager.className = 'chat-table-pager';
  const visible = () => {
    let rows = card.rows.filter((row) => !query ||
      row.some((value) => String(value ?? '').toLowerCase().includes(query)));
    if (sortAt !== null) {
      const type = card.columns[sortAt].type;
      rows = [...rows].sort((a, b) => {
        const left = type === 'number' ? Number(a[sortAt]) : String(a[sortAt] ?? '');
        const right = type === 'number' ? Number(b[sortAt]) : String(b[sortAt] ?? '');
        return (left > right ? 1 : left < right ? -1 : 0) * (descending ? -1 : 1);
      });
    }
    return rows;
  };
  const draw = () => {
    const rows = visible();
    page = Math.min(page, Math.max(0, Math.ceil(rows.length / pageSize) - 1));
    table.replaceChildren();
    const head = table.createTHead().insertRow();
    card.columns.forEach((column, index) => {
      const cell = document.createElement('th');
      cell.scope = 'col';
      const button = putText(cell, 'button', '', column.label +
        (sortAt === index ? (descending ? ' ↓' : ' ↑') : ''));
      button.type = 'button';
      if (column.type === 'number') cell.classList.add('numeric');
      cell.setAttribute('aria-sort', sortAt === index ? (descending ? 'descending' : 'ascending') : 'none');
      button.addEventListener('click', () => {
        descending = sortAt === index ? !descending : column.type === 'number';
        sortAt = index;
        draw();
      });
      head.append(cell);
    });
    const body = table.createTBody();
    rows.slice(page * pageSize, page * pageSize + pageSize).forEach((row) => {
      const line = body.insertRow();
      row.forEach((value, index) => {
        const cell = line.insertCell();
        cell.textContent = formatCell(card.columns[index].type, value);
        if (card.columns[index].type === 'number') cell.classList.add('numeric');
      });
    });
    pager.replaceChildren();
    putText(pager, 'span', '', `${rows.length} of ${card.rows.length} row${card.rows.length === 1 ? '' : 's'}` +
      (card.truncated ? ' · more exist; narrow the question' : ''));
    if (rows.length > pageSize) {
      const previous = putText(pager, 'button', 'chat-link-button', '‹ Prev');
      previous.type = 'button';
      previous.disabled = page === 0;
      previous.addEventListener('click', () => { page -= 1; draw(); });
      putText(pager, 'span', '', `${page + 1} / ${Math.ceil(rows.length / pageSize)}`);
      const next = putText(pager, 'button', 'chat-link-button', 'Next ›');
      next.type = 'button';
      next.disabled = (page + 1) * pageSize >= rows.length;
      next.addEventListener('click', () => { page += 1; draw(); });
    }
  };
  search.addEventListener('input', () => { query = search.value.trim().toLowerCase(); page = 0; draw(); });
  copy.addEventListener('click', async () => {
    const escape = (value) => `"${String(value ?? '').replaceAll('"', '""')}"`;
    const csv = [card.columns.map((column) => escape(column.label)).join(','),
      ...visible().map((row) => row.map(escape).join(','))].join('\n');
    try { await navigator.clipboard.writeText(csv); toast('Table copied as CSV.'); }
    catch { toast('Copy failed in this browser.'); }
  });
  draw();
  shell.append(tools, wrap, pager);
}

function renderBarChart(shell, card) {
  let byValue = true;
  const sort = putText(shell, 'button', 'chat-link-button chat-chart-sort', 'Sort by name');
  sort.type = 'button';
  const chart = document.createElement('div');
  chart.className = 'chat-bars';
  chart.setAttribute('role', 'list');
  const draw = () => {
    chart.replaceChildren();
    const max = Math.max(1, ...card.bars.map((bar) => Number(bar.value) || 0));
    [...card.bars].sort((a, b) => byValue ? Number(b.value) - Number(a.value)
      : String(a.label).localeCompare(String(b.label))).forEach((bar) => {
      const row = document.createElement('div');
      row.className = 'chat-bar-row';
      row.setAttribute('role', 'listitem');
      putText(row, 'span', 'chat-bar-label', bar.label);
      const track = document.createElement('span');
      track.className = 'chat-bar-track';
      const fill = document.createElement('span');
      fill.className = 'chat-bar-fill';
      fill.style.width = `${Math.max(2, (Number(bar.value) || 0) / max * 100)}%`;
      track.append(fill);
      row.append(track);
      putText(row, 'span', 'chat-bar-value', formatCell('number', bar.value));
      row.title = `${card.label}: ${bar.label} · ${card.value_label}: ${formatCell('number', bar.value)}`;
      chart.append(row);
    });
    sort.textContent = byValue ? 'Sort by name' : 'Sort by value';
  };
  sort.addEventListener('click', () => { byValue = !byValue; draw(); });
  draw();
  putText(shell, 'div', 'chat-chart-axis', `${card.value_label} by ${card.label.toLowerCase()}`);
  shell.append(chart);
}

function renderStats(shell, card) {
  const grid = document.createElement('div');
  grid.className = 'chat-stats';
  card.stats.forEach((stat) => {
    const tile = document.createElement('div');
    tile.className = 'chat-stat';
    putText(tile, 'strong', '', formatCell(stat.type, stat.value));
    putText(tile, 'span', '', stat.label);
    grid.append(tile);
  });
  shell.append(grid);
}

function renderDetail(shell, card) {
  const list = document.createElement('dl');
  list.className = 'chat-detail';
  card.fields.forEach((field) => {
    putText(list, 'dt', '', field.label);
    putText(list, 'dd', '', formatCell(field.type, field.value));
  });
  shell.append(list);
}

function renderComponent(parent, card) {
  const shell = componentShell(card);
  const draw = {timeline: renderTimeline, email_thread: renderEmailThread, table: renderTable,
    bar_chart: renderBarChart, stat_cards: renderStats, detail: renderDetail}[card.component];
  (draw || renderTable)(shell, card);
  parent.append(shell);
}

function renderOperatorCards(parent, allCards) {
  if (!allCards?.length) return;
  allCards.filter((card) => card.type === 'component').forEach((card) => renderComponent(parent, card));
  const cards = allCards.filter((card) => card.type !== 'component');
  if (!cards.length) return;
  const strip = document.createElement('div');
  strip.className = 'operator-chat-cards';
  const actionRows = Math.max(1, ...cards.filter((card) => card.type === 'delayed_shipment')
    .map((card) => Math.ceil(Math.max(1, card.actions?.length || 1) / 2)));
  strip.style.setProperty('--shipment-action-height',
    `${actionRows * 42 + (actionRows - 1) * 7}px`);
  if (cards.every((card) => card.type === 'delayed_shipment')) {
    strip.setAttribute('role', 'list');
    strip.setAttribute('aria-label', 'Delayed incoming shipments');
  }
  for (const card of cards) {
    if (card.type === 'delayed_shipment') {
      const panel = document.createElement('article');
      panel.className = 'operator-chat-card operator-shipment-card';
      panel.setAttribute('role', 'listitem');
      const top = document.createElement('div');
      top.className = 'operator-shipment-topline';
      putText(top, 'span', 'operator-shipment-status', 'Delayed incoming');
      putText(top, 'span', 'operator-shipment-source', 'OpenBoxes');
      panel.append(top);
      putText(panel, 'strong', 'operator-shipment-number', card.number);
      const route = document.createElement('div');
      route.className = 'operator-shipment-route';
      putText(route, 'span', '', card.origin);
      putText(route, 'span', 'operator-shipment-arrow', '→');
      putText(route, 'span', '', card.destination);
      panel.append(route);
      const arrival = document.createElement('div');
      arrival.className = 'operator-shipment-arrival';
      putText(arrival, 'span', '', 'Expected arrival');
      putText(arrival, 'strong', '', card.arrival_label);
      panel.append(arrival);
      const actions = document.createElement('div');
      actions.className = 'operator-shipment-actions';
      for (const action of card.actions?.length ? card.actions
        : [{id: 'focus', label: 'Focus on shipment'}]) {
        const button = putText(actions, 'button', 'operator-card-button is-secondary',
          action.label);
        button.type = 'button';
        button.disabled = operatorBusy ||
          (operatorReadOnly() && !READ_ONLY_CARD_ACTIONS.has(action.id));
        button.addEventListener('click', () => focusOperatorShipment(card, action));
      }
      panel.append(actions);
      strip.append(panel);
      continue;
    }
    if (card.type === 'tow_service') {
      const panel = document.createElement('article');
      panel.className = 'operator-chat-card operator-shipment-card operator-tow-card';
      const top = document.createElement('div');
      top.className = 'operator-shipment-topline';
      putText(top, 'span', 'operator-shipment-status', 'Tow service lead');
      putText(top, 'span', 'operator-shipment-source', 'Google Maps');
      panel.append(top);
      putText(panel, 'strong', 'operator-shipment-number', card.name);
      putText(panel, 'p', 'operator-tow-address', card.address);
      const facts = document.createElement('div');
      facts.className = 'operator-tow-facts';
      for (const [label, value] of [
        ['Phone', card.phone],
        ['From scene', card.miles != null ? `${card.miles} mi to listed address` : null],
        ['Published rate', card.published_rate],
        ['Quote', card.quote_status.replaceAll('_', ' ')],
      ]) {
        if (!value) continue;
        const row = document.createElement('p');
        putText(row, 'span', '', label);
        putText(row, 'strong', '', value);
        facts.append(row);
      }
      panel.append(facts);
      if (!card.heavy_duty_verified) putText(panel, 'p', 'operator-tow-note',
        'Heavy duty capability unconfirmed');
      if (card.reply_excerpt) putText(panel, 'p', 'operator-tow-reply', card.reply_excerpt);
      const footer = document.createElement('div');
      footer.className = 'operator-card-footer';
      const links = document.createElement('div');
      links.className = 'operator-tow-links';
      for (const [label, url] of [['Google Maps', card.google_maps_url],
        ['Company website', card.website], ['Route estimate', card.distance_source]]) {
        if (!url || (label === 'Route estimate' && card.miles == null)) continue;
        const link = putText(links, 'a', '', label === 'Company website' ? 'Website' : label);
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
      }
      footer.append(links);
      if (card.quote_status === 'replied' && !card.saved_vendor) {
        const button = putText(footer, 'button', 'operator-card-button is-primary',
          'Save as towing vendor');
        button.type = 'button';
        button.disabled = operatorBusy || operatorReadOnly();
        button.addEventListener('click', () => focusOperatorShipment(card, {
          prompt: `Save ${card.name} (${card.address}) as a towing vendor.`,
        }));
      }
      if (card.saved_vendor) putText(footer, 'span', 'operator-chat-card-status',
        'Saved · pending fleet vetting');
      panel.append(footer);
      strip.append(panel);
      continue;
    }
    const panel = document.createElement('div');
    panel.className = 'operator-chat-card';
    putText(panel, 'strong', '', card.name);
    putText(panel, 'span', `operator-chat-card-status ${card.quote_status === 'complete' ? 'is-complete' : ''}`,
      card.quote_status === 'complete' ? 'Complete quote' : card.quote_status.replaceAll('_', ' '));
    if (card.price_usd) putText(panel, 'p', '', `${card.mode || 'Mode unconfirmed'} · USD ${card.price_usd}`);
    if (card.quote_status === 'complete') putText(panel, 'p', '',
      `Pickup ${card.pickup_eta_hours} h · Delivery ${card.delivery_eta_hours} h after approval`);
    if (typeof card.reliability_prior === 'number') putText(panel, 'p', 'operator-card-prior',
      `${Math.round(card.reliability_prior * 100)}% synthetic reliability prior`);
    putText(panel, 'p', 'operator-card-basis', 'No verified past delivery history is on file.');
    if (card.unreviewed_reply) putText(panel, 'p', '', 'Newer reply awaiting review.');
    if (card.selected_by_operator) putText(panel, 'span', 'operator-chat-card-status', 'Chosen carrier');
    if (!card.selected_by_operator || !card.dispatch_steps_complete) {
      const button = putText(panel, 'button', `operator-card-button ${card.can_select || card.selected_by_operator ? 'is-primary' : 'is-secondary'}`, card.selected_by_operator
        ? 'Continue carrier and driver emails' : card.can_select
          ? 'Choose this carrier' : 'Request quote details');
      button.type = 'button';
      button.disabled = operatorBusy || operatorReadOnly();
      button.addEventListener('click', () => sendOperatorText(card.quote_status === 'complete'
        ? `Select ${card.name} for fleet review on ${card.shipment_number}`
        : `Follow up with ${card.name} on quote status for ${card.shipment_number}`));
    }
    strip.append(panel);
  }
  parent.append(strip);
}

function renderOperatorChat() {
  const log = $('operator-chat-log');
  const oldScrollTop = log.scrollTop;
  log.replaceChildren();
  const messages = [...operatorMessages];
  if (operatorPending && !messages.some((item) => item.id === operatorPending.id)) {
    messages.push(operatorPending);
  }
  if (!messages.length) {
    lastOperatorAssistantId = null;
    lastOperatorDividerId = null;
    lastOperatorPendingId = null;
    putText(log, 'div', 'empty-chat', 'Ask about a shipment or your Needs Attention items.');
  }
  if (operatorRecallNotices.length) {
    const notices = document.createElement('div');
    notices.className = 'operator-recall-notices';
    notices.setAttribute('aria-label', 'Warehouse recall notices');
    putText(notices, 'strong', '', 'Warehouse recall notices');
    operatorRecallNotices.forEach((notice) => {
      const row = document.createElement('article');
      putText(row, 'strong', '', notice.product_name);
      putText(row, 'span', '', `Lot ${notice.lot_number} · bins ${notice.bins}`);
      const link = putText(row, 'a', '', 'FDA source');
      link.href = notice.source_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      notices.append(row);
    });
    log.prepend(notices);
  }
  const newestAssistantMessage = [...messages].reverse().find((item) => item.role === 'assistant');
  const newestDividerMessage = [...messages].reverse().find((item) => item.role === 'divider');
  let newestAssistant = null;
  let newestDivider = null;
  messages.forEach((message) => {
    if (message.role === 'divider') {
      const divider = putText(log, 'div', 'operator-context-divider', message.text);
      divider.setAttribute('role', 'separator');
      if (message === newestDividerMessage) newestDivider = divider;
      return;
    }
    const wrap = document.createElement('article');
    const own = message.role === 'user';
    wrap.className = `chat-message ${own ? 'driver' : 'assistant'}`;
    const meta = putText(wrap, 'div', 'message-meta', own ? 'You' : 'Logistics agent');
    const traceId = !own ? null : message === operatorPending
      ? (traces.has('pending') ? 'pending' : null) : tracedIds.has(message.id) ? message.id : null;
    if (traceId !== null) {
      const button = putText(meta, 'button', 'run-order-button', traceId === 'pending'
        ? 'Run order · live' : 'Run order');
      button.type = 'button';
      button.title = 'Show the steps this message ran, in order';
      button.classList.toggle('active', traceId === activeTraceId);
      button.setAttribute('aria-pressed', String(traceId === activeTraceId));
      button.addEventListener('click', () => showTrace(traceId));
    }
    renderOperatorBubble(wrap, message);
    renderOperatorCards(wrap, message.cards);
    const sources = [...new Map((message.annotations || []).flatMap((item) =>
      (item.sources || []).map((source) => [
        `${source.kind}:${source.message_id || source.received_at || ''}`, source,
      ]))).values()];
    if (sources.length) {
      const row = document.createElement('div');
      row.className = 'operator-chat-sources';
      sources.forEach((source) => {
        const received = utcSourceLabel(source.received_at);
        putText(row, 'span', 'operator-chat-source',
          `Source: ${source.kind}${received ? ` · received ${received}` : ''}`);
      });
      wrap.append(row);
    }
    log.append(wrap);
    if (message === newestAssistantMessage) newestAssistant = wrap;
  });
  if (operatorBusy && !operatorFocusing) {
    const working = document.createElement('article');
    working.className = 'chat-message assistant working-message';
    putText(working, 'div', 'message-meta', 'Logistics agent · working');
    putText(working, 'div', 'bubble', operatorProgress || 'Starting…');
    log.append(working);
  }
  log.scrollTop = oldScrollTop;
  if (newestDividerMessage && newestDividerMessage.id !== lastOperatorDividerId) {
    focusMessageStart(log, newestDivider);
  } else if (newestAssistantMessage && newestAssistantMessage.id !== lastOperatorAssistantId) {
    focusMessageStart(log, newestAssistant);
  } else if (operatorPending && operatorPending.id !== lastOperatorPendingId) {
    log.scrollTop = log.scrollHeight;
  }
  lastOperatorAssistantId = newestAssistantMessage?.id || null;
  lastOperatorDividerId = newestDividerMessage?.id || null;
  lastOperatorPendingId = operatorPending?.id || null;
}

function operatorReadOnly() {
  return $('operator-read-only').checked;
}

function utcSourceLabel(value) {
  const date = value && new Date(value);
  return date && !Number.isNaN(date.getTime()) ? `${date.toLocaleString('en-GB', {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
    timeZone: 'UTC'})} UTC` : '';
}

// A chat tool or loop stage that an execution event or progress stage refers to.
function toolForEvent(graphId, node) {
  if (!toolCatalog) return null;
  if (graphId === 'operator_capabilities') return node;
  if (STAGE_IDS.includes(node)) return node;
  if (node === 'read_contact_email') return 'answer_operator';
  return null;
}

function boardFromEvents(events) {
  const open = new Map();
  const ran = new Set();
  const failed = new Set();
  for (const event of events) {
    const tool = toolForEvent(event.graph_id, event.node);
    if (!tool) continue;
    if (event.kind === 'started') open.set(event.span_id || tool, tool);
    else {
      open.delete(event.span_id || tool);
      (event.kind === 'failed' ? failed : ran).add(tool);
    }
  }
  return {running: [...open.values()].at(-1) || null, ran, failed};
}

function stepLabel(step) {
  const by = step.decided_by === 'classifier'
    ? `Jev ${typeof step.confidence === 'number' ? step.confidence.toFixed(2) : ''}`.trim()
    : 'request reader';
  const result = step.result || step.outcome;
  return `${by} · ${String(result || 'completed').replaceAll('_', ' ')}`;
}

function toolChip(parent, tool, stage = false) {
  const lastStep = [...toolRun.steps].reverse().find((item) => item.capability === tool.id);
  const latestRan = [...toolRun.steps].reverse().find((item) => !BLOCKED_OUTCOMES.has(item.outcome));
  const blocked = lastStep && BLOCKED_OUTCOMES.has(lastStep.outcome);
  const ran = toolRun.ran.has(tool.id) || (lastStep && !blocked);
  const locked = !stage && tool.requires_write && operatorReadOnly();
  const running = toolRun.running === tool.id;
  const state = running ? 'running' : toolRun.failed.has(tool.id) ? 'failed' : blocked ? 'blocked'
    : stage ? (ran ? 'ran' : 'idle')
    : latestRan?.capability === tool.id ? 'latest' : ran ? 'ran' : locked ? 'locked' : 'idle';
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = `tool-chip ${stage ? 'stage effect-stage' : `effect-${tool.effect}`} ${state}`;
  chip.setAttribute('role', stage ? 'group' : 'listitem');
  chip.dataset.tool = tool.id;
  const top = document.createElement('span');
  top.className = 'tool-chip-top';
  putText(top, 'span', 'tool-effect', stage ? 'stage' : tool.effect);
  putText(top, 'span', 'tool-state', {running: 'Running', failed: 'Failed', blocked: 'Blocked',
    latest: 'Ran last', ran: 'Ran', locked: 'Write only', idle: ''}[state]);
  chip.append(top);
  putText(chip, 'span', 'tool-chip-name', tool.label);
  let meta = '';
  if (lastStep) meta = stepLabel(lastStep);
  else if (stage && tool.id === 'answer_operator' && toolRun.answeredBy)
    meta = toolRun.answeredBy === 'writer' ? 'wrote the reply' : 'not needed';
  else if (!stage) meta = tool.needs_explicit_request ? 'operator must ask'
    : tool.effect === 'contact' ? `Jev ≥ ${tool.min_confidence} or operator ask`
      : `Jev ≥ ${tool.min_confidence}`;
  if (meta) putText(chip, 'span', 'tool-chip-meta', meta);
  chip.addEventListener('click', () => {
    $('node-details').textContent = [
      `${stage ? 'Loop stage' : 'Capability'} · ${tool.label} (${tool.id})`,
      tool.description,
      stage ? '' : `Effect: ${tool.effect} · write access: ${tool.requires_write ? 'required' : 'not required'} · ` +
        `Jev threshold: ${tool.min_confidence}${tool.needs_explicit_request ? ' · runs only on an explicit operator request' : ''}`,
      lastStep ? `\nLast step in this request:\n${JSON.stringify(lastStep, null, 2)}` : '',
    ].filter(Boolean).join('\n');
    // Jump to this tool's latest step in Run order, so its prompt opens in Model evidence.
    const card = [...(traces.get(activeTraceId)?.cards || [])].reverse()
      .find((item) => item.tool === tool.id);
    if (card) selectCard(card);
  });
  parent.append(chip);
}

function renderToolBoard() {
  if (!toolCatalog) return;
  const status = $('tool-board-status');
  const running = toolRun.running && [...toolCatalog.stages, ...toolCatalog.tools]
    .find((item) => item.id === toolRun.running);
  status.textContent = toolRun.status === 'working'
    ? `Running · ${running?.label || 'starting'}` : toolRun.status === 'viewing'
      ? 'Viewing a past run' : toolRun.status === 'done' ? 'Finished' : 'Idle';
  status.classList.toggle('working', toolRun.status === 'working');
  const stages = $('tool-stages');
  stages.replaceChildren();
  toolCatalog.stages.forEach((stage) => toolChip(stages, stage, true));
  const grid = $('tool-grid');
  grid.replaceChildren();
  toolCatalog.tools.forEach((tool) => toolChip(grid, tool));
}

function toolLabel(id) {
  return [...(toolCatalog?.stages || []), ...(toolCatalog?.tools || [])]
    .find((item) => item.id === id)?.label || String(id || 'step').replaceAll('_', ' ');
}

function toolEffect(id) {
  return toolCatalog?.tools.find((item) => item.id === id)?.effect || 'stage';
}

function seconds(start, end) {
  return typeof start === 'number' && typeof end === 'number' && end >= start
    ? `${(end - start).toFixed(1)} s` : '';
}

function readerSummary(answer) {
  if (!answer) return 'no reading returned';
  return [String(answer.intent_type || 'other').replaceAll('_', ' '),
    answer.shipment_reference, answer.intent].filter(Boolean).join(' · ');
}

// One ordered list of cards for a finished message: reader, each Jev decision followed by
// the tool it led to (with any model reads made inside that tool), then the reply.
function buildTraceCards(data) {
  const calls = data.model_calls || [];
  const steps = data.loop_steps || [];
  const cards = [];
  const entity = calls.find((call) => call.node === 'extract_shipment_entity');
  if (entity) cards.push({kind: 'stage', tool: 'extract_shipment_entity', title: 'Read the request',
    badge: 'DeepSeek', meta: readerSummary(entity.parsed_answer), calls: [entity]});
  const inside = (call, step) => typeof call.started_at === 'number' &&
    typeof step.finished_at === 'number' && call.started_at >= step.started_at &&
    call.started_at <= step.finished_at;
  const owned = new Map(steps.map((step) => [step, []]));
  const loose = [];
  calls.filter((call) => call !== entity).forEach((call) => {
    const step = steps.find((item) => inside(call, item));
    (step ? owned.get(step) : loose).push(call);
  });
  const items = [...loose.map((call) => ({at: call.started_at ?? Infinity, call})),
    ...steps.map((step) => ({at: step.started_at ?? Infinity, step}))]
    .sort((a, b) => a.at - b.at);
  let decision = null;
  const settle = (next) => {
    if (decision?.choice && decision.choice !== 'answer' && next?.capability !== decision.choice) {
      decision.meta += ' · not acted on';
      decision.state = 'skipped';
    }
    decision = null;
  };
  for (const item of items) {
    if (item.step) {
      const step = item.step;
      settle(step);
      const blocked = BLOCKED_OUTCOMES.has(step.outcome);
      const result = String(step.result || step.outcome || 'completed').replaceAll('_', ' ');
      cards.push({kind: 'tool', tool: step.capability, effect: toolEffect(step.capability),
        title: toolLabel(step.capability), badge: toolEffect(step.capability),
        meta: `${step.decided_by === 'classifier' ? 'picked by Jev' : 'picked by request reader'} · ${result}`,
        time: seconds(step.started_at, step.finished_at), state: blocked ? 'blocked' : '',
        step, calls: owned.get(step)});
    } else if (item.call.provider === 'OpenRouter Jev') {
      settle(null);
      const answer = item.call.response?.answers?.next_capability;
      decision = {kind: 'decision', tool: 'choose_next_capability', badge: 'Jev',
        title: answer ? (answer.choice === 'answer' ? 'Jev: enough to answer'
          : `Jev chose ${toolLabel(answer.choice)}`) : 'Jev decision failed',
        meta: answer ? `confidence ${Number(answer.confidence).toFixed(2)} · ${
          Object.keys(item.call.request?.questions?.next_capability?.criteria || {}).length} options`
          : `HTTP ${item.call.http_status ?? 'error'}`,
        choice: answer?.choice, state: answer ? '' : 'failed', calls: [item.call]};
      cards.push(decision);
    } else {
      settle(null);
      const last = cards.at(-1);
      if (last?.kind === 'writer') last.calls.push(item.call);
      else cards.push({kind: 'writer', tool: 'answer_operator', badge: 'DeepSeek',
        title: data.answered_by === 'writer' ? 'Write the reply' : 'Model read',
        meta: data.answered_by === 'writer' ? 'composed from the steps above' : '',
        calls: [item.call]});
    }
  }
  settle(null);
  (data.selection_errors || []).forEach((error) => cards.push({kind: 'note', badge: 'fallback',
    title: 'Jev unavailable', meta: `${error} · followed the request reader`, state: 'blocked',
    calls: []}));
  if (data.answered_by === 'capabilities') cards.push({kind: 'reply', tool: 'answer_operator',
    badge: 'reply', title: 'Reply from tool output', meta: 'no writer call needed', calls: []});
  return cards;
}

function startToolRun(text) {
  toolRun = {status: 'working', running: 'extract_shipment_entity', live: [], steps: [],
    decisions: [], answeredBy: null, errors: [], reader: null, ran: new Set(),
    failed: new Set()};
  traces.set('pending', {text, status: 'working', startedAt: Date.now(),
    cards: [{kind: 'stage', tool: 'extract_shipment_entity', badge: 'DeepSeek',
      title: 'Read the request', meta: 'reading your message…', state: 'running', calls: []}]});
  activeTraceId = 'pending';
  selectedCard = null;
  renderToolBoard();
  renderRunTrace();
  showModelEvidence();
}

function toolProgress(data) {
  if (toolRun.status !== 'working') return;
  const tool = toolForEvent(data.graph_id, data.stage);
  const trace = traces.get('pending');
  if (tool) {
    if (toolRun.running && toolRun.running !== tool) toolRun.ran.add(toolRun.running);
    toolRun.running = tool;
    const last = trace?.cards.at(-1);
    if (trace && last?.tool !== tool) {
      if (last) last.state = '';
      const stage = STAGE_IDS.includes(tool);
      trace.cards.push({kind: tool === 'choose_next_capability' ? 'decision'
        : stage ? 'stage' : 'tool', tool, effect: toolEffect(tool),
        badge: tool === 'choose_next_capability' ? 'Jev' : stage ? 'stage' : toolEffect(tool),
        title: toolLabel(tool), meta: '', state: 'running', calls: []});
    }
  }
  const last = trace?.cards.at(-1);
  if (last && data.message) last.meta = data.message;
  if (data.message && toolRun.live.at(-1) !== data.message) toolRun.live.push(data.message);
  renderToolBoard();
  renderRunTrace();
}

function finishToolRun(data) {
  toolRun = {...toolRun, status: 'done', running: null,
    steps: data.loop_steps || [], answeredBy: data.answered_by || null,
    errors: data.selection_errors || []};
  toolRun.ran.add('extract_shipment_entity');
  if ((data.model_calls || []).some((call) => call.provider === 'OpenRouter Jev'))
    toolRun.ran.add('choose_next_capability');
  if (toolRun.answeredBy === 'writer') toolRun.ran.add('answer_operator');
  const trace = traces.get('pending');
  if (trace) {
    trace.cards = buildTraceCards(data);
    trace.status = 'done';
    trace.finishedAt = Date.now();
    traces.delete('pending');
    if (data.user_message_id != null) {
      traces.set(data.user_message_id, trace);
      tracedIds.add(data.user_message_id);
    }
    if (activeTraceId === 'pending') {
      activeTraceId = data.user_message_id ?? null;
      selectedCard = [...trace.cards].reverse().find((card) => card.calls.length) || null;
    }
  }
  renderToolBoard();
  renderRunTrace();
  showModelEvidence();
}

function failToolRun(message) {
  toolRun = {...toolRun, status: 'done', running: null};
  toolRun.live.push(`Stopped: ${message}`);
  const trace = traces.get('pending');
  if (trace) {
    const last = trace.cards.at(-1);
    if (last) last.state = 'failed';
    trace.cards.push({kind: 'note', badge: 'stopped', title: 'Request stopped', meta: message,
      state: 'failed', calls: []});
    trace.status = 'failed';
    trace.finishedAt = Date.now();
  }
  renderToolBoard();
  renderRunTrace();
}

function selectCard(card) {
  selectedCard = card;
  renderRunTrace();
  showModelEvidence();
}

async function showTrace(id) {
  if (!traces.has(id)) {
    try {
      const saved = await request(`/api/console/run-trace/${consoleId}/${id}`);
      traces.set(id, {text: saved.text, status: 'done', cards: buildTraceCards(saved)});
    } catch (error) { toast(error.message); return; }
  }
  activeTraceId = id;
  const trace = traces.get(id);
  selectedCard = [...trace.cards].reverse().find((card) => card.calls.length) || null;
  renderOperatorChat();
  renderRunTrace();
  showModelEvidence();
  document.querySelector('.run-trace-board')?.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

function renderRunTrace() {
  const root = $('run-trace');
  const label = $('run-trace-message');
  root.replaceChildren();
  const trace = traces.get(activeTraceId);
  if (!trace) {
    label.textContent = 'No message selected';
    putText(root, 'p', 'run-trace-empty', tracedIds.size
      ? 'Select “Run order” beside one of your messages to see its steps.'
      : 'Send an operator message. Each step appears here in the order it ran.');
    return;
  }
  label.textContent = trace.status === 'working' ? 'Running'
    : `${trace.cards.length} steps${trace.finishedAt ? ` · ${seconds(trace.startedAt / 1000,
      trace.finishedAt / 1000)}` : ''}${trace.status === 'failed' ? ' · stopped' : ''}`;
  const text = trace.text.length > 90 ? `${trace.text.slice(0, 90)}…` : trace.text;
  putText(root, 'p', 'run-trace-text', `“${text}”`);
  const list = document.createElement('ol');
  list.className = 'run-cards';
  trace.cards.forEach((card, index) => {
    const item = document.createElement('li');
    item.className = `run-card kind-${card.kind} effect-${card.effect || card.kind} ${card.state || ''}` +
      (card === selectedCard ? ' selected' : '');
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('aria-pressed', String(card === selectedCard));
    putText(button, 'span', 'run-card-index', String(index + 1));
    const body = document.createElement('span');
    body.className = 'run-card-body';
    const top = document.createElement('span');
    top.className = 'run-card-top';
    putText(top, 'span', 'run-card-badge', card.badge || card.kind);
    putText(top, 'span', 'run-card-time', card.state === 'running' ? 'running'
      : [card.time, card.calls.length ? `${card.calls.length} prompt${card.calls.length === 1 ? '' : 's'}`
        : ''].filter(Boolean).join(' · '));
    body.append(top);
    putText(body, 'strong', '', card.title);
    if (card.meta) putText(body, 'span', 'run-card-meta', card.meta);
    button.append(body);
    button.addEventListener('click', () => selectCard(card));
    item.append(button);
    list.append(item);
  });
  root.append(list);
}

// Operator mode shows the prompt behind the selected run card; driver mode keeps its list.
function showModelEvidence() {
  if (chatMode !== 'operator') {
    renderModels(current?.model_calls || []);
    return;
  }
  renderedModelCalls = null;
  const list = $('model-calls');
  const card = selectedCard;
  list.replaceChildren();
  $('model-count').textContent = card ? `${card.calls.length} call${card.calls.length === 1 ? '' : 's'}`
    : 'no step';
  $('model-intro').textContent = card
    ? `Step: ${card.title}${card.meta ? ` · ${card.meta}` : ''}`
    : 'Select a step in Run order to see the exact prompt and response behind it.';
  if (!card) {
    putText(list, 'div', 'empty-model', 'Select a step in Runtime › Run order.');
    return;
  }
  if (card.step) {
    const details = document.createElement('details');
    details.className = 'model-call';
    details.open = !card.calls.length;
    const summary = document.createElement('summary');
    putText(summary, 'span', '', 'Tool result');
    putText(summary, 'span', '', card.step.capability);
    details.append(summary);
    const body = document.createElement('div');
    body.className = 'call-body';
    putText(body, 'pre', '', JSON.stringify(card.step, null, 2));
    details.append(body);
    list.append(details);
  }
  card.calls.forEach((call, index) => {
    const details = document.createElement('details');
    details.className = 'model-call';
    details.open = index === card.calls.length - 1;
    const summary = document.createElement('summary');
    putText(summary, 'span', '', card.calls.length > 1 ? `Attempt ${index + 1}` : 'Prompt and response');
    putText(summary, 'span', '', `${call.provider || 'OpenRouter'} · ${call.request?.model || ''}`);
    details.append(summary);
    const body = document.createElement('div');
    body.className = 'call-body';
    putText(body, 'div', 'call-label', 'Request sent to model');
    putText(body, 'pre', '', JSON.stringify(call.request || {}, null, 2));
    putText(body, 'div', 'call-label', 'Model response');
    const response = call.response || call.parsed_answer || call.raw_content ||
      {http_status: call.http_status, error_type: call.error_type || null};
    putText(body, 'pre', '', typeof response === 'string' ? response : JSON.stringify(response, null, 2));
    if (call.audit) {
      putText(body, 'div', 'call-label', 'Audit');
      putText(body, 'pre', '', JSON.stringify(call.audit, null, 2));
    }
    details.append(body);
    list.append(details);
  });
  if (!card.step && !card.calls.length) {
    putText(list, 'div', 'empty-model', card.state === 'running'
      ? 'This step is still running. Its prompt appears when the message finishes.'
      : 'This step ran code only; no model prompt was sent.');
  }
  list.scrollTop = 0;
}

function toolBoardFromExecution(detail) {
  if (!toolCatalog || !detail) return;
  const board = boardFromEvents(detail.events || []);
  if (operatorBusy && detail.run?.trigger === 'local_console_chat') {
    if (board.running) toolRun.running = board.running;
    board.ran.forEach((tool) => toolRun.ran.add(tool));
    board.failed.forEach((tool) => toolRun.failed.add(tool));
  } else if (!operatorBusy && detail.run?.graph_id === 'operator_chat' &&
             detail.run.id !== toolRun.runId) {
    toolRun = {status: 'viewing', running: board.running, live: [], steps: [],
      decisions: [], answeredBy: null, errors: [], reader: null, ran: board.ran,
      failed: board.failed, runId: null};
  } else return;
  renderToolBoard();
}

function currentRunEvents(events) {
  const terminal = (event) => !event.namespace.length &&
    (event.node === 'compose_reply' || event.node === 'needs_review');
  let last = -1;
  for (let index = events.length - 1; index >= 0; index--) {
    if (terminal(events[index])) { last = index; break; }
  }
  if (last === events.length - 1) {
    for (let index = last - 1; index >= 0; index--) {
      if (terminal(events[index])) { last = index; break; }
      if (index === 0) last = -1;
    }
  }
  return events.slice(last + 1);
}

function nodeRunning(graph, node, running = activeNode) {
  if (!running) return false;
  if (graph.id === 'incident') {
    return (!running.namespace.length && running.node === node) ||
      running.namespace.some((part) => part === node || part.startsWith(`${node}:`));
  }
  return running.node === node && running.namespace.some((part) =>
    part === graph.id || part.startsWith(`${graph.id}:`));
}

function graphVisited(graph, node, events) {
  return events.filter((event) => {
    if (event.node !== node) return false;
    if (event.kind === 'started') return false;
    if (graph.id === 'incident') return !event.namespace.length;
    return event.namespace.some((part) => part === graph.id || part.startsWith(`${graph.id}:`));
  });
}

function drawEdges(graph, steps, buttons, events, running) {
  const prior = steps.querySelector('svg.graph-edges');
  if (prior) prior.remove();
  if (!steps.offsetWidth || !steps.offsetHeight) return;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.classList.add('graph-edges');
  svg.setAttribute('viewBox', `0 0 ${steps.clientWidth} ${steps.clientHeight}`);
  const root = steps.getBoundingClientRect();
  for (const edge of graph.edges) {
    const source = buttons.get(edge.source);
    const target = buttons.get(edge.target);
    if (!source || !target) continue;
    const a = source.getBoundingClientRect();
    const b = target.getBoundingClientRect();
    const x1 = a.left + a.width / 2 - root.left;
    const y1 = a.bottom - root.top;
    const x2 = b.left + b.width / 2 - root.left;
    const y2 = b.top - root.top;
    const middle = y1 + (y2 - y1) / 2;
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', `M ${x1} ${y1} V ${middle} H ${x2} V ${y2 - 5}`);
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    const lit = graphVisited(graph, edge.target, events).length || nodeRunning(graph, edge.target, running);
    path.setAttribute('stroke', lit ? '#55bcae' : '#405875');
    svg.append(path);
    const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    arrow.setAttribute('d', `M ${x2 - 4} ${y2 - 7} L ${x2} ${y2 - 2} L ${x2 + 4} ${y2 - 7}`);
    arrow.setAttribute('fill', 'none');
    arrow.setAttribute('stroke', lit ? '#55bcae' : '#405875');
    arrow.setAttribute('stroke-width', '2');
    svg.append(arrow);
  }
  steps.prepend(svg);
}

function renderGraphs(graphs, events = [], external = false) {
  const list = $('graph-list');
  const previousOpen = new Map([...list.querySelectorAll('details.graph-group')]
    .map((section) => [section.dataset.graphId, section.open]));
  list.replaceChildren();
  const runEvents = external ? events : currentRunEvents(events);
  const latest = runEvents.filter((event) => event.kind !== 'started' &&
    event.node !== 'trigger').at(-1);
  const pending = new Map();
  if (external) runEvents.forEach((event) => {
    if (event.kind === 'started') pending.set(event.span_id, event);
    else pending.delete(event.span_id);
  });
  const running = external ? [...pending.values()].at(-1) || null : activeNode;
  $('step-count').textContent = `${events.length} events`;
  for (const graph of graphs) {
    const section = document.createElement('details');
    section.className = 'graph-group';
    section.dataset.graphId = graph.id;
    const activeGraph = runEvents.some((event) => event.namespace.some((part) =>
      part === graph.id || part.startsWith(`${graph.id}:`))) ||
      (running && running.namespace.some((part) =>
        part === graph.id || part.startsWith(`${graph.id}:`)));
    section.open = (!external && graph.id === 'incident') || activeGraph ||
      previousOpen.get(graph.id) === true;
    const summary = document.createElement('summary');
    putText(summary, 'span', '', graph.title);
    putText(summary, 'span', 'graph-tag', graph.id === 'incident' ? 'PARENT GRAPH' :
      graph.id === 'capabilities' ? 'CAPABILITIES' :
      graph.id === 'email_orchestrator' ? 'EMAIL RUN' :
      graph.id === 'recovery_quotes' ? 'EMAIL GRAPH' : 'GRAPH');
    section.append(summary);
    const steps = document.createElement('div');
    steps.className = `graph-steps ${graph.id === 'incident' ? 'parent-steps' : 'subgraph-steps'}`;
    const buttons = new Map();
    const positions = {
      receive_message: [1, '1 / -1'], jev_classification: [2, '1 / -1'],
      extract_required_facts: [3, '1'], needs_review: [3, '2'],
      accident_handler: [4, '1'], breakdown_handler: [4, '2'],
      fleet_manager: [5, '1 / -1'], coordinate_recovery: [6, '1 / -1'],
      compose_reply: [7, '1 / -1']
    };
    graph.nodes.forEach((node, index) => {
      const visited = graphVisited(graph, node.id, runEvents);
      const isLatest = !!latest && visited.some((event) => event.sequence === latest.sequence);
      const isRunning = nodeRunning(graph, node.id, running);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `graph-node${visited.length ? ' visited' : ''}${isLatest ? ' latest' : ''}${isRunning ? ' running' : ''}`;
      if (graph.id === 'incident' && positions[node.id]) {
        button.style.gridRow = String(positions[node.id][0]);
        button.style.gridColumn = positions[node.id][1];
      }
      putText(button, 'span', 'node-index', `${String(index + 1).padStart(2, '0')} · ${isRunning ? 'RUNNING' : visited.length ? 'EXECUTED' : 'WAITING'}`);
      putText(button, 'span', 'node-name', node.label);
      button.addEventListener('click', () => {
        const related = graph.edges.filter((edge) => edge.source === node.id || edge.target === node.id);
        $('node-details').textContent = `${graph.title} / ${node.label}\nConnections: ${related.map((edge) => `${edge.source} → ${edge.target}`).join(', ') || 'terminal'}\n\n${visited.length ? JSON.stringify(visited.at(-1).update, null, 2) : 'This node has not run in the current case.'}`;
      });
      steps.append(button);
      buttons.set(node.id, button);
    });
    section.append(steps);
    list.append(section);
    section.addEventListener('toggle', () => requestAnimationFrame(() =>
      drawEdges(graph, steps, buttons, runEvents, running)));
    requestAnimationFrame(() => drawEdges(graph, steps, buttons, runEvents, running));
  }
}

function renderModels(calls = []) {
  const list = $('model-calls');
  if (calls === renderedModelCalls) return;
  const previousScroll = list.scrollTop;
  const previousOpen = [...list.querySelectorAll('details.model-call')]
    .map((details) => details.open);
  renderedModelCalls = calls;
  list.replaceChildren();
  $('model-count').textContent = `${calls.length} call${calls.length === 1 ? '' : 's'}`;
  if (!calls.length) {
    putText(list, 'div', 'empty-model', 'Model calls will appear after the first driver update.');
    return;
  }
  calls.forEach((call, index) => {
    const details = document.createElement('details');
    details.className = 'model-call';
    details.open = index < previousOpen.length ? previousOpen[index] : index === calls.length - 1;
    const summary = document.createElement('summary');
    putText(summary, 'span', '', `${index + 1}. ${call.node.replaceAll('_', ' ')}`);
    putText(summary, 'span', '', call.provider || 'Model');
    details.append(summary);
    const body = document.createElement('div');
    body.className = 'call-body';
    putText(body, 'div', 'call-label', 'Request sent to model');
    putText(body, 'pre', '', JSON.stringify(call.request || {}, null, 2));
    putText(body, 'div', 'call-label', 'Model response');
    const response = call.response || call.parsed_answer || call.raw_content ||
      {http_status: call.http_status, error_type: call.error_type || null};
    putText(body, 'pre', '', typeof response === 'string' ? response : JSON.stringify(response, null, 2));
    if (call.audit) {
      putText(body, 'div', 'call-label', 'Audit');
      putText(body, 'pre', '', JSON.stringify(call.audit, null, 2));
    }
    details.append(body);
    list.append(details);
  });
  list.scrollTop = previousScroll;
}

function render(data) {
  current = data;
  setBusy(loading);
  localStorage.setItem('openboxes-workflow-session', data.session.id);
  const shipment = data.state.shipment || {};
  $('case-context').textContent = `${shipment.shipmentNumber || data.session.shipment_id} · ${shipment.origin || 'Origin unknown'} → ${shipment.destination || 'Destination unknown'} · ${shipment.status || 'Status unknown'}`;
  const status = loading ? 'running' : (data.state.status || 'waiting');
  $('case-status').textContent = status;
  $('case-status').className = `status-pill${status === 'review' ? ' review' : ''}`;
  renderChat(data.state);
  if (selectedExecutionId && executionDetail) {
    renderGraphs(executionDetail.graphs, executionDetail.events.map((event) => ({
      ...event, update: {kind: event.kind, at: event.at,
        error_type: event.error_type || null},
    })), true);
  } else {
    renderGraphs(data.graphs, data.events);
  }
  $('state-json').textContent = JSON.stringify(data.state, null, 2);
  showModelEvidence();
}

async function selectExecution(runId) {
  const previousId = selectedExecutionId;
  const previous = executionDetail;
  selectedExecutionId = runId === 'chat' ? null : runId;
  executionDetail = selectedExecutionId ?
    await request(`/api/executions/${encodeURIComponent(selectedExecutionId)}`) : null;
  const sameEvents = previousId === selectedExecutionId && previous && executionDetail &&
    previous.run.status === executionDetail.run.status &&
    previous.events.at(-1)?.sequence === executionDetail.events.at(-1)?.sequence;
  if (sameEvents) return;
  $('execution-select').value = selectedExecutionId || 'chat';
  toolBoardFromExecution(executionDetail);
  const run = executionDetail?.run;
  $('execution-status').textContent = run ?
    `${run.trigger} · ${run.status}${run.subject ? ` · ${run.subject}` : ''}` : 'Chat session';
  $('node-details').textContent = run ?
    `Observing ${run.graph_id.replaceAll('_', ' ')}. Select a node for its execution status.` :
    'Select a node to see its latest state update.';
  if (current) render(current);
  else renderGraphs(run ? executionDetail.graphs : graphCatalog,
    run ? executionDetail.events.map((event) => ({
      ...event, update: {kind: event.kind, at: event.at,
        error_type: event.error_type || null},
    })) : [], !!run);
}

async function refreshExecutions() {
  const response = await request('/api/executions?limit=25');
  const runs = response.runs || [];
  const picker = $('execution-select');
  const selected = selectedExecutionId || 'chat';
  picker.replaceChildren(new Option('Current chat', 'chat'));
  runs.forEach((run) => picker.add(new Option(
    `${run.trigger} · ${run.graph_id.replaceAll('_', ' ')} · ${run.status}`,
    run.id)));
  const newEmail = runs.find((run) => run.trigger === 'email' &&
    !seenExecutionIds?.has(run.id));
  const newOperator = operatorBusy && runs.find((run) =>
    run.trigger === 'local_console_chat' && !seenExecutionIds?.has(run.id));
  const firstPoll = seenExecutionIds === null;
  if (!seenExecutionIds) seenExecutionIds = new Set();
  runs.forEach((run) => seenExecutionIds.add(run.id));
  if (newOperator) selectedExecutionId = newOperator.id;
  else if (newEmail && (!firstPoll || !current)) selectedExecutionId = newEmail.id;
  picker.value = selectedExecutionId || selected;
  if (selectedExecutionId) {
    try { await selectExecution(selectedExecutionId); }
    catch { selectedExecutionId = null; executionDetail = null; picker.value = 'chat'; }
  }
}

async function readEventStream(path, body, onEvent) {
  const response = await fetch(path, {method: 'POST',
    headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
    body: JSON.stringify(body)});
  if (!response.ok) {
    let problem = {};
    try { problem = await response.json(); } catch { /* A proxy may return plain text. */ }
    throw new Error(problem.detail || `Request failed (${response.status})`);
  }
  if (!response.body) throw new Error('This browser cannot read the workflow stream.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let completed = false;
  const dispatch = (frame) => {
    let kind = 'message';
    const data = [];
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) kind = line.slice(6).trim();
      if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
    }
    if (!data.length) return;
    const payload = JSON.parse(data.join('\n'));
    if (kind === 'error') throw new Error(payload.detail || 'Workflow failed.');
    onEvent(kind, payload);
    if (kind === 'complete') completed = true;
  };
  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done}).replaceAll('\r\n', '\n');
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) !== -1) {
        dispatch(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
      }
      if (done) break;
    }
    if (buffer.trim()) dispatch(buffer);
    if (!completed) throw new Error('The workflow stream ended before completion.');
  } finally {
    reader.releaseLock();
  }
}

function applyStreamEvent(kind, payload) {
  if (kind === 'message_received') {
    if (pendingMessage) {
      pendingMessage.id = payload.message.id;
      pendingMessage.timestamp = payload.message.timestamp;
      pendingMessage.delivery = 'received';
    }
  } else if (kind === 'node_started') {
    activeNode = payload;
  } else if (kind === 'node_completed') {
    current.events.push(payload);
    if (payload.namespace[0] !== 'image_analysis' && payload.update &&
        typeof payload.update === 'object' && !Array.isArray(payload.update)) {
      current.state = {...current.state, ...payload.update};
      current.model_calls = current.events
        .filter((event) => event.node === 'resolve_shipment' && event.update?.model_call)
        .map((event) => event.update.model_call)
        .concat(current.state.model_calls || []);
    }
    if (current.state.messages?.some((message) => message.id === pendingMessage?.id)) {
      pendingMessage = null;
    }
    activeNode = null;
  } else if (kind === 'complete') {
    pendingMessage = null;
    activeNode = null;
    setBusy(false);
    render(payload);
    return;
  }
  render(current);
}

function createCase() {
  current = null;
  pendingMessage = null;
  activeNode = null;
  localStorage.removeItem('openboxes-workflow-session');
  $('case-context').textContent = 'Describe the incident and include the shipment number in your first message. The model will identify the shipment.';
  $('case-status').textContent = 'No case';
  $('chat-input').value = '';
  $('chat-image').value = '';
  $('image-name').textContent = 'No photo selected';
  $('state-json').textContent = 'Awaiting a case…';
  renderChat({messages: []});
  showModelEvidence();
  renderGraphs(graphCatalog);
  setBusy(false);
  $('chat-input').focus();
}

async function createSample() {
  setBusy(true);
  try {
    const data = await request('/api/sessions/sample', {method: 'POST'});
    current = data;
    setBusy(false);
    render(data);
    toast('Sample case created. Send a driver update to run the graph.');
    $('chat-input').focus();
  } catch (error) { toast(error.message); setBusy(false); }
}

async function loadOperatorHistory() {
  try {
    const data = await request(`/api/console/operator-history/${consoleId}`);
    operatorMessages = data.messages || [];
    operatorRecallNotices = data.recall_notices || [];
    tracedIds = new Set(data.traced_message_ids || []);
    lastOperatorDividerId = [...operatorMessages].reverse()
      .find((message) => message.role === 'divider')?.id || null;
    renderOperatorChat();
    // Open the newest message's run order so the panel is never blank after a reload.
    const newest = [...tracedIds].at(-1);
    if (newest != null && activeTraceId === null) await showTrace(newest);
  } catch (error) { toast(error.message); }
}

function stopOperatorProgress() {
  if (operatorProgressSocket) {
    operatorProgressSocket.onmessage = null;
    operatorProgressSocket.onclose = null;
    operatorProgressSocket.close();
  }
  operatorProgressSocket = null;
}

function watchOperatorProgress(requestId) {
  stopOperatorProgress();
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${scheme}//${window.location.host}/api/console/operator-progress/ws/${consoleId}/${requestId}`);
  operatorProgressSocket = socket;
  socket.onmessage = ({data: payload}) => {
    try {
      const data = JSON.parse(payload);
      if (operatorBusy) toolProgress(data);
      if (operatorBusy && data.message && data.message !== operatorProgress) {
        operatorProgress = data.message;
        renderOperatorChat();
      }
    } catch { /* The final chat response still arrives by HTTP. */ }
  };
  socket.onclose = () => {
    if (operatorProgressSocket === socket) operatorProgressSocket = null;
  };
}

async function sendOperatorText(text) {
  if (operatorBusy || !text.trim()) return;
  const requestId = window.crypto.randomUUID();
  operatorPending = {id: `pending-${Date.now()}`, role: 'user', text: text.trim()};
  operatorBusy = true;
  operatorProgress = 'Reading your request…';
  $('operator-send').disabled = true;
  $('operator-reset').disabled = true;
  $('operator-chat-input').disabled = true;
  $('operator-chat-input').value = '';
  renderOperatorChat();
  startToolRun(text.trim());
  watchOperatorProgress(requestId);
  try {
    const data = await request('/api/console/operator-message', {
      method: 'POST', body: JSON.stringify({console_id: consoleId, request_id: requestId,
        text: text.trim(), access: operatorReadOnly() ? 'read' : 'write'}),
    });
    operatorMessages = data.messages || [];
    operatorRecallNotices = data.recall_notices || [];
    operatorPending = null;
    finishToolRun(data);
    toolRun.runId = data.execution_run_id || null;
    if (data.shipment_context) {
      $('operator-case-context').textContent =
        `Local synthetic case · ${data.shipment_context.shipmentNumber}`;
    } else if (data.attention_context) {
      const attention = data.attention_context;
      $('operator-case-context').textContent =
        `Needs attention · ${(attention.delayedShipments || []).length} delayed incoming shipments · ${(attention.recallLots || []).length} recall review lots`;
    }
    if (data.workflow_session_id) {
      const snapshot = await request(`/api/sessions/${encodeURIComponent(data.workflow_session_id)}`);
      render(snapshot);
    } else {
      current = null;
      $('state-json').textContent = JSON.stringify(data.attention_context ||
        data.shipment_context || {}, null, 2);
    }
    showModelEvidence();
    await refreshExecutions();
    if (data.execution_run_id) await selectExecution(data.execution_run_id);
  } catch (error) {
    toast(error.message);
    $('operator-chat-input').value = text;
    operatorPending = null;
    failToolRun(error.message);
  } finally {
    stopOperatorProgress();
    operatorBusy = false;
    operatorProgress = '';
    $('operator-send').disabled = false;
    $('operator-reset').disabled = false;
    $('operator-chat-input').disabled = false;
    renderOperatorChat();
    $('operator-chat-input').focus();
  }
}

async function focusOperatorShipment(card, action = null) {
  if (operatorBusy || (!card.shipment_id && !card.number)) return;
  let focused = false;
  operatorBusy = true;
  operatorFocusing = true;
  $('operator-send').disabled = true;
  $('operator-reset').disabled = true;
  $('operator-chat-input').disabled = true;
  renderOperatorChat();
  try {
    const data = await request('/api/console/operator-focus', {
      method: 'POST', body: JSON.stringify({console_id: consoleId,
        shipment_id: card.shipment_id, shipment_number: card.number}),
    });
    operatorMessages = data.messages || [];
    focused = true;
    $('operator-case-context').textContent =
      [...operatorMessages].reverse().find((message) => message.role === 'divider')?.text ||
      'Shipment selected';
    $('operator-chat-input').value = '';
  } catch (error) {
    toast(error.message);
  } finally {
    operatorBusy = false;
    operatorFocusing = false;
    $('operator-send').disabled = false;
    $('operator-reset').disabled = false;
    $('operator-chat-input').disabled = false;
    renderOperatorChat();
    $('operator-chat-input').focus();
  }
  if (focused && action?.prompt) await sendOperatorText(action.prompt);
}

async function resetOperatorChat() {
  if (operatorBusy) return;
  try {
    await request(`/api/console/operator-history/${consoleId}`, {method: 'DELETE'});
    operatorMessages = [];
    operatorPending = null;
    traces = new Map();
    tracedIds = new Set();
    activeTraceId = null;
    selectedCard = null;
    renderRunTrace();
    showModelEvidence();
    $('operator-case-context').textContent =
      'Ask about a shipment or your Needs Attention work queue. The model identifies the request from your message.';
    renderOperatorChat();
  } catch (error) { toast(error.message); }
}

async function send(event) {
  event.preventDefault();
  if (loading) return;
  const text = $('chat-input').value.trim();
  if (!text) return;
  const file = $('chat-image').files[0];
  pendingMessage = {id: `pending-${Date.now()}`, role: 'driver', text,
    timestamp: new Date().toISOString(), delivery: 'sending',
    image: file ? {mime_type: file.type} : null};
  $('chat-input').value = '';
  $('chat-image').value = '';
  $('image-name').textContent = 'No photo selected';
  $('node-details').textContent = 'The current driver update is running.';
  setBusy(true);
  if (current) render(current);
  else renderChat({messages: []});
  try {
    if (file && file.size > 8 * 1024 * 1024) throw new Error('Photo must be 8 MB or smaller.');
    const image_data_url = file ? await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('Could not read the photo.'));
      reader.readAsDataURL(file);
    }) : null;
    if (!current) {
      const created = await request('/api/sessions/from-message',
        {method: 'POST', body: JSON.stringify({text})});
      render(created);
    }
    await readEventStream(`/api/sessions/${current.session.id}/messages/stream`,
      {text, image_data_url}, applyStreamEvent);
  } catch (error) {
    toast(error.message);
    if (current) {
      try {
        const saved = await request(`/api/sessions/${current.session.id}`);
        current = saved;
        const persisted = saved.state.messages?.some((message) => message.id === pendingMessage?.id);
        if (!persisted) $('chat-input').value = text;
      } catch { $('chat-input').value = text; }
    } else {
      $('chat-input').value = text;
      $('case-context').textContent = error.message;
    }
    pendingMessage = null;
    activeNode = null;
    setBusy(false);
    if (current) render(current);
    else renderChat({messages: []});
  }
}

async function initialize() {
  $('driver-mode').addEventListener('click', () => setChatMode('driver'));
  $('operator-mode').addEventListener('click', () => setChatMode('operator'));
  $('operator-chat-form').addEventListener('submit', (event) => {
    event.preventDefault();
    sendOperatorText($('operator-chat-input').value);
  });
  $('operator-chat-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault(); $('operator-chat-form').requestSubmit();
    }
  });
  $('operator-reset').addEventListener('click', resetOperatorChat);
  $('operator-read-only').checked = localStorage.getItem('openboxes-console-read-only') === 'true';
  $('operator-read-only').addEventListener('change', () => {
    localStorage.setItem('openboxes-console-read-only', String(operatorReadOnly()));
    renderOperatorChat();
    renderToolBoard();
  });
  request('/api/console/chat-tools').then((catalog) => {
    toolCatalog = catalog;
    renderToolBoard();
  }).catch((error) => toast(`Tool list unavailable: ${error.message}`));
  setChatMode(localStorage.getItem('openboxes-workflow-chat-mode') || 'operator');
  loadOperatorHistory();
  $('new-session').addEventListener('click', createCase);
  $('sample-session').addEventListener('click', createSample);
  $('chat-form').addEventListener('submit', send);
  $('execution-select').addEventListener('change', (event) =>
    selectExecution(event.target.value).catch((error) => toast(error.message)));
  $('chat-image').addEventListener('change', () => {
    $('image-name').textContent = $('chat-image').files[0]?.name || 'No photo selected';
  });
  $('chat-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('chat-form').requestSubmit(); }
  });
  $('copy-state').addEventListener('click', async () => {
    if (!current) return;
    await navigator.clipboard.writeText(JSON.stringify(current.state, null, 2));
    toast('Shared state copied.');
  });
  try {
    const [health, catalog] = await Promise.all([request('/api/health'), request('/api/graphs')]);
    $('connection').textContent = health.openrouter_configured ? 'Sidecar connected' : 'Model key missing';
    if (!health.openrouter_configured) $('connection').classList.add('error');
    graphCatalog = catalog.graphs;
    renderGraphs(graphCatalog);
    refreshExecutions().catch(() => {});
    setInterval(() => refreshExecutions().catch(() => {}), 2000);
    const saved = localStorage.getItem('openboxes-workflow-session');
    if (saved) {
      try { render(await request(`/api/sessions/${saved}`)); } catch { localStorage.removeItem('openboxes-workflow-session'); }
    }
  } catch (error) {
    $('connection').textContent = 'Connection error';
    $('connection').classList.add('error');
    toast(error.message);
  }
}

initialize();
