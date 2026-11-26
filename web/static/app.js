const i18n = {
  zh: {
    title: '数据采集', brandProduct: '采集控制台', captureActive: '本地采集中',
    tabList: '调用列表', tabStats: '统计概览', tabExport: '数据导出', tabLogs: '运行日志',
    callsEyebrow: '采集台账', callsTitle: '调用记录', statsEyebrow: '运行分析', statsTitle: '运行概览',
    exportEyebrow: '数据集工作台', exportTitle: '训练数据导出', logsEyebrow: '诊断工具', logsTitle: '运行日志', refresh: '刷新', close: '关闭',
    logLines: '行数', clearLogs: '清除日志', clearLogsConfirm: '确定清除当前代理日志？', loading: '加载中...', logLoadFailed: '日志加载失败',
    allHosts: '全部 Host', allModels: '全部模型', allProtocols: '全部协议', allStatus: '全部状态', success: '成功', error: '失败',
    search: '查询', modelPh: '模型', startTime: '开始时间', endTime: '结束时间', clearTimeRange: '清除时间范围',
    invalidTimeRange: '开始时间不能晚于结束时间。', filterLoadFailed: '无法加载筛选选项',
    colTime: '时间', colProtocol: '协议', colModel: '模型', colStatus: '状态',
    colDuration: '耗时', colFirstToken: '首Token', colStream: '流式', colActions: '操作',
    prevPage: '上一页', nextPage: '下一页',
    callDetail: '调用详情', metadata: '元数据', request: '请求', response: '响应',
    copyJson: '复制', copied: '已复制', copyFailed: '复制失败',
    headersSanitized: 'Headers (脱敏)',
    noData: '暂无数据',
    recordsSelected: '条记录已选择', selectPage: '选择当前页', selectFiltered: '选择全部筛选结果',
    clearSelection: '清空', exportSelected: '导出所选', changeSelection: '修改选择',
    deleteSelected: '删除所选', deleteSelectedConfirm: count => `确定删除选中的 ${count} 条记录？`,
    selectionReady: count => `已选择 ${count} 条调用`, selectionReadyNote: '检查和导出仅处理这些明确选择的记录。',
    selectionEmpty: '尚未选择调用', selectionEmptyNote: '请先到调用列表选择要导出的记录。',
    selectFilteredConfirm: count => `选择当前筛选条件下的全部 ${count} 条记录？`,
    pageInfo: (p, t, total) => `第 ${p} 页 / 共 ${t} 页 (${total} 条)`,
    statsByHost: '按 Host 统计', statsByProtocol: '按协议统计', statsByModel: '按模型统计',
    colCount: '调用数', colAvgDuration: '平均耗时(ms)',
    totalCalls: '总调用数', successCalls: '成功调用', avgDuration: '平均耗时', avgFirstToken: '平均首Token',
    streamBadge: '流式',
    delete: '删除', deleteConfirm: '确定删除这条记录？', deleteBtn: '删除', deleted: '已删除',
    exportSettings: '导出设置', exportFormat: '数据格式',
    includeMetadata: '包含溯源元数据', includeSkipped: '包含低质量样本', includeTools: '注入工具定义',
    enableContextLimit: '限制每条记录上下文',
    contextConstraint: '单条记录上下文约束', useRecommended: '使用最大目标预算',
    maxSeqLen: '每条记录最大估算 Token', charsPerToken: '字符 / Token', prefixRatio: '固定前缀最多占比',
    exportFormatHint: '决定导出文件的结构与目标训练框架。Canonical 信息最完整；Tool SFT、OpenAI 和 ShareGPT 更适合直接训练。',
    includeMetadataHint: '在每条样本中附加来源、模型、轨迹和统计等审计字段。会增加文件体积，通常不作为训练文本。Canonical 始终包含这些信息。',
    includeSkippedHint: '同时导出被质量规则标记为应跳过的样本，例如无法形成有效训练轨迹的调用。仅建议用于排查或人工复核。',
    includeToolsHint: '在 ShareGPT 对话开头写入可用工具定义，使工具调用轨迹具备完整上下文。可能明显增加每条记录的长度。',
    contextLimitHint: '把长轨迹拆分为以 Assistant 消息为目标的窗口，并控制每条输出记录的估算长度。适合有固定上下文窗口的训练任务。',
    maxSeqLenHint: '每条输出记录允许的最大估算 Token 数。估算包含消息、工具定义和结构化工具调用，不是 tokenizer 的精确计数。',
    charsPerTokenHint: '用于把字符数换算成 Token 的估算系数。数值越小，预算越保守；中文或代码较多时可适当调低。',
    prefixRatioHint: '系统提示、工具定义等固定前缀最多占总预算的比例。超出时会裁剪前缀，为对话历史和目标回复保留空间。',
    inspectData: '检查数据', startExport: '开始导出', datasetOverview: '数据概况', generatedFiles: '最近生成',
    fileName: '文件', fileSize: '大小', createdTime: '生成时间', download: '下载',
    callsAvailable: '调用', episodesAvailable: '可解析轨迹', assistantTargets: 'Assistant 目标',
    inspecting: '正在检查数据...', exporting: '正在导出，较大的数据集可能需要一些时间...', exportDone: '导出完成', exportFailed: '导出失败',
    noExports: '暂无导出文件', recommendedBudget: '建议窗口长度',
    formatCanonical: '信息最完整的中间格式，适合审计和二次转换。',
    formatToolSft: '保留结构化 tools、tool_calls 和 tool 结果。',
    formatOpenAI: 'OpenAI messages 格式，推理会写入 <think> 块。',
    formatWindowed: '每条样本以一个 assistant 消息为训练目标，适合长轨迹。',
    formatShareGPT: 'ShareGPT 对话数组，工具轨迹使用文本标签表示。',
    contextLimitStatus: (limit, p95, maxTarget) => `每条输出记录不超过 ${limit} 估算 Token · P95 目标最低 ${p95} · 最大目标最低 ${maxTarget}`,
    actualMaxUnits: '实际最大上下文',
  },
  en: {
    title: 'Data Collection', brandProduct: 'Capture Console', captureActive: 'Capturing locally',
    tabList: 'Calls', tabStats: 'Stats', tabExport: 'Export', tabLogs: 'Logs',
    callsEyebrow: 'Capture ledger', callsTitle: 'Call records', statsEyebrow: 'Operations', statsTitle: 'Run overview',
    exportEyebrow: 'Dataset workspace', exportTitle: 'Training data export', logsEyebrow: 'Diagnostics', logsTitle: 'Runtime logs', refresh: 'Refresh', close: 'Close',
    logLines: 'Lines', clearLogs: 'Clear logs', clearLogsConfirm: 'Clear the current proxy log?', loading: 'Loading...', logLoadFailed: 'Failed to load logs',
    allHosts: 'All Hosts', allModels: 'All Models', allProtocols: 'All Protocols', allStatus: 'All Status', success: 'Success', error: 'Error',
    search: 'Search', modelPh: 'Model', startTime: 'Start time', endTime: 'End time', clearTimeRange: 'Clear time range',
    invalidTimeRange: 'Start time cannot be after end time.', filterLoadFailed: 'Could not load filter options',
    colTime: 'Time', colProtocol: 'Protocol', colModel: 'Model', colStatus: 'Status',
    colDuration: 'Duration', colFirstToken: 'First Token', colStream: 'Stream', colActions: 'Actions',
    prevPage: 'Prev', nextPage: 'Next',
    callDetail: 'Call Detail', metadata: 'Metadata', request: 'Request', response: 'Response',
    copyJson: 'Copy', copied: 'Copied', copyFailed: 'Copy failed',
    headersSanitized: 'Headers (sanitized)',
    noData: 'No data',
    recordsSelected: 'records selected', selectPage: 'Select page', selectFiltered: 'Select all filtered',
    clearSelection: 'Clear', exportSelected: 'Export selected', changeSelection: 'Change selection',
    deleteSelected: 'Delete selected', deleteSelectedConfirm: count => `Delete the ${count} selected records?`,
    selectionReady: count => `${count} calls selected`, selectionReadyNote: 'Inspection and export process only these explicitly selected records.',
    selectionEmpty: 'No calls selected', selectionEmptyNote: 'Select the calls you want to export from the Calls view first.',
    selectFilteredConfirm: count => `Select all ${count} records matching the current filters?`,
    pageInfo: (p, t, total) => `Page ${p} of ${t} (${total} records)`,
    statsByHost: 'By Host', statsByProtocol: 'By Protocol', statsByModel: 'By Model',
    colCount: 'Count', colAvgDuration: 'Avg Duration (ms)',
    totalCalls: 'Total Calls', successCalls: 'Success', avgDuration: 'Avg Duration', avgFirstToken: 'Avg First Token',
    streamBadge: 'Stream',
    delete: 'Delete', deleteConfirm: 'Delete this record?', deleteBtn: 'Delete', deleted: 'Deleted',
    exportSettings: 'Export settings', exportFormat: 'Dataset format',
    includeMetadata: 'Include trace metadata', includeSkipped: 'Include low-quality samples', includeTools: 'Inject tool definitions',
    enableContextLimit: 'Limit context per record',
    contextConstraint: 'Per-record context constraint', useRecommended: 'Use max-target budget',
    maxSeqLen: 'Maximum estimated tokens per record', charsPerToken: 'Characters / token', prefixRatio: 'Maximum fixed-prefix ratio',
    exportFormatHint: 'Controls the output schema and target training stack. Canonical preserves the most detail; Tool SFT, OpenAI, and ShareGPT are closer to training-ready formats.',
    includeMetadataHint: 'Adds source, model, trajectory, and statistics fields for auditing. This increases file size and is normally not used as training text. Canonical always includes this information.',
    includeSkippedHint: 'Also exports samples rejected by quality rules, such as calls that cannot form a valid training trajectory. Recommended only for debugging or manual review.',
    includeToolsHint: 'Writes available tool definitions at the start of each ShareGPT conversation so tool calls have complete context. This can substantially increase record length.',
    contextLimitHint: 'Splits long trajectories into windows that target individual Assistant messages and caps the estimated length of every output record. Use this for models with a fixed context window.',
    maxSeqLenHint: 'Maximum estimated tokens allowed in each output record. The estimate includes messages, tool definitions, and structured tool calls; it is not an exact tokenizer count.',
    charsPerTokenHint: 'Approximation used to convert characters into tokens. Lower values are more conservative; consider lowering it for Chinese-heavy or code-heavy data.',
    prefixRatioHint: 'Maximum share of the budget reserved for fixed prefixes such as system prompts and tool definitions. Excess prefix content is trimmed to preserve history and the target response.',
    inspectData: 'Inspect data', startExport: 'Start export', datasetOverview: 'Dataset overview', generatedFiles: 'Recent files',
    fileName: 'File', fileSize: 'Size', createdTime: 'Created', download: 'Download',
    callsAvailable: 'Calls', episodesAvailable: 'Parsed episodes', assistantTargets: 'Assistant targets',
    inspecting: 'Inspecting data...', exporting: 'Exporting; large datasets may take some time...', exportDone: 'Export complete', exportFailed: 'Export failed',
    noExports: 'No export files yet', recommendedBudget: 'Recommended window length',
    formatCanonical: 'Highest-fidelity intermediate format for auditing and further conversion.',
    formatToolSft: 'Preserves structured tools, tool_calls, and tool results.',
    formatOpenAI: 'OpenAI messages format with reasoning emitted in <think> blocks.',
    formatWindowed: 'Each sample targets one assistant message and is suitable for long trajectories.',
    formatShareGPT: 'ShareGPT conversation array with tool trajectories represented as text tags.',
    contextLimitStatus: (limit, p95, maxTarget) => `Each output record is capped at ${limit} estimated tokens · P95 target minimum ${p95} · maximum target minimum ${maxTarget}`,
    actualMaxUnits: 'Actual largest context',
  }
};

