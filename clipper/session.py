"""Una sesión vigila un canal: chat + grabación + creación de clips."""
import json
import os
import re
import shutil
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from .chat import ChatReader
from .detector import SpikeDetector
from .recorder import SEG_SECS, Recorder, encode_vertical, fetch_metadata

ROOT = Path(__file__).resolve().parent.parent
CLIPS_DIR = ROOT / "clips"
BUFFER_DIR = ROOT / "buffer"
ENCODE_LOCK = threading.Lock()  # una sola codificación a la vez
OFFLINE_RETRY_SECS = 60
# Dentro de la carpeta de cada directo, los clips se separan según su estado
STATUS_DIRS = {"pendiente": "por revisar", "aprobado": "aprobados"}


def safe_name(text, maxlen=80):
    """Quita caracteres no válidos en nombres de carpeta de Windows."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")[:maxlen].strip(" .")
    return text or "sin titulo"


class Session(threading.Thread):
    def __init__(self, channel, settings):
        super().__init__(daemon=True)
        self.channel = channel.lower()
        self.s = settings
        self.status = "conectando"
        self.author = channel
        self.title = ""
        self.folder = None
        self.rate = 0.0
        self.baseline = None
        self.history = deque(maxlen=120)
        self.events = deque(maxlen=40)
        self.encoding = 0
        self.detector = SpikeDetector(settings)
        self.chat = ChatReader(self.channel, self._on_message)
        self.recorder = None
        self._pending = []  # clips esperando a que pasen los segundos posteriores
        self._lock = threading.Lock()
        self._clip_idx = 1
        self._stop_ev = threading.Event()

    # ---------- API pública ----------
    def stop(self):
        self._stop_ev.set()
        self.chat.stop()

    def manual_clip(self):
        if self.status != "grabando":
            return False
        now = time.time()
        self._schedule({"t": now, "t0": now - self.s["pre_secs"],
                        "t1": now + self.s["post_secs"], "reason": "Clip manual"})
        return True

    def state(self):
        return {
            "channel": self.channel,
            "author": self.author,
            "title": self.title,
            "status": self.status,
            "chat": self.chat.connected,
            "rate": round(self.rate, 2),
            "baseline": None if self.baseline is None else round(self.baseline, 2),
            "factor": self.s["factor"],
            "history": list(self.history),
            "buffered": round(self.recorder.buffered_secs()) if self.recorder and self.status == "grabando" else 0,
            "pending": len(self._pending) + self.encoding,
            "moment": self.detector.event is not None and self.status == "grabando",
            "events": list(self.events)[::-1],
        }

    # ---------- interno ----------
    def _log(self, text):
        self.events.append({"t": datetime.now().strftime("%H:%M:%S"), "text": text})

    def _on_message(self, user, text, ts):
        self.detector.add(user, text, ts)

    def _schedule(self, clip):
        """Encola un clip: {'t' momento del disparo, 't0'/'t1' tramo a guardar, 'reason'}."""
        with self._lock:
            self._pending.append(clip)
        self._log(f"{clip['reason']} → clip en preparación")

    def _schedule_moment(self, m, t):
        self._schedule({
            "t": t, "t0": m["t0"], "t1": m["t1"], "score": m["score"],
            "reason": f"Pico de chat (máx. {m['peak']} msg/s, base {m['baseline']}, "
                      f"{m['users']} personas)",
        })

    def run(self):
        self.chat.start()
        while not self._stop_ev.is_set():
            meta = fetch_metadata(self.channel)
            if self._stop_ev.is_set():
                break
            if not meta:
                if self.status != "esperando":
                    self._log("El canal no está en directo; se reintenta cada minuto")
                self.status = "esperando"
                self._stop_ev.wait(OFFLINE_RETRY_SECS)
                continue
            self._record_stream(meta)
        self.status = "detenido"

    def _record_stream(self, meta):
        self.author = meta["author"]
        self.title = meta["title"]
        day = datetime.now().strftime("%Y-%m-%d")
        self.folder = CLIPS_DIR / safe_name(self.author, 40) / safe_name(f"{day} {self.title}")
        self.folder.mkdir(parents=True, exist_ok=True)
        nums = [int(m.group(1)) for f in self.folder.rglob("clip_*")
                if (m := re.match(r"clip_(\d+)", f.name))]
        self._clip_idx = max(nums, default=0) + 1

        self.recorder = Recorder(self.channel, BUFFER_DIR / self.channel, self.s["quality"])
        self.recorder.start()
        self.detector.last_trigger = 0.0
        self.detector.event = None
        self.status = "grabando"
        self._log(f"Grabando: {self.title}")
        started = time.time()
        moment_t = 0.0  # cuándo se disparó el momento en curso

        while not self._stop_ev.wait(1) and self.recorder.alive():
            now = time.time()
            # el búfer debe cubrir el clip más largo posible, más margen para cortarlo
            keep = max(self.s["max_clip_secs"], self.s["pre_secs"] + self.s["post_secs"]) + 60
            self.recorder.prune(now, keep)
            self.rate, self.baseline, _, _ = self.detector.stats(now)
            self.history.append(round(self.rate, 2))
            if now - started > 15:  # deja que el búfer arranque
                step = self.detector.step(now)
                if step and step[0] == "open":
                    moment_t = now
                    self._log("Pico de chat: grabando el momento mientras dure")
                elif step:
                    self._schedule_moment(step[1], moment_t)
            self._process_pending(now)

        self.recorder.stop()
        unfinished = self.detector.finish(time.time())
        if unfinished:
            self._schedule_moment(unfinished, moment_t)
        self._process_pending(None)  # lo que quedara pendiente, con lo que haya
        self.recorder.cleanup()
        self.rate = 0.0
        if not self._stop_ev.is_set():
            self._log("El directo ha terminado")
            self.status = "esperando"

    def _process_pending(self, now):
        with self._lock:
            if now is None:
                ready, self._pending = self._pending, []
            else:
                ready = [p for p in self._pending if now >= p["t1"] + SEG_SECS * 2]
                self._pending = [p for p in self._pending if p not in ready]
        for p in ready:
            self._cut(p)

    def _cut(self, p):
        base = f"clip_{self._clip_idx:03d}"
        self._clip_idx += 1
        clip_dir = self.folder / STATUS_DIRS["pendiente"] / base  # cada clip en su carpeta
        clip_dir.mkdir(parents=True, exist_ok=True)
        raw = clip_dir / f"{base}_original.mp4"
        short = clip_dir / f"{base}.mp4"
        if not self.recorder.cut(p["t0"], p["t1"], raw):
            shutil.rmtree(clip_dir, ignore_errors=True)
            self._log(f"No se pudo cortar {base} (búfer vacío)")
            return
        meta = {
            "channel": self.channel,
            "author": self.author,
            "title": self.title,
            "created": datetime.fromtimestamp(p["t"]).isoformat(timespec="seconds"),
            "reason": p["reason"],
            "score": p.get("score"),  # veces por encima de lo normal; None en clips manuales
            "status": "pendiente",
            "short": short.name,
            "original": raw.name,
        }
        self.encoding += 1
        threading.Thread(target=self._encode, args=(raw, short, meta), daemon=True).start()

    def _encode(self, raw, short, meta):
        try:
            ok, meta["subs"] = render_short(raw, short, self.channel, self.s, self._log)
            if not ok:
                meta["short"] = None
            (short.with_suffix(".json")).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if ok:
                self._log(f"Clip listo: {short.name}")
        finally:
            self.encoding -= 1


# ---------- edición del clip vertical ----------
SUBS_MARGIN_BLUR = 470    # subtítulos bajo el vídeo centrado
SUBS_MARGIN_SPLIT = 1070  # subtítulos sobre la unión entre cámara y juego
# Texto de arriba: con fondo difuminado, apoyado justo encima del vídeo (que empieza en
# y=656, así que su borde inferior queda en y=620); en el dividido, arriba del todo.
TITLE_POS_BLUR = (2, 1300)
TITLE_POS_SPLIT = (8, 50)


def render_short(raw, short, channel, settings, log, trim=None, title=None):
    """Genera el clip vertical según los ajustes. Devuelve (ok, nombre del .ass o None).

    El .ass que hay junto al clip guarda los subtítulos referidos al original completo.
    Si ya existe se reutiliza (así se puede corregir el texto y rehacer la edición sin
    volver a transcribir). `trim` = [inicio, fin] en segundos recorta el clip.
    `title` es un texto que se ve encima del vídeo durante todo el clip.
    """
    with ENCODE_LOCK:
        cam = None
        if settings.get("layout") == "split":
            cam = settings.get("cams", {}).get(channel)
            if not cam:
                log(f"{short.name}: cámara sin marcar para {channel}, se usa fondo difuminado")
        from .subtitles import make_subtitles, read_lines, title_text, write_lines
        ass = short.with_suffix(".ass")
        lines = []
        if settings.get("subtitles"):
            try:
                if not ass.exists() and not make_subtitles(
                        raw, ass, settings["sub_language"], settings["whisper_model"],
                        settings.get("gpu", True)):
                    log(f"{short.name}: no se detectó voz, sin subtítulos")
                lines = read_lines(ass) if ass.exists() else []
                if trim:
                    t0, t1 = trim
                    lines = [{"start": max(ln["start"] - t0, 0), "end": min(ln["end"], t1) - t0,
                              "text": ln["text"]} for ln in lines]
                    lines = [ln for ln in lines if ln["end"] - ln["start"] > 0.05]
            except Exception as e:  # el clip sale igualmente, sin subtítulos
                lines = []
                log(f"Error en subtítulos de {short.name}: {str(e)[-120:]}")
        render_ass = None  # copia temporal con el margen, los tiempos y el texto de arriba
        if lines or title_text(title):
            render_ass = short.with_name(short.stem + ".render.ass")
            write_lines(render_ass, lines, SUBS_MARGIN_SPLIT if cam else SUBS_MARGIN_BLUR,
                        title, TITLE_POS_SPLIT if cam else TITLE_POS_BLUR)
        ass_name = ass.name if ass.exists() else None
        tmp = short.with_name(short.stem + ".tmp.mp4")
        ok, err = encode_vertical(raw, tmp, render_ass.name if render_ass else None, cam, trim,
                                  settings.get("gpu", True))
        short.with_name(short.stem + ".render.ass").unlink(missing_ok=True)
        if ok:
            try:
                os.replace(tmp, short)
            except OSError:
                ok, err = False, "el clip está en uso; cierra el vídeo y reinténtalo"
        if not ok:
            tmp.unlink(missing_ok=True)
            log(f"Error al editar {short.name}: {err.strip()[-120:]}")
        return ok, ass_name


REENCODING = set()  # ids de clips cuya edición se está rehaciendo


def reencode_clip(clip_id, settings):
    """Rehace en segundo plano el clip vertical con los ajustes actuales."""
    path = _clip_json(clip_id)
    if not path or clip_id in REENCODING:
        return False
    meta = json.loads(path.read_text(encoding="utf-8"))
    raw = path.parent / Path(meta.get("original") or "x").name
    if not raw.is_file():
        return False
    REENCODING.add(clip_id)

    def work():
        try:
            notes = []
            short = path.with_suffix(".mp4")
            ok, ass_name = render_short(raw, short, meta.get("channel", ""), settings,
                                        notes.append, meta.get("trim"), meta.get("overlay_text"))
            if not path.is_file():  # se descartó mientras tanto
                return
            cur = json.loads(path.read_text(encoding="utf-8"))
            if ok:
                cur["short"] = short.name
                cur["subs"] = ass_name
                cur["rev"] = cur.get("rev", 0) + 1
            cur["note"] = "; ".join(notes) or None
            path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            REENCODING.discard(clip_id)

    threading.Thread(target=work, daemon=True).start()
    return True


def clip_detail(clip_id):
    """Datos para el editor: título, recorte y líneas de subtítulos. None si no existe."""
    path = _clip_json(clip_id)
    if not path:
        return None
    meta = json.loads(path.read_text(encoding="utf-8"))
    ass = path.with_suffix(".ass")
    lines = []
    if ass.is_file():
        from .subtitles import read_lines
        lines = read_lines(ass)
    folder = path.parent.relative_to(CLIPS_DIR.resolve()).as_posix()
    return {
        "clip_title": meta.get("clip_title") or "",  # "title" es el título del directo
        "overlay_text": meta.get("overlay_text") or "",
        "trim": meta.get("trim"),
        "lines": lines,
        "original_url": f"/media/{folder}/{meta['original']}",
    }


def edit_clip(clip_id, title, trim, lines, settings, overlay_text=None):
    """Guarda título, texto de arriba, recorte y subtítulos corregidos y rehace el clip.

    Devuelve None si salió bien o un mensaje de error.
    """
    path = _clip_json(clip_id)
    if not path:
        return "Clip no encontrado"
    if clip_id in REENCODING:
        return "Espera a que termine la edición del clip"
    if trim is not None:
        try:
            t0, t1 = float(trim[0]), float(trim[1])
        except (TypeError, ValueError, IndexError):
            return "Recorte no válido"
        if t0 < 0 or t1 - t0 < 1:
            return "El recorte debe durar al menos 1 segundo"
        trim = [round(t0, 2), round(t1, 2)]
    if lines is not None:
        try:
            lines = [{"start": float(ln["start"]), "end": float(ln["end"]),
                      "text": str(ln["text"])[:200]} for ln in lines[:3000]]
        except (TypeError, ValueError, KeyError):
            return "Subtítulos no válidos"
        ass = path.with_suffix(".ass")
        if lines or ass.exists():  # sin líneas ni archivo previo no hay nada que guardar
            from .subtitles import write_lines
            write_lines(ass, lines)
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["clip_title"] = " ".join(str(title or "").split())[:100]
    if overlay_text is not None:  # se respetan los saltos de línea; vacío = sin texto
        rows = [" ".join(r.split()) for r in str(overlay_text).splitlines()]
        meta["overlay_text"] = "\n".join(r for r in rows if r)[:150]
    meta["trim"] = trim
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if not reencode_clip(clip_id, settings):
        return "No se pudo rehacer el clip (falta el original)"
    return None


def latest_original(channel):
    """Ruta del original del clip más reciente de un canal, o None."""
    for c in list_clips():
        if c.get("channel") == channel:
            p = CLIPS_DIR / c["folder"] / c["original"]
            if p.is_file():
                return p
    return None


# ---------- clips en disco ----------
def list_clips():
    clips = []
    for f in CLIPS_DIR.rglob("clip_*.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rel = f.relative_to(CLIPS_DIR).with_suffix("").as_posix()
        folder = f.parent.relative_to(CLIPS_DIR).as_posix()
        meta["id"] = rel
        meta["folder"] = folder
        meta["busy"] = rel in REENCODING
        if meta.get("score") is None:  # clips antiguos: se deduce del motivo guardado
            m = re.search(r"([\d.]+) msg/s, base ([\d.]+)", meta.get("reason", ""))
            if m:
                meta["score"] = round(float(m.group(1)) / max(float(m.group(2)), 0.1), 1)
        rev = meta.get("rev", 0)
        meta["short_url"] = f"/media/{folder}/{meta['short']}?v={rev}" if meta.get("short") else None
        meta["original_url"] = f"/media/{folder}/{meta['original']}"
        clips.append(meta)
    clips.sort(key=lambda c: c.get("created", ""), reverse=True)
    return clips


def _clip_json(clip_id):
    path = (CLIPS_DIR / f"{clip_id}.json").resolve()
    if CLIPS_DIR.resolve() not in path.parents or not path.is_file():
        return None
    return path


def _rmdir_if_empty(folder):
    try:
        folder.rmdir()  # solo borra si está vacía
    except OSError:
        pass


IN_USE = "El clip está en uso. Pausa el vídeo y vuelve a intentarlo"


def set_clip_status(clip_id, status):
    """Cambia el estado y mueve la carpeta del clip a "por revisar" o "aprobados".

    Devuelve None si salió bien o un mensaje de error.
    """
    path = _clip_json(clip_id)
    if not path or path.parent.name != path.stem:
        return "Clip no encontrado"
    if clip_id in REENCODING:
        return "Espera a que termine la edición del clip"
    clip_dir = path.parent
    target = clip_dir.parent.parent / STATUS_DIRS[status] / clip_dir.name
    if target != clip_dir:
        if target.exists():
            return f"Ya existe una carpeta {clip_dir.name} en «{STATUS_DIRS[status]}»"
        target.parent.mkdir(exist_ok=True)
        for _ in range(6):  # el navegador puede tener el vídeo abierto un instante
            try:
                clip_dir.rename(target)
                break
            except OSError:
                time.sleep(0.3)
        else:
            return IN_USE
        _rmdir_if_empty(clip_dir.parent)
        path = target / path.name
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["status"] = status
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return None


def delete_clip(clip_id):
    """Borra la carpeta del clip. Devuelve None si salió bien o un mensaje de error."""
    path = _clip_json(clip_id)
    if not path or path.parent.name != path.stem:
        return "Clip no encontrado"
    if clip_id in REENCODING:
        return "Espera a que termine la edición del clip"
    for _ in range(6):
        shutil.rmtree(path.parent, ignore_errors=True)
        if not path.parent.exists():
            _rmdir_if_empty(path.parent.parent)
            return None
        time.sleep(0.3)
    return IN_USE


def migrate_layout():
    """Pasa los clips guardados con la estructura antigua (todos los archivos sueltos en
    la carpeta del directo) a la actual: <directo>/<por revisar|aprobados>/clip_001/."""
    for f in list(CLIPS_DIR.rglob("clip_*.json")):
        if f.parent.name == f.stem:
            continue  # ya está en su carpeta
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sub = STATUS_DIRS.get(meta.get("status"), STATUS_DIRS["pendiente"])
        dest = f.parent / sub / f.stem
        dest.mkdir(parents=True, exist_ok=True)
        for name in (f"{f.stem}.mp4", f"{f.stem}_original.mp4", f"{f.stem}.ass", f.name):
            src = f.parent / name
            if src.is_file():
                try:
                    src.rename(dest / name)
                except OSError:
                    pass
