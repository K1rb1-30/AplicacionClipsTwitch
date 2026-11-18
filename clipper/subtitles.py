"""Subtítulos automáticos: transcribe el audio con Whisper y genera un archivo .ass."""
import threading

_model = None
_model_name = None
_model_lock = threading.Lock()

MAX_WORDS = 3     # palabras por línea de subtítulo
MAX_CHARS = 18    # caracteres por línea
MAX_GAP = 0.6     # un silencio más largo que esto empieza línea nueva
DEFAULT_MARGIN = 470  # píxeles desde abajo a los que se dibuja el texto

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,40,40,__MARGIN__,1
Style: Title,Arial,74,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,5,4,__TALIGN__,60,60,__TMARGIN__,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


_cuda_ok = None  # None = sin probar; False = la gráfica falló y ya no se intenta


def _add_cuda_dlls():
    """Hace visibles las librerías de NVIDIA instaladas con pip (cuBLAS, cuDNN)."""
    import importlib.util
    import os
    from pathlib import Path
    spec = importlib.util.find_spec("nvidia")
    for base in (spec.submodule_search_locations if spec else []):
        for d in Path(base).glob("*/bin"):
            os.add_dll_directory(str(d))
            if str(d) not in os.environ["PATH"]:
                os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


def _cuda_possible():
    global _cuda_ok
    if _cuda_ok is None:
        try:
            import ctranslate2
            _add_cuda_dlls()
            _cuda_ok = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            _cuda_ok = False
    return _cuda_ok


def _get_model(name, device):
    """Carga el modelo una sola vez (la primera vez se descarga de internet)."""
    global _model, _model_name
    with _model_lock:
        if _model is None or _model_name != (name, device):
            from faster_whisper import WhisperModel
            _model = None  # libera el anterior antes de cargar otro
            _model = WhisperModel(name, device=device,
                                  compute_type="float16" if device == "cuda" else "int8")
            _model_name = (name, device)
        return _model


def _transcribe(audio, language, model_name, gpu):
    """Devuelve la lista de palabras. Usa la gráfica si puede y, si falla, el procesador."""
    global _cuda_ok
    devices = (["cuda"] if gpu and _cuda_possible() else []) + ["cpu"]
    for device in devices:
        try:
            model = _get_model(model_name, device)
            segments, _info = model.transcribe(
                audio,
                language=None if language == "auto" else language,
                word_timestamps=True,
                vad_filter=True,  # ignora música y silencios, que provocan texto inventado
            )
            words = []
            for seg in segments:  # la transcripción se ejecuta al recorrer los segmentos
                for w in seg.words or []:
                    text = clean_text(w.word)
                    if text:
                        words.append({"text": text.upper(), "start": w.start, "end": w.end})
            return words
        except Exception:
            if device == "cpu":
                raise
            _cuda_ok = False  # faltan librerías o memoria: a partir de ahora, procesador


def _load_audio(src):
    """Extrae el audio con ffmpeg como mono a 16 kHz, el formato que espera Whisper."""
    import subprocess

    import numpy as np

    from .recorder import FFMPEG, NOWIN
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"],
        capture_output=True, creationflags=NOWIN,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-200:])
    return np.frombuffer(r.stdout, dtype=np.float32)


def _ts(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _secs(ts):
    h, m, s = ts.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def clean_text(text):
    """Quita caracteres que el formato .ass interpreta como órdenes."""
    for ch in "{}\\\r\n":
        text = text.replace(ch, " ")
    return " ".join(text.split())


def read_lines(ass_path):
    """Lee las líneas de un .ass como [{'start', 'end', 'text'}] (tiempos en segundos)."""
    lines = []
    with open(ass_path, encoding="utf-8") as f:
        for row in f:
            if not row.startswith("Dialogue:"):
                continue
            parts = row.rstrip("\n").split(",", 9)
            if len(parts) < 10:
                continue
            try:
                lines.append({"start": _secs(parts[1]), "end": _secs(parts[2]), "text": parts[9]})
            except ValueError:
                continue
    return lines


def title_text(text):
    """Texto del título para el .ass: cada salto de línea escrito se respeta (\\N)."""
    rows = [clean_text(r) for r in str(text or "").splitlines()]
    return "\\N".join(r for r in rows if r)


def write_lines(ass_path, lines, margin_v=DEFAULT_MARGIN, title=None, title_pos=(2, 1300)):
    """Escribe un .ass con las líneas dadas, dibujadas a `margin_v` píxeles desde abajo.

    `title` es un texto fijo que se ve durante todo el clip; `title_pos` = (alineación
    ASS, margen vertical): 2 = apoyado por abajo a ese margen, 8 = pegado arriba.
    """
    out = [ASS_HEADER.replace("__MARGIN__", str(int(margin_v)))
           .replace("__TALIGN__", str(title_pos[0])).replace("__TMARGIN__", str(title_pos[1]))]
    title = title_text(title)
    if title:  # {\q0}: este texto sí se parte en varias líneas si no cabe a lo ancho
        out.append(f"Dialogue: 1,0:00:00.00,9:59:59.00,Title,,0,0,0,,{{\\q0}}{title}\n")
    for ln in lines:
        text = clean_text(str(ln["text"]))
        if text:
            out.append(f"Dialogue: 0,{_ts(ln['start'])},{_ts(ln['end'])},Default,,0,0,0,,{text}\n")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.writelines(out)


def _group(words):
    """Agrupa palabras en líneas cortas, al estilo de los Shorts."""
    lines, cur = [], []
    for w in words:
        text = " ".join(x["text"] for x in cur + [w])
        if cur and (len(cur) >= MAX_WORDS or len(text) > MAX_CHARS
                    or w["start"] - cur[-1]["end"] > MAX_GAP):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def make_subtitles(src, ass_path, language="es", model_name="small", gpu=True):
    """Transcribe `src` y escribe `ass_path`. Devuelve el número de líneas (0 = sin voz,
    y en ese caso no se crea el archivo)."""
    audio = _load_audio(src)
    if audio.size == 0:
        return 0
    groups = _group(_transcribe(audio, language, model_name, gpu))
    if not groups:
        return 0
    lines = []
    for i, g in enumerate(groups):
        end = g[-1]["end"] + 0.15
        if i + 1 < len(groups):
            end = min(end, groups[i + 1][0]["start"])  # sin solaparse con la siguiente
        lines.append({"start": g[0]["start"], "end": end,
                      "text": " ".join(w["text"] for w in g)})
    write_lines(ass_path, lines)
    return len(lines)
