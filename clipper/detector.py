"""Detector de momentos a partir de la actividad del chat."""
import threading
import time
from collections import deque

# Una vez abierto un momento, el chat se considera "todavía caliente" mientras su ritmo
# supere esta fracción del umbral de disparo. Evita cortar por un bajón de un segundo.
HOT_FRACTION = 0.6


class SpikeDetector:
    """Compara el ritmo de mensajes de los últimos segundos con la media reciente.

    Cuando el chat se dispara abre un "momento", que sigue abierto mientras el chat
    siga caliente; así un momento largo o varios picos seguidos dan un solo clip.

    Lee los ajustes del diccionario compartido en cada paso, así que los cambios
    hechos desde la interfaz se aplican al instante.
    """

    def __init__(self, settings):
        self.s = settings
        self.msgs = deque()  # (timestamp, peso, usuario)
        self.lock = threading.Lock()
        self.started = time.time()
        self.last_trigger = 0.0
        self.event = None  # momento en curso

    def add(self, user, text, now):
        low = text.lower()
        weight = 2.0 if any(k in low for k in self.s["keywords"]) else 1.0
        with self.lock:
            self.msgs.append((now, weight, user))

    def stats(self, now):
        """Devuelve (ritmo actual msg/s, ritmo base o None, peso en la ventana,
        personas distintas en la ventana)."""
        win = self.s["window_secs"]
        base_secs = self.s["baseline_secs"]
        with self.lock:
            while self.msgs and self.msgs[0][0] < now - win - base_secs:
                self.msgs.popleft()
            span = min(base_secs, max(0.0, now - win - self.started))
            cur = base = 0.0
            users = set()
            for t, w, u in self.msgs:
                if t > now - win:
                    cur += w
                    users.add(u)
                elif t > now - win - span:
                    base += w
        base_rate = base / span if span >= 20 else None  # sin datos suficientes aún
        return cur / win, base_rate, cur, len(users)

    def step(self, now):
        """Llamar una vez por segundo. Devuelve None, ("open", momento) al empezar un
        momento o ("close", momento) al terminar, con t0/t1 = tramo a guardar."""
        rate, base, cur, users = self.stats(now)
        s = self.s
        if self.event:
            ev = self.event
            if rate >= HOT_FRACTION * s["factor"] * max(ev["baseline"], 0.1):
                ev["last_hot"] = now
            ev["peak"] = max(ev["peak"], rate)
            ev["users"] = max(ev["users"], users)
            # termina al calmarse el chat, o al llegar a la duración máxima del clip
            cap = max(ev["t"] + 5, ev["t"] - s["pre_secs"] + s["max_clip_secs"])
            end = min(ev["last_hot"] + s["post_secs"], cap)
            return ("close", self._close(end)) if now >= end else None
        if base is None or now - self.last_trigger < s["cooldown_secs"]:
            return None
        if (cur >= s["min_msgs"] and users >= s["min_users"]
                and rate >= s["factor"] * max(base, 0.1)):
            self.event = {"t": now, "last_hot": now, "baseline": base,
                          "peak": rate, "users": users}
            return ("open", self.event)
        return None

    def finish(self, now):
        """Cierra a la fuerza el momento en curso (p. ej. al acabar el directo)."""
        return self._close(now) if self.event else None

    def _close(self, end):
        ev, self.event = self.event, None
        self.last_trigger = end  # la espera entre clips cuenta desde el final
        base = max(ev["baseline"], 0.1)
        return {
            "t0": ev["t"] - self.s["pre_secs"],
            "t1": end,
            "peak": round(ev["peak"], 2),
            "baseline": round(ev["baseline"], 2),
            "users": ev["users"],
            "score": round(ev["peak"] / base, 1),  # cuántas veces por encima de lo normal
        }
