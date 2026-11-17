"""Grabación del directo en un búfer de trozos cortos y corte/edición de clips."""
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
SEG_SECS = 2


def fetch_metadata(channel):
    """Devuelve {'author','title','category'} si el canal está en directo; si no, None."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "streamlink", "--json", f"https://twitch.tv/{channel}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=NOWIN,
        )
        data = json.loads(r.stdout)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None
    if "error" in data or not data.get("streams"):
        return None
    m = data.get("metadata") or {}
    return {
        "author": m.get("author") or channel,
        "title": m.get("title") or "directo",
        "category": m.get("category") or "",
    }


class Recorder:
    def __init__(self, channel, buffer_dir, quality="best"):
        self.channel = channel
        self.dir = Path(buffer_dir)
        self.quality = quality
        self.sl = None
        self.ff = None
        self._logs = []

    def start(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        sl_log = open(self.dir / "streamlink.log", "w", encoding="utf-8")
        ff_log = open(self.dir / "ffmpeg.log", "w", encoding="utf-8")
        self._logs = [sl_log, ff_log]
        self.sl = subprocess.Popen(
            [sys.executable, "-m", "streamlink", "--stdout", "--loglevel", "warning",
             f"https://twitch.tv/{self.channel}", self.quality],
            stdout=subprocess.PIPE, stderr=sl_log, creationflags=NOWIN,
        )
        self.ff = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "warning", "-i", "pipe:0",
             "-c", "copy", "-f", "segment", "-segment_time", str(SEG_SECS),
             "-reset_timestamps", "1", str(self.dir / "seg_%06d.ts")],
            stdin=self.sl.stdout, stdout=subprocess.DEVNULL, stderr=ff_log,
            creationflags=NOWIN,
        )
        self.sl.stdout.close()

    def alive(self):
        return bool(self.sl and self.ff and self.sl.poll() is None and self.ff.poll() is None)

    def stop(self):
        for p in (self.sl, self.ff):
            if p and p.poll() is None:
                p.terminate()
        for p in (self.sl, self.ff):
            if p:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        for f in self._logs:
            f.close()
        self._logs = []

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def segments(self):
        """Lista de (ruta, inicio, fin) en hora del reloj de los trozos ya cerrados."""
        files = sorted(self.dir.glob("seg_*.ts"))
        if self.alive() and files:
            files = files[:-1]  # el último aún se está escribiendo
        out = []
        prev_end = None
        for f in files:
            try:
                end = f.stat().st_mtime
            except OSError:
                continue
            start = prev_end if prev_end and end - prev_end < SEG_SECS * 3 else end - SEG_SECS
            out.append((f, start, end))
            prev_end = end
        return out

    def snapshot(self, out_path):
        """Guarda una imagen del momento más reciente del búfer."""
        segs = self.segments()
        return bool(segs) and grab_frame(segs[-1][0], out_path)

    def buffered_secs(self):
        segs = self.segments()
        return segs[-1][2] - segs[0][1] if segs else 0.0

    def prune(self, now, keep_secs):
        for f, _start, end in self.segments():
            if end < now - keep_secs:
                try:
                    f.unlink()
                except OSError:
                    pass

    def cut(self, t0, t1, out_path):
        """Une sin recodificar los trozos entre t0 y t1. Devuelve True si salió bien."""
        segs = [s for s in self.segments() if s[2] > t0 and s[1] < t1]
        if not segs:
            return False
        listfile = self.dir / f"concat_{uuid.uuid4().hex}.txt"
        listfile.write_text(
            "".join(f"file '{s[0].as_posix()}'\n" for s in segs), encoding="utf-8"
        )
        try:
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                 "-safe", "0", "-i", str(listfile), "-c", "copy",
                 "-movflags", "+faststart", str(out_path)],
                capture_output=True, creationflags=NOWIN,
            )
        finally:
            listfile.unlink(missing_ok=True)
        return r.returncode == 0 and Path(out_path).exists()


def grab_frame(src, out_path, at=0):
    """Guarda un fotograma de `src` como imagen."""
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at),
         "-i", str(src), "-frames:v", "1", "-q:v", "3", str(out_path)],
        capture_output=True, creationflags=NOWIN,
    )
    return r.returncode == 0 and Path(out_path).exists()


CAM_H = 800  # alto de la cámara en el diseño dividido; el resto (1120) es el juego


def encode_vertical(src, dst, ass_name=None, cam=None, trim=None, gpu=True):
    """Convierte a 1080x1920.

    `trim` = (inicio, fin) en segundos recorta el clip a ese tramo del original; los
    tiempos del .ass deben ir ya referidos al tramo recortado.

    Sin `cam`: vídeo centrado sobre el mismo vídeo difuminado de fondo.
    Con `cam` = [x, y, ancho, alto] (fracciones 0-1 del fotograma): esa zona, la
    cámara del streamer, va arriba y el centro de la imagen va abajo.

    `ass_name` es el nombre de un archivo .ass en la misma carpeta que `dst`; si se
    indica, los subtítulos se incrustan en el vídeo.
    """
    if cam:
        x, y, w, h = (f"{v:.4f}" for v in cam)
        layout = (
            "[0:v]split=2[a][b];"
            f"[a]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
            f"scale=1080:{CAM_H}:force_original_aspect_ratio=increase,crop=1080:{CAM_H}[top];"
            f"[b]scale=1080:{1920 - CAM_H}:force_original_aspect_ratio=increase,"
            f"crop=1080:{1920 - CAM_H}[bot];"
            "[top][bot]vstack"
        )
    else:
        layout = (
            "[0:v]split=2[a][b];"
            "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            "boxblur=8:2,scale=1080:1920[bg];"
            "[b]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    # ffmpeg se ejecuta dentro de la carpeta del clip para pasar el .ass como nombre
    # relativo: las rutas de Windows (C:\...) son muy difíciles de escapar en el filtro.
    subs = f",subtitles={ass_name}" if ass_name else ""
    flt = f"{layout},setsar=1{subs}[v]"
    encoders = ([NVENC_ARGS] if gpu and nvenc_available() else []) + [X264_ARGS]
    for venc in encoders:  # si la gráfica falla a mitad, se repite con el procesador
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             *(["-ss", f"{trim[0]:.2f}", "-to", f"{trim[1]:.2f}"] if trim else []),
             "-i", str(src),
             "-filter_complex", flt, "-map", "[v]", "-map", "0:a?",
             *venc, "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=NOWIN, cwd=str(Path(dst).parent),
        )
        if r.returncode == 0:
            break
    return r.returncode == 0, r.stderr[-500:]


X264_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
NVENC_ARGS = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "29",
              "-b:v", "0", "-maxrate", "8M", "-bufsize", "16M"]
_nvenc = None


def nvenc_available():
    """True si ffmpeg puede codificar con la tarjeta gráfica NVIDIA (se prueba una vez)."""
    global _nvenc
    if _nvenc is None:
        try:
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=c=black:s=1080x1920:r=30", "-frames:v", "10",
                 *NVENC_ARGS, "-pix_fmt", "yuv420p", "-f", "null", "-"],
                capture_output=True, creationflags=NOWIN, timeout=30,
            )
            _nvenc = r.returncode == 0
        except (subprocess.SubprocessError, OSError):
            _nvenc = False
    return _nvenc
