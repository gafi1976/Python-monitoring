"""
web_server.py  —  Flask веб-сервер для просмотра карты сети в браузере
GET /           → интерактивная HTML-карта
GET /api/map    → JSON: устройства + соединения + метки
GET /api/stats  → JSON: счётчики online/offline/unknown
"""
from __future__ import annotations
import threading, time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from main import NetworkMapApp

try:
    from flask import Flask, jsonify, Response
    FLASK_OK = True
except ImportError:
    FLASK_OK = False

# ── Singleton ────────────────────────────────────────────────────────────────
_server_instance: Optional["NetMapWebServer"] = None

def get_or_create(app, host="0.0.0.0", port=5050):
    global _server_instance
    if _server_instance and _server_instance.running:
        return _server_instance
    _server_instance = NetMapWebServer(app, host, port)
    return _server_instance

def stop_server():
    global _server_instance
    if _server_instance:
        _server_instance.stop()
        _server_instance = None


# ── Сервер ───────────────────────────────────────────────────────────────────
class NetMapWebServer:
    def __init__(self, app, host, port):
        self.app     = app
        self.host    = host
        self.port    = port
        self.running = False
        self._thread = None
        self._flask  = None

    def start(self):
        if not FLASK_OK:
            return False, "Flask не установлен: pip install flask"
        if self.running:
            return True, f"http://localhost:{self.port}"
        self._flask = self._build_app()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.running = True
        self._thread.start()
        return True, f"http://localhost:{self.port}"

    def stop(self):
        self.running = False

    def _run(self):
        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self._flask.run(host=self.host, port=self.port,
                        debug=False, use_reloader=False, threaded=True)

    # ── Routes ────────────────────────────────────────────────────────────────
    def _build_app(self):
        app = Flask(__name__)
        app.json.ensure_ascii = False
        srv = self

        @app.route("/")
        def index():
            return Response(srv._html(), mimetype="text/html; charset=utf-8")

        @app.route("/api/map")
        def api_map():
            return jsonify(srv._map_data())

        @app.route("/api/stats")
        def api_stats():
            return jsonify(srv._stats())

        return app

    # ── Data ──────────────────────────────────────────────────────────────────
    def _map_data(self):
        tab = self.app.current_tab
        if not tab:
            return {"devices": [], "connections": [], "labels": []}

        devices = []
        for dev_id, dev in tab.devices.items():
            devices.append({
                "id":           dev_id,
                "name":         dev.name,
                "ip":           dev.ip,
                "dtype":        dev.dtype,
                "x":            float(dev.x),
                "y":            float(dev.y),
                "status":       dev.status.value,
                "latency":      dev.latency,
                "last_checked": dev.last_checked,
                "location":     getattr(dev, "location", ""),
                "snmp_info":    getattr(dev, "snmp_last_info", None) or {},
            })

        connections = [{"from": a, "to": b} for a, b in tab.connections]

        labels = []
        for (id1, id2), slots in self.app.conn_labels._labels.items():
            for s in slots:
                labels.append({
                    "from":  id1, "to": id2,
                    "label": s.get("oid_label", ""),
                    "value": s.get("_last_val"),
                    "unit":  s.get("unit", ""),
                })

        return {"devices": devices, "connections": connections, "labels": labels}

    def _stats(self):
        tab = self.app.current_tab
        if not tab:
            return {"total":0,"online":0,"offline":0,"unknown":0,"updated":""}
        from device import DeviceStatus
        devs = list(tab.devices.values())
        return {
            "total":    len(devs),
            "online":   sum(1 for d in devs if d.status == DeviceStatus.ONLINE),
            "offline":  sum(1 for d in devs if d.status == DeviceStatus.OFFLINE),
            "unknown":  sum(1 for d in devs if d.status not in
                           (DeviceStatus.ONLINE, DeviceStatus.OFFLINE)),
            "updated":  time.strftime("%H:%M:%S"),
        }

    # ── HTML ──────────────────────────────────────────────────────────────────
    def _html(self):
        return HTML_PAGE