const LANGUAGE_PREFERENCE_KEY = 'llm-tap-language-v2';
let lang = localStorage.getItem(LANGUAGE_PREFERENCE_KEY) || 'en';

function t(key) { return i18n[lang][key]; }

function setLang(l) {
  lang = l;
  localStorage.setItem(LANGUAGE_PREFERENCE_KEY, l);
  applyI18n();
  document.getElementById('lang-zh').classList.toggle('active', l === 'zh');
  document.getElementById('lang-en').classList.toggle('active', l === 'en');
  // 重新渲染动态内容
  loadCalls();
  if (document.getElementById('view-stats').style.display !== 'none') loadStats();
  if (document.getElementById('view-export').style.display !== 'none') {
    updateExportFields();
    renderExportSummary(lastExportReport);
    loadExports();
  }
  updateSelectionUi();
  loadFilterOptions();
}

function applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    if (i18n[lang][key]) el.textContent = i18n[lang][key];
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    const key = el.getAttribute('data-i18n-ph');
    if (i18n[lang][key]) el.placeholder = i18n[lang][key];
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    const key = el.getAttribute('data-i18n-title');
    if (i18n[lang][key]) {
      el.title = i18n[lang][key];
      el.setAttribute('aria-label', i18n[lang][key]);
    }
  });
  document.querySelectorAll('[data-i18n-tooltip]').forEach(el => {
    const key = el.getAttribute('data-i18n-tooltip');
    if (i18n[lang][key]) el.dataset.tooltip = i18n[lang][key];
  });
  document.querySelectorAll('[data-hint]').forEach(el => {
    const key = el.getAttribute('data-hint');
    const message = i18n[lang][key];
    if (message) {
      el.dataset.tooltip = message;
      el.setAttribute('aria-label', message);
    }
  });
  document.documentElement.lang = lang;
  document.title = 'llm-tap · ' + t('title');
}

