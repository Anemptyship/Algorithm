"use strict";
const $=s=>document.querySelector(s);
const state={sigs:{},focus:new URLSearchParams(location.search).get('id'),playing:null,busy:new Set(),first:true};
const STATE_LABEL={running:'생성 중',queued:'대기 중',cancelled:'중단됨',error:'오류로 멈춤'};

function toast(msg,kind){const t=$('#toast');t.textContent=msg;t.className='toast show'+(kind?' '+kind:'');clearTimeout(t._t);t._t=setTimeout(()=>t.className='toast',2800);}
const fmt=s=>(s||0).toFixed(1)+'초';
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// ---- player (one shared element, never re-created by the polling) ----
function play(job,k,label,url,kind){
  const v=$('#player');
  state.playing={id:job.id,k,kind};
  $('#plTitle').textContent=job.title;
  $('#plWhat').textContent=label;
  $('#playerCard').classList.add('show');
  v.onloadeddata=()=>{ $('#plWhat').textContent=label; };
  $('#plWhat').textContent=label+(kind==='upto'?' · 이어 붙이는 중…':'');
  v.src=url; v.load();
  v.play().catch(()=>{});
  $('#playerCard').scrollIntoView({block:'nearest',behavior:'smooth'});
  markOn();
}
function markOn(){
  document.querySelectorAll('.seg').forEach(b=>b.classList.remove('on'));
  const p=state.playing; if(!p) return;
  document.querySelectorAll(`.job[data-id="${p.id}"] .seg[data-k="${p.k}"]`).forEach(b=>b.classList.add('on'));
}
$('#plClose').onclick=()=>{const v=$('#player');v.pause();v.removeAttribute('src');v.load();state.playing=null;$('#playerCard').classList.remove('show');markOn();};

// ---- actions ----
async function call(url,opts,okMsg){
  try{
    const r=await fetch(url,opts);
    let d={}; try{d=await r.json();}catch(e){}
    if(!r.ok) throw new Error(typeof d.detail==='string'?d.detail:('오류 '+r.status));
    if(okMsg) toast(okMsg,'good');
    return d;
  }catch(e){ toast(e.message,'bad'); return null; }
}
async function stopJob(job){
  const left=job.total-job.done;
  if(!confirm(`이 생성을 중단할까요?\n\n끝난 ${job.done}개 구간은 보존돼요. 나중에 '이어서 생성'으로 나머지 ${left}개만 만들 수 있어요.`)) return;
  state.busy.add(job.id); await call(`api/jobs/${job.id}/cancel`,{method:'POST'},'중단 요청을 보냈어요. 지금 구간 연산은 안전한 지점까지 이어질 수 있어요.');
  state.busy.delete(job.id); refresh();
}
async function resumeJob(job,where){
  state.busy.add(job.id);
  const body=new FormData(); body.set('where',where);
  const d=await call(`api/scail/live/${job.id}/resume`,{method:'POST',body},where==='front'?'대기열 맨 앞에서 이어서 생성해요.':'대기열 맨 뒤에 넣었어요.');
  state.busy.delete(job.id); refresh();
}

