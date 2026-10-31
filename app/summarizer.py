"""Resumo de transcrições com um LLM local (GGUF via llama-cpp-python, na GPU).

Embutido no audioTXT: baixa um modelo instruct (Qwen2.5) do Hugging Face, carrega
na placa sob demanda e descarrega depois de ocioso, liberando a VRAM. O resumo sai
em Markdown; textos longos são resumidos em partes (map-reduce) para caber no contexto.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

from huggingface_hub import hf_hub_download

LLM_HOME = os.environ.get(
    "AUDIOTXT_LLM_DIR",
    os.path.join(os.environ.get("AUDIOTXT_DATA_DIR", "/data"), "llm"),
)
os.makedirs(LLM_HOME, exist_ok=True)

N_CTX = int(os.environ.get("AUDIOTXT_LLM_CTX", "8192"))

# Catálogo de modelos de resumo (GGUF quantizado Q4_K_M, arquivo único).
SUMMARY_MODELS: list[dict] = [
    {"id": "qwen2.5-7b", "repo": "bartowski/Qwen2.5-7B-Instruct-GGUF",
     "file": "Qwen2.5-7B-Instruct-Q4_K_M.gguf", "size_mb": 4680,
     "label": "Qwen2.5 7B Instruct",
     "desc": "Recomendado — ótimo resumo em português (~5 GB VRAM)"},
    {"id": "qwen2.5-3b", "repo": "bartowski/Qwen2.5-3B-Instruct-GGUF",
     "file": "Qwen2.5-3B-Instruct-Q4_K_M.gguf", "size_mb": 1930,
     "label": "Qwen2.5 3B Instruct",
     "desc": "Mais leve e rápido (~2.5 GB VRAM)"},
]
SUMMARY_BY_ID = {m["id"]: m for m in SUMMARY_MODELS}

DEFAULT_MODEL = os.environ.get("AUDIOTXT_SUMMARY_MODEL", "qwen2.5-7b")
if DEFAULT_MODEL not in SUMMARY_BY_ID:
    DEFAULT_MODEL = "qwen2.5-7b"


def _cache_dir_for(repo: str) -> str:
    return os.path.join(LLM_HOME, "models--" + repo.replace("/", "--"))


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                fp = os.path.join(root, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def _model_path(model_id: str, local_only: bool = True) -> str:
    m = SUMMARY_BY_ID[model_id]
    return hf_hub_download(m["repo"], m["file"], cache_dir=LLM_HOME,
                           local_files_only=local_only)


def is_downloaded(model_id: str) -> bool:
    if model_id not in SUMMARY_BY_ID:
        return False
    try:
        p = _model_path(model_id, local_only=True)
        return os.path.isfile(p) and os.path.getsize(p) > 50 * 1024 * 1024
    except Exception:
        return False


def catalog() -> list[dict]:
    return [{
        "id": m["id"], "label": m["label"], "desc": m["desc"],
        "size_mb": m["size_mb"], "downloaded": is_downloaded(m["id"]),
    } for m in SUMMARY_MODELS]


def delete_model(model_id: str) -> bool:
    m = SUMMARY_BY_ID.get(model_id)
    if not m:
        return False
    d = _cache_dir_for(m["repo"])
    removed = os.path.isdir(d)
    if removed:
        shutil.rmtree(d, ignore_errors=True)
    lock = os.path.join(LLM_HOME, ".locks",
                        "models--" + m["repo"].replace("/", "--"))
    if os.path.isdir(lock):
        shutil.rmtree(lock, ignore_errors=True)
    return removed


# ----------------------------------------------------------------------------
# Download em subprocesso (cancelável), progresso pelo tamanho do cache.
# ----------------------------------------------------------------------------
_DL_SCRIPT = (
    "import sys;"
    "from huggingface_hub.utils import disable_progress_bars;"
    "disable_progress_bars();"
    "from huggingface_hub import hf_hub_download;"
    "hf_hub_download(sys.argv[1], sys.argv[2], cache_dir=sys.argv[3])"
)


class SummaryDownloadJob:
    def __init__(self, model_id: str):
        self.model_id = model_id
        m = SUMMARY_BY_ID[model_id]
        self.repo = m["repo"]
        self.file = m["file"]
        self.total_mb = m["size_mb"]
        self.done = False
        self.cancelled = False
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None

    def start(self):
        env = dict(os.environ)
        env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _DL_SCRIPT, self.repo, self.file, LLM_HOME],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )

    def poll(self):
        if self.proc is None or self.done:
            return
        rc = self.proc.poll()
        if rc is None:
            return
        self.done = True
        if self.cancelled:
            self.error = "cancelado"
        elif rc != 0:
            try:
                err = self.proc.stderr.read().decode("utf-8", "replace")
            except Exception:
                err = ""
            self.error = (err.strip().splitlines() or ["falha no download"])[-1][-300:]

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.done = True
        delete_model(self.model_id)

    def progress_mb(self) -> float:
        return round(_dir_size(_cache_dir_for(self.repo)) / (1024 * 1024), 1)


# ----------------------------------------------------------------------------
# Carga do modelo (um por vez) com descarga por ociosidade — libera VRAM.
# ----------------------------------------------------------------------------
_loaded = None          # (model_id, Llama)
_loaded_device = None    # "GPU" | "CPU"
_load_lock = threading.Lock()
summarize_lock = threading.Lock()   # só um resumo por vez
IDLE_UNLOAD_SECONDS = float(os.environ.get("AUDIOTXT_LLM_IDLE_UNLOAD_SECONDS", "300") or 0)
_last_used = 0.0


def touch() -> None:
    global _last_used
    _last_used = time.time()


def _is_cuda_error(exc) -> bool:
    """Erro de contexto/dispositivo CUDA (ex.: driver recarregado por outro app)."""
    s = str(exc).lower()
    return any(k in s for k in (
        "device ordinal", "invalid device", "cuda", "cublas", "cudnn",
        "parallel_for", "no cuda-capable", "cuda error", "ggml",
    ))


def unload_model() -> bool:
    global _loaded, _loaded_device
    with _load_lock:
        if _loaded is None:
            return False
        _loaded = None
        _loaded_device = None
        import gc
        gc.collect()
        return True


def _idle_watcher() -> None:
    while True:
        time.sleep(30)
        if IDLE_UNLOAD_SECONDS <= 0 or _loaded is None:
            continue
        if time.time() - _last_used < IDLE_UNLOAD_SECONDS:
            continue
        if not summarize_lock.acquire(blocking=False):
            continue
        try:
            if _loaded is not None and time.time() - _last_used >= IDLE_UNLOAD_SECONDS:
                if unload_model():
                    print("[audiotxt] modelo de resumo descarregado por ociosidade "
                          "(VRAM liberada)", flush=True)
        finally:
            summarize_lock.release()


threading.Thread(target=_idle_watcher, name="llm-idle-unload", daemon=True).start()


def _free_vram_mb() -> float | None:
    """VRAM livre (MB) da GPU 0, ou None se não houver GPU/leitura."""
    try:
        from . import metrics
        gpus = metrics.read_metrics().get("gpus") or []
        if gpus:
            g = gpus[0]
            return float(g.get("mem_total_mb", 0)) - float(g.get("mem_used_mb", 0))
    except Exception:
        pass
    return None


def _ensure_loaded(model_id: str, log=lambda *_: None, prefer_cpu: bool = False):
    global _loaded, _loaded_device
    with _load_lock:
        touch()
        if _loaded is not None and _loaded[0] == model_id and not (
            prefer_cpu and _loaded_device == "GPU"
        ):
            return _loaded[1]
        if not is_downloaded(model_id):
            raise RuntimeError("Modelo de resumo não baixado. Baixe-o na aba Modelos.")
        # libera o Whisper da VRAM antes de carregar o LLM (16 GB são compartilhados)
        try:
            from . import transcriber
            if transcriber.unload_model():
                log("Liberando a VRAM do modelo de transcrição…")
        except Exception:
            pass
        if _loaded is not None:
            _loaded = None
            import gc
            gc.collect()

        from llama_cpp import Llama
        path = _model_path(model_id, local_only=True)
        label = SUMMARY_BY_ID[model_id]["label"]
        size_mb = SUMMARY_BY_ID[model_id]["size_mb"]

        # A GPU é compartilhada com outros apps do servidor; se não couber a
        # essa altura, roda na CPU (mais lento, mas sempre conclui).
        force_cpu = prefer_cpu or os.environ.get("AUDIOTXT_DEVICE", "auto") == "cpu"
        free = None if force_cpu else _free_vram_mb()
        need = size_mb * 1.15 + 700  # pesos + buffers/contexto, com folga
        attempts = []
        if not force_cpu and (free is None or free >= need):
            attempts.append((-1, "GPU"))
        attempts.append((0, "CPU"))

        last_err = None
        for ngl, dev in attempts:
            try:
                if dev == "GPU":
                    log(f"Carregando o modelo de resumo na GPU ({label})…")
                else:
                    if free is not None and not force_cpu:
                        log(f"GPU ocupada ({int(free)} MB livres) — resumo na CPU, "
                            "mais lento…")
                    else:
                        log(f"Carregando o modelo de resumo na CPU ({label})…")
                llm = Llama(model_path=path, n_gpu_layers=ngl, n_ctx=N_CTX,
                            verbose=False)
                _loaded = (model_id, llm)
                _loaded_device = dev
                log("Modelo pronto.")
                return llm
            except Exception as exc:
                last_err = exc
                log(f"Falha ao carregar em {dev}: {str(exc)[:120]}")
                continue
        raise RuntimeError(f"Não foi possível carregar o modelo de resumo: {last_err}")


# ----------------------------------------------------------------------------
# Resumo propriamente dito.
# ----------------------------------------------------------------------------
_SYS_FINAL = ("Você é um assistente que resume transcrições de áudio em português "
              "do Brasil, de forma fiel e objetiva. Nunca invente informações que "
              "não estejam no conteúdo.")
_SYS_MAP = ("Você resume trechos de transcrições de áudio em português do Brasil. "
            "Extraia apenas os pontos e fatos relevantes do trecho, em tópicos "
            "curtos. Não invente nada.")

_FINAL_INSTRUCT = (
    "A seguir está {kind} de um áudio. Escreva um resumo em Markdown, usando "
    "exatamente estas seções e nesta ordem:\n\n"
    "## Resumo\n(2 a 4 frases com a ideia geral)\n\n"
    "## Pontos principais\n- (tópicos com o que mais importa)\n\n"
    "## Tópicos abordados\n- (assuntos tratados)\n\n"
    "## Decisões e próximos passos\n- (ações, prazos e responsáveis; se não houver, "
    "escreva \"Nenhuma identificada.\")\n\n"
    "Responda só com o Markdown, sem comentários antes ou depois.\n\n"
    "Conteúdo:\n\n{content}"
)


def _chunks(llm, text: str, size_tokens: int):
    toks = llm.tokenize(text.encode("utf-8"), add_bos=False, special=False)
    for i in range(0, len(toks), size_tokens):
        piece = llm.detokenize(toks[i:i + size_tokens]).decode("utf-8", "ignore")
        if piece.strip():
            yield piece
    return


def summarize(text: str, model_id: str, log=lambda *_: None) -> str:
    """Devolve o corpo do resumo em Markdown (sem o cabeçalho com metadados).

    Se a GPU falhar por contexto inválido (ex.: driver recarregado por outro
    app), descarrega e refaz o resumo na CPU — sem derrubar a operação.
    """
    text = (text or "").strip()
    if not text:
        raise RuntimeError("Transcrição vazia — nada para resumir.")
    prefer_cpu = False
    for attempt in range(2):
        llm = _ensure_loaded(model_id, log=log, prefer_cpu=prefer_cpu)
        try:
            return _run_summary(llm, text, log=log)
        except Exception as exc:
            if attempt == 0 and _loaded_device == "GPU" and _is_cuda_error(exc):
                log(f"Erro de GPU ({str(exc)[:80]}). Recarregando o resumo na CPU…")
                unload_model()
                prefer_cpu = True
                continue
            raise


def _run_summary(llm, text: str, log=lambda *_: None) -> str:
    with summarize_lock:
        touch()
        n_tokens = len(llm.tokenize(text.encode("utf-8"), add_bos=False, special=False))
        single_budget = max(1024, N_CTX - 1400)  # espaço para prompt + saída
        if n_tokens <= single_budget:
            content, kind = text, "a transcrição"
        else:
            parts = list(_chunks(llm, text, max(1024, N_CTX - 3200)))
            log(f"Transcrição longa — resumindo em {len(parts)} partes…")
            notes = []
            for i, part in enumerate(parts, 1):
                log(f"Resumindo parte {i}/{len(parts)}…")
                r = llm.create_chat_completion(
                    messages=[{"role": "system", "content": _SYS_MAP},
                              {"role": "user",
                               "content": f"Trecho:\n\n{part}\n\nListe em tópicos os "
                                          "pontos relevantes deste trecho."}],
                    max_tokens=500, temperature=0.2)
                notes.append(r["choices"][0]["message"]["content"].strip())
                touch()
            content, kind = "\n\n".join(notes), "o conjunto de anotações dos trechos"

        log("Montando o resumo final…")
        out = llm.create_chat_completion(
            messages=[{"role": "system", "content": _SYS_FINAL},
                      {"role": "user",
                       "content": _FINAL_INSTRUCT.format(kind=kind, content=content)}],
            max_tokens=1100, temperature=0.3)
        md = out["choices"][0]["message"]["content"].strip()
    touch()
    return md
