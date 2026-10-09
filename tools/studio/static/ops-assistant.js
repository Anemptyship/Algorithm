"use strict";
const $=s=>document.querySelector(s);
const HEAD={'X-Ops-Assistant':'1'};
const STATUS={queued:'대기 중',running:'생각 중',answered:'답변 왔어요',proposed:'승인 필요',applied:'적용됨',undone:'되돌림',rejected:'거절함',
  failed:'실패',invalid:'검사 실패',stale:'파일이 바뀜',rolled_back:'자동 롤백'};
const state={items:[],targets:[],focus:new URLSearchParams(location.search).get('id'),open:new Set(),busy:new Set(),sig:{},timer:null,
  pending:[],restart:{},userPickedTarget:false};

function toast(msg,kind){const t=$('#toast');t.textContent=msg;t.className='toast show'+(kind?' '+kind:'');clearTimeout(t._t);t._t=setTimeout(()=>t.className='toast',3200);}
const when=t=>t?new Date(t*1000).toLocaleString('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';
// Everything that comes from the model or the files goes in as text, never as HTML.
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}

async function api(url,opts,quiet){
  try{
    const r=await fetch(url,Object.assign({headers:HEAD},opts||{}));
    let d={};try{d=await r.json();}catch(e){}
    if(!r.ok){const err=new Error(typeof d.detail==='string'?d.detail:('오류 '+r.status));err.status=r.status;throw err;}
    return d;
  }catch(e){ if(!quiet) toast(e.message,'bad'); throw e; }
}

// ---- composer ----
async function loadTargets(){
  const d=await api('api/ops/targets',{headers:{}},true);
  state.targets=d.items;
  const sel=$('#target');sel.textContent='';
  for(const t of d.items){const o=el('option',null,t.label);o.value=t.path;sel.appendChild(o);}
}
let suggestTimer=null;
$('#prompt').addEventListener('input',()=>{
  clearTimeout(suggestTimer);
  suggestTimer=setTimeout(async()=>{
    const text=$('#prompt').value.trim(); if(text.length<4) return;
    try{
      const d=await api('api/ops/suggest?prompt='+encodeURIComponent(text),{headers:{}},true);
      const t=state.targets.find(x=>x.path===d.target);
      $('#suggested').textContent=t?`(추천: ${t.label})`:'';
      if(!state.userPickedTarget&&t) $('#target').value=d.target;
    }catch(e){}
  },500);
});
$('#target').addEventListener('change',()=>{state.userPickedTarget=true;$('#suggested').textContent='';});

async function send(kind){
  const text=$('#prompt').value.trim();
  if(text.length<4){toast('요청을 4자 이상 적어 주세요','bad');return;}
  const body=new FormData();
  body.set('prompt',text);body.set('kind',kind);body.set('target',$('#target').value);
  body.set('include_logs',$('#logs').checked?'true':'false');body.set('preempt',$('#preempt').checked?'true':'false');
  $('#ask').disabled=$('#fix').disabled=true;
  try{
    const d=await api('api/ops/requests',{method:'POST',body});
    toast(d.preempted&&d.preempted.action==='cancelled'?'접수했어요. 돌던 영상 생성을 멈추고 먼저 처리해요.':'접수했어요. 영상 생성이 끝나면 바로 답해요.','good');
    $('#prompt').value='';state.focus=d.id;state.open.add(d.id);
    await refresh();
  }catch(e){}
  $('#ask').disabled=$('#fix').disabled=false;
}
$('#ask').onclick=()=>send('ask');
$('#fix').onclick=()=>send('fix');

