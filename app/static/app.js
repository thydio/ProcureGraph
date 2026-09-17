/* ProcureGraph console - view logic and hand-rolled SVG charts */

const S = { filters: null, lastSearch: null, view: 'overview' };
const DET_COLOR = { S1: 'var(--s1)', S2: 'var(--s2)', S3: 'var(--s3)', S4: 'var(--s4)', S5: 'var(--s5)' };
const DET_FULL = {
  S1: 'Relationship graph and linked vendors',
  S2: 'Cover bidding',
  S3: 'Shell company',
  S4: 'Price and contract value',
  S5: 'Award concentration, rotation and competition health',
};

const api = (p) => fetch(p).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function inr(x) {
  if (x === null || x === undefined || x === '') return 'n/a';
  x = Number(x);
  if (x >= 1e7) return '₹' + (x / 1e7).toFixed(2) + ' cr';
  if (x >= 1e5) return '₹' + (x / 1e5).toFixed(2) + ' lakh';
  return '₹' + x.toLocaleString('en-IN', { maximumFractionDigits: 0 });
}
const pct = (x, n = 0) => (100 * Number(x)).toFixed(n) + '%';
const tierCls = (t) => ({ 'Tier 1': 't1', 'Tier 2': 't2', 'Tier 3': 't3', 'Tier 4': 't4', 'Data gap': 'gap' }[t] || 't4');
const tierColor = (t) => ({ 'Tier 1': 'var(--t1)', 'Tier 2': 'var(--t2)', 'Tier 3': 'var(--t3)', 'Tier 4': 'var(--t4)', 'Data gap': 'var(--gap)' }[t] || 'var(--t4)');
const badge = (t) => `<span class="badge ${tierCls(t)}"><span class="dot"></span>${esc(t)}</span>`;
const meter = (v, color) => `<div class="meter"><i style="width:${Math.max(2, v * 100).toFixed(1)}%;background:${color}"></i></div>`;
const dateStr = (d) => d ? String(d).slice(0, 10) : '';

/* ---------------- navigation ---------------- */

function go(view) {
  S.view = view;
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  $('view-' + view).classList.add('active');
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (view === 'search' && !S.lastSearch) runSearch();
}

function goBack() { go(S.lastSearch ? 'search' : 'overview'); }

function openTid() {
  const v = $('tidbox').value.trim();
  if (v) openTender(v);
}

/* ---------------- overview ---------------- */

async function loadOverview() {
  const o = await api('/api/overview');
  const order = ['Tier 1', 'Tier 2', 'Tier 3', 'Tier 4', 'Data gap'];
  const byTier = Object.fromEntries(o.tiers.map(t => [t.tier, t]));
  const actions = {
    'Tier 1': 'Investigate now', 'Tier 2': 'Queue for review', 'Tier 3': 'Monitor',
    'Tier 4': 'No action', 'Data gap': 'Fix the data first',
  };
  $('tiles').innerHTML = order.map(t => {
    const r = byTier[t] || { n: 0, value: 0 };
    return `<div class="tile" data-tier="${t}" role="button" tabindex="0"
              onclick="filterTier('${t}')" onkeydown="if(event.key==='Enter')filterTier('${t}')">
      <div class="k">${t}</div>
      <div class="v">${r.n}</div>
      <div class="s">${actions[t]} &middot; ${inr(r.value)} at risk</div>
    </div>`;
  }).join('');

  $('coverage').innerHTML = o.coverage.map(c => {
    const tot = c.scored + c.not_applicable + c.insufficient;
    const w = (n) => (100 * n / tot).toFixed(1) + '%';
    return `<div style="margin-bottom:12px">
      <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px">
        <span><b style="color:${DET_COLOR[c.detector]}">${c.detector}</b> ${esc(c.detector_name)}</span>
        <span class="mono muted">${c.scored} scored &middot; mean c ${c.mean_confidence}</span>
      </div>
      <div style="display:flex;height:7px;border-radius:4px;overflow:hidden;background:var(--panel-2)">
        <div style="width:${w(c.scored)};background:${DET_COLOR[c.detector]}" title="scored"></div>
        <div style="width:${w(c.not_applicable)};background:var(--border)" title="not applicable"></div>
        <div style="width:${w(c.insufficient)};background:var(--gap);opacity:.6" title="insufficient data"></div>
      </div>
    </div>`;
  }).join('') + `<div class="tiny dim" style="margin-top:10px">
      Coloured = scored &middot; grey = test does not apply &middot; violet = data missing</div>`;

  const maxFlag = Math.max(...o.by_department.map(d => d.flagged), 1);
  $('bydept').innerHTML = o.by_department.map(d => `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:9px">
      <div style="width:190px;font-size:13px" title="${esc(d.department)}">${esc(d.department)}</div>
      <div style="flex:1">${meter(d.flagged / maxFlag, 'var(--t2)')}</div>
      <div class="mono tiny muted" style="width:96px;text-align:right">${d.flagged} / ${d.n} tenders</div>
    </div>`).join('');

  $('topqueue').innerHTML = resultTable(o.top, true);
}

