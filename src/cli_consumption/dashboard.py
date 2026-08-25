from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.engine import Engine

from cli_consumption.storage import read_table


def generate_dashboard(engine: Engine, output: Path) -> None:
    payload = {
        "conversations": read_table(engine, "conversations"),
        "modelCalls": read_table(engine, "model_calls"),
        "toolCalls": read_table(engine, "tool_calls"),
        "turns": read_table(engine, "turns"),
        "subagents": read_table(engine, "subagents"),
    }
    encoded = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_document(encoded), encoding="utf-8")


def _document(payload: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>CLI Consumption</title>
  <style>
    :root {{ color-scheme: dark; --bg:#07111f; --panel:#101e31; --ink:#eef6ff;
      --muted:#91a7bf; --accent:#5eead4; --accent2:#60a5fa; --line:#233b55; }}
    * {{ box-sizing:border-box }} body {{ margin:0; background:radial-gradient(circle at 85% 0,#12335a 0,transparent 32%),var(--bg); color:var(--ink); font:15px/1.5 Inter,ui-sans-serif,system-ui,sans-serif }}
    main {{ max-width:1280px; margin:auto; padding:42px 24px 64px }}
    h1 {{ margin:0; font-size:clamp(32px,5vw,58px); letter-spacing:-.04em }}
    .eyebrow {{ color:var(--accent); text-transform:uppercase; letter-spacing:.15em; font-size:12px; font-weight:800 }}
    .subtitle {{ color:var(--muted); max-width:720px }}
    .filters,.cards,.grid {{ display:grid; gap:14px }}
    .filters {{ grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); margin:30px 0 18px }}
    .cards {{ grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); margin-bottom:18px }}
    .grid {{ grid-template-columns:repeat(auto-fit,minmax(360px,1fr)) }}
    .panel,.card,select {{ border:1px solid var(--line); background:color-mix(in srgb,var(--panel) 92%,transparent); border-radius:16px }}
    .panel {{ padding:22px; overflow:auto }} .card {{ padding:18px }}
    .card strong {{ display:block; font-size:29px; letter-spacing:-.03em }} .card span,label {{ color:var(--muted) }}
    label {{ display:grid; gap:6px; font-size:12px; text-transform:uppercase; letter-spacing:.08em }}
    select {{ color:var(--ink); padding:10px 12px; width:100% }}
    h2 {{ margin:0 0 18px; font-size:18px }} table {{ width:100%; border-collapse:collapse; white-space:nowrap }}
    th,td {{ padding:9px 8px; border-bottom:1px solid var(--line); text-align:left }} th {{ color:var(--muted); font-size:11px; text-transform:uppercase }}
    .bars {{ display:grid; gap:10px }} .bar {{ display:grid; grid-template-columns:minmax(100px,1fr) 4fr auto; gap:10px; align-items:center }}
    .track {{ height:10px; background:#1a2d43; border-radius:99px; overflow:hidden }} .fill {{ height:100%; background:linear-gradient(90deg,var(--accent2),var(--accent)); border-radius:99px }}
    .empty {{ color:var(--muted); padding:30px 0 }} footer {{ color:var(--muted); margin-top:26px }}
  </style>
</head>
<body><main>
  <div class="eyebrow">Local-first AI CLI observability</div>
  <h1>CLI Consumption</h1>
  <p class="subtitle">Compare conversations, models, tokens, and tools without exporting prompts, responses, or tool arguments.</p>
  <section class="filters">
    <label>Provider<select id="provider"></select></label>
    <label>Machine<select id="machine"></select></label>
    <label>Project<select id="project"></select></label>
    <label>Model<select id="model"></select></label>
  </section>
  <section class="cards" id="cards"></section>
  <section class="grid">
    <article class="panel"><h2>Token consumption by day</h2><div class="bars" id="days"></div></article>
    <article class="panel"><h2>Token consumption by model</h2><div class="bars" id="models"></div></article>
    <article class="panel"><h2>Most-used tools</h2><div class="bars" id="tools"></div></article>
  </section>
  <article class="panel" style="margin-top:14px"><h2>Conversations</h2><div id="table"></div></article>
  <footer>Generated as a self-contained file. No network request is made.</footer>
</main>
<script>
const data={payload};
const $=id=>document.getElementById(id), fmt=n=>new Intl.NumberFormat().format(n||0);
const convById=Object.fromEntries(data.conversations.map(c=>[c.id,c]));
const modelsFor=c=>JSON.parse(c.models_json||'[]');
function options(id, values){{ const e=$(id), current=e.value; e.innerHTML='<option value="">All</option>'+[...new Set(values.filter(Boolean))].sort().map(v=>`<option>${{escapeHtml(v)}}</option>`).join(''); e.value=current; }}
function escapeHtml(v){{ return String(v).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
function selected(){{ return {{provider:$('provider').value,machine:$('machine').value,project:$('project').value,model:$('model').value}}; }}
function render(){{
  const f=selected();
  const conversations=data.conversations.filter(c=>(!f.provider||c.provider===f.provider)&&(!f.machine||c.source_machine===f.machine)&&(!f.project||c.project===f.project)&&(!f.model||modelsFor(c).includes(f.model)));
  const ids=new Set(conversations.map(c=>c.id));
  const calls=data.modelCalls.filter(c=>ids.has(c.conversation_id)&&(!f.model||c.model===f.model));
  const tools=data.toolCalls.filter(c=>ids.has(c.conversation_id));
  const turns=data.turns.filter(c=>ids.has(c.conversation_id));
  const machines=new Set(conversations.map(c=>c.source_machine));
  const subagents=data.subagents.filter(s=>(!f.provider||s.provider===f.provider)&&(!f.machine||s.source_machine===f.machine)&&machines.has(s.source_machine));
  const total=(rows,key)=>rows.reduce((n,r)=>n+(Number(r[key])||0),0);
  $('cards').innerHTML=[['Conversations',conversations.length],['Model calls',calls.length],['Total tokens',total(calls,'total_tokens')],['Cached input',total(calls,'cached_input_tokens')],['Output tokens',total(calls,'output_tokens')],['Subagents',subagents.length],['Max active turns',maxConcurrent(turns)]].map(([k,v])=>`<div class="card"><span>${{k}}</span><strong>${{fmt(v)}}</strong></div>`).join('');
  drawBars('days',group(calls.map(c=>({{...c,day:(c.timestamp||'unknown').slice(0,10)}})),'day','total_tokens'));
  drawBars('models',group(calls,'model','total_tokens'));
  drawBars('tools',group(tools,'tool_name'));
  $('table').innerHTML=conversations.length?`<table><thead><tr><th>Provider</th><th>Machine</th><th>Project</th><th>Started</th><th>Models</th><th>Turns</th><th>Tokens</th></tr></thead><tbody>${{conversations.slice().sort((a,b)=>(b.started_at||'').localeCompare(a.started_at||'')).map(c=>`<tr><td>${{escapeHtml(c.provider)}}</td><td>${{escapeHtml(c.source_machine)}}</td><td>${{escapeHtml(c.project)}}</td><td>${{escapeHtml(c.started_at||'')}}</td><td>${{escapeHtml(modelsFor(c).join(', '))}}</td><td>${{fmt(c.iterations)}}</td><td>${{fmt(c.total_tokens)}}</td></tr>`).join('')}}</tbody></table>`:'<div class="empty">No conversations match these filters.</div>';
}}
function group(rows,label,value){{ const out={{}}; rows.forEach(r=>out[r[label]||'unknown']=(out[r[label]||'unknown']||0)+(value?(Number(r[value])||0):1)); return Object.entries(out).sort((a,b)=>b[1]-a[1]).slice(0,12); }}
function maxConcurrent(turns){{ const points=[]; turns.forEach(t=>{{ if(t.started_at&&t.ended_at){{ points.push([t.started_at,1],[t.ended_at,-1]); }} }}); points.sort((a,b)=>a[0].localeCompare(b[0])||a[1]-b[1]); let active=0,max=0; points.forEach(p=>{{active+=p[1];max=Math.max(max,active)}}); return max; }}
function drawBars(id,rows){{ const max=Math.max(...rows.map(x=>x[1]),1); $(id).innerHTML=rows.map(([k,v])=>`<div class="bar"><span title="${{escapeHtml(k)}}">${{escapeHtml(k)}}</span><div class="track"><div class="fill" style="width:${{100*v/max}}%"></div></div><b>${{fmt(v)}}</b></div>`).join('')||'<div class="empty">No data.</div>'; }}
options('provider',data.conversations.map(c=>c.provider)); options('machine',data.conversations.map(c=>c.source_machine)); options('project',data.conversations.map(c=>c.project)); options('model',data.modelCalls.map(c=>c.model));
document.querySelectorAll('select').forEach(e=>e.addEventListener('change',render)); render();
</script></body></html>"""