// ---- restart banner ----
function renderBanner(){
  const b=$('#banner');b.textContent='';b.className='';
  const r=state.restart||{};
  if(r.state==='running'&&Date.now()/1000-(r.at||0)<420){
    b.className='banner';b.appendChild(el('div',null,'서버를 재시작하고 있어요. 앱이 정상으로 뜨는지 확인한 뒤 알려 드려요. (안 뜨면 방금 적용한 변경을 자동으로 되돌려요)'));return;
  }
  if(state.pending.length){
    b.className='banner';
    b.appendChild(el('div',null,`서버 코드 변경 ${state.pending.length}건이 아직 반영되지 않았어요. 재시작해야 적용돼요.`));
    const row=el('div','row');
    const go=el('button','btn primary','🔄 재시작해서 반영');go.type='button';
    go.onclick=()=>restart(false);row.appendChild(go);
    row.appendChild(el('span','note','재시작 뒤 앱이 안 뜨면 자동으로 이전 코드로 되돌려요.'));
    b.appendChild(row);return;
  }
  if(r.state==='rolled_back'||r.state==='rollback_failed'){
    b.className='banner bad';
    b.appendChild(el('div',null,r.state==='rolled_back'?'직전 재시작에서 앱이 뜨지 않아 변경을 자동으로 되돌렸고, 정상으로 복구됐어요.':'직전 재시작 뒤 앱이 정상으로 돌아오지 않았어요. 직접 확인이 필요해요.'));
    return;
  }
  if(r.state==='ok'&&Date.now()/1000-(r.at||0)<3600){
    b.className='banner ok';b.appendChild(el('div',null,'재시작이 끝났고 앱이 정상이에요. 변경이 반영됐어요.'));
  }
}
async function restart(confirmBusy){
  const body=new FormData();body.set('confirm_busy',confirmBusy?'true':'false');
  try{
    await api('api/ops/restart',{method:'POST',body},true);
    toast('재시작을 시작했어요','good');await refresh();
  }catch(e){
    if(e.status===409&&!confirmBusy&&e.message.includes('영상 생성')){
      if(confirm(e.message+'\n\n그래도 재시작할까요?')) return restart(true);
    }else toast(e.message,'bad');
  }
}

// ---- cards ----
function diffView(text){
  const pre=el('pre','diff');
  for(const line of text.split('\n')){
    const kind=line.startsWith('+++')||line.startsWith('---')||line.startsWith('@@')?'h':line.startsWith('+')?'a':line.startsWith('-')?'d':'';
    pre.appendChild(el('span',kind,line||' '));
  }
  return pre;
}
function sigOf(r){return JSON.stringify([r.status,r.updated,state.open.has(r.id),state.busy.has(r.id),state.focus===r.id,r.restart_needed]);}

function card(r){
  const c=el('article','card'+(state.focus===r.id?' focus':''));c.dataset.id=r.id;
  const top=el('div','top');
  const title=el('b',null,r.prompt);
  top.appendChild(title);top.appendChild(el('span','chip '+r.status,STATUS[r.status]||r.status));
  c.appendChild(top);
  c.appendChild(el('div','note',`${r.kind==='ask'?'질문':'수정 요청'} · ${r.target_label||r.target} · ${when(r.created)}`));
  if(r.status==='queued') c.appendChild(el('div','note','영상 생성이 끝나면 바로 처리해요. (GPU를 같이 써서 순서를 기다려요)'));
  if(!state.open.has(r.id)){
    const more=el('button','btn','자세히 보기');more.type='button';more.style.marginTop='8px';
    more.onclick=async()=>{state.open.add(r.id);await loadOne(r.id);render();};
    c.appendChild(more);return c;
  }
  const full=state.full&&state.full[r.id];
  if(!full){c.appendChild(el('div','note','불러오는 중…'));return c;}
  if(full.reply) c.appendChild(el('div','reply',full.reply));
  if(full.error) c.appendChild(el('div','err',full.error));
  (full.flags||[]).forEach(f=>c.appendChild(el('div','flag',f)));
  if((full.checks||[]).length){
    const ul=el('ul','checks');
    full.checks.forEach(([label,ok,note])=>ul.appendChild(el('li',ok?'ok':'no',label+(note?` — ${note}`:''))));
    c.appendChild(ul);
  }
  if(full.diff){
    c.appendChild(el('div','note',`바뀌는 줄 ${full.changed}줄 · ${full.tier==='python'?'서버 코드 (적용 뒤 재시작 필요)':'화면 파일 (적용하면 바로 반영)'}`));
    c.appendChild(diffView(full.diff));
  }
  const row=el('div','row');const busy=state.busy.has(r.id);
  const act=(label,cls,fn)=>{const b=el('button','btn '+cls,label);b.type='button';b.disabled=busy;b.onclick=fn;row.appendChild(b);};
  if(full.status==='proposed'){
    act('✅ 적용','primary',()=>doAct(r.id,'apply',null));
    act('거절','',()=>doAct(r.id,'reject',null));
  }
  if(full.status==='applied') act('↩ 되돌리기','danger',()=>doAct(r.id,'undo',null));
  if(['answered','failed','invalid','stale'].includes(full.status)) act('치우기','',()=>doAct(r.id,'reject',null));
  const close=el('button','btn','접기');close.type='button';close.onclick=()=>{state.open.delete(r.id);render();};row.appendChild(close);
  c.appendChild(row);
  return c;
}
async function doAct(id,what,extra){
  state.busy.add(id);render();
  try{
    const body=new FormData();if(extra)Object.entries(extra).forEach(([k,v])=>body.set(k,v));
    await api(`api/ops/requests/${id}/${what}`,{method:'POST',body},true);
    toast({apply:'적용했어요. 되돌리기로 언제든 원래대로 돌릴 수 있어요.',reject:'정리했어요.',undo:'되돌렸어요.'}[what],'good');
  }catch(e){
    if(what==='undo'&&e.status===409&&e.message.includes('또 바뀌었어요')&&confirm(e.message+'\n\n강제로 되돌릴까요?')){
      try{await api(`api/ops/requests/${id}/undo`,{method:'POST',body:(()=>{const b=new FormData();b.set('force','true');return b;})()},true);toast('강제로 되돌렸어요.','good');}
      catch(e2){toast(e2.message,'bad');}
    }else toast(e.message,'bad');
  }
  state.busy.delete(id);await loadOne(id);await refresh();
}