function filterTier(t) {
  go('search');
  $('f-tier').value = t;
  runSearch();
}

/* ---------------- search ---------------- */

async function loadFilters() {
  const f = await api('/api/filters');
  S.filters = f;
  const govs = [...new Set(f.governments.map(g => g.government_name))];
  $('f-gov').innerHTML = '<option value="">All governments</option>' +
    f.governments.map(g => `<option value="${esc(g.government_name)}">${esc(g.government_name)} (${esc(g.government_level)})</option>`).join('');
  $('f-cat').innerHTML = '<option value="">Any category</option>' +
    f.categories.map(c => `<option value="${esc(c.category_code)}">${esc(c.category_label)} (${c.n})</option>`).join('');
  $('f-method').innerHTML = '<option value="">Any method</option>' +
    f.methods.map(m => `<option value="${esc(m)}">${esc(m.replace(/_/g, ' '))}</option>`).join('');
  onGovChange(true);
}

function onGovChange(skip) {
  const gov = $('f-gov').value;
  const depts = [...new Set(S.filters.departments
    .filter(d => !gov || d.government_name === gov).map(d => d.department))].sort();
  $('f-dept').innerHTML = '<option value="">All departments</option>' +
    depts.map(d => `<option value="${esc(d)}">${esc(d)}</option>`).join('');
  onDeptChange(skip);
}

function onDeptChange(skip) {
  const gov = $('f-gov').value, dept = $('f-dept').value;
  const bs = S.filters.buyers.filter(b => (!gov || b.government_name === gov) && (!dept || b.department === dept));
  $('f-buyer').innerHTML = '<option value="">All offices</option>' +
    bs.map(b => `<option value="${esc(b.buyer_id)}">${esc(b.buyer_name)}</option>`).join('');
  if (!skip) runSearch();
}

function resetFilters() {
  ['f-gov', 'f-dept', 'f-buyer', 'f-cat', 'f-method', 'f-tier', 'f-text'].forEach(i => $(i).value = '');
  $('f-minp').value = 0; $('minp-val').textContent = '0.00'; $('f-sort').value = 'priority';
  onGovChange(true); runSearch();
}

async function runSearch() {
  const p = new URLSearchParams({
    government: $('f-gov').value, department: $('f-dept').value, buyer_id: $('f-buyer').value,
    category: $('f-cat').value, method: $('f-method').value, tier: $('f-tier').value,
    text: $('f-text').value, min_priority: $('f-minp').value, sort: $('f-sort').value, limit: 150,
  });
  $('result-count').innerHTML = '<span class="spinner"></span> searching…';
  const r = await api('/api/search?' + p);
  S.lastSearch = p.toString();
  $('result-count').innerHTML = r.total_matching
    ? `<b>${r.total_matching}</b> tenders match. Showing ${r.count}, ranked by ${$('f-sort').selectedOptions[0].text.toLowerCase()}.`
    : 'No tenders match these filters.';
  $('results').innerHTML = r.results.length
    ? resultTable(r.results)
    : '<div class="empty">Nothing matches. Try widening the filters.</div>';
}

