"""Lector anónimo del chat de Twitch por IRC (no necesita cuenta ni claves)."""
import random
import socket
import threading
import time

HOST = "irc.chat.twitch.tv"
PORT = 6667


class ChatReader(threading.Thread):
    def __init__(self, channel, on_message):
        super().__init__(daemon=True)
        self.channel = channel.lower()
        self.on_message = on_message  # on_message(usuario, texto, timestamp)
        self.connected = False
        self._stop_ev = threading.Event()
        self._sock = None

    def stop(self):
        self._stop_ev.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    def run(self):
        while not self._stop_ev.is_set():
            try:
                self._session()
            except OSError:
                pass
            self.connected = False
            self._stop_ev.wait(5)  # espera antes de reconectar

    def _session(self):
        sock = socket.create_connection((HOST, PORT), timeout=15)
        self._sock = sock
        sock.settimeout(2)
        nick = f"justinfan{random.randint(10000, 99999)}"
        sock.sendall(f"NICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
        self.connected = True
        buf = b""
        last_data = time.time()
        while not self._stop_ev.is_set():
            try:
                data = sock.recv(4096)
            except socket.timeout:
                if time.time() - last_data > 400:  # Twitch manda PING cada ~5 min
                    break
                continue
            if not data:
                break
            last_data = time.time()
            buf += data
            *lines, buf = buf.split(b"\r\n")
            for raw in lines:
                line = raw.decode("utf-8", errors="replace")
                if line.startswith("PING"):
                    sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                elif " PRIVMSG #" in line:
                    try:
                        prefix, text = line.split(" PRIVMSG #", 1)
                        user = prefix[1:].split("!", 1)[0]
                        text = text.split(" :", 1)[1]
                    except IndexError:
                        continue
                    self.on_message(user, text, time.time())
        sock.close()
