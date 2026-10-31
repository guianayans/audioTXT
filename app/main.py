"""audioTXT — transcrição de áudio local com Whisper (GPU) e painel web.

FastAPI serve o front-end, protege tudo com senha (definida no Coolify),
expõe métricas de CPU/GPU, gerencia o download dos modelos e transcreve o
áudio enviado, transmitindo cada trecho ao vivo via SSE.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import metrics, summarizer, transcriber

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.environ.get("AUDIOTXT_DATA_DIR", "/data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
RESULT_DIR = os.path.join(DATA_DIR, "results")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)

PASSWORD = os.environ.get("AUDIOTXT_PASSWORD", "")
SECRET = os.environ.get("AUDIOTXT_SESSION_SECRET") or secrets.token_hex(32)
COOKIE = "audiotxt_session"
MAX_AGE = int(os.environ.get("AUDIOTXT_SESSION_HOURS", "72")) * 3600

serializer = URLSafeTimedSerializer(SECRET, salt="audiotxt")
app = FastAPI(title="audioTXT", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Evita que o CDN/navegador sirvam JS/CSS antigos após um deploy.

    Com no-cache o cliente/CDN revalidam pelo ETag e pegam a versão nova assim
    que ela muda (os assets também são versionados com ?v= no HTML).
    """
    resp = await call_next(request)
    if request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

# metadados de transcrições em memória (job_id -> info)
JOBS: dict[str, dict] = {}
SUMMARY_JOBS: dict[str, dict] = {}


# --------------------------------------------------------------------------
# Autenticação por cookie assinado
# --------------------------------------------------------------------------
def is_authed(request: Request) -> bool:
    if not PASSWORD:
        # sem senha configurada: libera (mostra aviso no front)
        return True
    token = request.cookies.get(COOKIE)
    if not token:
        return False
    try:
        serializer.loads(token, max_age=MAX_AGE)
        return True
    except (BadSignature, Exception):
        return False


def require_auth(request: Request):
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="Não autorizado")
    return True


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if is_authed(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(os.path.join(WEB_DIR, "login.html"))


@app.post("/api/login")
def login(request: Request, password: str = Form("")):
    if not PASSWORD:
        return JSONResponse({"ok": True, "no_password": True})
    if secrets.compare_digest(password, PASSWORD):
        token = serializer.dumps("ok")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax",
            secure=os.environ.get("AUDIOTXT_COOKIE_SECURE", "true") == "true",
        )
        return resp
    return JSONResponse({"ok": False, "error": "Senha incorreta"}, status_code=401)


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


# --------------------------------------------------------------------------
# Config, métricas e modelos
# --------------------------------------------------------------------------
@app.get("/api/config")
def config(_: bool = Depends(require_auth)):
    return {
        "gpu_available": metrics.gpu_available(),
        "active_device": transcriber.active_device(),
        "default_model": transcriber.DEFAULT_MODEL,
        "no_password": not PASSWORD,
    }


@app.get("/api/metrics")
def get_metrics(_: bool = Depends(require_auth)):
    return metrics.read_metrics()


@app.get("/api/models")
def list_models(_: bool = Depends(require_auth)):
    return {"models": transcriber.catalog(),
            "default": transcriber.DEFAULT_MODEL}


_downloads: dict[str, transcriber.DownloadJob] = {}


@app.get("/api/models/{model_id}/download")
def download_model(model_id: str, _: bool = Depends(require_auth)):
    if model_id not in transcriber.MODELS_BY_ID:
        raise HTTPException(404, "Modelo desconhecido")

    def stream():
        job = transcriber.DownloadJob(model_id)
        _downloads[model_id] = job
        total = transcriber.MODELS_BY_ID[model_id]["size_mb"]
        if transcriber.is_downloaded(model_id):
            yield sse("done", {"model": model_id, "already": True})
            return
        job.start()
        yield sse("start", {"model": model_id, "total_mb": total})
        while True:
            job.poll()
            if job.done:
                break
            mb = job.progress_mb()
            yield sse("progress", {
                "mb": mb, "total_mb": total,
                "percent": round(min(mb / total * 100, 99.5), 1) if total else 0,
            })
            time.sleep(0.8)
        _downloads.pop(model_id, None)
        if job.cancelled:
            yield sse("cancelled", {"model": model_id})
        elif job.error:
            yield sse("error", {"message": job.error})
        else:
            yield sse("done", {"model": model_id,
                               "mb": job.progress_mb(), "total_mb": total})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers=_sse_headers())