function resultTable(rows, compact) {
  return `<table>
    <thead><tr>
      <th>Tender</th><th>What / who</th>${compact ? '' : '<th>Buying office</th>'}
      <th class="right">Value</th><th class="right">Bids</th>
      <th>Priority</th><th>Tier</th>
    </tr></thead>
    <tbody>${rows.map(r => `
      <tr onclick="openTender('${r.tender_id}')" tabindex="0"
          onkeydown="if(event.key==='Enter')openTender('${r.tender_id}')">
        <td><span class="tid">${r.tender_id}</span><div class="tiny dim">${dateStr(r.publish_date)}</div></td>
        <td><div>${esc(r.category_label)}</div>
            <div class="tiny muted">won by ${esc(r.awarded_vendor_name)}</div></td>
        ${compact ? '' : `<td><div class="small">${esc(r.buyer_name)}</div>
            <div class="tiny dim">${esc(r.department)}</div></td>`}
        <td class="right num">${inr(r.awarded_value)}</td>
        <td class="right num">${r.n_bids || '—'}</td>
        <td style="min-width:110px">
          <div class="num tiny" style="margin-bottom:3px">${Number(r.priority_p).toFixed(2)}</div>
          ${meter(r.priority_p, tierColor(r.tier))}
        </td>
        <td>${badge(r.tier)}</td>
      </tr>`).join('')}</tbody></table>`;
}

/* ---------------- dossier ---------------- */

async function openTender(tid) {
  go('tender');
  $('dossier').innerHTML = '<div class="empty"><span class="spinner"></span> loading dossier…</div>';
  let d;
  try {
    d = await api('/api/tender/' + encodeURIComponent(tid));
  } catch (e) {
    $('dossier').innerHTML = `<div class="empty">No tender found with id <b class="mono">${esc(tid)}</b>.
      <br><br><button class="btn" onclick="go('search')">Browse instead</button></div>`;
    return;
  }
  $('dossier').innerHTML = renderDossier(d);
}