// ---- rendering ----
function sigOf(j){return JSON.stringify([j.state,j.done,j.total,j.position,j.progress,j.can_resume,j.error,state.busy.has(j.id),state.focus===j.id]);}
function chunkProgress(j){
  const p=j.progress||{}; if(!j.active||!p.total_steps) return 0;
  return Math.max(0,Math.min(100,Math.round(((p.step||0)/p.total_steps)*100)));
}
function card(j){
  const el=document.createElement('article');el.className='card job'+(state.focus===j.id?' focus':'');el.dataset.id=j.id;
  const img=document.createElement('img');img.loading='lazy';img.alt='';img.src=j.thumb;img.onerror=()=>{img.style.visibility='hidden';};
  const body=document.createElement('div');body.className='body';
  const cur=j.state==='running'?j.done+1:0;
  const asked=new Date((j.created||0)*1000).toLocaleString('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
  let line=`요청 ${asked} · ${j.done}/${j.total} 구간 완료 · ${fmt(j.seconds_done)} / ${fmt(j.seconds_total)}`;
  if(j.state==='running'&&j.progress&&j.progress.message) line+=` · ${j.progress.message}`;
  if(j.state==='queued'&&j.position) line+=` · 대기 ${j.position}번째`;
  body.innerHTML=`<div class="top"><b>${esc(j.title)}</b><span class="chip ${esc(j.state)}">${STATE_LABEL[j.state]||esc(j.state)}</span></div>
    <div class="note">${esc(line)}</div>`+(j.error?`<div class="err">${esc(j.error)}</div>`:'');
  const segs=document.createElement('div');segs.className='segs';
  for(let k=1;k<=j.total;k++){
    const b=document.createElement('button');b.type='button';b.dataset.k=k;
    const part=j.parts[k-1];
    if(part){
      b.className='seg done';b.innerHTML=`${k}구간<small>${fmt(part.seconds)}</small>`;
      b.onclick=()=>play(j,k,`${k}구간만 · ${fmt(part.start)}~${fmt(part.start+part.seconds)}`,part.url,'part');
    }else if(k===cur){
      b.className='seg cur';b.style.setProperty('--p',chunkProgress(j)+'%');b.disabled=true;b.innerHTML=`${k}구간<small>생성 중 ${chunkProgress(j)}%</small>`;
    }else{
      b.className='seg pend';b.disabled=true;b.innerHTML=`${k}구간<small>대기</small>`;
    }
    segs.appendChild(b);
  }
  body.appendChild(segs);
  const row=document.createElement('div');row.className='row';
  if(j.done>0){
    const all=document.createElement('button');all.type='button';all.className='btn primary';
    all.textContent=j.done>1?`▶ 지금까지 이어보기 (1~${j.done}구간)`:'▶ 1구간 보기';
    all.onclick=()=>{
      const joined=j.done>1;
      play(j,joined?0:1,joined?`처음부터 ${j.done}구간까지 이어서 · ${fmt(j.seconds_done)}`:`1구간만 · ${fmt(j.parts[0].seconds)}`,
           joined?`api/scail/live/${j.id}/preview/${j.done}.mp4`:j.parts[0].url,joined?'upto':'part');
    };
    row.appendChild(all);
  }
  const busy=state.busy.has(j.id);
  if(j.state==='running'||j.state==='queued'){
    const s=document.createElement('button');s.type='button';s.className='btn stop';s.textContent='⏹ 중단';s.disabled=busy;
    s.onclick=()=>stopJob(j);row.appendChild(s);
  }
  if(j.can_resume){
    const a=document.createElement('button');a.type='button';a.className='btn primary';a.textContent='▶ 이어서 생성';a.disabled=busy;
    a.onclick=()=>resumeJob(j,'front');row.appendChild(a);
    const b=document.createElement('button');b.type='button';b.className='btn';b.textContent='대기열 맨 뒤로';b.disabled=busy;
    b.onclick=()=>resumeJob(j,'back');row.appendChild(b);
  }
  body.appendChild(row);
  el.appendChild(img);el.appendChild(body);
  return el;
}
function render(items){
  const box=$('#jobs');
  if(!items.length){box.innerHTML='<div class="empty">지금 생성 중이거나 중단된 영상이 없어요.<br><span class="note">구간이 하나라도 끝난 영상이 여기에 나타나요.</span></div>';state.sigs={};return;}
  if(box.querySelector('.empty')) box.innerHTML='';
  const seen=new Set();
  items.forEach((j,index)=>{
    seen.add(j.id);
    const sig=sigOf(j);
    let el=box.querySelector(`.job[data-id="${j.id}"]`);
    if(!el||state.sigs[j.id]!==sig){
      const fresh=card(j);
      if(el) el.replaceWith(fresh); else box.appendChild(fresh);
      el=fresh;state.sigs[j.id]=sig;
    }
    if(box.children[index]!==el) box.insertBefore(el,box.children[index]||null);
  });
  [...box.querySelectorAll('.job')].forEach(el=>{if(!seen.has(el.dataset.id)){el.remove();delete state.sigs[el.dataset.id];}});
  markOn();
  if(state.first&&state.focus){const f=box.querySelector(`.job[data-id="${state.focus}"]`);if(f)f.scrollIntoView({block:'center'});}
  state.first=false;
}
async function refresh(){
  try{
    const r=await fetch('api/scail/live',{headers:{'Accept':'application/json'}});
    if(!r.ok) throw new Error(r.status);
    render((await r.json()).items);
  }catch(e){ if(state.first) $('#jobs').innerHTML='<div class="empty">목록을 불러오지 못했습니다. 로그인(Z Studio) 상태를 확인하세요.</div>'; }
}
setInterval(()=>{ if(!document.hidden) refresh(); },3000);
document.addEventListener('visibilitychange',()=>{ if(!document.hidden) refresh(); });
refresh();
