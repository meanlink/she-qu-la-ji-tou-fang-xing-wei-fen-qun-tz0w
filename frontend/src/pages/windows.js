import {canApprove, canEdit, slots, state, taskById} from '../state.js';
import {code, empty, formError, input, labelSlot, options, paneHead, pct, rowSet, stat, text, today} from '../ui.js';
import {isOffline, queued} from '../offline-queue.js';

const backfillStates = {booked: ['confirmed', '已入账'], pending: ['paused', '待运营裁定'], returned: ['failed', '已退回']};
const statusBadge = row => {
  const [style, label] = backfillStates[row.state] ?? ['paused', row.state];
  return `<span class="badge ${style}"><i></i>${text(label)}</span>`;
};

function backfillTable(ledger) {
  if (!ledger.records.length) return empty('暂无补传记录。');
  return `<div class="table-wrap"><table><thead><tr><th>采样窗口</th><th>计数</th><th>去向</th><th>裁定</th></tr></thead><tbody>
    ${ledger.records.map(row => `<tr><td><strong>${text(row.name)}</strong><small>${code(row.code)} · ${row.sample_day} · ${labelSlot(row.slot)} · V${row.point_revision}</small></td>
      <td class="mono">${row.total} 次 / 正确 ${row.correct} / 满溢 ${row.overflow}</td>
      <td>${statusBadge(row)}${row.window_id ? `<small><a href="#conditions?window=${row.window_id}">窗口 #${row.window_id}</a></small>` : ''}
        ${row.state === 'pending' ? `<small>${text(ledger.reasons[row.reason] ?? row.reason)}</small>` : ''}
        ${row.state === 'booked' && row.decided_by ? `<small>裁定人 ${text(row.decided_by)}</small>` : ''}
        ${row.decision_note ? `<small>${text(row.decision_note)}</small>` : ''}</td>
      <td>${resolveForm(row)}</td></tr>`).join('')}
    </tbody></table></div>`;
}

function resolveForm(row) {
  if (row.state !== 'pending' || !canApprove()) return '<small>—</small>';
  return `<form class="quality-form" data-action="resolve-backfill" data-id="${row.id}">
    <label>裁定结果<select name="decision"><option value="apply">补入原班次</option><option value="return">退回</option></select></label>
    <label>裁定说明<textarea name="note" rows="2" maxlength="240" placeholder="退回时须注明原因"></textarea></label>${formError}
    <button>保存裁定</button></form>`;
}

function queuePanel() {
  const rows = queued(state.staff.username);
  if (!rows.length) return '';
  return `<div class="workflow-record offline-note"><strong>本机暂存 ${rows.length} 条离线观察</strong>
    <p>${isOffline() ? '当前无网络，联网后自动同步整批记录。' : '网络已恢复，可立即补传。'}</p>
    <ul class="members">${rows.map(row => `<li><span>${text(row.label)}</span><span>${row.total} 次 / 正确 ${row.correct} / 满溢 ${row.overflow}</span></li>`).join('')}</ul>
    <form data-action="sync-backfill"><button ${isOffline() ? 'disabled' : ''}>同步 ${rows.length} 条记录</button></form></div>`;
}

export function windowsPage(params) {
  const active = state.points.filter(point => !point.pending_location);
  const task = taskById(params.get('task'));
  const appointmentDay = params.get('day');
  const sampleDay = task && /^\d{4}-\d{2}-\d{2}$/.test(appointmentDay ?? '') && appointmentDay <= today() ? appointmentDay : today();
  const filter = params.get('filter') ?? 'all';
  const windows = state.windows;
  const shown = windows.filter(window => filter === 'all' || (filter === 'retest' ? window.task_id : !window.task_id));
  const tally = windows.reduce((sum, window) => sum + window.total, 0);
  const valid = task ? task.state === 'retesting' && active.some(point => point.id === task.point_id) : active.length > 0;
  const ledger = state.backfills ?? {records: [], reasons: {}};
  const pending = ledger.records.filter(row => row.state === 'pending').length;
  return `<div class="stats">${stat('采样窗口', windows.length, '最近 250 条内')}${stat('匿名观察', tally, '每次投放记为 1 次观察')}${stat('待裁定补传', pending, '原始记录已留存')}</div>
  <div class="split"><section class="pane">${paneHead('采样流水', '旧址样本保留在流水中，分群只读取当前地址版本。')}
    <div class="filters">${Object.entries({all: '全部窗口', baseline: '基线', retest: '复测'}).map(([key, label]) =>
      `<a href="#windows?filter=${key}" class="${filter === key ? 'chosen' : ''}">${label}</a>`).join('')}</div>
    <div class="table-wrap"><table><thead><tr><th>日期 / 时段</th><th>投放点</th><th>观察量</th><th>正确率</th><th>满溢</th><th>用途</th></tr></thead><tbody>
    ${rowSet(shown, w => `<tr><td>${code(w.sample_day)}<small>${labelSlot(w.slot)}</small></td><td>${text(w.point_name)}
    <small>${code(w.code)} · V${w.point_revision}${w.point_revision !== w.current_revision ? ' 旧址' : ''}</small></td>
    <td class="mono">${w.total}</td><td class="mono">${pct(w.correct / w.total, 0)}</td><td class="mono">${w.overflow}</td>
    <td>${w.task_id ? `<a href="#tasks?task=${w.task_id}">复测 #${w.task_id}</a>` : '基线'}${w.backfill_id ? '<small>离线补传</small>' : ''}<small><a href="#conditions?window=${w.id}">现场条件</a></small></td></tr>`, 6, '这个视图暂无采样窗口。')}
    </tbody></table></div>
    <div class="history">${paneHead('离线补传去向', '断网时的匿名观察联网后整批补传；原班次改约或已有计数的记录留待运营裁定，不计入基线或复测。')}
      ${backfillTable(ledger)}</div></section>
  <aside class="pane entry">${paneHead(task ? '录入宣教复测' : '登记匿名窗口', task ? `${task.point_name} · ${labelSlot(task.focus_slot)} · 任务 #${task.id}` : '仅记录合计数，至少 5 次观察可入账。')}
    ${canEdit() && valid ? `<form id="add-window" data-action="window" data-task="${task?.id ?? ''}">
      <label>投放点<select name="point_id" required>${options(Object.fromEntries(active.filter(p => !task || p.id === task.point_id).map(p => [p.id, `${p.code} · ${p.name}`])), task?.point_id ?? params.get('point'))}</select></label>
      <div class="input-pair">${input('sample_day', '采样日期', `type="date" max="${today()}" value="${sampleDay}"`)}
      <label>时段<select name="slot" required>${options(task ? {[task.focus_slot]: slots[task.focus_slot]} : slots, task?.focus_slot)}</select></label></div>
      ${input('total', '投放观察数', 'type="number" min="5" max="1000"')}
      <div class="input-pair">${input('correct', '分类正确数', 'type="number" min="0" max="1000"')}${input('overflow', '满溢记录数', 'type="number" min="0" max="1000"')}</div>
      ${formError}<button>保存${task ? '复测' : '采样'}</button></form>` : '<p class="aside-note">当前岗位或点位状态暂不可录入。</p>'}
    <p class="aside-note">断网时记录暂存本机，联网后按批次补传。时段口径 · 早间 06–10 / 午间 11–14 / 晚间 17–21</p>
    ${queuePanel()}
    ${task ? `<a href="#tasks?task=${task.id}" class="quiet-link">返回宣教任务 →</a>` : ''}
  </aside></div>`;
}