function renderDossier(d) {
  const a = d.analysis;
  const dets = d.detectors.slice().sort((x, y) => y.contribution_pct - x.contribution_pct);
  const scored = dets.filter(x => x.x > 0.0001);

  return `
  <div class="dossier-head">
    <div class="main">
      <h1>${a.tender_id}</h1>
      <div class="muted">${esc(a.title)}</div>
      <p class="headline">${esc(a.headline)}</p>
      <div class="kv-row">
        <div class="kv"><div class="k">Buying office</div><div class="v">${esc(a.buyer_name)}</div></div>
        <div class="kv"><div class="k">Department</div><div class="v">${esc(a.department)}</div></div>
        <div class="kv"><div class="k">Government</div><div class="v">${esc(a.government_name)}</div>
          <div class="tiny dim">${esc(a.government_level)}</div></div>
        <div class="kv"><div class="k">Method</div><div class="v">${esc(a.procurement_method.replace(/_/g, ' '))}</div></div>
      </div>
      <div class="kv-row">
        <div class="kv"><div class="k">Estimate</div><div class="v num">${a.estimate_value ? inr(a.estimate_value) : 'not published'}</div></div>
        <div class="kv"><div class="k">Awarded</div><div class="v num">${inr(a.awarded_value)}</div></div>
        <div class="kv"><div class="k">Final contract</div><div class="v num">${inr(a.final_contract_value)}</div></div>
        <div class="kv"><div class="k">Winner</div><div class="v">${esc(a.awarded_vendor_name)}</div></div>
        <div class="kv"><div class="k">Published</div><div class="v num">${dateStr(a.publish_date)}</div></div>
      </div>
    </div>
    <div class="scorebox">
      <div>
        <div class="lbl">Priority P</div>
        <div class="big" style="color:${tierColor(a.tier)}">${Number(a.priority_p).toFixed(2)}</div>
      </div>
      <div class="action">
        ${badge(a.tier)}
        <div class="small muted" style="margin-top:8px">${esc(a.action)}</div>
        <div class="tiny dim" style="margin-top:10px">
          risk R ${Number(a.risk_r).toFixed(2)} &middot; confidence ${Number(a.case_confidence).toFixed(2)}<br>
          ${a.n_detectors} detector(s) contributed<br>
          value at risk ${inr(a.value_at_risk)}
        </div>
      </div>
    </div>
  </div>

  ${a.corroboration_capped ? `<div class="note warn" style="margin-bottom:16px">
    <b>Corroboration cap applied.</b> Only one detector produced strong evidence, so the risk score is held at 0.55.
    One loud signal is worth queuing, not worth dropping everything for.</div>` : ''}

  <div class="grid cols-2" style="margin-bottom:16px">
    <div class="card">
      <h2>Why this case — detector contributions</h2>
      ${contributionChart(dets)}
      <div class="tiny dim" style="margin-top:8px">
        Shares come straight from the fusion arithmetic (−ln(1−x<sub>k</sub>) normalised) and always sum to 100%.
      </div>
    </div>
    <div class="card">
      <h2>Bids in this tender</h2>
      ${bidChart(d.bids, a.estimate_value)}
      ${bidTable(d.bids)}
    </div>
  </div>

  <div class="card" style="margin-bottom:16px">
    <h2>Evidence by detector</h2>
    <p class="small muted" style="margin:-4px 0 14px">
      Every score traces back to the measured value, the peer baseline it was compared with, and the weight applied.
    </p>
    ${dets.map((x, i) => detectorBlock(x, i === 0 && x.x > 0.05)).join('')}
  </div>

  <div class="grid cols-2" style="margin-bottom:16px">
    <div class="card">
      <h2>Relationship map</h2>
      ${relationshipGraph(d.bids, d.links, a.awarded_vendor_id)}
      ${d.links.length ? `<div style="margin-top:10px">${d.links.map(l => `
          <div class="small" style="margin-bottom:6px">
            <span class="mono" style="color:var(--t1)">${Number(l.strength).toFixed(2)}</span>
            ${esc(l.vendor_a_name)} &harr; ${esc(l.vendor_b_name)}
            <div class="tiny dim">${esc(l.basis)}</div>
          </div>`).join('')}</div>`
    : `<div class="note">No detectable link between the bidders in this tender.
           That is reported as "no detectable link", never as proven independence.</div>`}
    </div>
    <div class="card">
      <h2>Timeline</h2>
      <div class="timeline">
        ${d.timeline.map(t => `<div class="tl-item" data-kind="${t.kind}">
            <div class="tl-date">${dateStr(t.date)}</div>
            <div class="tl-text">${esc(t.text)}</div></div>`).join('')}
      </div>
    </div>
  </div>

  <div class="grid cols-3">
    <div class="card">
      <h2>Context applied (S6)</h2>
      ${contextBlock(dets, a)}
    </div>
    <div class="card">
      <h2>Innocent explanations to check</h2>
      ${d.innocent.length
      ? `<ul class="checks">${d.innocent.map(x => `<li>${esc(x.text)}
             <span class="tiny dim">(${x.detector})</span></li>`).join('')}</ul>
           <div class="note" style="margin-top:12px">Each of these is checkable, and each would close the case quickly if true.</div>`
      : '<p class="muted small">No detector fired strongly enough to need one.</p>'}
    </div>
    <div class="card">
      <h2>Data quality</h2>
      <ul class="checks">${d.data_quality.map(n => `<li>${esc(n)}</li>`).join('')}</ul>
      <div class="divider"></div>
      <div class="small muted">
        Peer group: <b class="mono">${a.peer_n}</b> comparable tenders<br>
        <span class="tiny dim">${esc(a.peer_label)} &middot; backoff level ${a.peer_level} &middot; c_peer ${a.c_peer}</span>
      </div>
      <div class="tiny dim" style="margin-top:10px">Rules version ${esc(a.rules_version)}</div>
    </div>
  </div>

  ${d.case_tenders.length > 1 ? `
  <div class="card" style="margin-top:16px">
    <h2>Case ${esc(a.case_id)} — ${d.case_tenders.length} related tenders, ${inr(d.case ? d.case.value_at_risk : 0)} at risk</h2>
    <p class="small muted" style="margin:-4px 0 12px">
      The same connected vendors meeting the same buying office. Presented as one case rather than ${d.case_tenders.length} separate alerts.
    </p>
    <div class="table-wrap">${resultTable(d.case_tenders.map(t => ({
      ...t, category_label: t.title, awarded_vendor_name: t.awarded_vendor_name,
      n_bids: '', buyer_name: a.buyer_name, department: a.department,
    })), true)}</div>
  </div>` : ''}

  <p class="tiny dim" style="margin-top:20px;max-width:80ch">
    ProcureGraph never concludes that a vendor, buyer or official is corrupt. An alert means this pattern is
    unusual compared with similar procurement, here is exactly why, and here are the innocent explanations worth
    checking. The decision stays with a human investigator. All data in this build is synthetic.
  </p>`;
}

