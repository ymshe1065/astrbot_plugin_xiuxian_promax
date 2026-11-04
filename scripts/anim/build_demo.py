# -*- coding: utf-8 -*-
"""生成动作集演示页 webchat_room/static/anim/acts.html（单文件、零外部请求）。

页面里是一个 480×72 的横版场景（黄昏天空 / 远山 / 松林 / 地面），
底下七个按钮切换动作，动作数据由 acts.py 生成后内联进页面。

用法::

    python scripts/anim/build_demo.py            # 只生成页面
    python scripts/anim/build_demo.py --shots    # 顺带逐帧截图并拼一览图（需 Chrome）

--shots 会把每个动作的每一帧在固定时刻截下来，拼成 gen/grid_acts.png，
用于肉眼检查帧间差异——静态定格看帧，比在页面里盯着看更容易发现问题。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acts  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # 插件仓库根
OUT_HTML = os.path.join(ROOT, 'webchat_room', 'static', 'anim', 'acts.html')

CHROME_CANDIDATES = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    '/usr/bin/google-chrome',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
]

TPL = r'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>修仙小人 · 动作集</title>
<style>
  *{box-sizing:border-box}
  html,body{margin:0;min-height:100%;background:#0d1117;color:#c9d6e2;
    font-family:ui-monospace,"SF Mono",Menlo,monospace;display:flex;flex-direction:column;
    align-items:center;justify-content:center;padding:20px;gap:14px}
  #wrap{position:relative;line-height:0;border-radius:6px;overflow:hidden;
    box-shadow:0 10px 48px rgba(0,0,0,.8)}
  canvas{display:block;image-rendering:pixelated}
  #hud{font-size:12px;letter-spacing:.08em;color:#7d8fa3}
  #acts{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;max-width:780px}
  button{background:#18212c;color:#93a8bd;border:1px solid #27333f;padding:7px 15px;
    border-radius:5px;font:inherit;font-size:12.5px;cursor:pointer;transition:.15s;letter-spacing:.06em}
  button:hover{background:#22303d;color:#d6e6f4;border-color:#3a4b5c}
  button.on{background:#1d3b3a;color:#8fe8d2;border-color:#37786e}
</style></head><body>
<div id="wrap"><canvas id="cv"></canvas></div>
<div id="acts"></div>
<div id="hud"><span id="info"></span></div>
<script>
__ACTS__
const LW=480, LH=72, S=3;
const cv=document.getElementById('cv'), ctx=cv.getContext('2d');
cv.width=LW*S; cv.height=LH*S; cv.style.width=LW*S+'px'; cv.style.height=LH*S+'px';
ctx.imageSmoothingEnabled=false;
const GY=58, HERO_X=LW*0.36;
const SW=48, SH=48, FOOT=44;

const IMG={};
for(const k in SPR_B64) IMG[k]=SPR_B64[k].map(b=>{const i=new Image(); i.src='data:image/png;base64,'+b; return i;});

const META={
  idle:   {fps:5,  name:'待机'},
  walk:   {fps:10, name:'走动'},
  float:  {fps:8,  name:'漂浮打坐'},
  alchemy:{fps:7,  name:'炼药'},
  slash:  {fps:12, name:'御剑'},
  look:   {fps:5,  name:'张望'},
  cast:   {fps:8,  name:'运功'},
};
let cur='idle', fi=0, acc=0, last=performance.now(), scroll=0, frozen=false;
const Q=new URLSearchParams(location.search);
if(Q.get('act')) cur=Q.get('act');
if(Q.get('f')!==null){ frozen=true; fi=+Q.get('f'); }

function R(x,y,w,h,c){ctx.fillStyle=c;ctx.fillRect(x|0,y|0,w|0,h|0);}

function sky(){
  const g=ctx.createLinearGradient(0,0,0,GY);
  g.addColorStop(0,'#101c26'); g.addColorStop(.34,'#1d3641');
  g.addColorStop(.62,'#3b5f60'); g.addColorStop(.84,'#7c8a6c'); g.addColorStop(1,'#d8ac72');
  ctx.fillStyle=g; ctx.fillRect(0,0,LW,GY);
  for(let i=0;i<24;i++){
    const x=(i*97+13)%LW, y=(i*37+5)%32;
    if(y<30) R(x,y,1,1, i%5?'rgba(200,225,240,.4)':'rgba(255,240,210,.7)');
  }
}
function ridge(off, baseY, amp, f, col){
  ctx.fillStyle=col;
  for(let x=0;x<LW;x++){
    const u=(x+off)*f;
    let y=baseY - Math.sin(u)*amp - Math.sin(u*2.31+1.7)*amp*.42 - Math.sin(u*.53+.4)*amp*.7;
    ctx.fillRect(x,y|0,1,GY+4-(y|0));
  }
}
function pines(off, baseY, h, col, step){
  ctx.fillStyle=col;
  for(let k=-1;k<LW/step+2;k++){
    const bx=Math.round(k*step - off%step);
    if(bx<-step||bx>LW+step) continue;
    const jitter=(Math.sin(k*3.7)*2)|0, top=baseY-h-jitter;
    R(bx,top,1,h,col);
    for(let t=0;t<3;t++){
      const cy=top+t*4, w=5-t;
      R(bx-w,cy,w*2+1,1,col); R(bx-w+1,cy+1,w*2-1,1,col);
    }
  }
}
function ground(){
  R(0,GY,LW,LH-GY,'#3a3128'); R(0,GY,LW,2,'#5d4b37'); R(0,GY+2,LW,1,'#463a2c');
  for(let i=0;i<LW;i+=2){
    const xx=(((i-scroll)%(LW+8))+LW+8)%(LW+8)-4, gh=1+((i*7)%3);
    R(xx,GY-gh,1,gh, i%6?'#2f4a33':'#3d5c40');
  }
  for(let i=0;i<14;i++){
    const xx=(((i*41-scroll)%(LW+60))+LW+60)%(LW+60)-30;
    R(xx,GY+3+((i*13)%7),2+(i%3),1+(i%2), i%3?'#4a3f33':'#5a4c3d');
  }
}
function hero(){
  const m=META[cur], arr=IMG[cur];
  const img=arr[fi%arr.length];
  if(!img||!img.complete) return;
  ctx.fillStyle='rgba(0,0,0,.26)';
  ctx.beginPath(); ctx.ellipse(HERO_X, GY+1, 8, 2, 0,0,Math.PI*2); ctx.fill();
  ctx.drawImage(img, Math.round(HERO_X-SW/2), Math.round(GY-FOOT));
}
function loop(now){
  const dt=Math.min(.05,(now-last)/1000); last=now;
  acc+=dt;
  const m=META[cur];
  if(!frozen && acc>1/m.fps){ acc=0; fi++; }
  scroll+=dt*(cur==='walk'?36:cur==='slash'?10:0);
  ctx.setTransform(S,0,0,S,0,0);
  ctx.clearRect(0,0,LW,LH);
  sky();
  ridge(scroll*.12, GY-16, 9, .011, '#233a44');
  ridge(scroll*.22, GY-9, 7, .019, '#1a2b34');
  pines(scroll*.5, GY, 14, '#14232a', 26);
  ground();
  hero();
  document.getElementById('info').textContent = m.name+' · '+IMG[cur].length+'帧 · '+m.fps+'fps';
  requestAnimationFrame(loop);
}
function buildUI(){
  const box=document.getElementById('acts');
  for(const k in META){
    const b=document.createElement('button');
    b.textContent=META[k].name;
    b.onclick=()=>{cur=k; fi=0; acc=0;
      [...box.children].forEach(c=>c.classList.remove('on')); b.classList.add('on');};
    if(k===cur) b.classList.add('on');
    box.appendChild(b);
  }
}
buildUI();
requestAnimationFrame(loop);
</script></body></html>'''


def build_html() -> str:
    html = TPL.replace('__ACTS__', acts.make_js())
    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    with open(OUT_HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    return OUT_HTML


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def shots():
    """逐帧定格截图并拼成一览图，用于检查帧间差异"""
    chrome = find_chrome()
    if not chrome:
        raise SystemExit('没找到 Chrome/Edge，--shots 需要浏览器无头截图')
    gen = os.path.join(HERE, 'gen')
    os.makedirs(gen, exist_ok=True)
    url = 'file:///' + OUT_HTML.replace('\\', '/')
    for k, frames in acts.ACTS.items():
        for f in range(len(frames)):
            png = os.path.join(gen, 'f_%s_%d.png' % (k, f))
            subprocess.run(
                [chrome, '--headless=new', '--disable-gpu', '--hide-scrollbars',
                 '--force-device-scale-factor=1', '--window-size=1460,246',
                 '--virtual-time-budget=1200', '--screenshot=' + png,
                 '%s?act=%s&f=%d' % (url, k, f)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    from PIL import Image, ImageDraw
    X0, X1, Y0, Y1 = 415, 655, 52, 246
    rows = []
    for k in acts.ACTS:
        ims = [Image.open(os.path.join(gen, 'f_%s_%d.png' % (k, f))).convert('RGB')
               .crop((X0, Y0, X1, Y1)) for f in range(len(acts.ACTS[k]))]
        rows.append((k, ims))
    w1, h1 = X1 - X0, Y1 - Y0
    maxn = max(len(v) for _, v in rows)
    sheet = Image.new('RGB', (96 + maxn * (w1 + 4), 18 + len(rows) * (h1 + 20)), (16, 19, 24))
    d = ImageDraw.Draw(sheet)
    y = 18
    for k, ims in rows:
        d.text((6, y + 8), k, fill=(150, 195, 215))
        for i, im in enumerate(ims):
            sheet.paste(im, (96 + i * (w1 + 4), y + 4))
        y += h1 + 20
    p = os.path.join(gen, 'grid_acts.png')
    sheet.save(p)
    return p


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--shots', action='store_true', help='顺带逐帧截图拼一览图')
    a = ap.parse_args()
    print('生成', build_html())
    if a.shots:
        print('一览图', shots())