function closeHints(except = null) {
  document.querySelectorAll('.hint-trigger[aria-expanded="true"]').forEach(el => {
    if (el !== except) el.setAttribute('aria-expanded', 'false');
  });
}

let currentPage = 1;
let total = 0;
let currentDetailCallId = null;
let currentDetailSections = [];
let currentPageCallIds = [];
const selectedCallIds = new Set();
const pageSize = 20;

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function setBusy(button, busy, labelKey = null) {
  if (!button) return;
  button.disabled = busy;
  button.classList.toggle('is-loading', busy);
  if (labelKey) button.textContent = t(labelKey);
}

function currentFilters() {
  const startValue = document.getElementById('f-start-time').value;
  const endValue = document.getElementById('f-end-time').value;
  return {
    host: document.getElementById('f-host').value,
    protocol: document.getElementById('f-protocol').value,
    model: document.getElementById('f-model').value,
    status: document.getElementById('f-status').value,
    start_time: startValue ? startValue + ':00' : '',
    end_time: endValue ? endValue + ':59.999999' : ''
  };
}

function applyCallFilters() {
  const startValue = document.getElementById('f-start-time').value;
  const endValue = document.getElementById('f-end-time').value;
  if (startValue && endValue && startValue > endValue) {
    alert(t('invalidTimeRange'));
    return;
  }
  currentPage = 1;
  loadCalls();
}