function detectorBlock(x, open) {
  const dead = x.status !== 'scored' || x.score < 0.0001;
  return `<details class="detector" ${open ? 'open' : ''}>
    <summary>
      <svg class="chev" viewBox="0 0 12 12" aria-hidden="true"><path d="M4 2l4 4-4 4" stroke="currentColor" stroke-width="1.8" fill="none"/></svg>
      <span class="dname" style="color:${DET_COLOR[x.detector]}">${x.detector} ${esc(x.detector_name)}</span>
      <span class="dscore">${Number(x.score).toFixed(2)}</span>
      <span class="dbar">${meter(x.score, DET_COLOR[x.detector])}</span>
      <span class="tiny muted" style="width:150px;text-align:right">
        ${dead ? esc(x.status.replace('_', ' ')) : `contributes ${x.contribution_pct.toFixed(1)}%`}
      </span>
    </summary>
    <div class="body">
      <div class="tiny dim" style="margin-bottom:10px">${esc(DET_FULL[x.detector])}
        ${x.subject ? ` &middot; subject: <b>${esc(x.subject)}</b>` : ''}</div>
      <div class="signal" style="border-left-color:${DET_COLOR[x.detector]};border-left-width:2px">
        <div class="sig-meta">
          <span>score S <b>${Number(x.score).toFixed(2)}</b></span>
          <span>confidence c <b>${Number(x.confidence).toFixed(2)}</b></span>
          <span>context m <b>${Number(x.m_factor).toFixed(2)}</b></span>
          <span>weight w <b>${Number(x.weight).toFixed(2)}</b></span>
          <span>x = w&middot;c&middot;m&middot;S <b>${Number(x.x).toFixed(3)}</b></span>
        </div>
      </div>
      ${x.signals.length ? x.signals.map(s => `
        <div class="signal">
          <div class="sig-head">
            <span class="sig-id">${esc(s.signal_id)}</span>
            <span class="sig-label">${esc(s.label)}</span>
            <span class="badge plain tiny">${esc(s.family.replace(/_/g, ' '))}</span>
          </div>
          <div class="sig-expl">${esc(s.explanation)}</div>
          <div class="sig-meta">
            <span>observed: <b>${esc(s.observed_text)}</b></span>
            <span>baseline: <b>${esc(s.baseline_text)}</b></span>
            <span>evidence e <b>${Number(s.evidence).toFixed(2)}</b> &times; weight w <b>${Number(s.weight).toFixed(2)}</b>
              = <b>${Number(s.strength).toFixed(2)}</b></span>
          </div>
        </div>`).join('')
      : `<p class="small muted">No signal from this detector.
           A zero means "no unusual pattern found here", not "clean".</p>`}
      ${x.note ? `<div class="note ${dead ? 'warn' : ''}">${esc(x.note)}</div>` : ''}
      ${x.context_rule ? `<div class="note ctx"><b>Context rule:</b> ${esc(x.context_rule)} &mdash;
        evidence dampened to ${pct(x.m_factor)} of its raw value.
        <div class="tiny dim" style="margin-top:4px">${esc(x.context_evidence)}</div></div>` : ''}
    </div>
  </details>`;
}

function contextBlock(dets, a) {
  const applied = dets.filter(d => d.context_rule);
  if (!applied.length) {
    return `<p class="small muted">No context rule fired. No proprietary certificate, emergency declaration or
      framework agreement covers this tender, so nothing was dampened.</p>`;
  }
  return applied.map(d => `<div style="margin-bottom:12px">
      <div class="small"><b style="color:${DET_COLOR[d.detector]}">${d.detector}</b> ${esc(d.context_rule)}</div>
      <div class="tiny muted">${esc(d.context_evidence)}</div>
      <div class="tiny dim">raw score ${Number(d.score).toFixed(2)} &times; m ${Number(d.m_factor).toFixed(2)}
        = adjusted ${(d.score * d.m_factor).toFixed(2)}</div>
    </div>`).join('') +
    `<div class="note ctx">Nothing is ever suppressed to zero. Every dampening factor has a floor, and relationship
      evidence from S1 keeps at least 70% of its value.</div>`;
}

/* ---------------- charts ---------------- */

