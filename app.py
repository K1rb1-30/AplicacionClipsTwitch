"""Servidor local con la interfaz. Ejecutar:  python app.py"""
import json
import os
import re
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

from clipper.recorder import grab_frame, nvenc_available
from clipper.session import (BUFFER_DIR, CLIPS_DIR, Session, clip_detail,
                             delete_clip, edit_clip, latest_original, list_clips, migrate_layout,
                             reencode_clip, set_clip_status)

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
PORT = int(os.environ.get("PORT", "8765"))

DEFAULTS = {
    "pre_secs": 45,        # segundos guardados antes del pico
    "post_secs": 15,       # segundos guardados después de que el chat se calme
    "max_clip_secs": 120,  # duración máxima de un clip automático
    "min_users": 3,        # personas distintas escribiendo para que cuente como pico
    "factor": 3.0,         # cuántas veces por encima de lo normal debe ir el chat
    "window_secs": 10,     # ventana en la que se mide el pico
    "baseline_secs": 120,  # periodo con el que se calcula "lo normal"
    "min_msgs": 10,        # mensajes mínimos en la ventana para disparar
    "cooldown_secs": 60,   # espera mínima entre dos clips
    "quality": "best",
    "layout": "blur",          # "blur" = fondo difuminado, "split" = cámara arriba + juego abajo
    "watched": [],             # canales vigilados; se reanudan al abrir la aplicación
    "cams": {},                # zona de la cámara por canal: [x, y, ancho, alto] en fracciones 0-1
    "gpu": True,               # usar la tarjeta gráfica NVIDIA si está disponible
    "subtitles": True,       # incrustar subtítulos automáticos
    "sub_language": "es",      # idioma del audio ("auto" para detectarlo)
    "whisper_model": "small",  # tiny/base/small/medium: más grande = mejor y más lento
    "keywords": ["kekw", "lul", "omegalul", "pog", "clip", "jaja", "xd", "lmao", "wtf", "???"],
}
NUMERIC = {
    "pre_secs": (5, 170), "post_secs": (0, 60), "factor": (1.2, 20),
    "window_secs": (3, 60), "baseline_secs": (30, 600),
    "min_msgs": (1, 500), "cooldown_secs": (5, 600),
    "max_clip_secs": (30, 180), "min_users": (1, 200),
}

settings = dict(DEFAULTS)
if CONFIG.exists():
    try:
        settings.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except ValueError:
        pass
settings["watched"] = list(settings.get("watched") or [])
settings["cams"] = dict(settings.get("cams") or {})

sessions = {}
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")


def parse_channel(text):
    text = (text or "").strip().rstrip("/").split("/")[-1].split("?")[0].lower()
    return text if re.fullmatch(r"[a-z0-9_]{2,25}", text) else None


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/api/state")
def state():
    return jsonify(
        sessions=[s.state() for s in sessions.values()],
        clips=list_clips(),
        settings=settings,
        gpu_found=nvenc_available(),
    )


@app.post("/api/sessions")
def start_session():
    channel = parse_channel((request.json or {}).get("channel"))
    if not channel:
        return jsonify(error="Nombre de canal no válido"), 400
    old = sessions.get(channel)
    if old and old.is_alive():
        return jsonify(error="Ese canal ya se está vigilando"), 400
    watch(channel)
    if channel not in settings["watched"]:
        settings["watched"].append(channel)
        save_config()
    return jsonify(ok=True)


@app.delete("/api/sessions/<channel>")
def stop_session(channel):
    s = sessions.pop(channel, None)
    if s:
        s.stop()
    if channel in settings["watched"]:
        settings["watched"].remove(channel)
        save_config()
    return jsonify(ok=True)


def watch(channel):
    sessions[channel] = Session(channel, settings)
    sessions[channel].start()


@app.post("/api/sessions/<channel>/clip")
def manual_clip(channel):
    s = sessions.get(channel)
    if not s or not s.manual_clip():
        return jsonify(error="El canal no se está grabando"), 400
    return jsonify(ok=True)