# ════════════════════════════════════════════════════════════════════════════
#  HTML + CSS + JS  (отдельная строка — нет конфликтов с Python-escape)
# ════════════════════════════════════════════════════════════════════════════
HTML_PAGE = """\
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NetMap Monitor</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a2e;color:#fff;font-family:Consolas,monospace;overflow:hidden}
#bar{position:fixed;top:0;left:0;right:0;height:50px;background:#23233a;
  border-bottom:1px solid #444466;display:flex;align-items:center;
  padding:0 14px;gap:10px;z-index:10}
#bar h1{font-size:14px;color:#ffb86c;margin-right:6px}
.badge{background:#2d2d44;border:1px solid #444466;border-radius:5px;
  padding:3px 10px;font-size:12px;display:flex;align-items:center;gap:5px}
#upd{margin-left:auto;font-size:11px;color:#666}
#btn-refresh{background:#2d2d44;border:1px solid #444466;color:#ffb86c;
  padding:5px 12px;border-radius:5px;cursor:pointer;font-size:12px;
  font-family:inherit}
#btn-refresh:hover{background:#444466}
#wrap{position:fixed;top:50px;left:0;right:300px;bottom:0;overflow:hidden}
#c{display:block;cursor:grab}
#side{position:fixed;top:50px;right:0;width:300px;bottom:0;background:#23233a;
  border-left:1px solid #444466;display:flex;flex-direction:column}
#side-hdr{padding:10px 12px;font-size:11px;color:#b0b0d0;
  background:#2d2d44;border-bottom:1px solid #444466}
#list{flex:1;overflow-y:auto}
.di{padding:7px 12px;cursor:pointer;border-bottom:1px solid #2d2d44}
.di:hover{background:#2d2d44}.di.sel{background:#3a3a55}
.dn{font-size:12px;font-weight:bold}
.dip{font-size:11px;color:#b0b0d0;margin-top:2px}
#det{height:220px;border-top:1px solid #444466;padding:10px 12px;
  overflow-y:auto;font-size:12px}
#det h3{color:#ffb86c;margin-bottom:7px}
.dr{display:flex;justify-content:space-between;padding:2px 0;
  border-bottom:1px solid #2a2a3a}
.dk{color:#888}.dv{color:#fff;text-align:right;max-width:160px;word-break:break-all}
#tip{position:fixed;pointer-events:none;display:none;background:#1e2233;
  border:1px solid #ffb86c;border-radius:5px;padding:7px 11px;
  font-size:12px;z-index:20;max-width:240px;line-height:1.5}
.on{color:#50fa7b}.off{color:#ff5555}.unk{color:#8be9fd}.chk{color:#f1fa8c}
</style>
</head>
<body>
<div id="bar">
  <h1>&#9783; NetMap</h1>
  <div class="badge"><span class="on">&#9679;</span>Online:&nbsp;<b id="s-on">0</b></div>
  <div class="badge"><span class="off">&#9679;</span>Offline:&nbsp;<b id="s-off">0</b></div>
  <div class="badge"><span class="unk">&#9679;</span>Unknown:&nbsp;<b id="s-unk">0</b></div>
  <button id="btn-refresh">&#8635; Refresh</button>
  <span id="upd"></span>
</div>
<div id="wrap"><canvas id="c"></canvas></div>
<div id="side">
  <div id="side-hdr">DEVICES</div>
  <div id="list"></div>
  <div id="det"><p style="color:#555;padding:8px">Click a device</p></div>
</div>
<div id="tip"></div>
<script>
// ── Константы ────────────────────────────────────────────────────────────────
var SC = {
  'Online':      '#50fa7b',
  'Offline':     '#ff5555',
  '\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u043e': '#8be9fd',
  '\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430...':           '#f1fa8c'
};
var DTYPE = {
  router:'R',switch:'SW',server:'SRV',pc:'PC',
  printer:'PR',camera:'CAM',phone:'TEL',ups:'UPS',other:'?'
};

// ── Состояние ────────────────────────────────────────────────────────────────
var data    = {devices:[], connections:[], labels:[]};
var selId   = null;
var ox=0, oy=0;
var drag=false, dragX=0, dragY=0, ox0=0, oy0=0;
var centered = false;

// ── Canvas ───────────────────────────────────────────────────────────────────
var wrap   = document.getElementById('wrap');
var canvas = document.getElementById('c');
var ctx    = canvas.getContext('2d');

function resize() {
  canvas.width  = wrap.clientWidth;
  canvas.height = wrap.clientHeight;
  draw();
}

// ── Центрирование при первой загрузке ────────────────────────────────────────
function centerMap() {
  if (!data.devices.length) return;
  var xs = data.devices.map(function(d){return d.x;});
  var ys = data.devices.map(function(d){return d.y;});
  var cx = (Math.min.apply(null,xs) + Math.max.apply(null,xs)) / 2;
  var cy = (Math.min.apply(null,ys) + Math.max.apply(null,ys)) / 2;
  ox = canvas.width  / 2 - cx;
  oy = canvas.height / 2 - cy;
}

// ── Рисование ────────────────────────────────────────────────────────────────
function draw() {
  var w = canvas.width, h = canvas.height;
  if (w === 0 || h === 0) return;
  ctx.clearRect(0, 0, w, h);

  // Сетка
  ctx.strokeStyle = '#2d2d44'; ctx.lineWidth = 1;
  var s = 40;
  for (var x = (ox%s+s)%s; x < w; x += s) {
    ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,h); ctx.stroke();
  }
  for (var y = (oy%s+s)%s; y < h; y += s) {
    ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(w,y); ctx.stroke();
  }

  // Словарь устройств
  var dm = {};
  data.devices.forEach(function(d){ dm[d.id] = d; });

  // Соединения
  data.connections.forEach(function(c) {
    var d1 = dm[c.from], d2 = dm[c.to];
    if (!d1 || !d2) return;
    var x1=d1.x+ox, y1=d1.y+oy, x2=d2.x+ox, y2=d2.y+oy;
    var both = d1.status==='Online' && d2.status==='Online';
    ctx.beginPath();
    ctx.strokeStyle = both ? '#50fa7b' : '#6272a4';
    ctx.lineWidth   = both ? 2 : 1;
    ctx.setLineDash(both ? [] : [5,5]);
    ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
    ctx.setLineDash([]);

    // Метки на линии
    var mx=(x1+x2)/2, my=(y1+y2)/2;
    var ll = data.labels.filter(function(l){
      return (l.from===c.from&&l.to===c.to)||(l.from===c.to&&l.to===c.from);
    });
    if (ll.length) {
      var lines = ll.map(function(l){
        var sh = l.label.replace(/ \\[.*?\\]$/,'');
        return sh + ': ' + (l.value!=null ? l.value+(l.unit?' '+l.unit:'') : '...');
      });
      drawBox(mx, my, lines);
    }
  });

  // Устройства
  data.devices.forEach(function(d) {
    var x=d.x+ox, y=d.y+oy, sz=28;
    var sc = SC[d.status] || '#8be9fd';
    var isSel = d.id === selId;

    if (isSel) {
      ctx.beginPath(); ctx.arc(x,y,sz+7,0,Math.PI*2);
      ctx.strokeStyle='#ffb86c'; ctx.lineWidth=2; ctx.stroke();
    }
    ctx.beginPath(); ctx.arc(x,y,sz,0,Math.PI*2);
    ctx.fillStyle='#2d2d44'; ctx.fill();
    ctx.strokeStyle=sc; ctx.lineWidth=isSel?3:2; ctx.stroke();

    // Тип устройства
    ctx.font = 'bold 9px Consolas';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillStyle = sc;
    ctx.fillText(DTYPE[d.dtype]||'?', x, y-3);

    // Индикатор статуса
    ctx.beginPath(); ctx.arc(x+sz-7, y-sz+5, 5, 0, Math.PI*2);
    ctx.fillStyle=sc; ctx.fill();
    ctx.strokeStyle='#1a1a2e'; ctx.lineWidth=1.5; ctx.stroke();

    // Latency
    if (d.status==='Online' && d.latency!=null) {
      var lc = d.latency<50 ? '#50fa7b' : d.latency<150 ? '#f1fa8c' : '#ff5555';
      ctx.font='8px Consolas'; ctx.fillStyle=lc;
      ctx.fillText(d.latency.toFixed(0)+'ms', x, y+14);
    }

    // Имя и IP
    ctx.font='bold 10px Consolas'; ctx.fillStyle='#ffffff';
    ctx.fillText(d.name, x, y+sz+12);
    ctx.font='9px Consolas'; ctx.fillStyle='#b0b0d0';
    ctx.fillText(d.ip, x, y+sz+23);
  });
}

function drawBox(mx, my, lines) {
  ctx.font = '9px Consolas';
  var lh=14, px=6, py=3;
  var mw = 0;
  lines.forEach(function(l){ var w=ctx.measureText(l).width; if(w>mw)mw=w; });
  var bw=mw+px*2, bh=lines.length*lh+py*2;
  var bx=mx-bw/2, by=my-bh/2;
  ctx.fillStyle='#1a2233'; ctx.strokeStyle='#444466'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.rect(bx,by,bw,bh); ctx.fill(); ctx.stroke();
  ctx.fillStyle='#ffb86c'; ctx.textAlign='center'; ctx.textBaseline='middle';
  lines.forEach(function(l,i){
    ctx.fillText(l, mx, by+py+(i+0.5)*lh);
  });
}

// ── Mouse events ─────────────────────────────────────────────────────────────
canvas.addEventListener('mousedown', function(e){
  drag=true; canvas.style.cursor='grabbing';
  dragX=e.clientX; dragY=e.clientY; ox0=ox; oy0=oy;
});
canvas.addEventListener('mousemove', function(e){
  if (drag) {
    ox = ox0 + (e.clientX-dragX);
    oy = oy0 + (e.clientY-dragY);
    draw();
  }
  showTip(e);
});
canvas.addEventListener('mouseup', function(e){
  var moved = Math.abs(e.clientX-dragX)>4 || Math.abs(e.clientY-dragY)>4;
  drag = false; canvas.style.cursor='grab';
  if (!moved) handleClick(e);
});
canvas.addEventListener('mouseleave', function(){
  drag=false;
  document.getElementById('tip').style.display='none';
});

function devAt(cx,cy) {
  for (var i=0; i<data.devices.length; i++) {
    var d=data.devices[i];
    var dx=d.x+ox-cx, dy=d.y+oy-cy;
    if (Math.sqrt(dx*dx+dy*dy) <= 30) return d;
  }
  return null;
}

function showTip(e) {
  var tip = document.getElementById('tip');
  var d = devAt(e.offsetX, e.offsetY);
  if (d) {
    var sc = SC[d.status]||'#8be9fd';
    var lines = [
      '<b>'+d.name+'</b>',
      d.ip,
      'Status: <span style="color:'+sc+'">'+d.status+'</span>'
    ];
    if (d.latency!=null) lines.push('Ping: '+d.latency.toFixed(1)+' ms');
    if (d.last_checked)  lines.push(d.last_checked);
    tip.innerHTML = lines.join('<br>');
    tip.style.display = 'block';
    tip.style.left = (e.clientX+14)+'px';
    tip.style.top  = (e.clientY-10)+'px';
  } else {
    tip.style.display = 'none';
  }
}

function handleClick(e) {
  var d = devAt(e.offsetX, e.offsetY);
  selId = d ? d.id : null;
  draw();
  updateList();
  showDetail(d);
}

// ── Боковая панель ────────────────────────────────────────────────────────────
function showDetail(d) {
  var el = document.getElementById('det');
  if (!d) { el.innerHTML='<p style="color:#555;padding:8px">Click a device</p>'; return; }
  var sc = SC[d.status]||'#8be9fd';
  var h = '<h3>'+d.name+'</h3>';
  var rows = [
    ['IP', d.ip],
    ['Status', '<span style="color:'+sc+'">'+d.status+'</span>'],
    ['Type', d.dtype],
    ['Ping', d.latency!=null ? d.latency.toFixed(1)+' ms' : '&mdash;'],
    ['Checked', d.last_checked||'&mdash;'],
    ['Location', d.location||'&mdash;']
  ];
  rows.forEach(function(r){
    h += '<div class="dr"><span class="dk">'+r[0]+'</span>'
       + '<span class="dv">'+r[1]+'</span></div>';
  });
  if (d.snmp_info && Object.keys(d.snmp_info).length) {
    h += '<div style="color:#264f78;padding:5px 0 2px;font-weight:bold">&#9135; SNMP &#9135;</div>';
    Object.keys(d.snmp_info).forEach(function(k){
      h += '<div class="dr"><span class="dk">'+k+'</span>'
         + '<span class="dv" style="color:#ffb454">'+d.snmp_info[k]+'</span></div>';
    });
  }
  el.innerHTML = h;
}

function updateList() {
  var html = '';
  data.devices.forEach(function(d){
    var sc  = SC[d.status]||'#8be9fd';
    var lat = d.latency!=null ? ' '+d.latency.toFixed(0)+'ms' : '';
    html += '<div class="di'+(d.id===selId?' sel':'')+'" '
          + 'onclick="clickDev(\''+d.id+'\')">'
          + '<div class="dn"><span style="color:'+sc+'">&#9679;</span> '+d.name
          + '<span style="color:'+sc+';font-size:10px">'+lat+'</span></div>'
          + '<div class="dip">'+d.ip+' &mdash; '+d.dtype+'</div>'
          + '</div>';
  });
  document.getElementById('list').innerHTML = html;
}

function clickDev(id) {
  selId = id;
  var d = null;
  for (var i=0; i<data.devices.length; i++) {
    if (data.devices[i].id===id) { d=data.devices[i]; break; }
  }
  if (d) { ox=canvas.width/2-d.x; oy=canvas.height/2-d.y; }
  draw(); updateList(); showDetail(d);
}

// ── Загрузка данных ────────────────────────────────────────────────────────
function loadData() {
  var p1 = fetch('/api/map').then(function(r){ return r.json(); });
  var p2 = fetch('/api/stats').then(function(r){ return r.json(); });
  Promise.all([p1, p2]).then(function(res){
    var map = res[0], st = res[1];
    var first = (data.devices.length===0 && map.devices.length>0);
    data = map;
    if (first) { centerMap(); }
    document.getElementById('s-on').textContent  = st.online||0;
    document.getElementById('s-off').textContent = st.offline||0;
    document.getElementById('s-unk').textContent = st.unknown||0;
    document.getElementById('upd').textContent   = 'updated '+st.updated;
    updateList();
    var sel = null;
    for (var i=0; i<data.devices.length; i++) {
      if (data.devices[i].id===selId) { sel=data.devices[i]; break; }
    }
    showDetail(sel);
    draw();
  }).catch(function(e){ console.error('loadData:', e); });
}

document.getElementById('btn-refresh').addEventListener('click', loadData);
window.addEventListener('resize', resize);

// Старт: ждём полной отрисовки страницы
window.addEventListener('load', function(){
  resize();
  loadData();
  setInterval(loadData, 5000);
});
</script>
</body>
</html>
"""
