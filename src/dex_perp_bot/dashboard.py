"""Zero-dependency LAN dashboard: serves logs/status.json, trade report and log tail over HTTP.

Run with:  python -m src.dex_perp_bot.dashboard   (env DASHBOARD_PORT, default 8765)
Only reads files under ./logs; it never touches exchange APIs or credentials.
"""

from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

from .control import read_control, write_control
from .decision_log import read_events, summarize

LOGS_DIR = Path("logs")
STATUS_PATH = LOGS_DIR / "status.json"
TRADES_PATH = LOGS_DIR / "trades.md"
# Optional second data source: the Blockchain_arbitrage project's logs directory (detect-and-attribute mode).
ARB_LOGS_DIR = Path(os.getenv("ARB_LOGS_DIR", "../Blockchain_arbitrage/logs"))

_ENTRY_RE = re.compile(r"^- `(\d{2}:\d{2}:\d{2})` \*\*(OPEN|CLOSE) (\S+)\*\* (\S+) on \*\*([^*]+)\*\*")
_QTY_RE = re.compile(r"Qty: ([\d.]+) \| Price: \$([\d.,]+) \| Notional: \$([\d,.]+)")
_LEV_RE = re.compile(r"Leverage: (\d+)x")


def parse_trades(path: Path = TRADES_PATH, limit: int = 200) -> List[Dict[str, Any]]:
    """Parse the markdown trade report into a newest-first list of dicts."""
    if not path.exists():
        return []
    trades: List[Dict[str, Any]] = []
    date = ""
    current: Dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m_day = re.match(r"^## (\d{4}-\d{2}-\d{2})", line)
        if m_day:
            date = m_day.group(1)
            continue
        m = _ENTRY_RE.match(line)
        if m:
            current = {
                "date": date, "time": m.group(1), "action": m.group(2), "symbol": m.group(3),
                "side": m.group(4), "venue": m.group(5).strip(), "qty": None, "price": None,
                "notional": None, "leverage": None, "details": "",
            }
            trades.append(current)
            continue
        if current is not None and line.startswith("  "):
            q = _QTY_RE.search(line)
            if q:
                current["qty"] = float(q.group(1))
                current["price"] = float(q.group(2).replace(",", ""))
                current["notional"] = float(q.group(3).replace(",", ""))
                continue
            lv = _LEV_RE.search(line)
            if lv:
                current["leverage"] = int(lv.group(1))
                continue
            current["details"] = (current["details"] + " " + line.strip()).strip()
    # File is newest-day-first but entries within a day are oldest-first.
    trades.sort(key=lambda t: (t["date"], t["time"]), reverse=True)
    return trades[:limit]


def latest_log_file() -> Path | None:
    logs = sorted(LOGS_DIR.glob("bot_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def tail_log(lines: int = 300) -> Dict[str, Any]:
    path = latest_log_file()
    if path is None:
        return {"file": None, "lines": []}
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - 256 * 1024))
        chunk = fh.read().decode("utf-8", errors="replace")
    return {"file": path.name, "lines": chunk.splitlines()[-lines:]}


def read_json(path: Path, missing: str) -> Dict[str, Any]:
    if not path.exists():
        return {"generated_at": None, "errors": [missing]}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"generated_at": None, "errors": [f"cannot read {path.name}: {exc}"]}


def read_jsonl_tail(path: Path, limit: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        return []
    return out[-limit:][::-1]


def arb_control_path() -> Path:
    return ARB_LOGS_DIR / "control.json"


def read_arb_control() -> Dict[str, Any]:
    p = arb_control_path()
    if not p.exists():
        return {"mode": "run"}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if d.get("mode") in ("run", "pause") else {"mode": "run"}
    except (OSError, ValueError):
        return {"mode": "run"}


def write_arb_control(mode: str, by: str) -> Dict[str, Any]:
    if mode not in ("run", "pause"):
        raise ValueError(f"invalid arb mode {mode!r}")
    from datetime import datetime, timezone
    data = {"mode": mode, "requested_at": datetime.now(timezone.utc).isoformat(), "by": by}
    p = arb_control_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)
    return data