function clearTimeRange() {
  document.getElementById('f-start-time').value = '';
  document.getElementById('f-end-time').value = '';
  applyCallFilters();
}

function renderFilterOptions(selectId, rows, allLabelKey) {
  const select = document.getElementById(selectId);
  const currentValue = select.value;
  const options = [`<option value="">${esc(t(allLabelKey))}</option>`];
  (rows || []).forEach(row => {
    options.push(`<option value="${esc(row.value)}">${esc(row.value)} (${esc(row.count)})</option>`);
  });
  select.innerHTML = options.join('');
  if ((rows || []).some(row => row.value === currentValue)) select.value = currentValue;
}

async function loadFilterOptions() {
  try {
    const res = await fetch('/api/filter-options');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    renderFilterOptions('f-host', data.hosts, 'allHosts');
    renderFilterOptions('f-model', data.models, 'allModels');
  } catch (err) {
    console.warn(t('filterLoadFailed'), err);
  }
}

function selectedIds() {
  return Array.from(selectedCallIds);
}

function updateSelectionUi() {
  const count = selectedCallIds.size;
  document.getElementById('selected-count').textContent = count;
  document.getElementById('clear-selection-btn').disabled = count === 0;
  document.getElementById('delete-selected-btn').disabled = count === 0;
  document.getElementById('export-selected-btn').disabled = count === 0;

  const scope = document.getElementById('selection-scope');
  scope.classList.toggle('is-empty', count === 0);
  document.getElementById('selection-scope-title').textContent = count ? t('selectionReady')(count) : t('selectionEmpty');
  document.getElementById('selection-scope-note').textContent = count ? t('selectionReadyNote') : t('selectionEmptyNote');
  document.getElementById('inspect-btn').disabled = count === 0;
  document.getElementById('export-btn').disabled = count === 0;

  document.querySelectorAll('.call-select').forEach(input => {
    input.checked = selectedCallIds.has(input.dataset.callId);
    const row = input.closest('tr');
    if (row) row.classList.toggle('is-selected', input.checked);
  });
  const pageCheckbox = document.getElementById('select-page-checkbox');
  const selectedOnPage = currentPageCallIds.filter(id => selectedCallIds.has(id)).length;
  pageCheckbox.checked = currentPageCallIds.length > 0 && selectedOnPage === currentPageCallIds.length;
  pageCheckbox.indeterminate = selectedOnPage > 0 && selectedOnPage < currentPageCallIds.length;
}

function toggleCallSelection(event, callId) {
  event.stopPropagation();
  if (event.currentTarget.checked) selectedCallIds.add(callId);
  else selectedCallIds.delete(callId);
  lastExportReport = null;
  renderExportSummary(null);
  updateSelectionUi();
}

function toggleCurrentPage(checked) {
  currentPageCallIds.forEach(callId => checked ? selectedCallIds.add(callId) : selectedCallIds.delete(callId));
  lastExportReport = null;
  renderExportSummary(null);
  updateSelectionUi();
}

function selectCurrentPage() {
  toggleCurrentPage(true);
}

async function selectFilteredCalls() {
  const button = document.getElementById('select-filtered-btn');
  setBusy(button, true);
  try {
    const params = new URLSearchParams(currentFilters());
    const res = await fetch('/api/call-ids?' + params);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    if (!confirm(t('selectFilteredConfirm')(data.total || 0))) return;
    (data.call_ids || []).forEach(callId => selectedCallIds.add(callId));
    lastExportReport = null;
    renderExportSummary(null);
    updateSelectionUi();
  } catch (err) {
    alert((err && err.message) ? err.message : String(err));
  } finally {
    setBusy(button, false);
  }
}

function clearSelection() {
  selectedCallIds.clear();
  lastExportReport = null;
  renderExportSummary(null);
  clearExportStatus();
  updateSelectionUi();
}