function contributionChart(dets) {
  const rows = dets.filter(d => d.contribution_pct > 0.05);
  if (!rows.length) return '<p class="small muted">No detector contributed to a risk score for this tender.</p>';
  const W = 420, rowH = 30, H = rows.length * rowH + 26, labelW = 132, barW = W - labelW - 46;
  const max = Math.max(...rows.map(r => r.contribution_pct), 10);
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
       aria-label="Detector contribution shares">
    ${rows.map((r, i) => {
    const y = i * rowH + 6, w = Math.max(2, barW * r.contribution_pct / max);
    return `<g>
        <text class="lbl" x="0" y="${y + 14}" style="fill:${DET_COLOR[r.detector]}">${r.detector}</text>
        <text class="lbl" x="24" y="${y + 14}">${esc(r.detector_name)}</text>
        <rect x="${labelW}" y="${y + 3}" width="${barW}" height="14" rx="3" fill="var(--panel-2)"/>
        <rect x="${labelW}" y="${y + 3}" width="${w}" height="14" rx="3" fill="${DET_COLOR[r.detector]}">
          <animate attributeName="width" from="0" to="${w}" dur="0.5s" fill="freeze"/>
        </rect>
        <text class="val" x="${labelW + barW + 6}" y="${y + 14}">${r.contribution_pct.toFixed(1)}%</text>
      </g>`;
  }).join('')}
    <line class="axis" x1="${labelW}" y1="${H - 16}" x2="${labelW + barW}" y2="${H - 16}"/>
    <text class="lbl" x="${labelW}" y="${H - 3}">share of the risk score</text>
  </svg>`;
}

function bidChart(bids, estimate) {
  const valid = bids.filter(b => b.status !== 'withdrawn');
  if (!valid.length) return '<div class="note warn">Only the award was published — no bid distribution to show.</div>';
  if (valid.length === 1) return '<div class="note warn">A single bid. There is no distribution to analyse; the absence of competition is handled by S5.</div>';

  const vals = valid.map(b => b.bid_value);
  const est = estimate ? Number(estimate) : null;
  let lo = Math.min(...vals, est || Infinity), hi = Math.max(...vals, est || -Infinity);
  const pad = (hi - lo) * 0.15 || hi * 0.05;
  lo -= pad; hi += pad;
  const W = 460, H = 128, L = 10, R = W - 10;
  const x = (v) => L + (R - L) * (v - lo) / (hi - lo);

  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => lo + f * (hi - lo));
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
      aria-label="Bid values in this tender">
    ${ticks.map(t => `<line class="grid-line" x1="${x(t).toFixed(1)}" y1="18" x2="${x(t).toFixed(1)}" y2="86"/>`).join('')}
    ${est ? `<line x1="${x(est).toFixed(1)}" y1="14" x2="${x(est).toFixed(1)}" y2="90"
        stroke="var(--gap)" stroke-width="1.4" stroke-dasharray="4 3"/>
      <text class="lbl" x="${x(est).toFixed(1)}" y="11" text-anchor="middle" style="fill:var(--gap)">estimate</text>` : ''}
    ${valid.map((b, i) => {
    const cx = x(b.bid_value), cy = 52 + (i % 2 ? 11 : -11);
    const col = b.is_winner ? 'var(--accent)' : (b.status === 'disqualified' ? 'var(--t1)' : 'var(--t2)');
    return `<g><circle cx="${cx.toFixed(1)}" cy="${cy}" r="7" fill="${col}" fill-opacity="0.85"
        stroke="var(--bg)" stroke-width="1.5"><title>${esc(b.legal_name)}: ${inr(b.bid_value)}${b.is_winner ? ' (winner)' : ''}</title></circle></g>`;
  }).join('')}
    <line class="axis" x1="${L}" y1="90" x2="${R}" y2="90"/>
    ${ticks.map(t => `<text class="val" x="${x(t).toFixed(1)}" y="104" text-anchor="middle">${(t / 1e5).toFixed(1)}L</text>`).join('')}
    <text class="lbl" x="${L}" y="121">bid value (₹ lakh)</text>
    <g transform="translate(${R - 168},116)">
      <circle cx="0" cy="-4" r="5" fill="var(--accent)"/><text class="lbl" x="9" y="0">winner</text>
      <circle cx="58" cy="-4" r="5" fill="var(--t2)"/><text class="lbl" x="67" y="0">losing</text>
      <circle cx="112" cy="-4" r="5" fill="var(--t1)"/><text class="lbl" x="121" y="0">disqualified</text>
    </g>
  </svg>`;
}

