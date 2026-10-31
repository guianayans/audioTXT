"use strict";
const $ = (id) => document.getElementById(id);
const ACTIVE_KEY = "audiotxt_active_job";
const THEME_KEY = "audiotxt_theme";

let MODELS = [];
let selectedModel = null;
let currentJob = null;
let currentSegments = [];   // [{start,end,text}] do job atual
let currentFile = null;     // File escolhido (p/ enviar)
let objURL = null;          // URL local do áudio (player)
let wavePeaks = null;
const activeDownloads = {};
let SUMMARY_MODELS = [];
let summaryDefault = null;
const activeSummaryDownloads = {};
let summaryES = null;
let SUMMARY_STYLES = [];
let selectedStyle = "completo";
const summaryCache = {};              // { jobId: { style: corpo } }
const libSummaryES = {};              // streams de resumo religados na biblioteca

const TAGS = { tiny: "Rápido", base: "Rápido", small: "Equilíbrio",
  medium: "Preciso", "large-v3-turbo": "Turbo", "large-v3": "Máxima precisão" };

// ---------- utils ----------
const fmtSize = (b) => b < 1048576 ? (b/1024).toFixed(0)+" KB"
  : b < 1073741824 ? (b/1048576).toFixed(1)+" MB" : (b/1073741824).toFixed(2)+" GB";
const fmtClock = (s) => { s = Math.max(0, s||0); const m = Math.floor(s/60), x = Math.floor(s%60);
  return `${String(m).padStart(2,"0")}:${String(x).padStart(2,"0")}`; };
const fmtTime = (s) => { s = Math.max(0, Math.round(s||0)); if (s<60) return s+"s";
  const m = Math.floor(s/60); return m<60 ? `${m}m ${s%60}s` : `${Math.floor(m/60)}h ${m%60}m`; };
const fmtDate = (ts) => new Date(ts*1000).toLocaleString("pt-BR",
  { day:"2-digit", month:"2-digit", year:"2-digit", hour:"2-digit", minute:"2-digit" });
