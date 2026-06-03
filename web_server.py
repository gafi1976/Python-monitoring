"""
web_server.py — Flask веб-сервер для просмотра карты сети в браузере
====================================================================
Запускается из NetworkMapApp (кнопка «🌐 Веб») и отдаёт:
  GET /          → HTML-страница с интерактивной картой
  GET /api/map   → JSON: устройства + соединения + метки
  GET /api/stats → JSON: счётчики online/offline/unknown
  GET /api/history/<dev_id> → JSON: последние SNMP-метрики из БД
"""
from __future__ import annotations
import json
import threading
import time
import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from main import NetworkMapApp

try:
    from flask import Flask, jsonify, Response
    FLASK_OK = True
except ImportError:
    FLASK_OK = False

# ─── Singleton сервер ────────────────────────────────────────────────────────

_server_instance: Optional["NetMapWebServer"] = None


def get_or_create(app: "NetworkMapApp", host: str = "0.0.0.0",
                  port: int = 5050) -> "NetMapWebServer":
    global _server_instance
    if _server_instance and _server_instance.running:
        return _server_instance
    _server_instance = NetMapWebServer(app, host, port)
    _server_instance.start()
    return _server_instance


def stop_server():
    global _server_instance
    if _server_instance:
        _server_instance.stop()
        _server_instance = None



# ─── Основной класс ──────────────────────────────────────────────────────────

