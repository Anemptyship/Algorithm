"use strict";
const $=s=>document.querySelector(s);
const FPS=24, MAX_SEC=(81-5-6)/FPS;           // backend window: ≈2.92s per repair
const state={identity:null,dur:0,start:0,end:0,originalUrl:null,poll:null,defaults:null,refBlob:null,last:null};

function toast(msg,kind){const t=$('#toast');t.textContent=msg;t.className='toast show'+(kind?' '+kind:'');clearTimeout(t._t);t._t=setTimeout(()=>t.className='toast',2600);}
function fmt(s){return (s||0).toFixed(2)+'s';}
function hex(n){let s='';for(let i=0;i<n;i++)s+=Math.floor(Math.random()*16).toString(16);return s;}

async function loadCandidates(){
  const box=$('#cands');
  try{
    const r=await fetch('api/scail/repair/candidates',{headers:{'Accept':'application/json'}});
    if(!r.ok) throw new Error(r.status);
    const {items}=await r.json();
    if(!items.length){box.innerHTML='<div class="empty">완성된 SCAIL 영상이 없습니다.</div>';return;}
    box.innerHTML='';
    for(const it of items){
      const el=document.createElement('div');el.className='cand';el.dataset.id=it.identity;
      const when=new Date((it.updated_at||0)*1000);
      const ago=isNaN(when)?'':when.toLocaleString('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
      const img=document.createElement('img');img.loading='lazy';img.src=it.reference;img.onerror=()=>{img.style.visibility='hidden';};
      const meta=document.createElement('div');meta.className='meta';
      meta.innerHTML=`<div class="t">${(it.title||it.identity).replace(/</g,'&lt;')}${it.version?`<span class="badge">수정본 ${it.version}</span>`:''}</div><div class="d">${ago}</div>`;
      el.appendChild(img);el.appendChild(meta);
      el.onclick=()=>pick(it);
      box.appendChild(el);
    }
    preselectFromURL(items);
  }catch(e){box.innerHTML='<div class="empty">목록을 불러오지 못했습니다. 로그인(Z Studio) 상태를 확인하세요.</div>';}
}

function preselectFromURL(items){
  const q=new URLSearchParams(location.search);
  const id=q.get('id'); if(!id||state.identity) return;
  let it=items.find(x=>x.identity===id);
  if(!it) it={identity:id, video:`comfy-output/scail_user/${id}/full.mp4`, reference:`api/scail/repair/thumb/${id}`};
  pick(it);
  const row=document.querySelector(`.cand[data-id="${id}"]`); if(row) row.scrollIntoView({block:'center'});
  const t=parseFloat(q.get('t')); if(!isNaN(t)) state._seekTo=t;
}

function pick(it){
  state.identity=it.identity;state.originalUrl=it.video+(it.video.includes('?')?'&':'?')+'t='+Date.now();
  document.querySelectorAll('.cand').forEach(c=>c.classList.toggle('active',c.dataset.id===it.identity));
  $('#noPick').style.display='none';$('#editor').style.display='';
  $('#status').className='status';
  const v=$('#vid');v.src=state.originalUrl;v.load();
  state.last=null; loadDefaults(it.identity); loadVersions(it.identity);
}

async function loadDefaults(identity){
  try{
    const r=await fetch(`api/scail/repair/${identity}/defaults`);
    if(!r.ok) throw new Error(r.status);
    const d=await r.json(); if(state.identity!==identity) return;
    state.defaults=d;
    $('#jobInfo').textContent=`이 작업: ${d.steps}스텝 · DPO ${d.dpo?'켬':'끔'} · 가속 LoRA ${d.lora_strength} · shift ${d.shift} — 비워 두면 이 값을 그대로 씁니다.`;
    $('#optDpoS').placeholder=d.dpo_strength; $('#optLora').placeholder=d.lora_strength;
    $('#optShift').placeholder=d.shift; $('#optCfg').placeholder=d.cfg; $('#optPose').placeholder=d.pose_strength; $('#optTail').placeholder=d.tail;
  }catch(e){ $('#jobInfo').textContent=''; }
}

// ---- timeline ----
const tl=$('#tl');
function pxToSec(px){const r=tl.getBoundingClientRect();return Math.max(0,Math.min(state.dur,(px-r.left)/r.width*state.dur));}
function secToPct(s){return state.dur?(s/state.dur*100):0;}
function renderSel(){
  $('#region').style.left=secToPct(state.start)+'%';
  $('#region').style.width=secToPct(state.end-state.start)+'%';
  $('#hStart').style.left=secToPct(state.start)+'%';
  $('#hEnd').style.left=secToPct(state.end)+'%';
  const len=state.end-state.start;
  $('#selRange').textContent=fmt(state.start)+' ~ '+fmt(state.end);
  $('#selLen').textContent='('+len.toFixed(2)+'s'+(len>MAX_SEC+1e-6?' · 최대 '+MAX_SEC.toFixed(2)+'s 초과':'')+')';
  $('#selLen').style.color=len>MAX_SEC+1e-6?'var(--bad)':'var(--muted)';
}
function clampRange(which){
  state.start=Math.max(0,Math.min(state.start,state.dur));
  state.end=Math.max(0,Math.min(state.end,state.dur));
  if(state.end<state.start+0.08){ if(which==='start')state.start=Math.max(0,state.end-0.08); else state.end=Math.min(state.dur,state.start+0.08);}
  if(state.end-state.start>MAX_SEC){ if(which==='start')state.start=state.end-MAX_SEC; else state.end=state.start+MAX_SEC;}
  renderSel();
}
function drag(handleId,which){
  const h=$(handleId);
  h.addEventListener('pointerdown',e=>{
    e.preventDefault();h.setPointerCapture(e.pointerId);
    const move=ev=>{const s=pxToSec(ev.clientX); if(which==='start')state.start=s; else state.end=s; clampRange(which); const v=$('#vid'); v.currentTime=which==='start'?state.start:state.end;};
    const up=ev=>{h.releasePointerCapture(e.pointerId);h.removeEventListener('pointermove',move);h.removeEventListener('pointerup',up);};
    h.addEventListener('pointermove',move);h.addEventListener('pointerup',up);
  });
}
drag('#hStart','start');drag('#hEnd','end');
tl.addEventListener('pointerdown',e=>{ if(e.target.classList.contains('handle'))return; const s=pxToSec(e.clientX); $('#vid').currentTime=s; });

$('#setStart').onclick=()=>{state.start=$('#vid').currentTime;clampRange('start');};
$('#setEnd').onclick=()=>{state.end=$('#vid').currentTime;clampRange('end');};

// ---- video ----
const vid=$('#vid');
vid.addEventListener('loadedmetadata',()=>{
  state.dur=vid.duration||0;
  if(state._seekTo!=null&&!isNaN(state._seekTo)){
    const c=Math.max(0,Math.min(state.dur,state._seekTo));
    state.start=Math.max(0,c-0.5);state.end=Math.min(state.dur,c+0.5);
    try{vid.currentTime=c;}catch(e){}
    state._seekTo=null;
  }else{
    state.start=Math.max(0,state.dur/2-0.5);state.end=Math.min(state.dur,state.start+1.0);
  }
  renderSel();
  const ticks=$('#ticks');ticks.innerHTML='';
  for(let s=0;s<=state.dur;s++){const d=document.createElement('div');d.className='tick';d.style.left=secToPct(s)+'%';ticks.appendChild(d);}
});
vid.addEventListener('timeupdate',()=>{
  $('#playhead').style.left=secToPct(vid.currentTime)+'%';
  $('#clock').textContent=fmt(vid.currentTime);
  if(vid._loop&&vid.currentTime>=state.end){vid.currentTime=state.start;}
});
$('#playBtn').onclick=()=>{vid._loop=false; vid.paused?vid.play():vid.pause();};
$('#loopBtn').onclick=()=>{vid._loop=true;vid.currentTime=state.start;vid.play();};
$('#seedRand').onclick=()=>{$('#seed').value='';toast('Seed 랜덤');};


// ---- advanced options ----
const PRESETS={
  default:{res:'native',steps:'',dpo:'',dpoS:'',lora:'',shift:'',cfg:'',pose:'',tail:'',color:true},
  hands:{res:'native',steps:'10',dpo:'1',dpoS:'1.2',lora:'',shift:'',cfg:'',pose:'',tail:'',color:true},
  fast:{res:'544x960',steps:'6',dpo:'',dpoS:'',lora:'',shift:'',cfg:'',pose:'',tail:'4',color:true},
  tiny:{res:'352x640',steps:'6',dpo:'',dpoS:'',lora:'',shift:'',cfg:'',pose:'',tail:'4',color:true},
};
function applyPreset(name){
  const p=PRESETS[name]; if(!p) return;
  $('#optRes').value=p.res; $('#optSteps').value=p.steps; $('#optDpo').value=p.dpo; $('#optDpoS').value=p.dpoS;
  $('#optLora').value=p.lora; $('#optShift').value=p.shift; $('#optCfg').value=p.cfg; $('#optPose').value=p.pose;
  $('#optTail').value=p.tail; $('#optColor').checked=p.color; syncRes();
  document.querySelectorAll('#presets .pill').forEach(x=>x.classList.toggle('on',x.dataset.preset===name));
}
function syncRes(){
  const v=$('#optRes').value;
  $('#customRes').style.display=v==='custom'?'':'none';
  $('#resWarn').style.display=v==='native'?'none':'';
}
document.querySelectorAll('#presets .pill').forEach(p=>p.onclick=()=>{applyPreset(p.dataset.preset);toast('프리셋 적용: '+p.textContent);});
$('#optRes').onchange=()=>{syncRes();document.querySelectorAll('#presets .pill').forEach(x=>x.classList.remove('on'));};

function num(id){const v=$(id).value.trim();return v===''?null:Number(v);}
function collectOptions(){
  const o={};
  const res=$('#optRes').value;
  if(res==='custom'){ o.width=num('#optW'); o.height=num('#optH'); }
  else if(res!=='native'){ const [w,h]=res.split('x').map(Number); o.width=w; o.height=h; }
  if($('#optSteps').value) o.steps=Number($('#optSteps').value);
  if($('#optDpo').value!=='') o.dpo=$('#optDpo').value==='1';
  const map=[['#optDpoS','dpo_strength'],['#optLora','lora_strength'],['#optShift','shift'],['#optCfg','cfg'],['#optPose','pose_strength'],['#optTail','tail']];
  for(const [id,key] of map){ const v=num(id); if(v!==null&&!Number.isNaN(v)) o[key]=v; }
  if(!$('#optColor').checked) o.color_match=false;
  const prompt=$('#optPrompt').value.trim(); if(prompt) o.prompt=prompt;
  if(state.refBlob&&$('#refScail').checked) o.ref_to_scail=true;
  return o;
}

// ---- reference image ----
function setRef(blob){
  state.refBlob=blob;
  const img=$('#refPrev');
  if(img._url) URL.revokeObjectURL(img._url);
  if(blob){ img._url=URL.createObjectURL(blob); img.src=img._url; img.style.display='inline-block'; $('#refClear').style.display=''; }
  else { img.removeAttribute('src'); img.style.display='none'; $('#refClear').style.display='none'; $('#refScail').checked=false; }
}
$('#refFileBtn').onclick=()=>$('#refFile').click();
$('#refFile').onchange=e=>{ const f=e.target.files[0]; if(f){ setRef(f); toast('참고 이미지를 넣었어요'); } e.target.value=''; };
$('#refClear').onclick=()=>setRef(null);
$('#refCapBtn').onclick=()=>{
  const v=$('#vid'); if(!v.videoWidth){ toast('영상이 아직 준비되지 않았어요','bad'); return; }
  const c=document.createElement('canvas'); c.width=v.videoWidth; c.height=v.videoHeight;
  try{
    c.getContext('2d').drawImage(v,0,0,c.width,c.height);
    c.toBlob(b=>{ if(!b){ toast('프레임을 캡처하지 못했어요','bad'); return; } setRef(b); toast(`${fmt(v.currentTime)} 프레임을 참고 이미지로 넣었어요`,'good'); },'image/jpeg',.92);
  }catch(e){ toast('프레임을 캡처하지 못했어요','bad'); }
};

// ---- submit + poll ----
async function submitRepair(p){
  if(!state.identity) return;
  const dur=p.end-p.start;
  if(dur<=0){ toast('구간을 확인해 주세요','bad'); return; }
  const rid=hex(32);
  const body=new FormData();
  body.set('request_id',rid);
  body.set('start',p.start.toFixed(3));
  body.set('end',p.end.toFixed(3));
  body.set('note',p.note||'');
  if(p.addition) body.set('addition',p.addition);
  if(p.reuse) body.set('reuse','true');
  if(p.seed!==''&&p.seed!=null) body.set('seed',String(p.seed));
  body.set('preempt',$('#preempt').checked?'true':'false');
  const opts=p.options||{};
  if(Object.keys(opts).length) body.set('options',JSON.stringify(opts));
  if(p.refBlob) body.set('reference',p.refBlob,'reference.jpg');
  $('#submit').disabled=true; setExtendDisabled(true);
  const st=$('#status');st.className='status show';st.innerHTML='요청 접수 중…';
  try{
    const r=await fetch('api/scail/jobs/'+state.identity+'/repair',{method:'POST',body});
    const data=await r.json();
    if(!r.ok) throw new Error(typeof data.detail==='string'?data.detail:('오류 '+r.status));
    const pr=data.preempted; let pre;
    if(pr&&pr.action==='cancelled') pre='현재 생성을 멈추고 수정부터 처리합니다. (완료 구간 보존 · 끝나면 이어서 재개)';
    else if(pr&&pr.action==='waited') pre='현재 생성이 거의 끝나 기다립니다. 끝나는 즉시 수정이 돌아갑니다.';
    else pre='최우선으로 등록됐습니다.';
    toast('접수 완료 · '+pre,'good');
    pollStatus(state.identity,rid);
  }catch(e){
    st.innerHTML='<span style="color:var(--bad)">접수 실패: '+e.message+'</span>';
    $('#submit').disabled=false; setExtendDisabled(false);
  }
}

$('#submit').onclick=()=>{
  if(state.end-state.start>MAX_SEC+1e-6){toast('선택 구간이 너무 깁니다 (최대 '+MAX_SEC.toFixed(2)+'s)','bad');return;}
  const opts=collectOptions();
  if((opts.width||opts.height)&&(!opts.width||!opts.height)){toast('해상도는 가로·세로를 모두 입력해 주세요','bad');return;}
  submitRepair({start:state.start,end:state.end,note:$('#note').value.trim(),addition:$('#addition').value.trim(),
                seed:$('#seed').value,options:opts,refBlob:state.refBlob});
};

function setExtendDisabled(v){document.querySelectorAll('#status .extbtn').forEach(b=>b.disabled=v);}

function extend(direction){
  const l=state.last; if(!l){ toast('먼저 구간 수정을 한 번 끝내 주세요','bad'); return; }
  const amount=parseFloat($('#extAmt').value);
  if(!(amount>0)){ toast('연장할 길이를 입력해 주세요','bad'); return; }
  let start,end;
  if(direction==='after'){ start=(l.last+1)/FPS; end=start+amount; }
  else { end=l.first/FPS; start=Math.max(0,end-amount); }
  if(end-start<1/FPS){ toast('더 이상 앞으로 연장할 수 없어요','bad'); return; }
  // 방금 만든 수정본 위에서 이어 만든다 (서버가 최신 수정본을 기준으로 삼으므로 이미 만든 프레임은 다시 계산하지 않음)
  state.originalUrl=l.video+'?t='+Date.now(); vid.src=state.originalUrl; vid.load();
  state._seekTo=(direction==='after'?start:end);
  submitRepair({start,end,note:l.note||'',addition:l.addition,reuse:true,seed:l.seed,options:l.options||{},refBlob:state.refBlob});
}

function fmtOpts(o){
  const parts=[]; if(o.width) parts.push(`${o.width}×${o.height}`); if(o.steps) parts.push(`${o.steps}스텝`);
  if(o.dpo!=null) parts.push('DPO '+(o.dpo?'켬':'끔')); if(o.dpo_strength!=null) parts.push('DPO '+o.dpo_strength);
  if(o.lora_strength!=null) parts.push('LoRA '+o.lora_strength); if(o.shift!=null) parts.push('shift '+o.shift);
  if(o.cfg!=null) parts.push('CFG '+o.cfg); if(o.pose_strength!=null) parts.push('포즈 '+o.pose_strength);
  if(o.tail!=null) parts.push('이음새 '+o.tail); return parts.join(' · ');
}

function pollStatus(identity,rid){
  const st=$('#status');st.className='status show';
  if(state.poll)clearInterval(state.poll);
  const tick=async()=>{
    try{
      const r=await fetch(`api/scail/repair/${identity}/${rid}/status`);
      if(!r.ok)throw new Error(r.status);
      const d=await r.json();
      if(d.state==='done'){
        clearInterval(state.poll);$('#submit').disabled=false;
        const span=`${(d.first/FPS).toFixed(2)}~${((d.last+1)/FPS).toFixed(2)}초`;
        const opt=fmtOpts(d.options||{});
        state.last={first:d.first,last:d.last,video:d.video,addition:d.addition,seed:d.seed,options:d.options||{},
                    note:(d.request&&d.request.note)||''};
        st.innerHTML=`<b style="color:var(--good)">완료!</b> 수정 구간 ${span}`+(opt?` <span class="note">(${opt})</span>`:'')+
          `<div class="compare">
             <figure><video src="${state.originalUrl}" playsinline muted loop autoplay></video><figcaption>이전</figcaption></figure>
             <figure><video src="${d.video}?t=${Date.now()}" playsinline muted loop autoplay></video><figcaption>수정본 전체</figcaption></figure>`+
             (d.segment?`<figure><video src="${d.segment}?t=${Date.now()}" playsinline muted loop autoplay></video><figcaption>수정 구간만</figcaption></figure>`:'')+
          `</div>
           <div class="note">추가 문장: ${(d.addition||'').replace(/</g,'&lt;')} · seed ${d.seed}</div>
           <div class="extend">
             <b>구간 연장</b> <span class="note">이미 만든 프레임은 다시 계산하지 않고, 같은 문장·시드·옵션으로 이어서 만들어요.</span>
             <div class="row">
               <button class="extbtn" id="extBefore" type="button">◀ 앞쪽으로 연장</button>
               <input type="number" id="extAmt" value="0.25" step="0.05" min="0.05" max="2"> 초
               <button class="extbtn" id="extAfter" type="button">뒤쪽으로 연장 ▶</button>
             </div>
             <div class="presets">
               <span class="pill" data-ext="0.1">+0.1초</span><span class="pill" data-ext="0.17">+4프레임</span>
               <span class="pill" data-ext="0.25">+0.25초</span><span class="pill" data-ext="0.5">+0.5초</span><span class="pill" data-ext="1">+1초</span>
             </div>
           </div>
           <div class="row"><button class="primary" id="useFixed" type="button">수정본을 기준으로 계속 수정</button></div>`;
        document.querySelectorAll('#status [data-ext]').forEach(p=>p.onclick=()=>{$('#extAmt').value=p.dataset.ext;});
        $('#extBefore').onclick=()=>extend('before'); $('#extAfter').onclick=()=>extend('after');
        $('#useFixed').onclick=()=>{state.originalUrl=d.video+'?t='+Date.now();vid.src=state.originalUrl;vid.load();st.className='status';loadCandidates();};
        loadCandidates(); loadVersions(identity);
        return;
      }
      if(d.state==='error'||d.state==='cancelled'){
        clearInterval(state.poll);$('#submit').disabled=false;
        st.innerHTML=`<span style="color:var(--bad)">중단됨: ${d.error||d.state}</span>`;return;
      }
      if(d.active){
        const pct=(d.total?Math.round((d.step||0)/d.total*100):0);
        st.innerHTML=`<b>생성 중…</b> ${d.message||''}`+(d.total?` (${d.step||0}/${d.total} 스텝)`:'')+
          `<div class="bar"><i style="width:${pct}%"></i></div>`;
      }else{
        st.innerHTML=`대기 중… ${d.position?`큐 ${d.position}번째 (최우선 적용됨)`:''}`;
      }
    }catch(e){/* transient */}
  };
  tick();state.poll=setInterval(tick,1500);
}


// ---- saved versions (watch on the web / export to the results tab + Telegram) ----
let versionsTimer=null;
function linkEl(href,text,cls){const a=document.createElement('a');a.href=href;a.target='_blank';a.rel='noopener';a.textContent=text;if(cls)a.className=cls;return a;}
async function loadVersions(identity){
  const box=$('#versions');
  try{
    const r=await fetch(`api/scail/repair/${identity}/versions`);
    if(!r.ok) throw new Error(r.status);
    const {items}=await r.json();
    if(state.identity!==identity) return;
    clearTimeout(versionsTimer);
    if(!items.length){ box.textContent='아직 저장된 수정본이 없어요.'; return; }
    box.innerHTML='';
    for(const v of items){
      const row=document.createElement('div');row.className='ver';
      const when=new Date((v.created||0)*1000).toLocaleString('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
      const span=(v.first!=null)?` · ${(v.first/FPS).toFixed(2)}~${((v.last+1)/FPS).toFixed(2)}초`:'';
      const t=document.createElement('div');t.className='vt';
      t.innerHTML=`<b>수정본 ${v.version}</b>${v.current?'<span class="tag">현재 기준</span>':''}${span} <span class="note">· ${when}</span>`+
                  (v.note?`<div class="note">${v.note.replace(/</g,'&lt;')}</div>`:'');
      row.appendChild(t);
      const links=document.createElement('div');links.className='vl';
      if(v.delivered){
        links.appendChild(linkEl(v.delivered,'▶ 수정본 (음악·후보정)','good'));
        if(v.segment_delivered) links.appendChild(linkEl(v.segment_delivered,'▶ 수정 구간만 (음악·후보정)','good'));
      }
      links.appendChild(linkEl(v.video,'▶ 원본 프레임(무음)'));
      if(v.segment) links.appendChild(linkEl(v.segment,'▶ 구간만(무음)'));
      if(!v.delivered){
        if(v.exporting){ const s=document.createElement('span');s.className='note';s.textContent='내보내는 중… (큐에서 순서대로 처리돼요)';links.appendChild(s); }
        else{
          const b=document.createElement('button');b.type='button';b.textContent='📤 웹·텔레그램으로 내보내기';
          b.onclick=()=>deliverVersion(identity,v.version,b);links.appendChild(b);
        }
      }
      row.appendChild(links);box.appendChild(row);
    }
    if(items.some(v=>v.exporting)) versionsTimer=setTimeout(()=>loadVersions(identity),5000);
  }catch(e){ box.textContent='수정본 목록을 불러오지 못했어요.'; }
}
async function deliverVersion(identity,version,btn){
  btn.disabled=true;
  try{
    const body=new FormData();body.set('version',String(version));
    const r=await fetch(`api/scail/repair/${identity}/deliver`,{method:'POST',body});
    const d=await r.json();
    if(!r.ok) throw new Error(typeof d.detail==='string'?d.detail:('오류 '+r.status));
    toast(`수정본 ${version} 내보내기 접수 · 끝나면 결과 탭과 텔레그램에 올라가요`,'good');
  }catch(e){ toast('내보내기 실패: '+e.message,'bad'); }
  loadVersions(identity);
}

applyPreset('default');
loadCandidates();