state.full={};
async function loadOne(id){try{state.full[id]=await api('api/ops/requests/'+id,{headers:{}},true);}catch(e){}}

function render(){
  const list=$('#list');
  if(!state.items.length){list.textContent='';list.appendChild(el('div','empty','아직 요청이 없어요. 위에 적어서 물어보거나 수정을 부탁해 보세요.'));state.sig={};return;}
  if(list.querySelector('.empty')) list.textContent='';
  const seen=new Set();
  state.items.forEach((r,i)=>{
    seen.add(r.id);
    const full=state.full[r.id];
    const s=sigOf(r)+(full?full.status+(full.applied_at||'')+(full.reply||'').length:'');
    let node=list.querySelector(`.card[data-id="${r.id}"]`);
    if(!node||state.sig[r.id]!==s){const fresh=card(r);if(node)node.replaceWith(fresh);else list.appendChild(fresh);node=fresh;state.sig[r.id]=s;}
    if(list.children[i]!==node) list.insertBefore(node,list.children[i]||null);
  });
  [...list.querySelectorAll('.card')].forEach(n=>{if(!seen.has(n.dataset.id)){n.remove();delete state.sig[n.dataset.id];}});
}

async function refresh(){
  try{
    const d=await api('api/ops/requests',{headers:{}},true);
    state.items=d.items.map(i=>Object.assign(i,{updated:i.finished_at||i.applied_at||i.created}));
    state.pending=d.pending_restart||[];state.restart=d.restart||{};
    for(const r of state.items){ if(state.open.has(r.id)&&(!state.full[r.id]||state.full[r.id].status!==r.status)) await loadOne(r.id); }
    renderBanner();render();
    if(state.focus&&!state.focusDone){const n=document.querySelector(`.card[data-id="${state.focus}"]`);if(n){n.scrollIntoView({block:'center'});state.focusDone=true;}}
  }catch(e){ if(!state.items.length) $('#list').textContent='목록을 불러오지 못했어요. 로그인(Z Studio) 상태를 확인하세요.'; }
  const active=state.items.some(r=>['queued','running'].includes(r.status))||state.restart.state==='running';
  clearTimeout(state.timer);state.timer=setTimeout(()=>{if(!document.hidden)refresh();else state.timer=setTimeout(refresh,15000);},active?3000:15000);
}

(async()=>{
  await loadTargets();
  if(state.focus) state.open.add(state.focus);
  await refresh();
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
})();
