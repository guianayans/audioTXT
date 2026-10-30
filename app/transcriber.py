"""Gerência de modelos Whisper (faster-whisper / CTranslate2) e transcrição.

Carrega na GPU quando possível e cai para CPU automaticamente se a GPU falhar.
Os modelos ficam no cache do Hugging Face (HF_HOME), que é um volume persistente.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np
from faster_whisper import WhisperModel

from . import metrics

FFMPEG = os.environ.get("AUDIOTXT_FFMPEG", "ffmpeg")
SAMPLE_RATE = 16000


def load_audio(path: str) -> np.ndarray:
    """Decodifica qualquer áudio/vídeo para PCM mono 16 kHz float32 via ffmpeg.

    Passar o array direto ao faster-whisper evita o decodificador PyAV (cujo
    `av.open` varia de assinatura entre versões) e aceita qualquer formato.
    """
    cmd = [
        FFMPEG, "-nostdin", "-threads", "0", "-i", path,
        "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le",
        "-ar", str(SAMPLE_RATE), "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[-500:]
        raise RuntimeError(f"Falha ao ler o áudio (ffmpeg): {err}")
    audio = np.frombuffer(proc.stdout, np.int16).astype(np.float32) / 32768.0
    return audio

# Catálogo de modelos. "repo" é o id no Hugging Face (formato CTranslate2).
# "size_mb" é aproximado, só para a barra de download.
MODELS: list[dict] = [
    {"id": "tiny", "repo": "Systran/faster-whisper-tiny", "size_mb": 75,
     "label": "Tiny", "desc": "Mais rápido, menor precisão (~1 GB VRAM)"},
    {"id": "base", "repo": "Systran/faster-whisper-base", "size_mb": 145,
     "label": "Base", "desc": "Rápido, leve (~1 GB VRAM)"},
    {"id": "small", "repo": "Systran/faster-whisper-small", "size_mb": 484,
     "label": "Small", "desc": "Bom equilíbrio (~2 GB VRAM)"},
    {"id": "medium", "repo": "Systran/faster-whisper-medium", "size_mb": 1530,
     "label": "Medium", "desc": "Preciso (~5 GB VRAM)"},
    {"id": "large-v3-turbo", "repo": "deepdml/faster-whisper-large-v3-turbo-ct2",
     "size_mb": 1620, "label": "Large v3 Turbo",
     "desc": "Rápido e muito preciso — recomendado p/ 16 GB (~6 GB VRAM)"},
    {"id": "large-v3", "repo": "Systran/faster-whisper-large-v3", "size_mb": 3090,
     "label": "Large v3", "desc": "Máxima precisão (~10 GB VRAM)"},
]
MODELS_BY_ID = {m["id"]: m for m in MODELS}

DEFAULT_MODEL = os.environ.get("AUDIOTXT_DEFAULT_MODEL", "large-v3-turbo")
if DEFAULT_MODEL not in MODELS_BY_ID:
    DEFAULT_MODEL = "large-v3-turbo"

HF_HOME = os.environ.get("HF_HOME", "/data/hf")


def _cache_dir_for(repo: str) -> str:
    folder = "models--" + repo.replace("/", "--")
    return os.path.join(HF_HOME, "hub", folder)


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


def is_downloaded(model_id: str) -> bool:
    m = MODELS_BY_ID.get(model_id)
    if not m:
        return False
    d = _cache_dir_for(m["repo"])
    if not os.path.isdir(d):
        return False
    # exige ao menos o model.bin baixado
    return _dir_size(d) > 20 * 1024 * 1024


def catalog() -> list[dict]:
    out = []
    for m in MODELS:
        out.append({
            "id": m["id"],
            "label": m["label"],
            "desc": m["desc"],
            "size_mb": m["size_mb"],
            "downloaded": is_downloaded(m["id"]),
        })
    return out


def delete_model(model_id: str) -> bool:
    """Apaga do disco o modelo (completo ou baixado pela metade)."""
    m = MODELS_BY_ID.get(model_id)
    if not m:
        return False
    removed = False
    d = _cache_dir_for(m["repo"])
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        removed = True
    # remove também o lock correspondente, se houver
    lock = os.path.join(HF_HOME, "hub", ".locks",
                        "models--" + m["repo"].replace("/", "--"))
    if os.path.isdir(lock):
        shutil.rmtree(lock, ignore_errors=True)
    return removed


# ----------------------------------------------------------------------------
# Download em subprocesso (cancelável) com barra de progresso desligada.
# Medimos o tamanho do cache para mostrar o avanço; o subprocesso pode ser morto.
# ----------------------------------------------------------------------------
_DL_SCRIPT = (
    "import sys;"
    "from huggingface_hub.utils import disable_progress_bars;"
    "disable_progress_bars();"
    "from huggingface_hub import snapshot_download;"
    "snapshot_download(sys.argv[1], cache_dir=sys.argv[2])"
)


class DownloadJob:
    def __init__(self, model_id: str):
        self.model_id = model_id
        self.repo = MODELS_BY_ID[model_id]["repo"]
        self.total_mb = MODELS_BY_ID[model_id]["size_mb"]
        self.done = False
        self.cancelled = False
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None

    def start(self):
        env = dict(os.environ)
        env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        env["HF_HOME"] = HF_HOME
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _DL_SCRIPT, self.repo,
             os.path.join(HF_HOME, "hub")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )

    def poll(self):
        """Atualiza done/error. Deve ser chamado periodicamente."""
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
# Carregamento e cache do modelo ativo (só um por vez — poupa VRAM).
# ----------------------------------------------------------------------------
@dataclass
class LoadedModel:
    model_id: str
    device: str
    compute_type: str
    model: WhisperModel


_loaded: LoadedModel | None = None
_load_lock = threading.Lock()
# Impede duas transcrições simultâneas (a GPU não tem memória sobrando).
transcribe_lock = threading.Lock()


def active_device() -> str:
    return _loaded.device if _loaded else ""


def load_model(model_id: str, log=lambda *_: None) -> LoadedModel:
    """Carrega o modelo, reaproveitando se já estiver ativo.

    Tenta GPU (float16) e cai para CPU (int8) se algo falhar.
    """
    global _loaded
    with _load_lock:
        if _loaded and _loaded.model_id == model_id:
            return _loaded

        repo = MODELS_BY_ID[model_id]["repo"]

        # libera o modelo anterior antes de carregar outro
        if _loaded is not None:
            log(f"Descarregando modelo anterior ({_loaded.model_id})…")
            _loaded = None
            import gc
            gc.collect()

        want_gpu = metrics.gpu_available() and os.environ.get(
            "AUDIOTXT_DEVICE", "auto"
        ) != "cpu"

        attempts = []
        if want_gpu:
            attempts.append(("cuda", "float16"))
        attempts.append(("cpu", "int8"))

        last_err = None
        for device, compute_type in attempts:
            try:
                log(f"Carregando {model_id} em {device} ({compute_type})…")
                # Sem download_root: o faster-whisper usa o cache do HF
                # (HF_HOME/hub), o mesmo de snapshot_download e _cache_dir_for.
                model = WhisperModel(
                    repo,
                    device=device,
                    compute_type=compute_type,
                )
                _loaded = LoadedModel(model_id, device, compute_type, model)
                log(f"Modelo pronto em {device}.")
                return _loaded
            except Exception as exc:  # pragma: no cover
                last_err = exc
                log(f"Falha em {device}: {exc}")
                continue
        raise RuntimeError(f"Não foi possível carregar o modelo: {last_err}")


# ----------------------------------------------------------------------------
# Transcrição — gerador que entrega cada trecho assim que é decodificado.
# ----------------------------------------------------------------------------
def _fmt_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(segments: list[dict]) -> str:
    out = []
    for i, seg in enumerate(segments, 1):
        out.append(str(i))
        out.append(f"{_fmt_ts(seg['start'])} --> {_fmt_ts(seg['end'])}")
        out.append(seg["text"].strip())
        out.append("")
    return "\n".join(out)


def transcribe(audio_path: str, model_id: str, language: str | None,
               task: str, log=lambda *_: None):
    """Gera eventos (dict) do progresso da transcrição.

    Eventos: {"type": "start"|"segment"|"log", ...}. O chamador monta o SSE.
    """
    lm = load_model(model_id, log=log)

    log("Lendo o áudio…")
    audio = load_audio(audio_path)
    duration_fallback = round(len(audio) / SAMPLE_RATE, 2)

    lang = None if (not language or language == "auto") else language
    segments, info = lm.model.transcribe(
        audio,
        language=lang,
        task=task,
        vad_filter=True,
        beam_size=5,
    )

    duration = float(getattr(info, "duration", 0) or 0) or duration_fallback
    detected = getattr(info, "language", None)
    yield {
        "type": "start",
        "duration": round(duration, 2),
        "language": detected,
        "language_prob": round(float(getattr(info, "language_probability", 0) or 0), 3),
        "device": lm.device,
        "compute_type": lm.compute_type,
        "model": model_id,
    }

    started = time.time()
    collected: list[dict] = []
    for seg in segments:
        item = {"start": float(seg.start), "end": float(seg.end),
                "text": seg.text}
        collected.append(item)
        elapsed = time.time() - started
        processed = max(item["end"], 0.001)
        if duration > 0:
            progress = min(processed / duration, 0.999)
            speed = processed / elapsed if elapsed > 0 else 0
            remaining = (duration - processed) / speed if speed > 0 else 0
        else:
            progress = 0.0
            remaining = 0.0
        yield {
            "type": "segment",
            "start": item["start"],
            "end": item["end"],
            "text": item["text"],
            "progress": round(progress, 4),
            "elapsed": round(elapsed, 1),
            "eta": round(remaining, 1),
        }

    elapsed = time.time() - started
    text = "".join(s["text"] for s in collected).strip()
    yield {
        "type": "done",
        "text": text,
        "srt": build_srt(collected),
        "segments": collected,
        "elapsed": round(elapsed, 1),
        "duration": round(duration, 2),
        "speed": round(duration / elapsed, 2) if elapsed > 0 else 0,
    }
