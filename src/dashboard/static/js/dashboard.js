let controlToken = null;
const money = v => v == null ? '—' : '₹' + Number(v).toLocaleString('en-IN', {maximumFractionDigits: 2});
const num = (v, n=2) => v == null ? '—' : Number(v).toFixed(n);
const time = v => v ? new Date(Number(v) * 1000).toLocaleString() : '—';
const esc = v => String(v ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(url, options={}) {
  if ((options.method || 'GET') !== 'GET') {
    if (!controlToken) controlToken = prompt('Dashboard control token:');
    if (!controlToken) throw new Error('Cancelled');
    options.headers = {...options.headers, Authorization: `Bearer ${controlToken}`, 'Content-Type':'application/json'};
  }
  const response = await fetch(url, options); const body = await response.json();
  if (!response.ok) { if (response.status === 401) controlToken = null; throw new Error(body.error || response.statusText); }
  return body;
}
function table(id, headers, rows) {
  const el=document.getElementById(id); if (!rows.length) { el.innerHTML='<p class="muted">No records</p>'; return; }
  el.innerHTML=`<table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
function render(d) {
  const s=d.sections||{}, service=s.service||{}, val=service.valuation||{}, account=s.accounting||{}, ready=d.readiness||{}, rec=d.recorder||{};
  document.getElementById('state-badge').textContent=rec.lifecycle||(rec.running?'RECORDING':'MONITORING'); document.getElementById('state-badge').className='state-badge '+(rec.running?'running':'stopped');
  document.getElementById('pulse-dot').className='pulse-dot '+(rec.running?'running':''); document.getElementById('recorder').textContent=rec.running?'Stop recorder':'Start recorder';
  const session=rec.session||{}; const market=service.account?.market_open?'OPEN':`CLOSED · ${(session.phase||'unknown').replaceAll('_',' ')}`;
  document.getElementById('market').textContent=market; document.getElementById('market').title=session.next_session_start?`Next recorder start: ${session.next_session_start}`:(session.reason||''); document.getElementById('last-event').textContent=time(service.last_event?.received);
  document.getElementById('qualification').textContent=ready.details?.qualification||'unavailable'; document.getElementById('backup-age').textContent=ready.details?.latest_backup_age_seconds==null?'missing':`${num(ready.details.latest_backup_age_seconds/3600,1)} h`;
  document.getElementById('equity').textContent=money(val.equity); document.getElementById('cash').textContent=money(account.cash); document.getElementById('daily-pnl').textContent=money(val.daily_pnl); document.getElementById('fees').textContent=money(account.total_fees); document.getElementById('position-count').textContent=(s.positions||[]).length;
  const problems=[...(ready.blockers||[]),...(ready.alerts||[]),...(d.errors||[])]; const banner=document.getElementById('alert-banner'); banner.textContent=problems.join(' · '); banner.className='alert-banner'+(problems.length?'':' hidden');
  table('quotes',['Symbol','Bid','Ask','Bid size','Ask size','Spread bps','Age sec','Latency sec','Fresh'],(s.quotes||[]).map(q=>[q.symbol,q.bid_price,q.ask_price,q.bid_size,q.ask_size,num(q.spread_bps),num(q.age_seconds,1),num(q.latency_seconds,3),q.fresh?'YES':'NO']));
  const lab=s.strategy_lab||{}, sel=lab.selector||{};
  table('selector',['Field','Value'],[['Mode',(sel.execution||'observation_only').toUpperCase()],['Universe',sel.universe_id],['State',sel.state],['Regime',sel.regime],['Confidence',sel.regime_confidence==null?'—':num(sel.regime_confidence,3)],['Eligible',(sel.eligible_families||[]).join(', ')||'none'],['Selected',sel.selected_family||'CASH / NONE'],['Reason',sel.reason],['Warm-up',sel.warmup_bars==null?'—':`${sel.warmup_bars}/${sel.required_warmup_bars}`],['Paper gate',sel.paper_schedule?.status||'blocked'],['Forward observations',JSON.stringify(sel.family_metrics||{})],['Targets',(sel.family_previews||[]).map(x=>`${x.family}: ${JSON.stringify(x.hypothetical_targets)}`).join(' · ')||'none']]);
  table('strategies',['Candidate','Kind','State','Completed','Pending','Net return %','Win rate %','Excess %'],(lab.candidates||[]).map(c=>[c.id,c.kind,c.state,c.metrics?.completed,c.metrics?.pending,c.metrics?.net_return==null?'—':num(c.metrics.net_return*100),c.metrics?.win_rate==null?'—':num(c.metrics.win_rate*100),c.metrics?.mean_excess_return==null?'—':num(c.metrics.mean_excess_return*100)]));
  const ops=s.operations||{}, bar=ops.last_completed_bar||{}, auth=service.authentication||{}, fin=service.finalization||{}, pf=service.preflight||{};
  table('operations',['Field','Value'],[['Lifecycle',rec.lifecycle],['Token health',auth.state||'UNKNOWN'],['Last completed bar',bar.session_date],['Missing symbols',(bar.missing_symbols||[]).join(', ')||'none'],['Session finalization',fin.status||'pending'],['Preflight',pf.passed===true?'PASS':pf.passed===false?'FAIL':'not run'],['VM heartbeat',time(service.heartbeat?.updated_at)],['Disk free GiB',ops.disk_free_bytes==null?'—':num(ops.disk_free_bytes/1073741824,2)],['Off-site backup',ops.offsite_backup?.status||'not reported'],['Alert delivery',ops.alert_delivery?.status||'not reported']]);
  const research=s.research||{}, econ=s.qualification_economics||{}; table('research',['Field','Value'],[['Holdout',research.holdout_status||'unavailable'],['Holdout accesses',(research.holdout_access||[]).length],['Unfinished experiments',research.unfinished_experiments],['Observation maturity',(s.regime_observations||[]).filter(x=>x.evaluated_day).length],['Benchmark',econ.benchmark_id||sel.regime_details?.benchmark_source||'NIFTY 50'],['After-tax/infra excess',econ.after_tax_and_infrastructure_excess],['Excess lower bound',econ.excess_lower_confidence_bound],['Break-even capital',money(econ.break_even_capital_inr)],['Net/benchmark/excess',JSON.stringify(sel.family_metrics||{})]]);
  const diffs=s.reconciliation?.differences||{}; table('positions',['Symbol','Local qty','Broker difference'],(s.positions||[]).map(p=>[p.symbol,p.quantity,diffs[p.symbol]?JSON.stringify(diffs[p.symbol]):'none']));
  table('readiness',['Type','Status'],[['Live execution','DISABLED'],...(ready.blockers||[]).map(x=>['Blocker',x]),...(ready.alerts||[]).map(x=>['Alert',x])]);
  table('orders',['ID','Broker ID','Status','Filled'],[...(s.orders||[]).map(o=>[o.intent_id,o.broker_id,o.status,o.filled]),...(s.paper_orders||[]).map(o=>[o.intent_id,'PAPER',o.status,`${o.filled}/${o.requested}`]),...(s.scheduled_intents||[]).map(i=>[i.intent_id,'—','SCHEDULED','—'])]);
  table('settlements',['Intent','Due','Symbol','Side','Qty','Cash'],(s.settlements||[]).map(x=>[x.intent_id,x.due_day,x.symbol,x.side,x.quantity,money(x.cash_amount)]));
  table('fills',['Time','Intent','Symbol','Side','Qty','Price','Fee'],(s.fills||[]).map(x=>[time(x.timestamp),x.intent_id,x.symbol,x.side,x.quantity,money(x.price),money(x.fee)]));
  table('events',['Time','Type','Details'],(s.events||[]).map(x=>[time(x.received),x.kind,JSON.stringify(x.data)]));
}
async function refresh(){ try { render(await api('/api/fyers/dashboard')); } catch(e){ const b=document.getElementById('alert-banner'); b.textContent=e.message; b.className='alert-banner'; } }
document.getElementById('recorder').onclick=async()=>{try{const running=document.getElementById('recorder').textContent.startsWith('Stop');await api(`/api/fyers/recorder/${running?'stop':'start'}`,{method:'POST'});await refresh();}catch(e){alert(e.message)}};
document.getElementById('backup').onclick=async()=>{try{const r=await api('/api/fyers/backup',{method:'POST'});alert(`Backup verified: ${r.path}`);await refresh();}catch(e){alert(e.message)}};
document.getElementById('halt').onclick=async()=>{const reason=prompt('Reason for blocking new entries:');if(!reason)return;try{await api('/api/fyers/halt',{method:'POST',body:JSON.stringify({reason})});await refresh();}catch(e){alert(e.message)}};
refresh(); setInterval(refresh,5000);