@app.post("/api/models/{model_id}/cancel")
def cancel_download(model_id: str, _: bool = Depends(require_auth)):
    if model_id not in transcriber.MODELS_BY_ID:
        raise HTTPException(404, "Modelo desconhecido")
    job = _downloads.get(model_id)
    if job:
        job.cancel()  # mata o subprocesso e apaga o parcial
        _downloads.pop(model_id, None)
        return {"ok": True, "cancelled": True}
    # sem download ativo: ainda assim remove resíduo parcial, se houver
    transcriber.delete_model(model_id)
    return {"ok": True, "cancelled": False}


@app.delete("/api/models/{model_id}")
def delete_model_route(model_id: str, _: bool = Depends(require_auth)):
    if model_id not in transcriber.MODELS_BY_ID:
        raise HTTPException(404, "Modelo desconhecido")
    job = _downloads.get(model_id)
    if job:
        job.cancel()
        _downloads.pop(model_id, None)
    removed = transcriber.delete_model(model_id)
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------
# Modelos de resumo (LLM) — baixar/cancelar/apagar, no mesmo padrão do Whisper
# --------------------------------------------------------------------------
_summary_downloads: dict[str, summarizer.SummaryDownloadJob] = {}


@app.get("/api/summary/models")
def list_summary_models(_: bool = Depends(require_auth)):
    return {"models": summarizer.catalog(), "default": summarizer.DEFAULT_MODEL}


@app.get("/api/summary/models/{model_id}/download")
def download_summary_model(model_id: str, _: bool = Depends(require_auth)):
    if model_id not in summarizer.SUMMARY_BY_ID:
        raise HTTPException(404, "Modelo desconhecido")

    def stream():
        job = summarizer.SummaryDownloadJob(model_id)
        _summary_downloads[model_id] = job
        total = summarizer.SUMMARY_BY_ID[model_id]["size_mb"]
        if summarizer.is_downloaded(model_id):
            yield sse("done", {"model": model_id, "already": True})
            return
        job.start()
        yield sse("start", {"model": model_id, "total_mb": total})
        while True:
            job.poll()
            if job.done:
                break
            mb = job.progress_mb()
            yield sse("progress", {
                "mb": mb, "total_mb": total,
                "percent": round(min(mb / total * 100, 99.5), 1) if total else 0,
            })
            time.sleep(0.8)
        _summary_downloads.pop(model_id, None)
        if job.cancelled:
            yield sse("cancelled", {"model": model_id})
        elif job.error:
            yield sse("error", {"message": job.error})
        else:
            yield sse("done", {"model": model_id,
                               "mb": job.progress_mb(), "total_mb": total})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers=_sse_headers())


@app.post("/api/summary/models/{model_id}/cancel")
def cancel_summary_download(model_id: str, _: bool = Depends(require_auth)):
    if model_id not in summarizer.SUMMARY_BY_ID:
        raise HTTPException(404, "Modelo desconhecido")
    job = _summary_downloads.get(model_id)
    if job:
        job.cancel()
        _summary_downloads.pop(model_id, None)
        return {"ok": True, "cancelled": True}
    summarizer.delete_model(model_id)
    return {"ok": True, "cancelled": False}


@app.delete("/api/summary/models/{model_id}")
def delete_summary_model(model_id: str, _: bool = Depends(require_auth)):
    if model_id not in summarizer.SUMMARY_BY_ID:
        raise HTTPException(404, "Modelo desconhecido")
    job = _summary_downloads.get(model_id)
    if job:
        job.cancel()
        _summary_downloads.pop(model_id, None)
    removed = summarizer.delete_model(model_id)
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------
# Upload + transcrição em segundo plano (sobrevive a reload; reconectável)
# --------------------------------------------------------------------------
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _check_id(job_id: str):
    if not _ID_RE.match(job_id):
        raise HTTPException(400, "id inválido")


def _meta_path(job_id: str) -> str:
    return os.path.join(RESULT_DIR, f"{job_id}.json")


def _load_meta(job_id: str) -> dict | None:
    p = _meta_path(job_id)
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None
    return None