async function deleteSelectedCalls() {
  const count = selectedCallIds.size;
  if (!count || !confirm(t('deleteSelectedConfirm')(count))) return;
  const button = document.getElementById('delete-selected-btn');
  setBusy(button, true);
  try {
    const res = await fetch('/api/calls', {
      method: 'DELETE',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({call_ids: selectedIds()}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    total = Math.max(0, total - (data.deleted_count || 0));
    clearSelection();
    currentPage = Math.min(currentPage, Math.max(1, Math.ceil(total / pageSize)));
    await loadCalls();
    if (document.getElementById('view-stats').style.display !== 'none') loadStats();
  } catch (err) {
    alert((err && err.message) ? err.message : String(err));
  } finally {
    setBusy(button, false);
    updateSelectionUi();
  }
}

function openSelectedExport() {
  if (!selectedCallIds.size) return;
  switchTab('export');
}

async function loadCalls() {
  const refreshButton = document.getElementById('calls-refresh');
  setBusy(refreshButton, true);
  const params = new URLSearchParams({page: currentPage, page_size: pageSize, ...currentFilters()});
  const tbody = document.getElementById('calls-tbody');
  try {
    const res = await fetch('/api/calls?' + params);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    total = data.total;
    currentPageCallIds = data.data.map(row => row.call_id);
    tbody.innerHTML = data.data.map(r => `
      <tr onclick="showDetail('${esc(r.call_id)}')">
        <td class="select-cell"><input type="checkbox" class="call-select" data-call-id="${esc(r.call_id)}" onclick="event.stopPropagation()" onchange="toggleCallSelection(event, '${esc(r.call_id)}')" aria-label="${esc(t('exportSelected'))}"></td>
        <td>${esc(fmtTime(r.started_at))}</td>
        <td>${esc(r.upstream_provider || '-')}</td>
        <td><span class="tag">${esc(r.protocol)}</span></td>
        <td>${esc(r.upstream_model || '-')}</td>
        <td>${r.upstream_status === 200 ? '<span class="badge badge-success">200</span>' : '<span class="badge badge-error">'+esc(r.upstream_status)+'</span>'}</td>
        <td>${r.duration_ms ? esc(r.duration_ms)+'ms' : '-'}</td>
        <td>${r.first_token_ms ? esc(r.first_token_ms)+'ms' : '-'}</td>
        <td>${r.is_stream ? '<span class="badge badge-stream">'+t('streamBadge')+'</span>' : '-'}</td>
        <td class="action-cell"><button class="danger-btn" onclick="deleteCall(event, '${esc(r.call_id)}')">${t('delete')}</button></td>
      </tr>
    `).join('') || '<tr><td colspan="10" style="text-align:center;color:#87939a;padding:40px">'+t('noData')+'</td></tr>';
    const totalPages = Math.max(1, Math.ceil(total/pageSize));
    document.getElementById('page-info').textContent = t('pageInfo')(currentPage, totalPages, total);
    document.getElementById('btn-prev').disabled = currentPage <= 1;
    document.getElementById('btn-next').disabled = currentPage >= totalPages;
    updateSelectionUi();
  } catch (err) {
    currentPageCallIds = [];
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#a33;padding:40px">'+esc((err && err.message) ? err.message : String(err))+'</td></tr>';
  } finally {
    setBusy(refreshButton, false);
  }
}

async function refreshCallsView() {
  await Promise.all([loadFilterOptions(), loadCalls()]);
}

function changePage(d) {
  currentPage += d;
  if (currentPage < 1) currentPage = 1;
  loadCalls();
}

async function deleteCall(event, callId, closeAfterDelete = false) {
  event.stopPropagation();
  if (!callId) return;
  if (!confirm(t('deleteConfirm') + '\n' + callId)) return;
  const btn = event.currentTarget;
  btn.disabled = true;
  try {
    const res = await fetch('/api/calls/' + encodeURIComponent(callId), { method: 'DELETE' });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || ('HTTP ' + res.status));
    }
    total = Math.max(0, total - 1);
    selectedCallIds.delete(callId);
    lastExportReport = null;
    currentPage = Math.min(currentPage, Math.max(1, Math.ceil(total / pageSize)));
    if (closeAfterDelete) closeModal();
    await loadCalls();
    if (document.getElementById('view-stats').style.display !== 'none') loadStats();
  } catch (err) {
    alert((err && err.message) ? err.message : String(err));
    btn.disabled = false;
  }
}

function highlightJson(value) {
  const json = JSON.stringify(value, null, 2);
  if (json === undefined) return '';
  const tokenPattern = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
  let html = '';
  let lastIndex = 0;
  let match;
  while ((match = tokenPattern.exec(json)) !== null) {
    html += esc(json.slice(lastIndex, match.index));
    if (match[1]) {
      const className = match[2] ? 'json-key' : 'json-string';
      html += '<span class="' + className + '">' + esc(match[1]) + '</span>';
      if (match[2]) html += esc(match[2]);
    } else if (match[3]) {
      const className = match[3] === 'null' ? 'json-null' : 'json-boolean';
      html += '<span class="' + className + '">' + match[3] + '</span>';
    } else {
      html += '<span class="json-number">' + esc(match[0]) + '</span>';
    }
    lastIndex = tokenPattern.lastIndex;
  }
  return html + esc(json.slice(lastIndex));
}

const COPY_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"></rect><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3"></path></svg>';
const CHECK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l4 4L19 6"></path></svg>';

async function copySectionJson(event, index) {
  event.preventDefault();
  event.stopPropagation();
  const button = event.currentTarget;
  const json = currentDetailSections[index];
  if (json === undefined) return;
  button.disabled = true;
  try {
    let copied = false;
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(json);
        copied = true;
      } catch (_) {
        // Fall back to a temporary textarea when clipboard permission is denied.
      }
    }
    if (!copied) {
      const textarea = document.createElement('textarea');
      textarea.value = json;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      copied = document.execCommand('copy');
      textarea.remove();
      if (!copied) throw new Error(t('copyFailed'));
    }
    button.classList.add('copied');
    button.innerHTML = CHECK_ICON;
    button.dataset.tooltip = t('copied');
    button.setAttribute('aria-label', t('copied'));
    clearTimeout(button._copyResetTimer);
    button._copyResetTimer = setTimeout(() => {
      button.classList.remove('copied');
      button.innerHTML = COPY_ICON;
      button.dataset.tooltip = t('copyJson');
      button.setAttribute('aria-label', t('copyJson'));
    }, 5000);
  } catch (err) {
    alert(t('copyFailed') + ': ' + ((err && err.message) ? err.message : String(err)));
  } finally {
    button.disabled = false;
  }
}

