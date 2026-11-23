import {panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

export function correctionPanel(state, selected) {
  const book = state.corrections;
  if (!book) return '';
  const {batch, orders, coverage} = book;
  const coverageRows = coverage.map(row=>[esc(row['配方编号']), `${row['生效水位']} ≤ 水位 < ${row['失效水位']}`,
    esc(row['釉料配方']), esc(row['更正编号']), esc(row['替代更正'] || '—')]);
  const orderRows = orders.map(row=>[esc(row['batch_seq']), esc(row['request_id']), esc(row['note'] || '—'),
    esc(row['author']), esc(date(row['created_at']))]);
  const editable = state.session.role==='operator';
  const form = editable ? `<form id="correction-form" data-case="${esc(selected?.id ?? '')}" class="correction-form">
    <h2>提交更正单</h2>
    <p class="note">生效水位为读取水位区间，含起点不含终点；失效水位留空表示直到已提交水位。区间与已生效更正交叠时，须在「替代更正」中逐条写出被替代的更正编号，否则整单退回。</p>
    <label>提交单号<input name="request_id" required minlength="8" maxlength="64" placeholder="例如 QA-2026-1118-01" /></label>
    <label>更正说明<input name="note" maxlength="500" placeholder="质检发现旧釉方抄错" /></label>
    <label>更正行（JSON 数组，可一次改多支釉）<textarea name="lines" rows="10" spellcheck="false" required>${esc(JSON.stringify([{"formula_id":"GL-Q01","effective_from":30,"effective_to":61,"recipe":"青岚釉｜长石 60 g、石英 30 g、高岭土 10 g（已补正）","supersedes":[]}], null, 2))}</textarea></label>
    <div class="actions"><button>提交更正单</button></div></form>` : '';
  return panel('生效更正', coverageRows.length ? records(['配方编号', '生效区间', '补正后配方', '更正编号', '替代更正'], coverageRows)
      : `<p class="empty">当前生效更正批次为 ${esc(batch)}，尚无补正。</p>`)
    + (orderRows.length ? panel('更正单来路', records(['批次', '提交单号', '说明', '经办', '时间'], orderRows)) : '')
    + form;
}

export function bindCorrections(state, api, update, fail) {
  const form = document.getElementById('correction-form');
  if (!form) return;
  const caseId = Number(form.dataset.case) || state.cases[0]?.id;
  submit('correction-form', async data=>{
    let lines;
    try { lines = JSON.parse(data.get('lines')); }
    catch { throw new Error('更正行须为合法的 JSON 数组。'); }
    await api(`/cases/${caseId}/corrections`, {method:'POST', data:{
      request_id:data.get('request_id'), note:data.get('note') || '', lines}});
    await update();
  }, fail);
}