def read_status() -> Dict[str, Any]:
    if not STATUS_PATH.exists():
        return {"generated_at": None, "errors": ["status.json not written yet - is the bot running?"]}
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"generated_at": None, "errors": [f"cannot read status.json: {exc}"]}


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dex Perp Bot</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e8ee;--muted:#8b93a7;--pos:#3ddc84;--neg:#ff5c5c;--acc:#5aa2ff;font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-size:14px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;padding:14px 20px;border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0}header .meta{color:var(--muted);font-size:12px}
main{padding:16px 20px;display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px;min-width:0}
section.wide{grid-column:1/-1}h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 10px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.tile{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px}
.tile .k{color:var(--muted);font-size:11px;text-transform:uppercase}.tile .v{font-size:20px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500;font-size:12px}td.num,th.num{text-align:right}
.pos{color:var(--pos)}.neg{color:var(--neg)}.muted{color:var(--muted)}.tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;background:#232838}
.tag.hl{color:#9ad1ff}.tag.aster{color:#ffd27a}
pre{margin:0;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px;max-height:420px;overflow:auto;font-size:12px;line-height:1.45;font-family:ui-monospace,Consolas,monospace}
.l-ERROR{color:var(--neg)}.l-WARNING{color:#ffb454}.overflow{overflow-x:auto}.err{color:var(--neg);font-size:12px}
button{background:#232838;color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:4px 10px;cursor:pointer}button.danger{background:#4a1f24;border-color:#7a2e36}button:disabled{opacity:.4;cursor:default}.mode{padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600}.mode.run{background:#173b2a;color:var(--pos)}.mode.pause{background:#3d2f12;color:#ffb454}.mode.flatten{background:#4a1f24;color:var(--neg)}
</style></head><body>
<header><h1>Dex Perp Bot</h1><span class="meta" id="meta">loading…</span><span style="flex:1"></span><span id="mode" class="mode"></span><button id="btn-pause" onclick="setMode('pause')" title="No new trades or rebalances. Open positions are kept.">Pause trading</button><button id="btn-flatten" class="danger" onclick="setMode('flatten')" title="Cancel all orders and close all positions on both venues, then pause.">Close all &amp; pause</button><button id="btn-run" onclick="setMode('run')">Resume</button><button onclick="refresh()">refresh</button></header>
<main>
<section><h2>Balances</h2><div class="tiles" id="balances"></div></section>
<section><h2>P&amp;L (realized, USD)</h2><div class="overflow"><table id="pnl"></table></div><div class="muted" style="font-size:12px;margin-top:6px">Unrealized: <span id="upnl"></span></div></section>
<section class="wide"><h2>Open positions</h2><div class="overflow"><table id="positions"></table></div></section>
<section class="wide"><h2>Last funding scan</h2><div class="overflow"><table id="opps"></table></div></section>
<section class="wide"><h2>Execution &amp; signals <span class="muted" id="dsum"></span></h2><div class="overflow"><table id="tactics"></table></div><div class="overflow" style="margin-top:8px"><table id="events"></table></div></section>
<section class="wide"><h2>Last trades</h2><div class="overflow"><table id="trades"></table></div></section>
<section class="wide"><h2>On-chain arb (Base) · detect &amp; attribute <span class="muted" id="arbsum"></span>
<span style="float:right"><span id="arbmode" class="mode"></span> <button id="arb-pause" onclick="setArbMode('pause')">Pause detector</button> <button id="arb-run" onclick="setArbMode('run')">Resume</button></span></h2>
<div class="tiles" id="arbtiles"></div>
<div class="overflow" style="margin-top:8px"><table id="arbtokens"></table></div>
<div class="overflow" style="margin-top:8px"><table id="arbboard"></table></div>
<div class="overflow" style="margin-top:8px"><table id="arbevents"></table></div></section>
<section class="wide"><h2>Log <span class="muted" id="logfile"></span></h2><pre id="log"></pre></section>
<section class="wide" id="errsec" hidden><h2>Status errors</h2><div class="err" id="errs"></div></section>
</main>
<script>
const $=s=>document.querySelector(s);
const usd=v=>(v==null?'—':(v<0?'-':'')+'$'+Math.abs(v).toFixed(2));
const cls=v=>v>0?'pos':v<0?'neg':'muted';
const venueTag=v=>`<span class="tag ${v==='Hyperliquid'?'hl':'aster'}">${v}</span>`;
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function j(u){const r=await fetch(u,{cache:'no-store'});return r.json()}
function ago(iso){if(!iso)return '—';const s=(Date.now()-new Date(iso))/1000;return s<90?Math.round(s)+'s ago':s<5400?Math.round(s/60)+'m ago':(s/3600).toFixed(1)+'h ago'}
let refresh=async function(){
  const st=await j('/api/status');
  $('#meta').textContent=`status ${ago(st.generated_at)} · bot up since ${st.started_at?new Date(st.started_at).toLocaleString():'—'} · next window ${st.next_window_utc?new Date(st.next_window_utc).toLocaleTimeString():'—'}`;
  const b=st.balances||{};const hl=b.hyperliquid||{},as=b.aster||{};
  $('#balances').innerHTML=[['Hyperliquid',hl.total],['Aster',as.total],['Deployable (min)',Math.min(hl.total??0,as.total??0)],['Total',(hl.total??0)+(as.total??0)]]
    .map(([k,v])=>`<div class="tile"><div class="k">${k}</div><div class="v">${usd(v)}</div></div>`).join('');
  const p=st.pnl||{};const rows=['total','hyperliquid','aster'].filter(k=>p[k]);
  $('#pnl').innerHTML='<tr><th></th><th class=num>24h</th><th class=num>7d</th><th class=num>30d</th></tr>'+rows.map(k=>{
    const w=p[k];return `<tr><td>${k}</td>`+['24h','7d','30d'].map(x=>`<td class="num ${cls(w[x].net)}" title="funding ${usd(w[x].funding)} · trading ${usd(w[x].trading)} · fees ${usd(w[x].fees)}">${usd(w[x].net)}</td>`).join('')+'</tr>'}).join('');
  $('#upnl').innerHTML=`<span class="${cls(st.unrealized_pnl)}">${usd(st.unrealized_pnl)}</span>`;
  const ps=st.positions||[];
  $('#positions').innerHTML=ps.length?'<tr><th>Venue</th><th>Symbol</th><th>Side</th><th class=num>Size</th><th class=num>Entry</th><th class=num>Mark</th><th class=num>Lev</th><th class=num>Liq.</th><th class=num>uPnL</th></tr>'+
    ps.map(x=>`<tr><td>${venueTag(x.venue)}</td><td>${esc(x.symbol)}</td><td class="${x.side==='long'?'pos':'neg'}">${x.side}</td><td class=num>${x.size}</td><td class=num>${x.entry}</td><td class=num>${x.mark}</td><td class=num>${x.leverage||'—'}x</td><td class=num>${x.liquidation||'—'}</td><td class="num ${cls(x.unrealized_pnl)}">${usd(x.unrealized_pnl)}</td></tr>`).join('')
    :'<tr><td class=muted>No open positions</td></tr>';
  const op=st.opportunities||[];
  $('#opps').innerHTML=op.length?'<tr><th>Symbol</th><th>Long</th><th>Short</th><th class=num>Net APY</th><th>Basis</th><th>Imminent</th><th>Actionable</th><th class=num>Aster rate</th><th class=num>HL rate</th></tr>'+
    op.map(o=>`<tr><td>${esc(o.symbol)}</td><td>${venueTag(o.long_venue)}</td><td>${venueTag(o.short_venue)}</td><td class="num ${cls(o.net_apy)}">${o.net_apy.toFixed(1)}%</td><td>${o.basis}</td><td>${o.imminent?'yes':''}</td><td>${o.actionable?'yes':'<span class=neg>no</span>'}</td><td class=num>${(o.rate_aster*100).toFixed(4)}%</td><td class=num>${(o.rate_hyperliquid*100).toFixed(4)}%</td></tr>`).join('')
    :'<tr><td class=muted>No scan yet (first scan happens in the next trading window)</td></tr>';
  const tr=await j('/api/trades?limit=50');
  $('#trades').innerHTML=tr.length?'<tr><th>Date</th><th>Time</th><th>Action</th><th>Symbol</th><th>Side</th><th>Venue</th><th class=num>Qty</th><th class=num>Price</th><th class=num>Notional</th><th>Details</th></tr>'+
    tr.map(t=>`<tr><td>${t.date}</td><td>${t.time}</td><td class="${t.action==='OPEN'?'pos':'muted'}">${t.action}</td><td>${esc(t.symbol)}</td><td>${t.side}</td><td>${venueTag(t.venue)}</td><td class=num>${t.qty??'—'}</td><td class=num>${t.price??'—'}</td><td class=num>${t.notional!=null?usd(t.notional):'—'}</td><td class=muted>${esc(t.details)}</td></tr>`).join('')
    :'<tr><td class=muted>No trades yet</td></tr>';
  const d=await j('/api/decisions?limit=40');const sm=d.summary||{};
  const bx=sm.basis_exits||{count:0,gain_bps_sum:0};const pr=sm.pairs||{count:0,hedged:0};
  $('#dsum').textContent=`· ${sm.events||0} events · pairs ${pr.hedged}/${pr.count} hedged · basis exits ${bx.count} (${bx.gain_bps_sum.toFixed(1)} bps)`;
  const tk=Object.entries(sm.tactics||{});const gt=Object.entries(sm.gates||{});
  $('#tactics').innerHTML=(tk.length?'<tr><th>Planned tactic</th><th class=num>Legs</th><th class=num>Fill rate</th><th class=num>Crossed after wait</th><th class=num>Avg slippage vs mid</th><th class=num>Avg wait</th></tr>'+
    tk.map(([k,v])=>`<tr><td>${k}</td><td class=num>${v.count}</td><td class=num>${(v.fill_rate*100).toFixed(0)}%</td><td class=num>${v.crossed_after_wait}</td><td class="num ${cls(-v.avg_slippage_bps)}">${v.avg_slippage_bps.toFixed(2)} bps</td><td class=num>${v.avg_wait_s.toFixed(0)}s</td></tr>`).join(''):'')+
    (gt.length?'<tr><th colspan=6>Gate outcomes</th></tr>'+gt.map(([k,v])=>`<tr><td colspan=5>${esc(k)}</td><td class=num>${v}</td></tr>`).join(''):'');
  const fmt=v=>typeof v==='number'?(Math.abs(v)>=100?v.toFixed(0):v.toFixed(2)):(v==null?'':esc(v));
  const keyf={scan:['actionable'],gate:['decision','reason_code','symbol','long_venue','net_apy_pct','breakeven_hours','effective_cost_bps','favorable_z','basis_samples'],
    leg_plan:['venue','symbol','side','tactic','imbalance','queue_qty','expected_wait_s','reason'],leg_order:['venue','symbol','side','tactic','price','context'],
    leg_fill:['venue','symbol','side','planned_tactic','final_tactic','filled','avg_price','slippage_bps','wait_s','error'],
    pair_result:['context','hedged','elapsed_s'],basis_exit:['symbol','gain_bps','threshold_bps','favorable_z'],note:['message']};
  const ev=d.events||[];
  $('#events').innerHTML=ev.length?'<tr><th>Time</th><th>Kind</th><th>Details</th></tr>'+ev.map(e=>`<tr><td>${new Date(e.ts).toLocaleString()}</td><td>${e.kind}</td><td class=muted style="white-space:normal">${(keyf[e.kind]||Object.keys(e).filter(k=>k!=='ts'&&k!=='kind')).filter(k=>e[k]!=null&&e[k]!=='').map(k=>`<b>${k}</b>=${fmt(e[k])}`).join(' · ')}</td></tr>`).join('')
    :'<tr><td class=muted>No decisions logged yet</td></tr>';
  try{
    const ar=await j('/api/arb/status');const ac=await j('/api/arb/control');const ae=await j('/api/arb/events?limit=30');
    const am=$('#arbmode');am.className='mode '+(ac.mode==='pause'?'pause':'run');am.textContent=ac.mode==='pause'?'PAUSED':'DETECTING';
    $('#arb-pause').disabled=ac.mode==='pause';$('#arb-run').disabled=ac.mode!=='pause';
    if(ar.errors&&ar.errors.length&&!ar.last_block){$('#arbsum').textContent='· '+ar.errors.join('; ');$('#arbtiles').innerHTML='';$('#arbtokens').innerHTML='';$('#arbboard').innerHTML='';$('#arbevents').innerHTML='';}
    else{
      const c=ar.counters||{};
      $('#arbsum').textContent=`· status ${ago(ar.generated_at)} · block ${ar.last_block}`;
      $('#arbtiles').innerHTML=[['Blocks seen',c.blocks],['Spreads ≥ '+ar.min_spread_pct+'%',c.spreads_opened],['Attributed',c.attributed],['Unattributed',c.unattributed],['Our block latency p50',ar.seen_latency_p50_s!=null?ar.seen_latency_p50_s+' s':'—']]
        .map(([k,v])=>`<div class="tile"><div class="k">${k}</div><div class="v">${v??'—'}</div></div>`).join('');
      const tk=ar.tokens||[];
      $('#arbtokens').innerHTML=tk.length?'<tr><th>Token</th><th class=num>Pools</th><th>Buy on</th><th>Sell on</th><th class=num>Spread now</th><th>Open</th></tr>'+tk.map(t=>`<tr><td>${esc(t.symbol||'')}</td><td class=num>${t.n_pools}</td><td>${esc(t.buy_dex||'—')}</td><td>${esc(t.sell_dex||'—')}</td><td class="num ${t.spread_pct!=null&&t.spread_pct>=ar.min_spread_pct?'pos':'muted'}">${t.spread_pct!=null?t.spread_pct.toFixed(3)+'%':'—'}</td><td>${t.open?'<span class=pos>yes</span>':''}</td></tr>`).join(''):'';
      const lb=ar.leaderboard||[];
      $('#arbboard').innerHTML=lb.length?'<tr><th>Who takes the spreads (sender)</th><th class=num>Wins</th><th class=num>Both pools in one tx</th><th>Tokens</th><th>Contract</th></tr>'+lb.map(r=>`<tr><td class=muted>${esc(r.from)}</td><td class=num>${r.count}</td><td class=num>${r.both_pools}</td><td>${esc((r.tokens||[]).join(', '))}</td><td class=muted>${esc((r.contracts||[]).join(', '))}</td></tr>`).join(''):'<tr><td class=muted>No attributed spread yet</td></tr>';
      const ak={spread_open:['symbol','block','spread_pct','buy_dex','sell_dex','seen_latency_s'],spread_close:['symbol','open_block','close_block','blocks_open','seconds_open','max_spread_pct','reason'],attribution:['symbol','open_block','close_block','n_candidates','our_first_sight_latency_s'],note:['message','tokens','pools']};
      const fa=(e)=>{const ks=ak[e.kind]||Object.keys(e).filter(k=>k!=='ts'&&k!=='kind');let out=ks.filter(k=>e[k]!=null).map(k=>`<b>${k}</b>=${typeof e[k]==='number'?(Number.isInteger(e[k])?e[k]:e[k].toFixed(3)):esc(e[k])}`);if(e.kind==='attribution'&&e.winner)out.push(`<b>winner</b>=${esc(e.winner.from||'?')} → ${esc(e.winner.to||'?')} (tx #${e.winner.tx_index}, ${e.winner.priority_fee_gwei!=null?e.winner.priority_fee_gwei.toFixed(4)+' gwei':'—'})`);return out.join(' · ')};
      $('#arbevents').innerHTML=ae.length?'<tr><th>Time</th><th>Kind</th><th>Details</th></tr>'+ae.map(e=>`<tr><td>${new Date(e.ts).toLocaleString()}</td><td>${e.kind}</td><td class=muted style="white-space:normal">${fa(e)}</td></tr>`).join(''):'<tr><td class=muted>No events yet</td></tr>';
    }
  }catch(err){$('#arbsum').textContent='· '+err}
  const lg=await j('/api/log?lines=300');
  $('#logfile').textContent=lg.file?'· '+lg.file:'';
  const pre=$('#log');pre.innerHTML=lg.lines.map(l=>{const m=l.match(/ (ERROR|WARNING) /);return `<span class="${m?'l-'+m[1]:''}">${esc(l)}</span>`}).join('\n');pre.scrollTop=pre.scrollHeight;
  const errs=st.errors||[];$('#errsec').hidden=!errs.length;$('#errs').innerHTML=errs.map(esc).join('<br>');
}
async function setMode(mode){
  const msg={pause:'Pause trading? No new trades; open positions stay open.',flatten:'CLOSE ALL POSITIONS on both venues and pause? The bot picks this up within ~30 s (longer if a rebalance is in progress).',run:'Resume trading?'}[mode];
  if(!confirm(msg))return;
  const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
  if(!r.ok){alert('failed: '+await r.text());return}
  refresh();
}
async function renderControl(){
  const c=await j('/api/control');const el=$('#mode');el.className='mode '+c.mode;
  el.textContent={run:'TRADING',pause:'PAUSED',flatten:'FLATTENING…'}[c.mode]+(c.note?' · '+c.note:'');
  el.title=(c.requested_at?'since '+new Date(c.requested_at).toLocaleString()+' by '+c.by:'');
  $('#btn-pause').disabled=c.mode!=='run';$('#btn-flatten').disabled=c.mode==='flatten';$('#btn-run').disabled=c.mode==='run';
}
async function setArbMode(mode){
  if(!confirm(mode==='pause'?'Pause the on-chain detector?':'Resume the on-chain detector?'))return;
  const r=await fetch('/api/arb/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
  if(!r.ok){alert('failed: '+await r.text());return}
  refresh();
}
const _refresh=refresh;refresh=async()=>{await renderControl();await _refresh()};
refresh();setInterval(refresh,30000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "DexPerpDashboard/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/status":
                self._json(read_status())
            elif url.path == "/api/trades":
                self._json(parse_trades(limit=int(qs.get("limit", ["200"])[0])))
            elif url.path == "/api/log":
                self._json(tail_log(lines=int(qs.get("lines", ["300"])[0])))
            elif url.path == "/api/control":
                self._json(read_control())
            elif url.path == "/api/arb/status":
                self._json(read_json(ARB_LOGS_DIR / "arb_status.json", f"arb_status.json not found in {ARB_LOGS_DIR} - is the detector running?"))
            elif url.path == "/api/arb/events":
                self._json(read_jsonl_tail(ARB_LOGS_DIR / "arb_events.jsonl", int(qs.get("limit", ["50"])[0])))
            elif url.path == "/api/arb/control":
                self._json(read_arb_control())
            elif url.path == "/api/decisions":
                self._json({"summary": summarize(), "events": read_events(limit=int(qs.get("limit", ["100"])[0]))})
            elif url.path == "/api/health":
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # keep the server alive on any handler error
            self._json({"error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path not in ("/api/control", "/api/arb/control"):
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            mode = str(payload.get("mode", ""))
            client = self.client_address[0]
            if url.path == "/api/arb/control":
                state = write_arb_control(mode, by=f"dashboard@{client}")
            else:
                state = write_control(mode, by=f"dashboard@{client}")
            print(f"control -> {mode} (from {client})", flush=True)
            self._json(state)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        if os.getenv("DASHBOARD_VERBOSE"):
            super().log_message(fmt, *args)


def main() -> int:
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Dashboard listening on http://{host}:{port}/ (logs dir: {LOGS_DIR.resolve()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