async function showDetail(callId) {
  currentDetailCallId = callId;
  currentDetailSections = [];
  document.getElementById('modal-delete-btn').disabled = false;
  const res = await fetch('/api/calls/' + callId);
  const data = await res.json();
  const call = data.call || {};
  const meta = call.meta || data.meta || {};
  document.getElementById('modal-title').textContent = t('callDetail');
  function preBlock(obj) { return '<pre>' + highlightJson(obj) + '</pre>'; }
  function detailBlock(title, obj, open) {
    const index = currentDetailSections.push(JSON.stringify(obj, null, 2)) - 1;
    const copyLabel = esc(t('copyJson'));
    return '<details class="detail-section"'+(open ? ' open' : '')+'><summary><span>'+esc(title)+'</span><button type="button" class="section-copy-btn" onclick="copySectionJson(event, '+index+')" data-i18n-tooltip="copyJson" data-tooltip="'+copyLabel+'" aria-label="'+copyLabel+'">'+COPY_ICON+'</button></summary>' + preBlock(obj) + '</details>';
  }
  let html = '';
  html += detailBlock(t('metadata'), meta, false);
  if (call.request) html += detailBlock(t('request'), call.request, false);
  if (call.response) html += detailBlock(t('response'), call.response, true);
  if (call.headers) html += detailBlock(t('headersSanitized'), call.headers, false);
  document.getElementById('modal-body').innerHTML = html;
  document.getElementById('modal').classList.add('show');
  document.getElementById('modal').setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
}

function closeModal() {
  currentDetailCallId = null;
  currentDetailSections = [];
  document.getElementById('modal').classList.remove('show');
  document.getElementById('modal').setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
}

async function loadStats() {
  const refreshButton = document.getElementById('stats-refresh');
  setBusy(refreshButton, true);
  try {
    const res = await fetch('/api/stats');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    const ov = data.overview || {};
    document.getElementById('stats-overview').innerHTML = `
      <div class="stat-card"><div class="label">${t('totalCalls')}</div><div class="value">${esc(ov.total||0)}</div></div>
      <div class="stat-card"><div class="label">${t('successCalls')}</div><div class="value metric-success">${esc(ov.success||0)}</div></div>
      <div class="stat-card"><div class="label">${t('avgDuration')}</div><div class="value">${esc(Math.round(ov.avg_duration||0))}<span>ms</span></div></div>
      <div class="stat-card"><div class="label">${t('avgFirstToken')}</div><div class="value">${esc(Math.round(ov.avg_first_token||0))}<span>ms</span></div></div>
    `;
    document.getElementById('stats-host').innerHTML = (data.by_host||[]).map(r =>
      `<tr><td>${esc(r.host||'-')}</td><td>${esc(r.count)}</td><td class="metric-success">${esc(r.success||0)}</td><td>${esc(Math.round(r.avg_duration||0))}</td></tr>`
    ).join('') || '<tr><td colspan="4" style="text-align:center;color:#87939a">'+t('noData')+'</td></tr>';
    document.getElementById('stats-protocol').innerHTML = (data.by_protocol||[]).map(r =>
      `<tr><td><span class="tag">${esc(r.protocol)}</span></td><td>${esc(r.count)}</td></tr>`
    ).join('') || '<tr><td colspan="2" style="text-align:center;color:#87939a">'+t('noData')+'</td></tr>';
    document.getElementById('stats-model').innerHTML = (data.by_model||[]).map(r =>
      `<tr><td>${esc(r.model||'-')}</td><td>${esc(r.count)}</td></tr>`
    ).join('') || '<tr><td colspan="2" style="text-align:center;color:#87939a">'+t('noData')+'</td></tr>';
  } finally {
    setBusy(refreshButton, false);
  }
}

const formatDescriptionKeys = {
  canonical: 'formatCanonical',
  tool_sft: 'formatToolSft',
  openai: 'formatOpenAI',
  sharegpt: 'formatShareGPT'
};
let lastExportReport = null;