@app.post("/api/settings")
def save_settings():
    data = request.json or {}
    for key, (lo, hi) in NUMERIC.items():
        if key in data:
            try:
                val = float(data[key])
            except (TypeError, ValueError):
                continue
            val = min(hi, max(lo, val))
            settings[key] = val if key == "factor" else int(val)
    if data.get("layout") in ("blur", "split"):
        settings["layout"] = data["layout"]
    if "gpu" in data:
        settings["gpu"] = data["gpu"] in (True, "true", "on", "1")
    if "subtitles" in data:
        settings["subtitles"] = data["subtitles"] in (True, "true", "on", "1")
    if data.get("sub_language") in ("es", "en", "auto"):
        settings["sub_language"] = data["sub_language"]
    if data.get("whisper_model") in ("tiny", "base", "small", "medium"):
        settings["whisper_model"] = data["whisper_model"]
    if isinstance(data.get("keywords"), str):
        settings["keywords"] = [k.strip().lower() for k in data["keywords"].split(",") if k.strip()]
    save_config()
    return jsonify(ok=True, settings=settings)


def save_config():
    CONFIG.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/snapshot/<channel>")
def snapshot(channel):
    """Imagen del canal para marcar la cámara: del directo si se graba, si no del último clip."""
    channel = parse_channel(channel)
    if not channel:
        return jsonify(error="Canal no válido"), 400
    BUFFER_DIR.mkdir(exist_ok=True)
    out = BUFFER_DIR / f"snap_{channel}.jpg"
    out.unlink(missing_ok=True)
    s = sessions.get(channel)
    ok = bool(s and s.recorder and s.status == "grabando" and s.recorder.snapshot(out))
    if not ok:
        src = latest_original(channel)
        ok = bool(src and grab_frame(src, out, at=1))
    if not ok:
        return jsonify(error="Sin imagen"), 404
    return send_file(out, mimetype="image/jpeg", max_age=0)


@app.post("/api/cams")
def set_cam():
    data = request.json or {}
    channel = parse_channel(data.get("channel"))
    if not channel:
        return jsonify(error="Canal no válido"), 400
    rect = data.get("rect")
    if rect is None:
        settings["cams"].pop(channel, None)
    else:
        try:
            x, y, w, h = (float(v) for v in rect)
        except (TypeError, ValueError):
            return jsonify(error="Zona no válida"), 400
        x, y = min(max(x, 0), 0.97), min(max(y, 0), 0.97)
        w, h = min(w, 1 - x), min(h, 1 - y)
        if w < 0.03 or h < 0.03:
            return jsonify(error="La zona marcada es demasiado pequeña"), 400
        settings["cams"][channel] = [round(v, 4) for v in (x, y, w, h)]
    save_config()
    return jsonify(ok=True)


@app.get("/api/clips/detail")
def clip_detail_route():
    detail = clip_detail(request.args.get("id", ""))
    return jsonify(detail) if detail else (jsonify(error="Clip no encontrado"), 404)


@app.post("/api/clips/edit")
def clip_edit():
    data = request.json or {}
    err = edit_clip(data.get("id", ""), data.get("title"), data.get("trim"),
                    data.get("lines"), settings, data.get("overlay_text"))
    return (jsonify(error=err), 400) if err else (jsonify(ok=True), 200)


@app.post("/api/clips/reencode")
def clip_reencode():
    ok = reencode_clip((request.json or {}).get("id", ""), settings)
    return (jsonify(ok=True), 200) if ok else (jsonify(error="No se puede rehacer ese clip"), 400)


@app.post("/api/clips/status")
def clip_status():
    data = request.json or {}
    if data.get("status") not in ("pendiente", "aprobado"):
        return jsonify(error="Estado no válido"), 400
    err = set_clip_status(data.get("id", ""), data["status"])
    return (jsonify(error=err), 400) if err else (jsonify(ok=True), 200)


@app.post("/api/clips/delete")
def clip_delete():
    err = delete_clip((request.json or {}).get("id", ""))
    return (jsonify(error=err), 400) if err else (jsonify(ok=True), 200)


@app.get("/media/<path:rel>")
def media(rel):
    return send_from_directory(CLIPS_DIR, rel, conditional=True)


if __name__ == "__main__":
    CLIPS_DIR.mkdir(exist_ok=True)
    migrate_layout()  # adapta clips guardados con la estructura de carpetas antigua
    for ch in list(settings["watched"]):  # reanuda los canales vigilados la última vez
        if parse_channel(ch) == ch and ch not in sessions:
            watch(ch)
    if settings["watched"]:
        print("Vigilando de nuevo:", ", ".join(settings["watched"]))
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"Interfaz en http://127.0.0.1:{PORT}  (Ctrl+C para salir)")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
