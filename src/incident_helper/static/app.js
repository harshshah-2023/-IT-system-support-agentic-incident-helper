// No-build single page app. Everything server-provided goes through esc() before reaching the DOM.
const view = document.getElementById('view');
let status = {};

const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const api = async (path, options = {}) => {
  const res = await fetch(`/api${path}`, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `${res.status} ${res.statusText}`);
  return res.json();
};
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body ?? {}) });

function markdown(src) {
  const inline = t => esc(t).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>');
  const out = []; let list = null, table = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } if (table) { out.push('</table></div>'); table = null; } };
  for (const line of String(src ?? '').split('\n')) {
    const t = line.trim(); let m;
    if (t.startsWith('|')) {
      const cells = t.replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      if (cells.every(c => /^:?-+:?$/.test(c))) continue;
      if (!table) { close(); out.push('<div class="table-wrap"><table>'); table = true; out.push(`<tr>${cells.map(c => `<th>${inline(c)}</th>`).join('')}</tr>`); }
      else out.push(`<tr>${cells.map(c => `<td>${inline(c)}</td>`).join('')}</tr>`);
    } else if ((m = t.match(/^(#{1,4})\s+(.*)/))) { close(); out.push(`<h3>${inline(m[2])}</h3>`); }
    else if ((m = t.match(/^[-*]\s+(.*)/))) { if (list !== 'ul') { close(); out.push('<ul>'); list = 'ul'; } out.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = t.match(/^\d+\.\s+(.*)/))) { if (list !== 'ol') { close(); out.push('<ol>'); list = 'ol'; } out.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = t.match(/^>\s?(.*)/))) { close(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); }
    else if (!t) close();
    else { close(); out.push(`<p>${inline(t)}</p>`); }
  }
  close();
  return out.join('');
}

const sevTag = s => `<span class="tag ${s === 'SEV1' ? 'critical' : s === 'SEV2' ? 'warning' : ''}">${esc(s)}</span>`;
const statusTag = s => `<span class="tag ${s === 'awaiting_approval' ? 'warning' : s === 'mitigated' || s === 'resolved' ? 'good' : ''}">${esc(String(s).replace('_', ' '))}</span>`;
const stateTag = s => `<span class="tag ${s === 'healthy' ? 'good' : s === 'degraded' ? 'critical' : ''}">${esc(s)}</span>`;

async function streamPost(path, body, onEvent) {
  const res = await fetch(`/api${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value.replace(/\r\n/g, '\n');
    let cut;
    while ((cut = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, cut); buffer = buffer.slice(cut + 2);
      const kind = frame.match(/^event: (.*)$/m)?.[1];
      const data = [...frame.matchAll(/^data: (.*)$/gm)].map(m => m[1]).join('\n');
      if (kind && data) onEvent(kind, JSON.parse(data));
    }
  }
}

// ------------------------------------------------------------------ incidents
async function incidentsPage() {
  const [incidents, estate, inbox] = await Promise.all([api('/incidents'), api('/estate'), api('/inbox')]);
  view.innerHTML = `
    <div class="page-head"><div><h1>Incidents</h1><p>Alerts and complaints triaged into incidents, each with its own investigation thread.</p></div>
      <div class="row"><button id="reset" title="Restore the simulated estate">Reset simulation</button>
      <button id="triage" class="primary" ${incidents.length ? '' : ''}>Triage ${inbox.length} messages</button></div></div>
    <div id="msg"></div>
    <div class="stack">
      <section class="card"><h2>Open incidents</h2>${incidents.length ? `<div class="table-wrap"><table>
        <tr><th>ID</th><th>Severity</th><th>Status</th><th>Summary</th><th>Service</th><th>Sentiment</th></tr>
        ${incidents.map(i => `<tr class="link" data-id="${esc(i.id)}"><td><a href="#/incident/${esc(i.id)}">${esc(i.id)}</a></td><td>${sevTag(i.severity)}</td><td>${statusTag(i.status)}</td><td>${esc(i.summary || i.title)}</td><td><code>${esc(i.service)}</code></td><td>${esc(i.sentiment)}</td></tr>`).join('')}
      </table></div>` : `<p class="muted">Nothing triaged yet. Triage turns the ${inbox.length} raw messages below into structured incidents using a batched LLM call.</p>`}</section>
      <div class="cols">
        <section class="card"><h2>Service health</h2><div class="health">${estate.map(s => `<div class="svc"><strong>${esc(s.service)}</strong>${stateTag(s.state)}<br><small>v${esc(s.version)} · p95 ${esc(s.p95_ms)} ms · ${esc(s.error_rate_pct)}% errors</small></div>`).join('')}</div></section>
        <section class="card"><h2>Intake</h2><form id="new" class="stack">
          <label>Title<input name="title" required maxlength="120"></label>
          <div class="row"><label class="grow">Severity<select name="severity"><option>SEV1</option><option>SEV2</option><option selected>SEV3</option><option>SEV4</option></select></label>
          <label class="grow">Service<select name="service">${estate.map(s => `<option>${esc(s.service)}</option>`).join('')}</select></label></div>
          <button>Open incident</button></form>
          <h3>Raw inbox</h3>${inbox.map(m => `<details class="cite"><summary>${esc(m.channel)}: ${esc(m.subject)}</summary><pre>${esc(m.body)}</pre></details>`).join('')}
        </section>
      </div>
    </div>`;
  document.querySelectorAll('tr.link').forEach(r => r.onclick = () => { location.hash = `#/incident/${r.dataset.id}`; });
  document.getElementById('reset').onclick = async () => { await post('/reset'); route(); };
  document.getElementById('new').onsubmit = async e => {
    e.preventDefault();
    const created = await post('/incidents', Object.fromEntries(new FormData(e.target)));
    location.hash = `#/incident/${created.id}`;
  };
  const button = document.getElementById('triage');
  button.onclick = async () => {
    button.disabled = true; button.textContent = 'Triaging…';
    try { await post('/triage'); route(); }
    catch (err) { document.getElementById('msg').innerHTML = `<p class="error">${esc(err.message)}</p>`; button.disabled = false; }
  };
}

// ------------------------------------------------------------------ incident workspace
function messageHtml(m) {
  if (m.role === 'human') return `<div class="msg human">${esc(m.text).replace(/^\[system\].*/, '')}</div>`;
  if (m.role === 'ai' && m.text) return `<div class="msg ai md">${markdown(m.text)}</div>`;
  return '';
}

function timelineItem(kind, title, detail) {
  const body = detail ? `<details><summary>${esc(title)}</summary><pre>${esc(detail)}</pre></details>` : esc(title);
  return `<li class="${kind}">${body}</li>`;
}

async function incidentPage(id) {
  const data = await api(`/incidents/${id}`);
  const i = data.incident;
  view.innerHTML = `
    <div class="page-head"><div><div class="row">${sevTag(i.severity)}<span id="st">${statusTag(i.status)}</span><small>${esc(i.id)}</small></div>
      <h1 style="margin-top:6px">${esc(i.title)}</h1><p>${esc(i.summary)}</p></div>
      <a href="#/incidents">All incidents</a></div>
    <div id="approval"></div>
    <div class="cols">
      <div class="stack">
        <section class="card"><div class="chat" id="chat" aria-live="polite"></div>
          <form id="ask" class="row" style="margin-top:10px"><input class="grow" name="q" aria-label="Message" required
            placeholder="Why are payments slow after last night's release?"><button class="primary">Send</button></form></section>
        <section class="card"><div class="row" style="justify-content:space-between"><h2 style="margin:0">Root cause report</h2>
          <div class="row"><select id="fault" aria-label="Fault injection" style="width:auto"><option value="">No fault</option><option value="root_cause">Fail Root Cause worker</option><option value="timeline">Fail Timeline worker</option></select>
          <button id="rca">Run investigation</button></div></div><div id="rcaBody"><p class="muted" style="margin-top:8px">A planner splits the work across Timeline, Root Cause and Prevention investigators, who write files that a synthesizer combines.</p></div></section>
      </div>
      <div class="stack">
        <section class="card"><h2>Live activity</h2><ul class="timeline" id="tl"></ul></section>
        <section class="card"><h2>First steps</h2>${(i.first_steps || []).length ? `<ol style="margin:0;padding-left:18px">${i.first_steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>` : '<p class="muted">None recorded.</p>'}</section>
        <section class="card" id="sec"></section>
      </div>
    </div>`;
  const chat = document.getElementById('chat');
  const timeline = document.getElementById('tl');
  chat.innerHTML = data.messages.map(messageHtml).join('') || '<p class="muted">Ask a question to start the investigation. Each incident keeps its own memory.</p>';
  chat.scrollTop = chat.scrollHeight;
  renderSecurity(data); renderApproval(id, data.pending);

  const run = async (path, body, label) => {
    const form = document.getElementById('ask');
    form.querySelector('button').disabled = true;
    const live = document.createElement('div');
    live.className = 'msg ai md'; let text = '';
    chat.append(live);
    timeline.insertAdjacentHTML('beforeend', timelineItem('', label));
    try {
      await streamPost(path, body, (kind, d) => {
        if (kind === 'token') { text = d.text; live.innerHTML = markdown(text); chat.scrollTop = chat.scrollHeight; }
        else if (kind === 'node') { text = ''; timeline.insertAdjacentHTML('beforeend', timelineItem(d.name.includes('guard') ? 'warn' : '', d.name)); }
        else if (kind === 'tool_start') timeline.insertAdjacentHTML('beforeend', timelineItem('tool', `Tool: ${d.name}`, d.args));
        else if (kind === 'tool_end') timeline.insertAdjacentHTML('beforeend', timelineItem('tool', `Result: ${d.name}`, d.output));
        else if (kind === 'error') timeline.insertAdjacentHTML('beforeend', timelineItem('warn', d.message));
        timeline.scrollTop = timeline.scrollHeight;
      });
    } catch (err) { timeline.insertAdjacentHTML('beforeend', timelineItem('warn', err.message)); }
    const fresh = await api(`/incidents/${id}`);
    chat.innerHTML = fresh.messages.map(messageHtml).join('');
    chat.scrollTop = chat.scrollHeight;
    document.getElementById('st').innerHTML = statusTag(fresh.incident.status);
    renderSecurity(fresh); renderApproval(id, fresh.pending, run);
    form.querySelector('button').disabled = false;
  };
  renderApproval(id, data.pending, run);
  document.getElementById('ask').onsubmit = e => {
    e.preventDefault();
    const q = e.target.q.value; e.target.reset();
    chat.insertAdjacentHTML('beforeend', `<div class="msg human">${esc(q)}</div>`);
    run(`/incidents/${id}/chat`, { message: q }, 'Question sent');
  };
  document.getElementById('rca').onclick = () => startRca(id);
}

function renderSecurity(data) {
  const events = data.security_events || [];
  const approvals = data.approvals || [];
  document.getElementById('sec').innerHTML = `<h2>Security and approvals</h2>
    ${events.length ? `<ul class="timeline">${events.slice(-8).map(e => `<li class="warn">${esc(e.stage)}: ${esc(e.type)} ${esc((e.kinds || []).join(', '))}</li>`).join('')}</ul>` : '<p class="muted">No guardrail events.</p>'}
    ${approvals.map(a => `<p style="margin-top:8px"><span class="tag ${a.outcome === 'rejected' || a.outcome === 'unauthorised' ? 'critical' : 'good'}">${esc(a.outcome)}</span> ${esc(a.approver)}: <code>${esc(JSON.stringify(a.tool_calls?.[0]?.args))}</code> ${esc(a.reason)}</p>`).join('')}`;
}

function renderApproval(id, pending, run) {
  const host = document.getElementById('approval');
  if (!pending) { host.innerHTML = ''; return; }
  const call = pending.tool_calls[0];
  host.innerHTML = `<section class="card approval stack" style="margin-bottom:14px">
    <div><h2>Rollback waiting for approval</h2><p>The graph is paused at a checkpoint. Nothing has changed, and it will stay paused until an on-call engineer decides, even across a restart.</p></div>
    <form id="decide" class="stack">
      <div class="row">
        <label class="grow">Service<input name="service" value="${esc(call.args.service)}" readonly></label>
        <label class="grow">Version (edit to choose another)<input name="version" value="${esc(call.args.version)}" required></label>
        <label class="grow">On-call engineer<select name="approver">${status.on_call.map(n => `<option>${esc(n)}</option>`).join('')}</select></label>
      </div>
      <label>Reason (needed to reject)<input name="reason"></label>
      <div class="row"><button class="primary" value="approve">Approve</button><button value="reject" class="danger">Reject</button></div>
    </form></section>`;
  const form = document.getElementById('decide');
  form.onsubmit = e => {
    e.preventDefault();
    const clicked = e.submitter.value; const f = new FormData(form);
    const edited = f.get('version') !== call.args.version;
    const body = clicked === 'reject'
      ? { action: 'reject', approver: f.get('approver'), reason: f.get('reason') || 'Rejected by on-call engineer' }
      : edited ? { action: 'edit', approver: f.get('approver'), args: { version: f.get('version') } }
        : { action: 'approve', approver: f.get('approver') };
    host.innerHTML = '';
    run(`/incidents/${id}/decision`, body, `Decision: ${body.action}`);
  };
}

async function startRca(id) {
  const body = document.getElementById('rcaBody');
  const button = document.getElementById('rca');
  button.disabled = true;
  const { run_id } = await post(`/incidents/${id}/rca`, { fault: document.getElementById('fault').value || null });
  const label = { timeline: 'Timeline Investigator', root_cause: 'Root Cause Investigator', prevention: 'Prevention Investigator' };
  const poll = async () => {
    const run = await api(`/rca/${run_id}`);
    if (location.hash !== `#/incident/${id}`) return;
    const plan = run.events.find(e => e.kind === 'plan');
    const started = run.events.filter(e => e.kind === 'worker_start').map(e => e.id);
    const failed = run.events.filter(e => e.kind === 'worker_failed').map(e => e.id);
    body.innerHTML = `<div class="steps" style="margin-top:10px">${Object.entries(label).map(([key, name]) => `<div class="step"><strong>${name}</strong><br>${run.drafts?.[key] ? '<span class="tag good">File saved</span>' : failed.includes(key) ? '<span class="tag critical">Failed</span>' : started.includes(key) ? '<span class="tag warning">Working</span>' : '<span class="tag">Waiting</span>'}<br><small>${esc(plan?.subtasks.find(t => t.id === key)?.objective)}</small></div>`).join('')}</div>
      <ul class="timeline" style="margin-top:10px">${run.events.filter(e => ['replan', 'worker_failed', 'gate'].includes(e.kind) && (e.kind !== 'gate' || e.errors.length)).map(e => `<li class="warn">${esc(e.kind)}: ${esc(e.reasoning || e.error || e.errors?.join('; '))}</li>`).join('')}</ul>
      ${run.report ? `<p style="margin-top:10px"><span class="tag ${run.status === 'complete' ? 'good' : 'warning'}">${esc(run.status)}</span> <a href="/api/rca/${run_id}/report.md">Download Markdown</a></p><div class="md">${markdown(run.report)}</div>` : ''}
      ${run.status === 'failed' ? `<p class="error">${esc((run.errors || []).join('; '))}</p>` : ''}`;
    if (run.status === 'running') setTimeout(poll, 1500); else button.disabled = false;
  };
  poll();
}

// ------------------------------------------------------------------ retrieval
const SAMPLE_QUESTIONS = ['If the main database fails over, which services need a restart and in what order?', 'How do we switch to the backup database?', 'Which laptop should our team buy?'];

function ragColumn(title, r) {
  return `<section class="card"><h2>${title}</h2>
    <div class="row" style="margin-bottom:8px"><span class="tag ${r.grounded ? 'good' : 'warning'}">${r.grounded ? 'Grounded' : 'Not grounded'}</span>
    <span class="tag">${r.stats.latency_ms.toFixed(0)} ms</span><span class="tag">${r.stats.input_tokens + r.stats.output_tokens} tokens</span><span class="tag">${r.stats.llm_calls} LLM calls</span>
    ${r.similarity_score != null ? `<span class="tag">similarity ${r.similarity_score}</span>` : ''}<span class="tag">confidence ${r.confidence}</span></div>
    <div class="md">${markdown(r.answer)}</div>
    ${r.traversal.length ? `<h3>Traversal</h3>${r.traversal.map(t => `<div class="trace">${esc(t)}</div>`).join('')}` : ''}
    ${r.citations.length ? `<h3>Citations</h3>${r.citations.map(c => `<div class="cite"><strong>p.${esc(c.page)}</strong> ${esc(c.section)}${c.similarity != null ? ` <small>(${esc(c.similarity)})</small>` : ''}</div>`).join('')}` : ''}
    ${r.generation_called ? '' : '<p class="notice" style="margin-top:8px">The generation prompt was not called.</p>'}</section>`;
}

async function retrievalPage() {
  const stats = await api('/rag/stats');
  view.innerHTML = `<div class="page-head"><div><h1>Runbook search</h1><p>The same document searched by vector similarity and by reasoning over its table of contents.</p></div></div>
    <section class="card stack"><form id="q" class="row"><input class="grow" name="q" aria-label="Question" required value="${esc(SAMPLE_QUESTIONS[0])}"><button class="primary">Compare</button></form>
    <div class="row">${SAMPLE_QUESTIONS.map(q => `<button type="button" data-q="${esc(q)}">${esc(q.slice(0, 44))}…</button>`).join('')}</div></section>
    <div id="out" class="two" style="margin-top:14px"></div>
    ${stats.table ? `<section class="card md" style="margin-top:14px"><h2>Measured comparison</h2>${markdown(stats.table)}</section>` : ''}`;
  const form = document.getElementById('q'); const out = document.getElementById('out');
  const go = async () => {
    out.innerHTML = '<p class="muted">Searching both ways…</p>';
    try { const r = await post('/rag/compare', { question: form.q.value }); out.innerHTML = ragColumn('Vector search', r.vector) + ragColumn('Contents navigation (vectorless)', r.vectorless); }
    catch (err) { out.innerHTML = `<p class="error">${esc(err.message)}</p>`; }
  };
  form.onsubmit = e => { e.preventDefault(); go(); };
  document.querySelectorAll('[data-q]').forEach(b => b.onclick = () => { form.q.value = b.dataset.q; go(); });
}

// ------------------------------------------------------------------ evaluation
async function evalsPage() {
  const runs = await api('/evals');
  const clean = runs.filter(r => !r.tampered);
  const tampered = runs.filter(r => r.tampered);
  const metrics = [...new Set(clean.flatMap(r => Object.keys(r.means)))];
  const colour = { vector: 'var(--series-1)', vectorless: 'var(--series-2)' };
  const dest = runs[0]?.destination;
  view.innerHTML = `<div class="page-head"><div><h1>Evaluation</h1><p>LLM judges grading both retrieval methods against the six-question answer key. Run with <code>incident task6</code>.</p></div></div>
    ${runs.length ? '' : '<p class="notice">No results yet. Run <code>incident task6</code>.</p>'}
    ${dest && !dest.startsWith('langsmith') ? `<p class="notice" style="margin-bottom:14px">These results ran locally through <code>evaluate()</code> and were not uploaded, because no LangSmith key is configured.</p>` : ''}
    <div class="two">
      <section class="card"><h2>Mean score by metric</h2>
        <div class="legend">${clean.map(r => `<span><i class="swatch" style="background:${colour[r.method]}"></i>${esc(r.method)}</span>`).join('')}</div>
        ${metrics.map(m => `<div class="bar-group"><span class="bar-label">${esc(m.replace('_', ' '))}</span>${clean.map(r => `<div class="bar-line" title="${esc(r.method)}: ${r.means[m]}"><div class="bar-track"><div class="bar" style="width:${(r.means[m] ?? 0) * 100}%;background:${colour[r.method]}"></div></div><small>${(r.means[m] ?? 0).toFixed(2)}</small></div>`).join('')}</div>`).join('')}
      </section>
      <section class="card"><h2>Hallucination test</h2>
        <p class="muted">A fabricated sentence is appended to each answer. The groundedness judge should catch it.</p>
        <div class="table-wrap"><table><tr><th>Method</th><th class="num">Normal</th><th class="num">Tampered</th></tr>
        ${tampered.map(t => `<tr><td>${esc(t.method)}</td><td class="num">${(clean.find(c => c.method === t.method)?.means.groundedness ?? 0).toFixed(2)}</td><td class="num">${(t.means.groundedness ?? 0).toFixed(2)}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">Not run yet.</td></tr>'}
        </table></div></section>
    </div>
    ${clean.map(r => `<section class="card" style="margin-top:14px"><h2>${esc(r.experiment)}</h2><div class="table-wrap"><table>
      <tr><th>Question</th><th>Kind</th>${metrics.map(m => `<th class="num">${esc(m.replace('_', ' '))}</th>`).join('')}</tr>
      ${r.rows.map(row => `<tr><td>${esc(row.question)}</td><td>${esc(row.kind)}</td>${metrics.map(m => `<td class="num" title="${esc(row.scores[m]?.reasoning)}">${row.scores[m]?.score?.toFixed(2) ?? ''}</td>`).join('')}</tr>`).join('')}
    </table></div></section>`).join('')}`;
}

// ------------------------------------------------------------------ gateway
async function gatewayPage() {
  const { summary, recent } = await api('/gateway');
  const hitRate = summary.calls ? Math.round(100 * summary.cache_hits / summary.calls) : 0;
  view.innerHTML = `<div class="page-head"><div><h1>Gateway</h1><p>Every model call passes through routing, fallback, caching and cost tracking.</p></div></div>
    <div class="tiles">
      <div class="tile"><b>${summary.calls}</b><span>Model requests</span></div>
      <div class="tile"><b>$${summary.total_cost_usd.toFixed(4)}</b><span>Estimated cost</span></div>
      <div class="tile"><b>${hitRate}%</b><span>Cache hit rate</span></div>
      <div class="tile"><b>${summary.fallbacks}</b><span>Fallbacks used</span></div>
    </div>
    <section class="card stack" style="margin-top:14px"><h2 style="margin:0">Try the router</h2>
      <form id="g" class="row"><input class="grow" name="q" aria-label="Prompt" required value="Is the login service up?"><button class="primary">Send</button></form><div id="gout"></div></section>
    <section class="card" style="margin-top:14px"><h2>By model</h2><div class="table-wrap"><table><tr><th>Model</th><th class="num">Calls</th><th class="num">Input tokens</th><th class="num">Output tokens</th><th class="num">Avg latency (ms)</th><th class="num">Cost (USD)</th></tr>
      ${Object.entries(summary.by_model).map(([k, v]) => `<tr><td><code>${esc(k)}</code></td><td class="num">${v.calls}</td><td class="num">${v.input_tokens}</td><td class="num">${v.output_tokens}</td><td class="num">${v.avg_latency_ms}</td><td class="num">${v.cost_usd.toFixed(5)}</td></tr>`).join('')}</table></div></section>
    <section class="card" style="margin-top:14px"><h2>Recent calls</h2><div class="table-wrap"><table><tr><th>Purpose</th><th>Model</th><th>Route</th><th>Cache</th><th>Fallback</th><th class="num">Latency (ms)</th><th class="num">Tokens</th><th class="num">Cost</th></tr>
      ${recent.map(r => `<tr><td>${esc(r.purpose)}</td><td><code>${esc(r.model_used)}</code></td><td>${esc(r.route)}</td><td>${r.cache_hit ? `<span class="tag good">${esc(r.cache_kind)}</span>` : ''}</td><td>${r.fallback_triggered ? '<span class="tag warning">used</span>' : ''}</td><td class="num">${r.latency_ms}</td><td class="num">${r.input_tokens + r.output_tokens}</td><td class="num">${r.estimated_cost_usd ?? 'unknown'}</td></tr>`).join('')}</table></div></section>`;
  const form = document.getElementById('g');
  form.onsubmit = async e => {
    e.preventDefault();
    const out = document.getElementById('gout'); out.innerHTML = '<p class="muted">Sending…</p>';
    try {
      const r = await post('/gateway/ask', { question: form.q.value });
      out.innerHTML = `<div class="md">${markdown(r.content)}</div><pre>${esc(JSON.stringify(r.telemetry, null, 1))}</pre>`;
    } catch (err) { out.innerHTML = `<p class="error">${esc(err.message)}</p>`; }
  };
}

async function auditPage() {
  const rows = await api('/audit');
  view.innerHTML = `<div class="page-head"><div><h1>Audit log</h1><p>Every approval decision and write to the simulated estate.</p></div></div>
    <section class="card"><div class="table-wrap"><table><tr><th>Time</th><th>Action</th><th>Detail</th></tr>
    ${rows.map(r => `<tr><td>${esc(r.at)}</td><td>${esc(r.action)}</td><td>${esc(r.detail)}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">No actions recorded.</td></tr>'}</table></div></section>`;
}

const routes = { incidents: incidentsPage, retrieval: retrievalPage, evals: evalsPage, gateway: gatewayPage, audit: auditPage };

async function route() {
  const [, page, arg] = location.hash.split('/');
  const name = page || 'incidents';
  document.querySelectorAll('[data-nav]').forEach(a => {
    if (a.dataset.nav === (name === 'incident' ? 'incidents' : name)) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  try { await (name === 'incident' ? incidentPage(arg) : (routes[name] || incidentsPage)()); }
  catch (err) { view.innerHTML = `<p class="error">${esc(err.message)}</p>`; }
}

(async () => {
  status = await api('/status');
  document.getElementById('runtime').innerHTML = `<div><span class="dot ${status.llm_configured ? 'on' : 'off'}"></span>LLM ${status.llm_configured ? 'connected' : 'not configured'}</div>
    <div><span class="dot ${status.langsmith_configured ? 'on' : 'off'}"></span>LangSmith ${status.langsmith_configured ? 'connected' : 'not configured'}</div>
    <div style="margin-top:6px">${esc(status.document)}</div>`;
  addEventListener('hashchange', route);
  route();
})();