function updateExportFields() {
  const format = document.getElementById('export-format').value;
  const contextLimited = document.getElementById('export-context-limit').checked;
  document.getElementById('sharegpt-tools-row').style.display = format === 'sharegpt' ? 'flex' : 'none';
  document.getElementById('window-options').style.display = contextLimited ? '' : 'none';
  document.getElementById('export-metadata').disabled = format === 'canonical';
  document.getElementById('format-description').textContent = t(formatDescriptionKeys[format]);
  updateContextConstraintStatus();
}

function updateContextConstraintStatus() {
  const budget = (lastExportReport && lastExportReport.window_budget) || {};
  const limit = Number(document.getElementById('export-max-seq').value || 4096);
  const p95 = budget.p95_min_max_seq_len || '-';
  const maxTarget = budget.recommended_min_max_seq_len || '-';
  document.getElementById('context-limit-status').textContent = t('contextLimitStatus')(limit, p95, maxTarget);
  document.getElementById('use-recommended-btn').disabled = !budget.recommended_min_max_seq_len;
}

function useRecommendedBudget() {
  const budget = (lastExportReport && lastExportReport.window_budget) || {};
  if (!budget.recommended_min_max_seq_len) return;
  document.getElementById('export-max-seq').value = budget.recommended_min_max_seq_len;
  updateContextConstraintStatus();
}

function setExportStatus(message, kind = 'info') {
  const box = document.getElementById('export-status');
  box.className = 'status-box show status-' + kind;
  box.textContent = message;
}

function clearExportStatus() {
  const box = document.getElementById('export-status');
  box.className = 'status-box';
  box.textContent = '';
}

function fmtBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return value + ' B';
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + ' KB';
  if (value < 1024 * 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + ' MB';
  return (value / 1024 / 1024 / 1024).toFixed(1) + ' GB';
}

function renderExportSummary(report) {
  const summary = document.getElementById('export-summary');
  if (!report) {
    summary.innerHTML = '<div class="summary-item"><span>'+t('callsAvailable')+'</span><strong>-</strong></div>' +
      '<div class="summary-item"><span>'+t('episodesAvailable')+'</span><strong>-</strong></div>' +
      '<div class="summary-item"><span>'+t('assistantTargets')+'</span><strong>-</strong></div>';
    document.getElementById('window-budget-note').textContent = '';
    return;
  }
  const budget = report.window_budget || {};
  summary.innerHTML = `
    <div class="summary-item"><span>${t('callsAvailable')}</span><strong>${report.calls || 0}</strong></div>
    <div class="summary-item"><span>${t('episodesAvailable')}</span><strong>${report.episodes || 0}</strong></div>
    <div class="summary-item"><span>${t('assistantTargets')}</span><strong>${budget.assistant_targets || '-'}</strong></div>
  `;
  document.getElementById('window-budget-note').textContent = budget.recommended_min_max_seq_len
    ? t('recommendedBudget') + ': ' + budget.recommended_min_max_seq_len +
      ' · P95: ' + (budget.p95_min_max_seq_len || '-') + ' · ' + (budget.chars_per_token || 4) + ' chars/token'
    : '';
  updateContextConstraintStatus();
}

async function loadExportInspect(showStatus = false) {
  const inspectBtn = document.getElementById('inspect-btn');
  if (!selectedCallIds.size) {
    updateSelectionUi();
    return;
  }
  setBusy(inspectBtn, true);
  if (showStatus) setExportStatus(t('inspecting'), 'info');
  try {
    const res = await fetch('/api/export/inspect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        call_ids: selectedIds(),
        include_window_budget: true,
        chars_per_token: Number(document.getElementById('export-chars-token').value || 4.0)
      })
    });
    const report = await res.json();
    if (!res.ok) throw new Error(report.error || ('HTTP ' + res.status));
    lastExportReport = report;
    renderExportSummary(report);
    if (showStatus) clearExportStatus();
  } catch (err) {
    setExportStatus((err && err.message) ? err.message : String(err), 'error');
  } finally {
    setBusy(inspectBtn, false);
    updateSelectionUi();
  }
}