const sizeGB = (mb) => (mb/1000).toFixed(mb<1000?2:1);
const esc = (s) => String(s).replace(/[&<>"']/g, (c)=>({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
function toast(msg) { const t=$("toast"); t.textContent=msg; t.classList.add("show");
  clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove("show"), 2600); }

// ---------- navegação ----------
function switchView(name) {
  document.querySelectorAll(".view").forEach((v)=>v.classList.toggle("active", v.id === "view-"+name));
  document.querySelectorAll(".nav-item").forEach((n)=>n.classList.toggle("active", n.dataset.view === name));
  $("sidebar").classList.remove("open");
  if (name === "library") loadHistory();
}
document.querySelectorAll("[data-view]").forEach((el)=>el.addEventListener("click",()=>switchView(el.dataset.view)));
$("menuBtn").addEventListener("click", ()=>$("sidebar").classList.toggle("open"));
$("modelActiveBtn").addEventListener("click", ()=>switchView("models"));
$("changeModelBtn").addEventListener("click", ()=>switchView("models"));

// ---------- tema ----------
function applyTheme(t){ document.documentElement.setAttribute("data-theme", t);
  $("themeBtn").textContent = t === "light" ? "🌙" : "☀"; }
applyTheme(localStorage.getItem(THEME_KEY) || "dark");
function toggleTheme(){ const t = document.documentElement.getAttribute("data-theme")==="light"?"dark":"light";
  localStorage.setItem(THEME_KEY, t); applyTheme(t); }
$("themeBtn").addEventListener("click", toggleTheme);
$("themeBtn2").addEventListener("click", toggleTheme);

// ---------- conta ----------
$("avatarBtn").addEventListener("click", ()=>$("accountMenu").classList.toggle("open"));
document.addEventListener("click",(e)=>{ if(!e.target.closest(".top-icons")) $("accountMenu").classList.remove("open");
  if(!e.target.closest(".export-wrap")) $("exportMenu").classList.remove("open"); });
async function logout(){ await fetch("/api/logout",{method:"POST"}); location.href="/login"; }
$("logoutBtn").addEventListener("click", logout);
$("logoutBtn2").addEventListener("click", logout);

// ---------- init ----------
async function init() {
  let cfg = {};
  try { cfg = await fetch("/api/config").then((r)=>r.json()); } catch(_){}
  const hasGpu = cfg.gpu_available;
  $("gpuStatus").classList.toggle("cpu", !hasGpu);
  $("gpuDot").classList.toggle("off", !hasGpu);
  $("gpuStatusTitle").textContent = hasGpu ? "GPU ativa" : "CPU";
  $("gpuStatusSub").textContent = hasGpu ? "Acelerado por GPU" : "GPU indisponível";
  $("gpuMiniDot").classList.toggle("off", !hasGpu);
  if (hasGpu) $("gpuMiniDot").classList.add("idle");
  $("setDevice").textContent = hasGpu ? "GPU (CUDA) com fallback p/ CPU" : "CPU";
  if (!hasGpu) { $("gpuMetric").classList.add("hidden"); $("vramMetric").classList.add("hidden"); $("techFoot").classList.add("hidden"); }
  $("setMail").textContent = "Sessão por senha";
  $("accountMail").textContent = "Sessão ativa";
  await loadModels(cfg.default_model);
  loadSummaryModels();
  await loadSummaryStyles();
  startMetrics();
  await loadHistory();
  resumeActive();
  reconnectSummaries();
}

// ---------- modelos ----------
const card = (id)=>document.querySelector(`.model[data-id="${id}"]`);
const actionEl = (id)=>card(id) && card(id).querySelector(".m-action");

async function loadModels(def){
  let data; try { data = await fetch("/api/models").then((r)=>r.json()); } catch(_){ return; }
  MODELS = data.models;
  $("setDefaultModel").textContent = (TAGS[data.default]||"") + " (" + data.default + ")";
  renderModels(def || data.default);
}
function renderModels(preferId){
  const grid = $("modelGrid"); grid.innerHTML = "";
  MODELS.forEach((m)=>{
    const el = document.createElement("div"); el.className="model"; el.dataset.id=m.id;
    el.innerHTML = `<div class="m-info"><div class="m-top"><span class="m-name">${m.label}</span>
      <span class="m-size">${sizeGB(m.size_mb)} GB</span></div><div class="m-desc">${m.desc}</div></div>
      <div class="m-action"></div>`;
    el.addEventListener("click",(e)=>{ if(e.target.closest("button")) return; selectModel(m.id); });
    grid.appendChild(el); renderAction(m);
  });
  const want = MODELS.find((m)=>m.id===preferId), firstDl = MODELS.find((m)=>m.downloaded);
  selectModel(want && want.downloaded ? preferId : firstDl ? firstDl.id : want ? preferId : MODELS[0].id);
}
function selectModel(id){
  selectedModel = id;
  document.querySelectorAll(".model").forEach((n)=>n.classList.toggle("active", n.dataset.id===id));
  const m = MODELS.find((x)=>x.id===id);
  $("activeModelName").textContent = m ? m.label : "—";
  $("activeModelTag").textContent = TAGS[id] || "";
}
function renderAction(m){
  const el = actionEl(m.id); if(!el) return;
  if (m.downloaded) { el.innerHTML = `<span class="badge-on">no servidor</span>
    <button class="m-btn m-del" type="button">Apagar</button>`;
    el.querySelector(".m-del").onclick = ()=>deleteModel(m.id); }
  else { el.innerHTML = `<button class="m-btn m-get" type="button">Baixar modelo</button>`;
    el.querySelector(".m-get").onclick = ()=>startDownload(m.id); }
}
function startDownload(id){
  selectModel(id); const el = actionEl(id);
  el.innerHTML = `<div class="dl-row"><div class="dl-bar"><i></i></div><span class="dl-pct">iniciando…</span>
    <button class="m-btn m-cancel" type="button">Cancelar</button></div>`;
  const bar = el.querySelector(".dl-bar i"), pct = el.querySelector(".dl-pct");
  el.querySelector(".m-cancel").onclick = ()=>cancelDownload(id);
  const es = new EventSource(`/api/models/${id}/download`); activeDownloads[id]=es;
  es.addEventListener("progress",(e)=>{ const d=JSON.parse(e.data); bar.style.width=d.percent+"%";
    pct.textContent = `${d.percent}% · ${sizeGB(d.mb)} GB`; });
  es.addEventListener("done",()=>{ es.close(); delete activeDownloads[id]; markDownloaded(id,true); toast("Modelo baixado ✓"); });
  es.addEventListener("cancelled",()=>{ es.close(); delete activeDownloads[id]; markDownloaded(id,false); });
  es.addEventListener("error",(e)=>{ let msg="erro"; try{msg=JSON.parse(e.data).message;}catch(_){}
    es.close(); delete activeDownloads[id]; markDownloaded(id,false);
    if(msg&&msg!=="erro") toast("Falha no download: "+msg); });
}
function cancelDownload(id){ const es=activeDownloads[id]; if(es){es.close(); delete activeDownloads[id];}
  const el=actionEl(id); if(el) el.innerHTML=`<span class="badge-dl">cancelando…</span>`;
  fetch(`/api/models/${id}/cancel`,{method:"POST"}).finally(()=>markDownloaded(id,false)); }
function deleteModel(id){ const m=MODELS.find((x)=>x.id===id);
  if(!confirm(`Apagar o modelo "${m?m.label:id}" do servidor?`)) return;
  const el=actionEl(id); if(el) el.innerHTML=`<span class="badge-dl">apagando…</span>`;
  fetch(`/api/models/${id}`,{method:"DELETE"}).finally(()=>{ markDownloaded(id,false); toast("Modelo apagado"); }); }
function markDownloaded(id,val){ const m=MODELS.find((x)=>x.id===id); if(m){ m.downloaded=val; renderAction(m);
  if(id===selectedModel) selectModel(id); } }

// ---------- modelos de resumo (LLM) ----------
async function loadSummaryModels(){
  let data; try{ data=await fetch("/api/summary/models").then((r)=>r.json()); }catch(_){ return; }
  SUMMARY_MODELS=data.models||[]; summaryDefault=data.default; renderSummaryModels();
}
const sumCard=(id)=>document.querySelector(`#summaryModelGrid .model[data-id="${id}"]`);
const sumActionEl=(id)=>sumCard(id)&&sumCard(id).querySelector(".m-action");
function renderSummaryModels(){
  const grid=$("summaryModelGrid"); if(!grid) return; grid.innerHTML="";
  SUMMARY_MODELS.forEach((m)=>{
    const el=document.createElement("div"); el.className="model"; el.dataset.id=m.id;
    el.innerHTML=`<div class="m-info"><div class="m-top"><span class="m-name">${m.label}</span>
      <span class="m-size">${sizeGB(m.size_mb)} GB</span></div><div class="m-desc">${m.desc}</div></div>
      <div class="m-action"></div>`;
    grid.appendChild(el); renderSummaryAction(m);
  });
}
function renderSummaryAction(m){
  const el=sumActionEl(m.id); if(!el) return;
  if(m.downloaded){ el.innerHTML=`<span class="badge-on">no servidor</span>
    <button class="m-btn m-del" type="button">Apagar</button>`;
    el.querySelector(".m-del").onclick=()=>deleteSummaryModel(m.id); }
  else { el.innerHTML=`<button class="m-btn m-get" type="button">Baixar modelo</button>`;
    el.querySelector(".m-get").onclick=()=>startSummaryDownload(m.id); }
}
function startSummaryDownload(id){
  const el=sumActionEl(id);
  el.innerHTML=`<div class="dl-row"><div class="dl-bar"><i></i></div><span class="dl-pct">iniciando…</span>
    <button class="m-btn m-cancel" type="button">Cancelar</button></div>`;
  const bar=el.querySelector(".dl-bar i"), pct=el.querySelector(".dl-pct");
  el.querySelector(".m-cancel").onclick=()=>cancelSummaryDownload(id);
  const es=new EventSource(`/api/summary/models/${id}/download`); activeSummaryDownloads[id]=es;
  es.addEventListener("progress",(e)=>{ const d=JSON.parse(e.data); bar.style.width=d.percent+"%";
    pct.textContent=`${d.percent}% · ${sizeGB(d.mb)} GB`; });
  es.addEventListener("done",()=>{ es.close(); delete activeSummaryDownloads[id]; markSummaryDownloaded(id,true); toast("Modelo de resumo baixado ✓"); });
  es.addEventListener("cancelled",()=>{ es.close(); delete activeSummaryDownloads[id]; markSummaryDownloaded(id,false); });
  es.addEventListener("error",(e)=>{ let msg="erro"; try{msg=JSON.parse(e.data).message;}catch(_){}
    es.close(); delete activeSummaryDownloads[id]; markSummaryDownloaded(id,false);
    if(msg&&msg!=="erro") toast("Falha no download: "+msg); });
}
function cancelSummaryDownload(id){ const es=activeSummaryDownloads[id]; if(es){es.close(); delete activeSummaryDownloads[id];}
  const el=sumActionEl(id); if(el) el.innerHTML=`<span class="badge-dl">cancelando…</span>`;
  fetch(`/api/summary/models/${id}/cancel`,{method:"POST"}).finally(()=>markSummaryDownloaded(id,false)); }
function deleteSummaryModel(id){ const m=SUMMARY_MODELS.find((x)=>x.id===id);
  if(!confirm(`Apagar o modelo "${m?m.label:id}" do servidor?`)) return;
  const el=sumActionEl(id); if(el) el.innerHTML=`<span class="badge-dl">apagando…</span>`;
  fetch(`/api/summary/models/${id}`,{method:"DELETE"}).finally(()=>{ markSummaryDownloaded(id,false); toast("Modelo apagado"); }); }
function markSummaryDownloaded(id,val){ const m=SUMMARY_MODELS.find((x)=>x.id===id); if(m){ m.downloaded=val; renderSummaryAction(m); } }
function summaryModelToUse(){ const d=SUMMARY_MODELS.find((m)=>m.id===summaryDefault&&m.downloaded); if(d) return d.id;
  const any=SUMMARY_MODELS.find((m)=>m.downloaded); return any?any.id:null; }

// ---------- upload / arquivo ----------
const dz = $("dropzone");
["pickBtn","dzPick"].forEach((b)=>$(b).addEventListener("click",()=>$("fileInput").click()));
dz.addEventListener("dragover",(e)=>{e.preventDefault(); dz.classList.add("drag");});
dz.addEventListener("dragleave",()=>dz.classList.remove("drag"));
dz.addEventListener("drop",(e)=>{e.preventDefault(); dz.classList.remove("drag");
  if(e.dataTransfer.files[0]) pickFile(e.dataTransfer.files[0]);});
$("fileInput").addEventListener("change",()=>{ if($("fileInput").files[0]) pickFile($("fileInput").files[0]); });

function pickFile(f){
  currentFile = f; currentSegments = [];
  $("jobCard").classList.remove("hidden");
  $("jobName").textContent = f.name || "gravacao.webm";
  $("jobMeta").textContent = fmtSize(f.size) + " · " + (f.type || "áudio");
  $("startBtn").disabled = false;
  $("startBtn").classList.remove("hidden");
  $("procBar").classList.add("hidden");
  $("statusCard").classList.add("hidden");
  setEditorTools(false);
  $("transcript").innerHTML = '<p class="muted placeholder">Clique em “Transcrever” para começar.</p>';
  $("segmentsPlain").innerHTML = "";
  // Mostra "extrair áudio" só quando ajuda: vídeo, ou áudio grande/sem compressão.
  const nm = (f.name||"").toLowerCase(), ty = (f.type||"");
  const isVideo = ty.startsWith("video/") ||
    (!ty.startsWith("audio/") && /\.(mp4|m4v|mkv|mov|webm|avi|3gp|ts|mpg|mpeg|wmv|flv|ogv)$/i.test(nm));
  const heavyAudio = /\.(wav|aif|aiff|aifc|caf|flac|wv|ape|pcm|au)$/i.test(nm) || f.size > 80*1024*1024;
  $("extractOpt").classList.toggle("hidden", !(isVideo || heavyAudio));
  loadAudio(f);
  $("jobCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------- gravação por microfone ----------
let mediaRec=null, recChunks=[], recTimer=null, recStart=0;
$("recBtn").addEventListener("click", async ()=>{
  if(!navigator.mediaDevices || !window.MediaRecorder){ toast("Gravação não suportada neste navegador"); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio:true });
    recChunks=[]; mediaRec = new MediaRecorder(stream);
    mediaRec.ondataavailable=(e)=>{ if(e.data.size) recChunks.push(e.data); };
    mediaRec.onstop=()=>{ stream.getTracks().forEach((t)=>t.stop());
      const blob = new Blob(recChunks, { type: recChunks[0]?.type || "audio/webm" });
      const f = new File([blob], `gravacao_${Date.now()}.webm`, { type: blob.type });
      stopRecUI(); pickFile(f); };
    mediaRec.start(); recStart=Date.now();
    $("recLive").classList.remove("hidden"); $("recBtn").classList.add("hidden"); $("pickBtn").classList.add("hidden");
    recTimer=setInterval(()=>{ $("recTime").textContent = fmtClock((Date.now()-recStart)/1000); }, 500);
  } catch(_){ toast("Permissão de microfone negada"); }
});
$("recStop").addEventListener("click", ()=>{ if(mediaRec && mediaRec.state!=="inactive") mediaRec.stop(); });
$("recCancel").addEventListener("click", ()=>{ if(mediaRec){ mediaRec.onstop=null;
  if(mediaRec.state!=="inactive") mediaRec.stop(); mediaRec.stream?.getTracks?.().forEach((t)=>t.stop()); } stopRecUI(); });
function stopRecUI(){ clearInterval(recTimer); $("recLive").classList.add("hidden");
  $("recBtn").classList.remove("hidden"); $("pickBtn").classList.remove("hidden"); mediaRec=null; }

// ---------- player + waveform ----------
const audio = $("audio");
function loadAudio(file){
  if(objURL) URL.revokeObjectURL(objURL);
  objURL = URL.createObjectURL(file); audio.src = objURL; wavePeaks = null;
  $("player").classList.remove("hidden");
  audio.onerror = ()=>{ $("player").classList.add("hidden"); }; // formato sem playback no navegador
  audio.onloadedmetadata = ()=>{ $("pDur").textContent = fmtClock(audio.duration); drawWave(0); };
  decodePeaks(file);
}
async function decodePeaks(file){
  try {
    const buf = await file.arrayBuffer();
    const ac = new (window.AudioContext||window.webkitAudioContext)();
    const audioBuf = await ac.decodeAudioData(buf);
    const ch = audioBuf.getChannelData(0); const N = 160; const block = Math.floor(ch.length/N);
    const peaks = []; for(let i=0;i<N;i++){ let max=0; for(let j=0;j<block;j++){ const v=Math.abs(ch[i*block+j]||0); if(v>max) max=v; } peaks.push(max); }
    const norm = Math.max(...peaks)||1; wavePeaks = peaks.map((p)=>p/norm);
    ac.close(); drawWave(audio.currentTime/(audio.duration||1));
  } catch(_){ wavePeaks=null; }
}
function drawWave(progress){
  const c=$("wave"); const r=window.devicePixelRatio||1;
  const w=c.clientWidth||300, h=c.height; c.width=w*r; const ctx=c.getContext("2d"); ctx.scale(r,1);
  ctx.clearRect(0,0,w,h);
  const peaks = wavePeaks || Array(160).fill(0.12);
  const n=peaks.length, bw=w/n, mid=h/2;
  for(let i=0;i<n;i++){ const bh=Math.max(2, peaks[i]*(h-6));
    ctx.fillStyle = (i/n)<=progress ? "#22D3EE" : "rgba(255,255,255,.18)";
    ctx.fillRect(i*bw+bw*0.2, mid-bh/2, bw*0.6, bh); }
}
$("playBtn").addEventListener("click", ()=>{ if(audio.paused) audio.play(); else audio.pause(); });
audio.addEventListener("play", ()=>$("playBtn").textContent="⏸");
audio.addEventListener("pause", ()=>$("playBtn").textContent="▶");
audio.addEventListener("timeupdate", ()=>{ $("pCur").textContent=fmtClock(audio.currentTime);
  drawWave(audio.currentTime/(audio.duration||1)); highlightPlaying(audio.currentTime); });
$("pSpeed").addEventListener("change",()=>audio.playbackRate=parseFloat($("pSpeed").value));
$("pVol").addEventListener("input",()=>audio.volume=parseFloat($("pVol").value));
$("wave").addEventListener("click",(e)=>{ const rect=e.target.getBoundingClientRect();
  const p=(e.clientX-rect.left)/rect.width; if(audio.duration) audio.currentTime=p*audio.duration; });
function highlightPlaying(t){ document.querySelectorAll(".seg-row").forEach((row)=>{
  const s=parseFloat(row.dataset.start), e=parseFloat(row.dataset.end);
  row.classList.toggle("playing", t>=s && t<e); }); }

// ---------- transcrição ----------
function setEditorTools(show){
  document.querySelectorAll(".editor-tool").forEach((el)=>el.classList.toggle("hidden", !show));
}
function resetProc(stage){
  $("procBar").classList.remove("hidden");
  $("statusCard").classList.remove("hidden");
  setEditorTools(false);
  ensureProcChart(); resetProcChart();
  $("transcript").innerHTML=""; $("segmentsPlain").innerHTML="";
  $("statElapsed").textContent="00:00"; $("statEta").textContent="—";
  $("statDuration").textContent="—"; $("statSpeed").textContent="—";
  $("stageLabel").textContent=stage; setRing(0,"Processando…");
}
function setProgress(pct,indet){ const b=$("progressBar"); b.classList.toggle("indeterminate",!!indet);
  if(!indet){ b.style.width=pct+"%"; trackProc(pct); } }

// gráfico de progresso (% x tempo) acima da barra
let procChart=null, procT0=0, lastProcPct=0, procMax=0;
function ensureProcChart(){
  if(procChart || !window.Chart) return;
  procChart = new Chart($("procChart"), {
    type:"line",
    data:{ datasets:[{ data:[], borderColor:"#7C6CFF", borderWidth:2, tension:.3, pointRadius:0, fill:true,
      backgroundColor:(c)=>{ const {ctx,chartArea}=c.chart; if(!chartArea) return "transparent";
        const g=ctx.createLinearGradient(0,chartArea.top,0,chartArea.bottom);
        g.addColorStop(0,"#7C6CFF55"); g.addColorStop(1,"#22D3EE00"); return g; } }] },
    options:{ responsive:true, maintainAspectRatio:false, animation:false, parsing:false,
      plugins:{ legend:{display:false}, tooltip:{enabled:false} },
      scales:{
        x:{ type:"linear", min:0, max:5, ticks:{ color:"#8b93ad", font:{size:10}, maxTicksLimit:6,
            callback:(v)=>(v/60).toFixed(v<60?1:0)+" min" },
            grid:{ color:"rgba(255,255,255,.05)" } },
        y:{ min:0, max:100, ticks:{ color:"#8b93ad", font:{size:10}, stepSize:25, callback:(v)=>v+"%" },
            grid:{ color:"rgba(255,255,255,.05)" } } } }
  });
}
function trackProc(pct){
  ensureProcChart(); if(!procChart) return;
  const ds=procChart.data.datasets[0].data;
  if(pct < lastProcPct - 5 || ds.length===0){ procT0=Date.now(); ds.length=0; procMax=0; } // novo ciclo (upload→transcrição)
  lastProcPct=pct;
  const el=(Date.now()-procT0)/1000;
  ds.push({ x:+el.toFixed(1), y:+(+pct).toFixed(1) });
  // eixo X fixo ~ tempo estimado de término (nunca corta o ponto atual)
  const est = pct>2 ? el/(pct/100) : 0;
  procMax = Math.max(procMax||0, est, el*1.05, 5);
  procChart.options.scales.x.max = Math.ceil(procMax);
  procChart.update("none");
}
function resetProcChart(){ lastProcPct=0; procT0=Date.now(); procMax=0;
  if(procChart){ procChart.data.datasets[0].data.length=0; procChart.options.scales.x.max=5; procChart.update("none"); } }
function setRing(pct,state){ const C=326.7; $("ringFg").style.strokeDashoffset = C*(1-(pct/100));
  $("ringPct").textContent=Math.round(pct)+"%"; if(state!==undefined) $("ringState").textContent=state; }

$("startBtn").addEventListener("click", startTranscription);
function startTranscription(){
  if(!currentFile){ toast("Escolha um arquivo"); return; }
  const model = selectedModel; const m = MODELS.find((x)=>x.id===model);
  if(m && !m.downloaded){ toast(`Baixe o modelo "${m.label}" primeiro (aba Modelos)`); switchView("models"); return; }
  $("startBtn").disabled=true; resetProc("Preparando…"); setProgress(0,true);
  const lang=$("language").value, task=$("task").value;
  prepareFile(currentFile).then((toSend)=>{
  setProgress(0,false);
  uploadChunked(toSend, (sent,total)=>{ const p=total?sent/total*100:0;
    setProgress(p,false); setRing(p,"Enviando…");
    $("stageLabel").textContent=`Enviando áudio… ${Math.round(p)}% (${fmtSize(sent)} / ${fmtSize(total)})`; })
    .then((res)=>{
      fetch(`/api/transcribe/${res.job_id}/start?model=${encodeURIComponent(model)}&language=${lang}&task=${task}`,{method:"POST"})
        .then((r)=>r.json()).then(()=>{ localStorage.setItem(ACTIVE_KEY,res.job_id); connectStream(res.job_id); })
        .catch(()=>fail("Falha ao iniciar"));
    })
    .catch((err)=>fail(err && err.message ? err.message : "Falha no envio"));
  }).catch((err)=>fail(err && err.message ? err.message : "Falha ao preparar o áudio"));
}

// ---------- transcrever a partir de um link (YouTube/vídeo) ----------
$("urlBtn").addEventListener("click", startUrlTranscription);
$("urlInput").addEventListener("keydown",(e)=>{ if(e.key==="Enter"){ e.preventDefault(); startUrlTranscription(); } });
async function startUrlTranscription(){
  const url=$("urlInput").value.trim();
  if(!url){ toast("Cole um link"); return; }
  if(!/^https?:\/\//i.test(url)){ toast("Link inválido (use http/https)"); return; }
  const model=selectedModel; const m=MODELS.find((x)=>x.id===model);
  if(m && !m.downloaded){ toast(`Baixe o modelo "${m.label}" primeiro (aba Modelos)`); switchView("models"); return; }
  const lang=$("language").value, task=$("task").value;
  currentFile=null; currentSegments=[];
  $("jobCard").classList.remove("hidden");
  $("jobName").textContent="Baixando do link…";
  $("jobMeta").textContent=url.replace(/^https?:\/\//,"").slice(0,64);
  $("player").classList.add("hidden");
  $("startBtn").classList.add("hidden");
  setEditorTools(false);
  $("transcript").innerHTML='<p class="muted placeholder">Baixando o áudio do link…</p>';
  $("segmentsPlain").innerHTML="";
  $("jobCard").scrollIntoView({ behavior:"smooth", block:"nearest" });
  $("urlBtn").disabled=true; resetProc("Baixando o áudio…"); setProgress(0,true);
  try{
    const res=await fetch("/api/url/submit",{ method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ url, model, language:lang, task }) })
      .then(async (r)=>{ if(!r.ok){ let d="Falha"; try{d=(await r.json()).detail||d;}catch(_){ } throw new Error(d); } return r.json(); });
    localStorage.setItem(ACTIVE_KEY,res.job_id);
    connectStream(res.job_id);
  }catch(err){ $("urlBtn").disabled=false; fail(err && err.message ? err.message : "Falha ao baixar o link"); }
}

// Extrai só o áudio (16 kHz mono WAV) no navegador quando a opção está ligada e
// isso realmente reduz o tamanho (ex.: vídeos). Caso contrário, envia o original.
async function prepareFile(file){
  if($("extractOpt").classList.contains("hidden") || !$("extractAudio").checked) return file;
  try {
    $("stageLabel").textContent="Lendo o arquivo…"; setRing(0,"Preparando…");
    const buf = await file.arrayBuffer();
    $("stageLabel").textContent="Decodificando o áudio…";
    const AC = window.AudioContext || window.webkitAudioContext;
    const ac = new AC();
    const decoded = await ac.decodeAudioData(buf);
    ac.close();
    $("stageLabel").textContent="Convertendo para 16 kHz…";
    const rate = 16000;
    const off = new OfflineAudioContext(1, Math.ceil(decoded.duration * rate), rate);
    const src = off.createBufferSource(); src.buffer = decoded; src.connect(off.destination); src.start();
    const rendered = await off.startRendering();
    const wav = encodeWAV(rendered.getChannelData(0), rate);
    const base = (file.name.replace(/\.[^.]+$/, "") || "audio");
    const out = new File([wav], base + "_16k.wav", { type: "audio/wav" });
    if (out.size < file.size * 0.95) {
      toast(`Áudio extraído: ${fmtSize(file.size)} → ${fmtSize(out.size)}`);
      return out;
    }
    return file; // extração não ajudou (ex.: áudio já comprimido) → envia original
  } catch (e) {
    toast("Não deu para extrair o áudio aqui; enviando o arquivo original.");
    return file;
  }
}
function encodeWAV(samples, rate){
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true);
  v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, samples.length * 2, true);
  let o = 44;
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7FFF, true); o += 2;
  }
  return buf;
}