@app.post("/api/upload")
async def upload(request: Request, file: UploadFile,
                 _: bool = Depends(require_auth)):
    job_id = uuid.uuid4().hex
    safe_name = os.path.basename(file.filename or "audio")
    dest = os.path.join(UPLOAD_DIR, f"{job_id}__{safe_name}")
    size = 0
    with open(dest, "wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            fh.write(chunk)
    JOBS[job_id] = _new_job(job_id, dest, safe_name, size)
    return {"job_id": job_id, "name": safe_name, "size": size}


def _new_job(job_id: str, path: str, name: str, size: int, status="uploaded") -> dict:
    return {
        "path": path, "name": name, "size": size, "created": time.time(),
        "status": status, "model": None, "language": None, "task": None,
        "segments": [], "logs": [], "duration": None, "detected_lang": None,
        "lang_prob": None, "device": None, "compute_type": None,
        "progress": 0.0, "elapsed": 0.0, "eta": 0.0, "speed": None,
        "text": None, "base": None, "error": None,
    }


# Upload em pedaços (chunks) — necessário p/ arquivos grandes atrás do Cloudflare,
# que limita o tamanho de cada requisição. O cliente envia em sequência.
@app.post("/api/upload/init")
async def upload_init(request: Request, _: bool = Depends(require_auth)):
    data = await request.json()
    job_id = uuid.uuid4().hex
    safe_name = os.path.basename(str(data.get("name") or "audio"))
    dest = os.path.join(UPLOAD_DIR, f"{job_id}__{safe_name}")
    open(dest, "wb").close()  # começa vazio
    JOBS[job_id] = _new_job(job_id, dest, safe_name, 0, status="uploading")
    return {"job_id": job_id}


@app.put("/api/upload/{job_id}/chunk")
async def upload_chunk(job_id: str, request: Request, _: bool = Depends(require_auth)):
    job = JOBS.get(job_id)
    if not job or job["status"] != "uploading":
        raise HTTPException(404, "Upload não encontrado")
    body = await request.body()
    with open(job["path"], "ab") as fh:
        fh.write(body)
    return {"ok": True, "received": os.path.getsize(job["path"])}


@app.post("/api/upload/{job_id}/finish")
async def upload_finish(job_id: str, _: bool = Depends(require_auth)):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Upload não encontrado")
    job["size"] = os.path.getsize(job["path"])
    job["status"] = "uploaded"
    return {"job_id": job_id, "name": job["name"], "size": job["size"]}


@app.post("/api/url/submit")
async def url_submit(request: Request, _: bool = Depends(require_auth)):
    """Recebe um link (YouTube/vídeo), baixa o áudio no servidor e transcreve.

    Não passa pelo upload do navegador, então ignora o limite do Cloudflare.
    """
    data = await request.json()
    url = str(data.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise HTTPException(400, "Link inválido — use um endereço http/https.")
    model = str(data.get("model") or "")
    language = str(data.get("language") or "auto")
    task = str(data.get("task") or "transcribe")
    model_id = (model if model in transcriber.MODELS_BY_ID
                else transcriber.DEFAULT_MODEL)
    job_id = uuid.uuid4().hex
    JOBS[job_id] = _new_job(job_id, "", "link", 0, status="fetching")
    JOBS[job_id].update(model=model_id, language=language, task=task,
                        source_url=url)
    threading.Thread(target=_run_url_job,
                     args=(job_id, url, model_id, language, task),
                     daemon=True).start()
    return {"job_id": job_id}


def _run_url_job(job_id: str, url: str, model_id: str, language: str, task: str):
    """Baixa o áudio do link e, em seguida, roda a transcrição normal."""
    job = JOBS.get(job_id)
    if not job:
        return
    job["status"] = "fetching"
    job["logs"].append("Preparando o download do link…")
    try:
        path, title = transcriber.download_media(
            url, UPLOAD_DIR, job_id, log=lambda m: job["logs"].append(m))
        job["path"] = path
        ext = os.path.splitext(path)[1]
        job["name"] = f"{title}{ext}" if title else os.path.basename(path)
        job["size"] = os.path.getsize(path)
        job["logs"].append(f"Baixado ✓ ({_fmt_mb(job['size'])}) — iniciando transcrição…")
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        return
    _run_job(job_id, model_id, language, task)


def _fmt_mb(size: int) -> str:
    mb = size / (1024 * 1024)
    return f"{mb:.0f} MB" if mb >= 1 else f"{size / 1024:.0f} KB"


def _run_job(job_id: str, model_id: str, language: str, task: str):
    """Worker em segundo plano. Roda mesmo se o cliente fechar a página."""
    job = JOBS.get(job_id)
    if not job:
        return
    job["status"] = "queued"
    if transcriber.transcribe_lock.locked():
        job["logs"].append("Na fila — aguardando outra transcrição terminar…")
    transcriber.transcribe_lock.acquire()  # bloqueia → vira fila natural
    try:
        if JOBS.get(job_id) is not job:  # foi apagado enquanto esperava
            return
        job["status"] = "running"

        def log(msg):
            job["logs"].append(msg)

        for event in transcriber.transcribe(job["path"], model_id, language,
                                             task, log=log):
            t = event["type"]
            if t == "start":
                job["duration"] = event["duration"]
                job["detected_lang"] = event["language"]
                job["lang_prob"] = event.get("language_prob")
                job["device"] = event["device"]
                job["compute_type"] = event["compute_type"]
            elif t == "segment":
                job["segments"].append({
                    "start": event["start"], "end": event["end"],
                    "text": event["text"], "progress": event["progress"],
                    "elapsed": event["elapsed"], "eta": event["eta"],
                })
                job["progress"] = event["progress"]
                job["elapsed"] = event["elapsed"]
                job["eta"] = event["eta"]
            elif t == "done":
                _save_results(job_id, job, event)
                job["text"] = event["text"]
                job["elapsed"] = event["elapsed"]
                job["speed"] = event["speed"]
                job["progress"] = 1.0
                job["eta"] = 0.0
                job["status"] = "done"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        try:
            os.remove(job["path"])  # apaga o áudio mesmo em erro
        except OSError:
            pass
    finally:
        transcriber.touch()  # a contagem para descarregar o modelo começa ao fim da transcrição
        transcriber.transcribe_lock.release()


@app.post("/api/transcribe/{job_id}/start")
def transcribe_start(job_id: str, model: str = "", language: str = "auto",
                     task: str = "transcribe", _: bool = Depends(require_auth)):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Arquivo não encontrado (reenvie)")
    if job["status"] in ("queued", "running"):
        return {"ok": True, "status": job["status"]}
    model_id = (model if model in transcriber.MODELS_BY_ID
                else transcriber.DEFAULT_MODEL)
    job.update(model=model_id, language=language, task=task, segments=[],
               logs=[], error=None, progress=0.0, text=None,
               duration=None, status="queued")
    threading.Thread(target=_run_job, args=(job_id, model_id, language, task),
                     daemon=True).start()
    return {"ok": True, "status": "queued", "model": model_id}


@app.get("/api/transcribe/{job_id}/status")
def transcribe_status(job_id: str, _: bool = Depends(require_auth)):
    job = JOBS.get(job_id)
    if job:
        return {"found": True, "status": job["status"], "name": job["name"],
                "progress": job["progress"], "model": job["model"]}
    meta = _load_meta(job_id) if _ID_RE.match(job_id) else None
    if meta:
        return {"found": True, "status": "done",
                "name": meta.get("orig_name"), "progress": 1.0}
    return {"found": False}


@app.get("/api/transcribe/{job_id}/stream")
def transcribe_stream(job_id: str, _: bool = Depends(require_auth)):
    if job_id not in JOBS:
        raise HTTPException(404, "Job não encontrado")

    def stream():
        last_seg = 0
        last_log = 0
        sent_start = False
        while True:
            job = JOBS.get(job_id)
            if not job:
                yield sse("error", {"message": "Transcrição removida"})
                return
            while last_log < len(job["logs"]):
                yield sse("log", {"message": job["logs"][last_log]})
                last_log += 1
            if not sent_start and job["duration"] is not None:
                sent_start = True
                yield sse("start", {
                    "duration": job["duration"], "language": job["detected_lang"],
                    "language_prob": job["lang_prob"], "device": job["device"],
                    "compute_type": job["compute_type"], "model": job["model"],
                    "name": job["name"],
                })
            while last_seg < len(job["segments"]):
                yield sse("segment", job["segments"][last_seg])
                last_seg += 1
            if job["status"] == "done":
                yield sse("done", {
                    "text": job["text"], "elapsed": job["elapsed"],
                    "duration": job["duration"], "speed": job["speed"],
                    "job_id": job_id,
                })
                return
            if job["status"] == "error":
                yield sse("error", {"message": job["error"] or "falha"})
                return
            time.sleep(0.4)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers=_sse_headers())


def _save_results(job_id: str, job: dict, done: dict):
    base = os.path.splitext(job["name"])[0] or "transcricao"
    with open(os.path.join(RESULT_DIR, f"{job_id}.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(transcriber.to_lines(done["text"]))
    with open(os.path.join(RESULT_DIR, f"{job_id}.srt"), "w",
              encoding="utf-8") as fh:
        fh.write(done["srt"])
    meta = {
        "job_id": job_id, "base": base, "orig_name": job["name"],
        "date": time.time(), "model": job.get("model"),
        "language": job.get("detected_lang"), "duration": done.get("duration"),
        "elapsed": done.get("elapsed"), "speed": done.get("speed"),
        "chars": len(done["text"]), "segments": len(done.get("segments", [])),
    }
    with open(_meta_path(job_id), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    job["base"] = base
    try:
        os.remove(job["path"])  # áudio enviado é apagado; ficam só os resultados
    except OSError:
        pass


@app.get("/api/result/{job_id}.{ext}")
def download_result(job_id: str, ext: str, _: bool = Depends(require_auth)):
    _check_id(job_id)
    if ext not in ("txt", "srt"):
        raise HTTPException(404, "Formato inválido")
    path = os.path.join(RESULT_DIR, f"{job_id}.{ext}")
    if not os.path.isfile(path):
        raise HTTPException(404, "Resultado não encontrado")
    base = (_load_meta(job_id) or {}).get("base", "transcricao")
    if ext == "txt":
        # Reformata na hora (uma frase por linha), cobrindo também os .txt antigos.
        with open(path, encoding="utf-8") as fh:
            body = transcriber.to_lines(fh.read())
        ascii_base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "transcricao"
        disp = (f"attachment; filename=\"{ascii_base}.txt\"; "
                f"filename*=UTF-8''{quote(base + '.txt')}")
        return PlainTextResponse(body, headers={"Content-Disposition": disp})
    return FileResponse(path, media_type="text/plain; charset=utf-8",
                        filename=f"{base}.{ext}")


# --------------------------------------------------------------------------
# Resumo (LLM) de uma transcrição — sob demanda, salvo como .summary.md
# --------------------------------------------------------------------------
def _summary_md_path(job_id: str, style: str = summarizer.DEFAULT_STYLE) -> str:
    return os.path.join(RESULT_DIR, f"{job_id}.{style}.summary.md")


def _legacy_summary_path(job_id: str) -> str:
    return os.path.join(RESULT_DIR, f"{job_id}.summary.md")


def _summary_file_for(job_id: str, style: str) -> str | None:
    """Caminho do resumo salvo para (job, estilo), com fallback ao formato antigo."""
    p = _summary_md_path(job_id, style)
    if os.path.isfile(p):
        return p
    legacy = _legacy_summary_path(job_id)
    if os.path.isfile(legacy):
        return legacy
    return None


def _summary_styles_on_disk(job_id: str) -> list[str]:
    found = [s for s in summarizer.SUMMARY_STYLES
             if os.path.isfile(_summary_md_path(job_id, s))]
    if not found and os.path.isfile(_legacy_summary_path(job_id)):
        found = [summarizer.DEFAULT_STYLE]
    return found


def _transcript_for(job_id: str) -> str | None:
    job = JOBS.get(job_id)
    if job and job.get("text"):
        return job["text"]
    txt_path = os.path.join(RESULT_DIR, f"{job_id}.txt")
    if os.path.isfile(txt_path):
        with open(txt_path, encoding="utf-8") as fh:
            return fh.read()
    return None


def _summary_markdown(name: str, meta: dict, body: str, style: str) -> str:
    when = time.strftime("%d/%m/%Y %H:%M", time.localtime(meta.get("date") or time.time()))
    model = meta.get("summary_model") or ""
    label = summarizer.SUMMARY_STYLES.get(style, {}).get("label", "")
    head = f"# Resumo — {name}\n\n> Gerado por audioTXT · {when}"
    if label:
        head += f" · {label}"
    if model:
        head += f" · modelo {model}"
    return f"{head}\n\n{body.strip()}\n"


def _run_summary(job_id: str, text: str, model_id: str, style: str):
    sj = SUMMARY_JOBS[job_id]
    try:
        body = summarizer.summarize(text, model_id, style,
                                    log=lambda m: sj["logs"].append(m))
        # salva só o corpo; o cabeçalho é montado no download
        with open(_summary_md_path(job_id, style), "w", encoding="utf-8") as fh:
            fh.write(body.strip() + "\n")
        meta = _load_meta(job_id)
        if meta:
            meta["has_summary"] = True
            meta["summary_model"] = model_id
            summaries = meta.get("summaries") or {}
            summaries[style] = model_id
            meta["summaries"] = summaries
            with open(_meta_path(job_id), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
        sj["body"] = body.strip()
        sj["status"] = "done"
    except Exception as exc:
        sj["status"] = "error"
        sj["error"] = str(exc)
    finally:
        summarizer.touch()


def _norm_style(style: str) -> str:
    return style if style in summarizer.SUMMARY_STYLES else summarizer.DEFAULT_STYLE


@app.get("/api/summary/styles")
def list_summary_styles(_: bool = Depends(require_auth)):
    return {"styles": summarizer.styles_catalog(), "default": summarizer.DEFAULT_STYLE}


@app.get("/api/summarize/active")
def summaries_active(_: bool = Depends(require_auth)):
    jobs = [{"job_id": jid, "style": sj.get("style", summarizer.DEFAULT_STYLE)}
            for jid, sj in SUMMARY_JOBS.items() if sj.get("status") == "running"]
    return {"jobs": jobs}


@app.post("/api/summarize/{job_id}")
def summarize_start(job_id: str, model: str = "", style: str = summarizer.DEFAULT_STYLE,
                    _: bool = Depends(require_auth)):
    _check_id(job_id)
    style = _norm_style(style)
    text = _transcript_for(job_id)
    if not text or not text.strip():
        raise HTTPException(404, "Transcrição não encontrada para resumir.")
    model_id = model if model in summarizer.SUMMARY_BY_ID else summarizer.DEFAULT_MODEL
    if not summarizer.is_downloaded(model_id):
        raise HTTPException(400, "Baixe o modelo de resumo primeiro (aba Modelos).")
    sj = SUMMARY_JOBS.get(job_id)
    if sj and sj["status"] == "running":
        return {"ok": True, "status": "running", "style": sj.get("style", style)}
    SUMMARY_JOBS[job_id] = {"status": "running", "logs": [], "body": None,
                            "error": None, "model": model_id, "style": style}
    threading.Thread(target=_run_summary, args=(job_id, text, model_id, style),
                     daemon=True).start()
    return {"ok": True, "status": "running", "model": model_id, "style": style}


@app.get("/api/summarize/{job_id}/stream")
def summarize_stream(job_id: str, _: bool = Depends(require_auth)):
    if job_id not in SUMMARY_JOBS:
        raise HTTPException(404, "Resumo não encontrado")

    def stream():
        last_log = 0
        while True:
            sj = SUMMARY_JOBS.get(job_id)
            if not sj:
                yield sse("error", {"message": "Resumo removido"})
                return
            style = sj.get("style", summarizer.DEFAULT_STYLE)
            while last_log < len(sj["logs"]):
                yield sse("log", {"message": sj["logs"][last_log], "style": style})
                last_log += 1
            if sj["status"] == "done":
                yield sse("done", {"md": sj["body"], "job_id": job_id, "style": style})
                return
            if sj["status"] == "error":
                yield sse("error", {"message": sj["error"] or "falha", "style": style})
                return
            time.sleep(0.4)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers=_sse_headers())


@app.get("/api/summary/{job_id}/view")
def view_summary(job_id: str, style: str = summarizer.DEFAULT_STYLE,
                 _: bool = Depends(require_auth)):
    _check_id(job_id)
    style = _norm_style(style)
    path = _summary_file_for(job_id, style)
    if not path:
        raise HTTPException(404, "Resumo não encontrado")
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    # arquivos antigos guardavam o cabeçalho junto; tira para exibir só o corpo
    body = re.sub(r"^#\s*Resumo —.*?\n\n>[^\n]*\n\n", "", body, count=1, flags=re.S)
    return {"job_id": job_id, "style": style, "md": body.strip()}


@app.get("/api/summary/{job_id}.md")
def download_summary(job_id: str, style: str = summarizer.DEFAULT_STYLE,
                     _: bool = Depends(require_auth)):
    _check_id(job_id)
    style = _norm_style(style)
    path = _summary_file_for(job_id, style)
    if not path:
        raise HTTPException(404, "Resumo não encontrado")
    with open(path, encoding="utf-8") as fh:
        body = fh.read().strip()
    meta = _load_meta(job_id) or {}
    name = os.path.splitext(meta.get("base") or "transcrição")[0]
    meta_head = dict(meta)
    mid = (meta.get("summaries") or {}).get(style) or meta.get("summary_model")
    if mid in summarizer.SUMMARY_BY_ID:
        meta_head["summary_model"] = summarizer.SUMMARY_BY_ID[mid]["label"]
    # arquivos novos guardam só o corpo; os antigos já vêm com cabeçalho
    full = body if body.lstrip().startswith("# Resumo") else \
        _summary_markdown(name, meta_head, body, style)
    base = os.path.splitext(meta.get("base", "resumo"))[0] or "resumo"
    suffix = "-resumo" if style == summarizer.DEFAULT_STYLE else f"-resumo-{style}"
    ascii_base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "resumo"
    disp = (f"attachment; filename=\"{ascii_base}{suffix}.md\"; "
            f"filename*=UTF-8''{quote(base + suffix + '.md')}")
    return PlainTextResponse(full, media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": disp})


@app.delete("/api/summary/{job_id}")
def delete_summary(job_id: str, style: str = "", _: bool = Depends(require_auth)):
    """Apaga o resumo de um estilo (ou todos, se style vazio). Recusa se em curso."""
    _check_id(job_id)
    sj = SUMMARY_JOBS.get(job_id)
    if sj and sj.get("status") == "running":
        raise HTTPException(409, "Resumo em andamento — aguarde terminar.")
    targets = [style] if style in summarizer.SUMMARY_STYLES else list(summarizer.SUMMARY_STYLES)
    removed = False
    for s in targets:
        p = _summary_md_path(job_id, s)
        if os.path.isfile(p):
            os.remove(p)
            removed = True
    if not style:  # exclusão total: remove também o formato antigo
        legacy = _legacy_summary_path(job_id)
        if os.path.isfile(legacy):
            os.remove(legacy)
            removed = True
    meta = _load_meta(job_id)
    if meta:
        sums = meta.get("summaries") or {}
        if style:
            sums.pop(style, None)
        else:
            sums = {}
        meta["summaries"] = sums
        meta["has_summary"] = bool(_summary_styles_on_disk(job_id))
        with open(_meta_path(job_id), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
    SUMMARY_JOBS.pop(job_id, None)
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------
# Histórico de transcrições (persistido em /data/results)
# --------------------------------------------------------------------------
@app.get("/api/history")
def history(_: bool = Depends(require_auth)):
    items = []
    for fn in os.listdir(RESULT_DIR):
        if fn.endswith(".json"):
            meta = _load_meta(fn[:-5])
            if meta:
                items.append(meta)
    items.sort(key=lambda m: m.get("date", 0), reverse=True)
    return {"items": items}


@app.get("/api/history/{job_id}")
def history_item(job_id: str, _: bool = Depends(require_auth)):
    _check_id(job_id)
    meta = _load_meta(job_id)
    if not meta:
        raise HTTPException(404, "Não encontrado")
    txt_path = os.path.join(RESULT_DIR, f"{job_id}.txt")
    text = ""
    if os.path.isfile(txt_path):
        with open(txt_path, encoding="utf-8") as fh:
            text = fh.read()
    out = dict(meta)
    out["text"] = transcriber.to_lines(text)
    return out


@app.delete("/api/history/{job_id}")
def history_delete(job_id: str, _: bool = Depends(require_auth)):
    _check_id(job_id)
    removed = False
    for ext in ("txt", "srt", "json"):
        p = os.path.join(RESULT_DIR, f"{job_id}.{ext}")
        if os.path.isfile(p):
            os.remove(p)
            removed = True
    for s in summarizer.SUMMARY_STYLES:
        p = _summary_md_path(job_id, s)
        if os.path.isfile(p):
            os.remove(p)
    legacy = _legacy_summary_path(job_id)
    if os.path.isfile(legacy):
        os.remove(legacy)
    JOBS.pop(job_id, None)
    SUMMARY_JOBS.pop(job_id, None)
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------
# Helpers SSE + estáticos
# --------------------------------------------------------------------------
def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_headers() -> dict:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