async function createExport() {
  const button = document.getElementById('export-btn');
  if (!selectedCallIds.size) {
    updateSelectionUi();
    return;
  }
  setBusy(button, true);
  setExportStatus(t('exporting'), 'info');
  const payload = {
    format: document.getElementById('export-format').value,
    call_ids: selectedIds(),
    include_metadata: document.getElementById('export-metadata').checked,
    include_skipped: document.getElementById('export-skipped').checked,
    include_tools: document.getElementById('export-tools').checked,
    context_limit: document.getElementById('export-context-limit').checked,
    max_seq_len: Number(document.getElementById('export-max-seq').value),
    chars_per_token: Number(document.getElementById('export-chars-token').value),
    prefix_budget_ratio: Number(document.getElementById('export-prefix-ratio').value)
  };
  try {
    const res = await fetch('/api/export', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const result = await res.json();
    if (!res.ok) throw new Error(result.error || ('HTTP ' + res.status));
    const written = result.written == null ? 0 : result.written;
    const link = result.download_url;
    const box = document.getElementById('export-status');
    box.className = 'status-box show status-success';
    const maxUnits = result.context_limited && result.max_estimated_units != null
      ? ` · ${t('actualMaxUnits')}: ${result.max_estimated_units}/${result.max_seq_len}`
      : '';
    box.innerHTML = `${t('exportDone')}: ${written} · ${fmtBytes(result.size_bytes)}${maxUnits} · <a class="download-link" href="${link}">${t('download')}</a>`;
    await loadExports();
  } catch (err) {
    setExportStatus(t('exportFailed') + ': ' + ((err && err.message) ? err.message : String(err)), 'error');
  } finally {
    setBusy(button, false);
    updateSelectionUi();
  }
}

async function loadExports() {
  const tbody = document.getElementById('exports-tbody');
  try {
    const res = await fetch('/api/exports');
    const result = await res.json();
    if (!res.ok) throw new Error(result.error || ('HTTP ' + res.status));
    tbody.innerHTML = (result.data || []).map(row => `
      <tr>
        <td title="${esc(row.filename)}">${esc(row.filename)}</td>
        <td>${esc(fmtBytes(row.size_bytes))}</td>
        <td>${esc(fmtTime(row.created_at))}</td>
        <td><a class="download-link" href="${esc(row.download_url)}">${t('download')}</a></td>
      </tr>
    `).join('') || '<tr><td colspan="4" style="text-align:center;color:#87939a;padding:30px">'+t('noExports')+'</td></tr>';
  } catch (err) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#a33;padding:30px">'+
      esc((err && err.message) ? err.message : String(err))+'</td></tr>';
  }
}

async function refreshExportView() {
  const button = document.getElementById('export-refresh');
  setBusy(button, true);
  try {
    const tasks = [loadExports()];
    if (selectedCallIds.size) tasks.push(loadExportInspect(false));
    await Promise.all(tasks);
  } finally {
    setBusy(button, false);
  }
}

async function loadLogs() {
  const button = document.getElementById('logs-refresh');
  setBusy(button, true);
  const viewer = document.getElementById('log-viewer');
  try {
    const limit = document.getElementById('log-limit').value;
    const res = await fetch('/api/logs?limit=' + encodeURIComponent(limit));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    document.getElementById('log-path').textContent = data.path || '';
    viewer.textContent = (data.lines || []).join('\n') || t('noData');
    viewer.scrollTop = viewer.scrollHeight;
  } catch (err) {
    viewer.textContent = t('logLoadFailed') + ': ' + ((err && err.message) ? err.message : String(err));
  } finally {
    setBusy(button, false);
  }
}

async function clearLogs() {
  if (!confirm(t('clearLogsConfirm'))) return;
  try {
    const res = await fetch('/api/logs', {method: 'DELETE'});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    await loadLogs();
  } catch (err) {
    alert((err && err.message) ? err.message : String(err));
  }
}

function switchTab(tab) {
  document.getElementById('view-list').style.display = tab === 'list' ? '' : 'none';
  document.getElementById('view-stats').style.display = tab === 'stats' ? '' : 'none';
  document.getElementById('view-export').style.display = tab === 'export' ? '' : 'none';
  document.getElementById('view-logs').style.display = tab === 'logs' ? '' : 'none';
  document.getElementById('tab-list').classList.toggle('active', tab === 'list');
  document.getElementById('tab-stats').classList.toggle('active', tab === 'stats');
  document.getElementById('tab-export').classList.toggle('active', tab === 'export');
  document.getElementById('tab-logs').classList.toggle('active', tab === 'logs');
  document.getElementById('tab-list').setAttribute('aria-current', tab === 'list' ? 'page' : 'false');
  document.getElementById('tab-stats').setAttribute('aria-current', tab === 'stats' ? 'page' : 'false');
  document.getElementById('tab-export').setAttribute('aria-current', tab === 'export' ? 'page' : 'false');
  document.getElementById('tab-logs').setAttribute('aria-current', tab === 'logs' ? 'page' : 'false');
  sessionStorage.setItem('activeTab', tab);
  if (tab === 'stats') loadStats();
  if (tab === 'logs') loadLogs();
  if (tab === 'export') {
    updateExportFields();
    updateSelectionUi();
    if (selectedCallIds.size && !lastExportReport) loadExportInspect(false);
    loadExports();
  }
}

function fmtTime(s) {
  if (!s) return '-';
  return s.replace('T', ' ').substring(0, 19);
}

// 初始化
setLang(lang);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    closeHints();
    if (document.getElementById('modal').classList.contains('show')) closeModal();
  }
});
document.addEventListener('click', event => {
  const trigger = event.target.closest('.hint-trigger');
  if (trigger) {
    const willOpen = trigger.getAttribute('aria-expanded') !== 'true';
    closeHints(trigger);
    trigger.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    event.stopPropagation();
    return;
  }
  closeHints();
});
switchTab(sessionStorage.getItem('activeTab') || 'list');