class NetMapWebServer:
    def __init__(self, app: "NetworkMapApp", host: str, port: int):
        self.app   = app
        self.host  = host
        self.port  = port
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._flask: Optional[Flask] = None

    # ── Запуск / остановка ───────────────────────────────────────────────────

    def start(self):
        if not FLASK_OK:
            return False, "Flask не установлен. Выполните: pip install flask"
        if self.running:
            return True, f"http://{self.host}:{self.port}"
        self._flask = self._build_flask()
        self._thread = threading.Thread(
            target=self._run_flask, daemon=True, name="FlaskWebServer"
        )
        self.running = True
        self._thread.start()
        return True, f"http://localhost:{self.port}"

    def stop(self):
        self.running = False
        # Flask dev server нельзя остановить чисто — завершим через werkzeug
        try:
            import requests
            requests.get(f"http://127.0.0.1:{self.port}/_shutdown", timeout=1)
        except Exception:
            pass

    def _run_flask(self):
        import logging
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)
        self._flask.run(host=self.host, port=self.port,
                        debug=False, use_reloader=False, threaded=True)

    # ── Flask routes ─────────────────────────────────────────────────────────

    def _build_flask(self) -> Flask:
        flask_app = Flask(__name__)
        flask_app.json.ensure_ascii = False
        srv = self  # захват self для замыканий

        @flask_app.route("/")
        def index():
            return Response(srv._html(), mimetype="text/html")

        @flask_app.route("/api/map")
        def api_map():
            return jsonify(srv._map_data())

        @flask_app.route("/api/stats")
        def api_stats():
            return jsonify(srv._stats())

        @flask_app.route("/api/history/<dev_id>")
        def api_history(dev_id):
            return jsonify(srv._history(dev_id))

        @flask_app.route("/_shutdown")
        def shutdown():
            func = flask_app.environ.get("werkzeug.server.shutdown")
            if func:
                func()
            return "bye"

        return flask_app


    # ── Данные для API ────────────────────────────────────────────────────────

    def _map_data(self) -> dict:
        tab = self.app.current_tab
        if not tab:
            return {"devices": [], "connections": [], "labels": []}

        # canvas_offset из Tkinter — нужно вычесть чтобы получить «мировые» координаты
        cox = tab.canvas_offset[0] if tab.canvas_offset else 0
        coy = tab.canvas_offset[1] if tab.canvas_offset else 0

        devices = []
        for dev_id, dev in tab.devices.items():
            snmp = getattr(dev, "snmp_last_info", None) or {}
            devices.append({
                "id":           dev_id,
                "name":         dev.name,
                "ip":           dev.ip,
                "dtype":        dev.dtype,
                # Вычитаем offset — возвращаем реальные координаты на карте
                "x":            dev.x + cox,
                "y":            dev.y + coy,
                "status":       dev.status.value,
                "latency":      dev.latency,
                "last_checked": dev.last_checked,
                "description":  dev.description,
                "location":     dev.location,
                "snmp_info":    snmp,
            })

        connections = [
            {"from": a, "to": b} for a, b in tab.connections
        ]

        # Метки на линиях
        labels = []
        for (id1, id2), slots in self.app.conn_labels._labels.items():
            for s in slots:
                labels.append({
                    "from":  id1,
                    "to":    id2,
                    "label": s.get("oid_label", ""),
                    "value": s.get("_last_val"),
                    "error": s.get("_error"),
                    "unit":  s.get("unit", ""),
                    "ts":    s.get("_last_ts", 0),
                })

        return {"devices": devices, "connections": connections, "labels": labels}

    def _stats(self) -> dict:
        tab = self.app.current_tab
        if not tab:
            return {"total": 0, "online": 0, "offline": 0, "unknown": 0}
        from device import DeviceStatus
        devs = list(tab.devices.values())
        return {
            "total":   len(devs),
            "online":  sum(1 for d in devs if d.status == DeviceStatus.ONLINE),
            "offline": sum(1 for d in devs if d.status == DeviceStatus.OFFLINE),
            "unknown": sum(1 for d in devs if d.status == DeviceStatus.UNKNOWN),
            "checking":sum(1 for d in devs if d.status == DeviceStatus.CHECKING),
            "updated": time.strftime("%H:%M:%S"),
        }

    def _history(self, dev_id: str) -> dict:
        try:
            import reporter
            rows = reporter.history_db.get_metrics_for_device(
                dev_id, limit=200
            )
            return {"records": [
                {"ts": r[0], "metric": r[1], "value": r[2], "numeric": r[3], "unit": r[4]}
                for r in rows
            ]}
        except Exception as e:
            return {"error": str(e), "records": []}


    # ── HTML страница ─────────────────────────────────────────────────────────

    def _html(self) -> str:
        # HTML генерируется как конкатенация — безопасно для Python unicode
        css = """
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#1a1a2e; color:#fff; font-family:Consolas,monospace; overflow:hidden; }
#toolbar { position:fixed; top:0; left:0; right:0; height:52px;
  background:#23233a; border-bottom:1px solid #444466;
  display:flex; align-items:center; padding:0 16px; gap:12px; z-index:100; }
#toolbar h1 { font-size:15px; color:#ffb86c; white-space:nowrap; }
.sep { width:1px; height:32px; background:#444466; }
.stat { background:#2d2d44; border:1px solid #444466; border-radius:6px;
        padding:4px 10px; font-size:12px; display:flex; align-items:center; gap:5px; }
#updated { margin-left:auto; font-size:11px; color:#666; }
button { background:#2d2d44; border:1px solid #444466; color:#ffb86c;
         padding:5px 14px; border-radius:5px; cursor:pointer; font-family:inherit; font-size:12px; }
button:hover { background:#444466; }
#canvas-wrap { position:fixed; top:52px; left:0; right:320px; bottom:0; overflow:hidden; }
canvas { display:block; cursor:grab; }
#side { position:fixed; top:52px; right:0; width:320px; bottom:0;
        background:#23233a; border-left:1px solid #444466;
        display:flex; flex-direction:column; overflow:hidden; }
#side-title { padding:10px 14px; font-size:12px; color:#b0b0d0;
              border-bottom:1px solid #444466; background:#2d2d44; }
#device-list { flex:1; overflow-y:auto; padding:6px 0; }
.di { padding:8px 14px; cursor:pointer; border-bottom:1px solid #2d2d44; }
.di:hover { background:#2d2d44; }  .di.sel { background:#444466; }
.dn { font-size:13px; font-weight:bold; }
.dip { font-size:11px; color:#b0b0d0; margin-top:2px; }
#detail { height:230px; border-top:1px solid #444466; padding:12px 14px;
          overflow-y:auto; font-size:12px; }
#detail h3 { color:#ffb86c; margin-bottom:8px; font-size:13px; }
.dr { display:flex; justify-content:space-between; padding:3px 0;
      border-bottom:1px solid #2d2d44; }
.dk { color:#b0b0d0; } .dv { color:#fff; max-width:170px; word-break:break-all; text-align:right; }
#tip { position:fixed; pointer-events:none; display:none;
       background:#23233a; border:1px solid #ffb86c; border-radius:6px;
       padding:8px 12px; font-size:12px; z-index:200; max-width:260px; white-space:pre; }
.on { color:#50fa7b; } .off { color:#ff5555; }
.unk { color:#8be9fd; } .chk { color:#f1fa8c; }
"""
        js = r"""
const ICONS={router:'NET',switch:'SW',server:'SRV',pc:'PC',
             printer:'PRN',camera:'CAM',phone:'TEL',ups:'UPS',other:'DEV'};
const SC={'Online':'#50fa7b','Offline':'#ff5555',
          'Неизвестно':'#8be9fd',
          'Проверка...':'#f1fa8c'};
let mapData={devices:[],connections:[],labels:[]};
let selId=null,ox=0,oy=0,drag=false,dragStart={x:0,y:0},offStart={x:0,y:0};
const wrap=document.getElementById('canvas-wrap');
const canvas=document.getElementById('c');
const ctx=canvas.getContext('2d');
function resize(){canvas.width=wrap.clientWidth;canvas.height=wrap.clientHeight;draw();}
window.addEventListener('resize',resize);
canvas.addEventListener('mousedown',e=>{
  drag=true;canvas.style.cursor='grabbing';
  dragStart={x:e.clientX,y:e.clientY};offStart={x:ox,y:oy};
});
canvas.addEventListener('mousemove',e=>{
  if(drag){ox=offStart.x+(e.clientX-dragStart.x);oy=offStart.y+(e.clientY-dragStart.y);draw();}
  hoverTip(e);
});
canvas.addEventListener('mouseup',e=>{
  if(drag&&Math.abs(e.clientX-dragStart.x)<4&&Math.abs(e.clientY-dragStart.y)<4)onClick(e);
  drag=false;canvas.style.cursor='grab';
});
canvas.addEventListener('mouseleave',()=>{
  drag=false;document.getElementById('tip').style.display='none';
});
function draw(){
  const w=canvas.width,h=canvas.height;
  ctx.clearRect(0,0,w,h);
  ctx.strokeStyle='#2d2d44';ctx.lineWidth=1;
  const s=40;
  for(let x=(ox%s)-s;x<w;x+=s){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,h);ctx.stroke();}
  for(let y=(oy%s)-s;y<h;y+=s){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();}
  const dm={};mapData.devices.forEach(d=>dm[d.id]=d);
  mapData.connections.forEach(c=>{
    const d1=dm[c.from],d2=dm[c.to];if(!d1||!d2)return;
    const x1=d1.x+ox,y1=d1.y+oy,x2=d2.x+ox,y2=d2.y+oy;
    const both=d1.status==='Online'&&d2.status==='Online';
    ctx.beginPath();
    ctx.strokeStyle=both?'#50fa7b':'#6272a4';
    ctx.lineWidth=both?2:1;
    if(!both)ctx.setLineDash([5,5]);else ctx.setLineDash([]);
    ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();ctx.setLineDash([]);
    const mx=(x1+x2)/2,my=(y1+y2)/2;
    const ll=mapData.labels.filter(l=>(l.from===c.from&&l.to===c.to)||(l.from===c.to&&l.to===c.from));
    if(ll.length){
      const lines=ll.map(l=>{
        const sh=l.label.replace(/ \[.*?\]$/,'');
        return sh+': '+(l.value!=null?l.value+(l.unit?' '+l.unit:''):'...');
      });
      drawBox(ctx,mx,my,lines);
    }
  });
  mapData.devices.forEach(d=>{
    const x=d.x+ox,y=d.y+oy,sz=32,sc=SC[d.status]||'#8be9fd',sel=d.id===selId;
    if(sel){ctx.beginPath();ctx.arc(x,y,sz+8,0,Math.PI*2);ctx.strokeStyle='#ffb86c';ctx.lineWidth=2;ctx.stroke();}
    ctx.beginPath();ctx.arc(x,y,sz,0,Math.PI*2);ctx.fillStyle='#2d2d44';ctx.fill();
    ctx.strokeStyle=sc;ctx.lineWidth=sel?3:2;ctx.stroke();
    ctx.font='bold 10px Consolas';ctx.textAlign='center';ctx.textBaseline='middle';
    ctx.fillStyle=sc;ctx.fillText(ICONS[d.dtype]||'DEV',x,y-4);
    ctx.beginPath();ctx.arc(x+sz-8,y-sz+6,6,0,Math.PI*2);
    ctx.fillStyle=sc;ctx.fill();ctx.strokeStyle='#1a1a2e';ctx.lineWidth=1.5;ctx.stroke();
    if(d.status==='Online'&&d.latency!=null){
      const lc=d.latency<50?'#50fa7b':d.latency<150?'#f1fa8c':'#ff5555';
      ctx.font='9px Consolas';ctx.fillStyle=lc;ctx.fillText(d.latency.toFixed(0)+'ms',x,y+18);
    }
    ctx.font='bold 11px Consolas';ctx.fillStyle='#fff';ctx.fillText(d.name,x,y+sz+14);
    ctx.font='10px Consolas';ctx.fillStyle='#b0b0d0';ctx.fillText(d.ip,x,y+sz+27);
  });
}
function drawBox(ctx,mx,my,lines){
  ctx.font='10px Consolas';
  const lh=15,px=6,py=4,mw=Math.max(...lines.map(l=>ctx.measureText(l).width));
  const bw=mw+px*2,bh=lines.length*lh+py*2,bx=mx-bw/2,by=my-bh/2;
  ctx.fillStyle='#1e2a3a';ctx.strokeStyle='#444466';ctx.lineWidth=1;
  ctx.beginPath();ctx.rect(bx,by,bw,bh);ctx.fill();ctx.stroke();
  ctx.fillStyle='#ffb86c';ctx.textAlign='center';ctx.textBaseline='middle';
  lines.forEach((l,i)=>ctx.fillText(l,mx,by+py+(i+0.5)*lh));
}
function devAt(cx,cy){
  return mapData.devices.find(d=>{
    const dx=d.x+ox-cx,dy=d.y+oy-cy;
    return Math.sqrt(dx*dx+dy*dy)<=34;
  });
}
function hoverTip(e){
  const tip=document.getElementById('tip'),d=devAt(e.offsetX,e.offsetY);
  if(d){
    const sc=SC[d.status]||'#8be9fd';
    let t=d.name+'
'+d.ip+'
'+d.status;
    if(d.latency!=null)t+='
Ping: '+d.latency.toFixed(1)+' ms';
    if(d.last_checked)t+='
'+d.last_checked;
    tip.innerText=t;tip.style.display='block';
    tip.style.left=(e.clientX+14)+'px';tip.style.top=(e.clientY-10)+'px';
  } else { tip.style.display='none'; }
}
function onClick(e){
  const d=devAt(e.offsetX,e.offsetY);
  selId=d?d.id:null;draw();renderList();showDetail(d||null);
  if(d){ox=canvas.width/2-d.x;oy=canvas.height/2-d.y;draw();}
}
function showDetail(d){
  const el=document.getElementById('detail');
  if(!d){el.innerHTML='<p style="color:#666">Select a device</p>';return;}
  const sc=SC[d.status]||'#8be9fd';
  let h='<h3>'+d.name+'</h3>';
  [['IP',d.ip],['Status','<span style="color:'+sc+'">'+d.status+'</span>'],
   ['Type',d.dtype],['Ping',d.latency!=null?d.latency.toFixed(1)+' ms':'—'],
   ['Checked',d.last_checked||'—'],['Location',d.location||'—'],
  ].forEach(([k,v])=>h+='<div class="dr"><span class="dk">'+k+'</span><span class="dv">'+v+'</span></div>');
  if(d.snmp_info&&Object.keys(d.snmp_info).length){
    h+='<div style="color:#264f78;padding:6px 0 2px;font-weight:bold">── SNMP ──</div>';
    Object.entries(d.snmp_info).forEach(([k,v])=>
      h+='<div class="dr"><span class="dk">'+k+'</span><span class="dv" style="color:#ffb454">'+v+'</span></div>');
  }
  el.innerHTML=h;
}
function renderList(){
  document.getElementById('device-list').innerHTML=mapData.devices.map(d=>{
    const sc=SC[d.status]||'#8be9fd',lat=d.latency!=null?' '+d.latency.toFixed(0)+'ms':'';
    return '<div class="di'+(d.id===selId?' sel':'')+'" onclick="selectDev(\'' +d.id+ '\')">'
      +'<div class="dn"><span style="color:'+sc+'">&#9679;</span> '+d.name
      +'<span style="color:'+sc+';font-size:11px">'+lat+'</span></div>'
      +'<div class="dip">'+d.ip+' &mdash; '+d.dtype+'</div></div>';
  }).join('');
}
function selectDev(id){
  selId=id;const d=mapData.devices.find(x=>x.id===id);
  draw();renderList();showDetail(d||null);
  if(d){ox=canvas.width/2-d.x;oy=canvas.height/2-d.y;draw();}
}
async function loadData(){
  try{
    const[mr,sr]=await Promise.all([fetch('/api/map'),fetch('/api/stats')]);
    mapData=await mr.json();const s=await sr.json();
    document.getElementById('s-on').textContent=s.online||0;
    document.getElementById('s-off').textContent=s.offline||0;
    document.getElementById('s-unk').textContent=(s.unknown||0)+(s.checking||0);
    document.getElementById('updated').textContent='updated: '+(s.updated||'');
    renderList();const sel=mapData.devices.find(d=>d.id===selId);
    showDetail(sel||null);draw();
  }catch(e){console.error(e);}
}
function centerMap(){
  if(!mapData.devices.length) return;
  const xs=mapData.devices.map(d=>d.x);
  const ys=mapData.devices.map(d=>d.y);
  const minX=Math.min(...xs), maxX=Math.max(...xs);
  const minY=Math.min(...ys), maxY=Math.max(...ys);
  const cx=(minX+maxX)/2, cy=(minY+maxY)/2;
  ox = canvas.width/2  - cx;
  oy = canvas.height/2 - cy;
}
async function loadData(){
  try{
    const[mr,sr]=await Promise.all([fetch('/api/map'),fetch('/api/stats')]);
    const newData=await mr.json(); const s=await sr.json();
    const firstLoad=(mapData.devices.length===0 && newData.devices.length>0);
    mapData=newData;
    if(firstLoad) centerMap();
    document.getElementById('s-on').textContent=s.online||0;
    document.getElementById('s-off').textContent=s.offline||0;
    document.getElementById('s-unk').textContent=(s.unknown||0)+(s.checking||0);
    document.getElementById('updated').textContent='updated: '+(s.updated||'');
    renderList();
    const sel=mapData.devices.find(d=>d.id===selId);
    showDetail(sel||null);
    draw();
  } catch(e){ console.error('loadData error:',e); }
}
// Ждём полной загрузки layout перед resize — иначе canvas.width = 0
window.addEventListener('load', function(){
  resize();
  loadData();
  setInterval(loadData, 5000);
});
"""
        html = (
            "<!DOCTYPE html><html lang='ru'><head>"
            "<meta charset='UTF-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>NetMap Monitor</title>"
            "<style>" + css + "</style>"
            "</head><body>"
            "<div id='toolbar'>"
            "  <h1>&#128506; NetMap Monitor</h1><div class='sep'></div>"
            "  <div class='stat'><span class='on'>&#9679;</span> Online: <b id='s-on'>0</b></div>"
            "  <div class='stat'><span class='off'>&#9679;</span> Offline: <b id='s-off'>0</b></div>"
            "  <div class='stat'><span class='unk'>&#9679;</span> Unknown: <b id='s-unk'>0</b></div>"
            "  <button onclick='loadData()'>&#8635; Refresh</button>"
            "  <span id='updated'></span>"
            "</div>"
            "<div id='canvas-wrap'><canvas id='c'></canvas></div>"
            "<div id='side'>"
            "  <div id='side-title'>DEVICES</div>"
            "  <div id='device-list'></div>"
            "  <div id='detail'><p style='color:#666'>Select a device</p></div>"
            "</div>"
            "<div id='tip'></div>"
            "<script>" + js + "</script>"
            "</body></html>"
        )
        return html