function bidTable(bids) {
  if (!bids.length) return '';
  const win = bids.find(b => b.is_winner);
  return `<table style="margin-top:6px;background:transparent">
    <thead><tr><th>Bidder</th><th class="right">Bid</th><th class="right">vs winner</th><th>Status</th></tr></thead>
    <tbody>${bids.map(b => `<tr onclick="event.stopPropagation()" style="cursor:default">
      <td><div class="small">${esc(b.legal_name)}${b.is_winner ? ' <span class="badge plain tiny">won</span>' : ''}</div>
          <div class="tiny dim">inc. ${dateStr(b.incorporation_date)} &middot; capital ${inr(b.paid_up_capital)}
          ${b.msme_registered ? ' &middot; MSE' : ''}${b.startup_registered ? ' &middot; start-up' : ''}</div></td>
      <td class="right num">${inr(b.bid_value)}</td>
      <td class="right num tiny ${b.is_winner ? 'dim' : 'muted'}">${win && !b.is_winner
      ? '+' + pct(b.bid_value / win.bid_value - 1, 2) : '—'}</td>
      <td class="tiny">${b.status === 'disqualified'
      ? `<span style="color:var(--t1)">disqualified</span><div class="dim">${esc(b.disqualification_reason)}</div>`
      : '<span class="dim">valid</span>'}</td>
    </tr>`).join('')}</tbody></table>`;
}

function relationshipGraph(bids, links, winnerId) {
  const nodes = bids.map(b => ({ id: b.vendor_id, name: b.legal_name, win: b.vendor_id === winnerId }));
  if (!nodes.length) return '<p class="small muted">No bidder records published for this tender.</p>';
  const W = 440, H = Math.max(210, 60 + nodes.length * 26);
  const cx = W / 2, cy = H / 2 - 6, r = Math.min(120, 42 + nodes.length * 14);
  const pos = {};
  nodes.forEach((n, i) => {
    const ang = -Math.PI / 2 + (2 * Math.PI * i) / nodes.length;
    pos[n.id] = { x: cx + r * Math.cos(ang), y: cy + r * Math.sin(ang) * 0.72 };
  });

  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
      aria-label="Relationship map for the bidders in this tender">
    ${links.map(l => {
    const a = pos[l.vendor_a], b = pos[l.vendor_b];
    if (!a || !b) return '';
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    const strong = l.strength >= 0.5;
    return `<g>
        <line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"
          stroke="${strong ? 'var(--t1)' : 'var(--dim)'}" stroke-width="${strong ? 2 : 1.2}"
          stroke-dasharray="${strong ? '' : '4 3'}"/>
        <rect x="${(mx - 19).toFixed(1)}" y="${(my - 9).toFixed(1)}" width="38" height="17" rx="8"
          fill="var(--panel)" stroke="${strong ? 'var(--t1)' : 'var(--border)'}" stroke-width="1"/>
        <text class="val" x="${mx.toFixed(1)}" y="${(my + 3).toFixed(1)}" text-anchor="middle"
          style="fill:${strong ? '#FF9878' : 'var(--muted)'}">${Number(l.strength).toFixed(2)}</text>
        <title>${esc(l.basis)}</title>
      </g>`;
  }).join('')}
    ${nodes.map(n => {
    const p = pos[n.id];
    const short = n.name.split(' ').slice(0, 2).join(' ');
    return `<g>
        <circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="19"
          fill="${n.win ? 'rgba(45,212,191,.16)' : 'var(--panel-2)'}"
          stroke="${n.win ? 'var(--accent)' : 'var(--border)'}" stroke-width="2"/>
        <text x="${p.x.toFixed(1)}" y="${(p.y + 4).toFixed(1)}" text-anchor="middle"
          style="fill:var(--text);font-size:12px;font-family:var(--mono)">${esc(n.name.slice(0, 2).toUpperCase())}</text>
        <text class="lbl" x="${p.x.toFixed(1)}" y="${(p.y + 33).toFixed(1)}" text-anchor="middle">${esc(short)}</text>
        ${n.win ? `<text class="lbl" x="${p.x.toFixed(1)}" y="${(p.y + 45).toFixed(1)}" text-anchor="middle"
          style="fill:var(--accent)">winner</text>` : ''}
        <title>${esc(n.name)}</title>
      </g>`;
  }).join('')}
  </svg>`;
}

/* ---------------- boot ---------------- */

(async function init() {
  try {
    await loadFilters();
    await loadOverview();
  } catch (e) {
    $('tiles').innerHTML = `<div class="empty">Could not load data. Has the pipeline been run?
      <div class="tiny dim">python scripts/generate_data.py &rarr; load_sqlite.py &rarr; analyze.py</div></div>`;
  }
})();