// Envia o arquivo em pedaços de 16 MB (passa pelo limite de requisição do Cloudflare).
const CHUNK_SIZE = 16 * 1024 * 1024;
async function uploadChunked(file, onProgress){
  const init = await fetch("/api/upload/init", { method:"POST",
    headers:{ "Content-Type":"application/json" },
    body: JSON.stringify({ name: file.name, size: file.size }) }).then((r)=>{ if(!r.ok) throw new Error("Falha ao iniciar envio"); return r.json(); });
  const jobId = init.job_id;
  let sent = 0;
  for (let start=0; start<file.size; start+=CHUNK_SIZE){
    const blob = file.slice(start, Math.min(start+CHUNK_SIZE, file.size));
    const base = sent;
    await putChunk(jobId, blob, (loaded)=>onProgress(base+loaded, file.size));
    sent += blob.size; onProgress(sent, file.size);
  }
  return fetch(`/api/upload/${jobId}/finish`, { method:"POST" }).then((r)=>{ if(!r.ok) throw new Error("Falha ao finalizar envio"); return r.json(); });
}
function putChunk(jobId, blob, onProg){
  return new Promise((resolve, reject)=>{
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/upload/${jobId}/chunk`);
    xhr.upload.onprogress = (e)=>{ if(e.lengthComputable) onProg(e.loaded); };
    xhr.onload = ()=> xhr.status===200 ? resolve() : reject(new Error("Erro no envio ("+xhr.status+")"));
    xhr.onerror = ()=> reject(new Error("Erro de rede no envio"));
    xhr.ontimeout = ()=> reject(new Error("Tempo esgotado no envio"));
    xhr.send(blob);
  });
}
function fail(msg){ $("stageLabel").textContent="Erro: "+msg; $("startBtn").disabled=false; $("urlBtn").disabled=false;
  localStorage.removeItem(ACTIVE_KEY); setRing(0,"Erro"); setProgress(0,false); toast("⚠ "+msg); }

function connectStream(jobId){
  currentJob=jobId; currentSegments=[]; $("startBtn").disabled=true;
  resetSummaryTab();
  resetProc("Retomando…"); setProgress(0,true);
  const t=$("transcript");
  const es=new EventSource(`/api/transcribe/${jobId}/stream`);
  es.addEventListener("log",(e)=>{ const msg=JSON.parse(e.data).message; $("stageLabel").textContent=msg; $("ringState").textContent=msg.slice(0,18); });
  es.addEventListener("start",(e)=>{ const d=JSON.parse(e.data); currentSegments=[]; t.innerHTML="";
    if(d.name){ $("jobName").textContent=d.name.replace(/\.[^.]+$/,""); if(!currentFile) $("jobMeta").textContent="do link · "+(d.model||""); }
    $("stageLabel").textContent=`Transcrevendo (${(d.device||"").toUpperCase()}${d.compute_type?" · "+d.compute_type:""})`;
    $("statDuration").textContent=fmtClock(d.duration); setRing(0,"Processando…"); setProgress(0,false); });
  es.addEventListener("segment",(e)=>{ const d=JSON.parse(e.data);
    currentSegments.push({start:d.start,end:d.end,text:d.text}); addSegRow(d); t.scrollTop=t.scrollHeight;
    const p=(d.progress||0)*100; setProgress(p,false); setRing(p,"Processando…");
    $("statElapsed").textContent=fmtTime(d.elapsed); $("statEta").textContent=fmtTime(d.eta); });
  es.addEventListener("done",(e)=>{ const d=JSON.parse(e.data); setProgress(100,false); setRing(100,"Concluído ✓");
    $("stageLabel").textContent="Concluído ✓"; $("statElapsed").textContent=fmtTime(d.elapsed); $("statEta").textContent="0s";
    $("statSpeed").textContent=d.speed?d.speed+"×":"—";
    if(!currentSegments.length) t.innerHTML='<p class="muted">(silêncio — nada transcrito)</p>';
    buildSegmentsPlain(); setEditorTools(currentSegments.length>0);
    $("startBtn").disabled=false; $("urlBtn").disabled=false; $("urlInput").value=""; es.close(); localStorage.removeItem(ACTIVE_KEY);
    loadHistory(); loadModels(selectedModel); toast("Transcrição concluída ✓"); });
  es.addEventListener("error",(e)=>{ let msg="Falha na transcrição"; try{msg=JSON.parse(e.data).message;}catch(_){}
    fail(msg); es.close(); });
}
function addSegRow(d){
  const row=document.createElement("div"); row.className="seg-row";
  row.dataset.start=d.start; row.dataset.end=d.end;
  row.innerHTML=`<span class="seg-ts">${fmtClock(d.start)}</span>
    <button class="seg-play" title="Tocar">▶</button>
    <div class="seg-text" contenteditable="true" spellcheck="false">${esc(d.text.trim())}</div>
    <div class="seg-tools"><button class="seg-copy" title="Copiar">⧉</button></div>`;
  row.querySelector(".seg-play").onclick=()=>{ if(audio.src){ audio.currentTime=d.start; audio.play(); } };
  row.querySelector(".seg-copy").onclick=()=>{ navigator.clipboard.writeText(row.querySelector(".seg-text").innerText); toast("Trecho copiado"); };
  $("transcript").appendChild(row);
}
function buildSegmentsPlain(){
  $("segmentsPlain").innerHTML = currentSegments.map((s)=>
    `<span class="ts">[${fmtClock(s.start)}]</span> ${esc(s.text.trim())}`).join("\n");
}
function collectText(){ return [...document.querySelectorAll("#transcript .seg-text")].map((e)=>e.innerText.trim()).join(" "); }
// Uma frase por linha (mesma lógica do servidor): junta tudo e quebra após . ! ? …
function toLines(text){ text=(text||"").replace(/\s+/g," ").trim(); if(!text) return "";
  return text.split(/(?<=[.!?…])\s+/).map((s)=>s.trim()).filter(Boolean).join("\n"); }
function collectSegEdited(){ return [...document.querySelectorAll("#transcript .seg-row")].map((row)=>({
  start:parseFloat(row.dataset.start), end:parseFloat(row.dataset.end), text:row.querySelector(".seg-text").innerText.trim() })); }

async function resumeActive(){
  const jobId=localStorage.getItem(ACTIVE_KEY); if(!jobId) return;
  try { const s=await fetch(`/api/transcribe/${jobId}/status`).then((r)=>r.json());
    if(s.found && (s.status==="running"||s.status==="queued"||s.status==="fetching")){ $("jobCard").classList.remove("hidden");
      $("jobName").textContent=(s.status==="fetching"?"Baixando do link…":(s.name||"áudio"));
      $("startBtn").classList.add("hidden"); $("player").classList.add("hidden"); connectStream(jobId); }
    else localStorage.removeItem(ACTIVE_KEY);
  } catch(_){ localStorage.removeItem(ACTIVE_KEY); }
}

// ---------- abas ----------
document.querySelectorAll(".tab").forEach((tab)=>tab.addEventListener("click",()=>{
  document.querySelectorAll(".tab").forEach((t)=>t.classList.remove("active"));
  tab.classList.add("active");
  document.querySelectorAll(".tab-panel").forEach((p)=>p.classList.add("hidden"));
  const panel=$("panel-"+tab.dataset.tab); if(panel) panel.classList.remove("hidden");
}));

// ---------- busca no texto ----------
$("searchInput").addEventListener("input",(e)=>{
  const q=e.target.value.trim().toLowerCase();
  document.querySelectorAll("#transcript .seg-row").forEach((row)=>{
    const el=row.querySelector(".seg-text"); const txt=el.innerText;
    if(!q){ row.style.display=""; row.classList.remove("hit"); el.innerHTML=esc(txt); return; }
    const hit=txt.toLowerCase().includes(q);
    row.style.display = hit ? "" : "none"; row.classList.toggle("hit",hit);
    if(hit){ const re=new RegExp("("+q.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","ig");
      el.innerHTML=esc(txt).replace(re,"<mark>$1</mark>"); }
  });
});

// ---------- copiar / exportar (job atual, respeita edições) ----------
$("copyBtn").addEventListener("click",()=>{ navigator.clipboard.writeText(collectText()).then(()=>toast("Transcrição copiada ✓")); });
$("exportBtn").addEventListener("click",(e)=>{ e.stopPropagation(); $("exportMenu").classList.toggle("open"); });
document.querySelectorAll("#exportMenu button").forEach((b)=>b.addEventListener("click",()=>{
  $("exportMenu").classList.remove("open");
  const base=(($("jobName").textContent||"transcricao").replace(/\.[^.]+$/,""))||"transcricao";
  if(b.dataset.exp==="txt") downloadBlob(toLines(collectText()), base+".txt");
  else downloadBlob(buildSRT(collectSegEdited()), base+".srt");
}));
function srtTime(s){ s=Math.max(0,s); const ms=Math.round(s*1000); const h=Math.floor(ms/3600000),
  m=Math.floor(ms%3600000/60000), x=Math.floor(ms%60000/1000), q=ms%1000;
  return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(x).padStart(2,"0")},${String(q).padStart(3,"0")}`; }
function buildSRT(segs){ return segs.map((s,i)=>`${i+1}\n${srtTime(s.start)} --> ${srtTime(s.end)}\n${s.text}\n`).join("\n"); }
function downloadBlob(text,name){ const b=new Blob([text],{type:"text/plain;charset=utf-8"});
  const u=URL.createObjectURL(b); const a=document.createElement("a"); a.href=u; a.download=name; a.click();
  setTimeout(()=>URL.revokeObjectURL(u),1000); }

// ---------- resumo (LLM) ----------
function activateTab(name){
  document.querySelectorAll(".tab").forEach((t)=>t.classList.toggle("active", t.dataset.tab===name));
  document.querySelectorAll(".tab-panel").forEach((p)=>p.classList.add("hidden"));
  const panel=$("panel-"+name); if(panel) panel.classList.remove("hidden");
}
async function loadSummaryStyles(){
  try { const d=await fetch("/api/summary/styles").then((r)=>r.json());
    SUMMARY_STYLES=d.styles||[]; selectedStyle=d.default||"completo"; }
  catch(_){ SUMMARY_STYLES=[{id:"completo",label:"Completo"},{id:"topicos",label:"Em tópicos"}]; }
  renderStylePills();
}
function renderStylePills(){
  const wrap=$("summaryStyles"); if(!wrap) return; wrap.innerHTML="";
  SUMMARY_STYLES.forEach((s)=>{
    const b=document.createElement("button"); b.type="button";
    b.className="style-pill"+(s.id===selectedStyle?" active":""); b.dataset.style=s.id;
    b.textContent=s.label; b.title=s.desc||"";
    b.onclick=()=>{ if(selectedStyle===s.id) return; selectedStyle=s.id; renderStylePills(); showStyleForCurrent(); };
    wrap.appendChild(b);
  });
}
function summaryIsRunning(){ return summaryES!=null; }
// Mostra o resumo do estilo selecionado para o job atual: cache → servidor → vazio.
async function showStyleForCurrent(){
  if(summaryIsRunning()) return;            // não atropela uma geração em curso
  if(!currentJob){ summaryPlaceholder(); return; }
  const cached=(summaryCache[currentJob]||{})[selectedStyle];
  if(cached!=null){ renderSummaryDone(currentJob, selectedStyle, cached); return; }
  try {
    const r=await fetch(`/api/summary/${currentJob}/view?style=${selectedStyle}`);
    if(r.ok){ const d=await r.json(); (summaryCache[currentJob]=summaryCache[currentJob]||{})[selectedStyle]=d.md;
      renderSummaryDone(currentJob, selectedStyle, d.md); return; }
  } catch(_){}
  summaryPlaceholder();                     // ainda não gerado neste estilo
}
function summaryPlaceholder(){
  $("genSummaryBtn").disabled=false; $("genSummaryBtn").textContent="✦ Gerar resumo";
  $("dlSummaryBtn").classList.add("hidden"); $("summaryStage").textContent="";
  $("summaryBody").innerHTML='<p class="muted placeholder">Escolha o tipo de resumo e gere com IA rodando na sua GPU. O resultado pode ser baixado em Markdown (.md).</p>';
}
function renderSummaryDone(jobId, style, body){
  $("genSummaryBtn").disabled=false; $("genSummaryBtn").textContent="✦ Gerar de novo";
  $("summaryStage").textContent="";
  $("summaryBody").innerHTML=mdToHtml(body||"");
  const a=$("dlSummaryBtn"); a.href=`/api/summary/${jobId}.md?style=${style}`; a.classList.remove("hidden");
}
function resetSummaryTab(){
  if(summaryES){ summaryES.close(); summaryES=null; }
  showStyleForCurrent();
}
$("genSummaryBtn").addEventListener("click", ()=>generateSummary(selectedStyle));
function generateSummary(style){
  if(!currentJob){ toast("Transcreva algo primeiro"); return; }
  const model=summaryModelToUse();
  if(!model){ toast("Baixe um modelo de resumo primeiro (aba Modelos)"); switchView("models"); return; }
  $("genSummaryBtn").disabled=true; $("dlSummaryBtn").classList.add("hidden");
  $("summaryStage").textContent="Iniciando…";
  $("summaryBody").innerHTML='<p class="muted placeholder">Gerando o resumo… isso usa a GPU e pode levar alguns segundos.</p>';
  activateTab("summary");
  fetch(`/api/summarize/${currentJob}?model=${encodeURIComponent(model)}&style=${style}`,{method:"POST"})
    .then(async (r)=>{ if(!r.ok){ let d="Falha"; try{d=(await r.json()).detail||d;}catch(_){ } throw new Error(d);} return r.json(); })
    .then(()=>connectSummaryStream(currentJob, style))
    .catch((err)=>{ $("genSummaryBtn").disabled=false; $("summaryStage").textContent="";
      summaryPlaceholder();
      toast("⚠ "+(err&&err.message?err.message:"falha")); });
}
function connectSummaryStream(jobId, style){
  if(summaryES){ summaryES.close(); }
  const es=new EventSource(`/api/summarize/${jobId}/stream`); summaryES=es;
  es.addEventListener("log",(e)=>{ $("summaryStage").textContent=JSON.parse(e.data).message; });
  es.addEventListener("done",(e)=>{ const d=JSON.parse(e.data); es.close(); summaryES=null;
    const st=d.style||style;
    (summaryCache[jobId]=summaryCache[jobId]||{})[st]=d.md||"";
    if(jobId===currentJob && st===selectedStyle) renderSummaryDone(jobId, st, d.md||"");
    loadHistory(); toast("Resumo pronto ✓"); });
  es.addEventListener("error",(e)=>{ let msg="falha"; try{msg=JSON.parse(e.data).message;}catch(_){ }
    es.close(); summaryES=null; $("genSummaryBtn").disabled=false;
    $("summaryStage").textContent="Erro: "+msg; toast("⚠ "+msg); });
}
// Religa os resumos que continuaram rodando no servidor (após refresh).
async function reconnectSummaries(){
  let data; try { data=await fetch("/api/summarize/active").then((r)=>r.json()); } catch(_){ return; }
  (data.jobs||[]).forEach((j)=>{
    if(j.job_id===currentJob){ selectedStyle=j.style; renderStylePills();
      $("genSummaryBtn").disabled=true; $("summaryStage").textContent="Gerando…";
      $("summaryBody").innerHTML='<p class="muted placeholder">Retomando o resumo em andamento…</p>';
      connectSummaryStream(j.job_id, j.style); }
    else attachLibrarySummary(j.job_id, j.style);
  });
}
// Markdown mínimo (títulos, listas, negrito, código) para exibir o resumo.
function mdToHtml(md){
  const lines=String(md||"").replace(/\r/g,"").split("\n"); let html="", inList=false;
  const inline=(s)=>esc(s).replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>").replace(/`([^`]+?)`/g,"<code>$1</code>");
  const close=()=>{ if(inList){ html+="</ul>"; inList=false; } };
  for(const raw of lines){ const line=raw.trim(); if(!line){ close(); continue; } let m;
    if((m=line.match(/^(#{1,4})\s+(.*)$/))){ close(); const lv=Math.min(m[1].length+1,5); html+=`<h${lv} class="md-h">${inline(m[2])}</h${lv}>`; }
    else if((m=line.match(/^[-*]\s+(.*)$/))){ if(!inList){ html+="<ul class='md-ul'>"; inList=true; } html+=`<li>${inline(m[1])}</li>`; }
    else { close(); html+=`<p>${inline(line)}</p>`; } }
  close(); return html||'<p class="muted">(resumo vazio)</p>';
}

// ---------- biblioteca / histórico ----------
let HISTORY=[];
async function loadHistory(){
  let data; try { data=await fetch("/api/history").then((r)=>r.json()); } catch(_){ return; }
  HISTORY=data.items||[]; $("navHistCount").textContent = HISTORY.length || "";
  renderHistory(HISTORY);
}
function renderHistory(items){
  const wrap=$("historyList");
  if(!items.length){ wrap.innerHTML='<p class="muted placeholder">Nenhuma transcrição ainda. As concluídas ficam salvas aqui.</p>'; return; }
  wrap.innerHTML="";
  items.forEach((m)=>{
    const row=document.createElement("div"); row.className="hist-item"; row.dataset.id=m.job_id;
    row.innerHTML=`<div class="hist-head"><div class="hist-main">
      <span class="hist-name">${esc(m.orig_name||"áudio")}</span>
      <span class="muted tiny">${fmtDate(m.date)} · ${m.duration?fmtClock(m.duration):"—"} · ${m.model||""} · ${m.chars||0} car.</span>
      </div><div class="hist-actions">
      <a class="m-btn" href="/api/result/${m.job_id}.txt" download>.txt</a>
      <a class="m-btn" href="/api/result/${m.job_id}.srt" download>.srt</a>
      ${histSummaryActions(m)}
      <button class="m-btn hist-del" type="button" title="Apagar">✕</button></div></div>
      <div class="hist-body"></div>`;
    row.querySelector(".hist-head").addEventListener("click",(e)=>{ if(e.target.closest("a")||e.target.closest("button")) return;
      toggleHist(row,m.job_id); });
    row.querySelector(".hist-del").onclick=()=>delHist(m.job_id,m.orig_name);
    row.querySelectorAll(".hist-sum").forEach((b)=>{ b.onclick=()=>genHistSummary(m.job_id,row,b.dataset.style); });
    wrap.appendChild(row);
    if(libSummaryES[m.job_id]){ const b=row.querySelector(".hist-sum"); if(b){ b.disabled=true; b.textContent="gerando…"; } }
  });
}
function shortStyle(id){ return id==="topicos" ? "tópicos" : "resumo"; }
function histSummaryActions(m){
  const styles = SUMMARY_STYLES.length ? SUMMARY_STYLES
    : [{id:"completo",label:"Completo"},{id:"topicos",label:"Em tópicos"}];
  const have = m.summaries || (m.has_summary ? {completo:true} : {});
  let html="";
  styles.forEach((s)=>{ if(have[s.id])
    html += `<a class="m-btn m-md" href="/api/summary/${m.job_id}.md?style=${s.id}" download title="Baixar resumo ${s.label} (.md)">⬇ ${shortStyle(s.id)}</a>`; });
  const missing = styles.find((s)=>!have[s.id]);
  if(missing) html += `<button class="m-btn hist-sum" type="button" data-style="${missing.id}" title="Gerar resumo ${missing.label}">✦ ${shortStyle(missing.id)}</button>`;
  return html;
}
async function toggleHist(row,id){
  const body=row.querySelector(".hist-body");
  if(row.classList.contains("open")){ row.classList.remove("open"); body.innerHTML=""; return; }
  row.classList.add("open"); body.innerHTML='<p class="muted tiny">carregando…</p>';
  try { const d=await fetch(`/api/history/${id}`).then((r)=>r.json()); body.textContent=d.text||"(vazio)"; }
  catch(_){ body.innerHTML='<p class="muted tiny">erro ao carregar</p>'; }
}
function genHistSummary(id,row,style){
  style = style || "completo";
  const model=summaryModelToUse();
  if(!model){ toast("Baixe um modelo de resumo primeiro (aba Modelos)"); switchView("models"); return; }
  const btn=row.querySelector(".hist-sum"); if(btn){ btn.disabled=true; btn.textContent="gerando…"; }
  fetch(`/api/summarize/${id}?model=${encodeURIComponent(model)}&style=${style}`,{method:"POST"})
    .then(async (r)=>{ if(!r.ok){ let d="Falha"; try{d=(await r.json()).detail||d;}catch(_){ } throw new Error(d);} return r.json(); })
    .then(()=>attachLibrarySummary(id, style))
    .catch((err)=>{ if(btn){ btn.disabled=false; btn.textContent="✦ "+shortStyle(style); } toast("⚠ "+(err&&err.message?err.message:"falha")); });
}
// Acompanha (ou religa) um resumo rodando no servidor e atualiza a biblioteca.
function attachLibrarySummary(id, style){
  if(libSummaryES[id]) return;
  const es=new EventSource(`/api/summarize/${id}/stream`); libSummaryES[id]=es;
  const setBtn=(txt,dis)=>{ const row=document.querySelector(`.hist-item[data-id="${id}"]`);
    const b=row&&row.querySelector(".hist-sum"); if(b){ b.disabled=dis; b.textContent=txt; } };
  setBtn("gerando…", true);
  es.addEventListener("done",(e)=>{ let st=style; try{st=JSON.parse(e.data).style||style;}catch(_){ }
    es.close(); delete libSummaryES[id]; if(summaryCache[id]) delete summaryCache[id];
    loadHistory(); toast("Resumo pronto ✓"); });
  es.addEventListener("error",(e)=>{ let m="falha"; try{m=JSON.parse(e.data).message;}catch(_){ }
    es.close(); delete libSummaryES[id]; setBtn("✦ "+shortStyle(style), false); toast("⚠ "+m); });
}
function delHist(id,name){ if(!confirm(`Apagar a transcrição de "${name||"áudio"}"?`)) return;
  fetch(`/api/history/${id}`,{method:"DELETE"}).finally(()=>{ loadHistory(); toast("Transcrição apagada"); }); }
$("libSearch").addEventListener("input",(e)=>{ const q=e.target.value.trim().toLowerCase();
  renderHistory(HISTORY.filter((m)=>(m.orig_name||"").toLowerCase().includes(q))); });

// ---------- painel técnico ----------
$("techToggle").addEventListener("click",()=>$("techCard").classList.toggle("collapsed"));

// ---------- gráficos / métricas ----------
const POINTS=40;
function makeChart(ctx,color){ return new Chart(ctx,{ type:"line",
  data:{ labels:Array(POINTS).fill(""), datasets:[{ data:Array(POINTS).fill(0), borderColor:color, borderWidth:2,
    tension:.35, fill:true, pointRadius:0, backgroundColor:(c)=>{ const {ctx,chartArea}=c.chart; if(!chartArea) return "transparent";
      const g=ctx.createLinearGradient(0,chartArea.top,0,chartArea.bottom); g.addColorStop(0,color+"55"); g.addColorStop(1,color+"00"); return g; } }] },
  options:{ responsive:true, maintainAspectRatio:false, animation:false, plugins:{legend:{display:false}},
    scales:{ x:{display:false}, y:{min:0,max:100,display:false} } } }); }
let cpuChart,gpuChart;
function push(ch,v){ if(!ch) return; const d=ch.data.datasets[0].data; d.push(v); d.shift(); ch.update("none"); }
function startMetrics(){ cpuChart=makeChart($("cpuChart"),"#22D3EE");
  if(!$("gpuMetric").classList.contains("hidden")) gpuChart=makeChart($("gpuChart"),"#7C6CFF");
  tick(); setInterval(tick,1800); }
async function tick(){
  let m; try { m=await fetch("/api/metrics").then((r)=>r.json()); } catch(_){ return; }
  $("cpuNow").textContent=Math.round(m.cpu.percent)+"%"; push(cpuChart,m.cpu.percent);
  $("ramBar").style.width=m.mem.percent+"%";
  $("ramTxt").textContent=`${(m.mem.used_mb/1024).toFixed(1)} / ${(m.mem.total_mb/1024).toFixed(1)} GB`;
  if(m.disk){ $("storageBar").style.width=m.disk.percent+"%";
    $("storageTxt").textContent=`${m.disk.used_gb} GB / ${m.disk.total_gb} GB`; }
  if(m.gpus && m.gpus.length){ const g=m.gpus[0];
    $("gpuNow").textContent=g.util+"%"; push(gpuChart,g.util);
    $("vramBar").style.width=g.mem_percent+"%";
    $("vramTxt").textContent=`${(g.mem_used_mb/1024).toFixed(1)} / ${(g.mem_total_mb/1024).toFixed(1)} GB`;
    $("tempTxt").textContent=g.temp!=null?g.temp+"°C":"—";
    $("powerTxt").textContent=g.power!=null?g.power+" W":"—";
    $("gpuMiniName").textContent=g.name;
    // ocupada = a placa está realmente trabalhando (util acima do ruído de ociosidade)
    const busy = (g.util||0) >= 4;
    const dot = $("gpuMiniDot");
    dot.classList.remove("off");
    dot.classList.toggle("busy", busy);
    dot.classList.toggle("idle", !busy);
    if(busy){
      // uso em tempo real, discreto: placa + memória
      $("gpuMiniVram").textContent=`GPU ${g.util}% · ${(g.mem_used_mb/1024).toFixed(1)} GB`;
    } else {
      $("gpuMiniVram").textContent=`${(g.mem_total_mb/1024).toFixed(0)} GB VRAM · ociosa`;
    }
  }
}

window.addEventListener("resize",()=>{ if(!$("player").classList.contains("hidden")) drawWave(audio.currentTime/(audio.duration||1)); });
init();
