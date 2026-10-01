#!/usr/bin/env python
"""Cliente de voz para la laptop: micrófono + barra espaciadora (push-to-talk) y parlante.

    MANTENER la barra espaciadora -> graba.   SOLTAR -> envía al backend y la voz de la IA suena en el parlante.
    Los avisos del sistema ("encontré estas horas...") se consultan solos y suenan cuando nadie está hablando.
    Ctrl+C para salir.

    python mic_client.py                    # backend en http://localhost:8000
    python mic_client.py --test-speaker     # comprueba que el parlante suena (sin backend)
    python mic_client.py --file prueba.wav  # envía un WAV sin usar el micrófono
    python mic_client.py --toggle           # ENTER inicia/termina (si no hay teclado global)
"""

from __future__ import annotations

import argparse
import io
import math
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import wave
from urllib.parse import unquote

import httpx
import sounddevice as sd

SAMPLE_RATE = 16_000  # lo que Whisper usa internamente
MIN_SECONDS = 0.4
POLL_EVERY_S = 1.5
POLL_MAX_S = 600
SPEECH_POLL_S = 1.5


def wav_bytes(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def play(wav: bytes) -> None:
    """Reproduce un WAV por el parlante de la laptop y espera a que termine de sonar."""
    with wave.open(io.BytesIO(wav)) as w:
        pcm, rate, channels = w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
    with sd.RawOutputStream(samplerate=rate, channels=channels, dtype="int16") as out:
        out.write(pcm)
        out.stop()  # espera a que suene todo; cerrar sin stop() descarta el final del audio


def beep(seconds: float = 0.6, freq: float = 440.0, rate: int = 24_000) -> bytes:
    n = int(seconds * rate)
    fade = int(0.02 * rate)
    samples = [int(9000 * math.sin(2 * math.pi * freq * i / rate) * min(1, i / fade, (n - i) / fade)) for i in range(n)]
    return wav_bytes(struct.pack(f"<{n}h", *samples), rate)


def audio_output_warning() -> str | None:
    """Avisa si el audio de salida de la laptop está silenciado (Linux con PulseAudio/PipeWire)."""
    try:
        out = subprocess.run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if out.split(":")[-1].strip().lower() in ("yes", "sí", "si"):
        return "El audio de la laptop está SILENCIADO: no se oirá la voz. Actívalo con:  pactl set-sink-mute @DEFAULT_SINK@ 0"
    return None


class Recorder:
    """Mantiene el micrófono abierto y solo guarda lo que llega mientras se está grabando."""

    def __init__(self, device: int | str | None):
        self._chunks: list[bytes] = []
        self._recording = False
        self._lock = threading.Lock()
        self._stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=device, callback=self._on_audio)

    def _on_audio(self, data, frames, time_info, status) -> None:
        if self._recording:
            with self._lock:
                self._chunks.append(bytes(data))

    def __enter__(self):
        self._stream.start()
        return self

    def __exit__(self, *exc):
        self._stream.stop()
        self._stream.close()

    def start(self) -> None:
        with self._lock:
            self._chunks = []
        self._recording = True

    def stop(self) -> bytes:
        self._recording = False
        with self._lock:
            return b"".join(self._chunks)


class Client:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(timeout=90.0, transport=transport)
        self.speech_http = httpx.Client(timeout=30.0, transport=transport)  # aparte: se usa desde otro hilo

    def send(self, wav: bytes) -> list[threading.Thread]:
        """Envía el audio, muestra y reproduce la respuesta. Devuelve los hilos que siguen las tareas."""
        t0 = time.time()
        try:
            r = self.http.post(f"{self.base_url}/api/process-audio", content=wav, headers={"Content-Type": "audio/wav"})
        except httpx.HTTPError as exc:
            print(f"\n[error] No pude hablar con el backend ({self.base_url}): {exc}")
            return []
        if r.status_code != 200:
            print(f"\n[error] El backend respondió {r.status_code}: {r.text[:300]}")
            return []
        transcript = unquote(r.headers.get("x-transcript", ""))
        reply = unquote(r.headers.get("x-reply", ""))
        task_ids = [t for t in r.headers.get("x-task-ids", "").split(",") if t]
        print(f"\n  Usted dijo : {transcript or '(nada)'}")
        print(f"  Asistente  : {reply}   [{time.time() - t0:.1f}s]")
        if r.headers.get("x-error"):
            print(f"  (aviso: {r.headers['x-error']})")
        followers = [threading.Thread(target=self.follow_task, args=(t,), daemon=True) for t in task_ids]
        for f in followers:
            f.start()
        try:
            play(r.content)
        except Exception as exc:
            print(f"[error] No pude reproducir el audio: {exc}")
        return followers

    def fetch_speech(self) -> tuple[str, bytes] | None:
        """Pide el siguiente aviso hablado. Devuelve (texto, audio) o None si no hay."""
        try:
            r = self.speech_http.get(f"{self.base_url}/api/speech/next")
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        return unquote(r.headers.get("x-reply", "")), r.content

    def poll_speech(self, ptt: "PushToTalk", every: float = SPEECH_POLL_S, stop: threading.Event | None = None) -> None:
        """Hilo de fondo: reproduce los avisos del sistema cuando nadie está grabando ni escuchando otra respuesta."""
        stop = stop or threading.Event()
        while not stop.wait(every):
            if not ptt.claim_audio():
                continue
            try:
                got = self.fetch_speech()
                if got is not None:
                    print(f"\n  Asistente (aviso): {got[0]}")
                    try:
                        play(got[1])
                    except Exception as exc:
                        print(f"[error] No pude reproducir el aviso: {exc}")
            finally:
                ptt.release_audio()

    def follow_task(self, task_id: str) -> None:
        print(f"  Tarea {task_id}: en segundo plano...")
        last, deadline = "", time.time() + POLL_MAX_S
        while time.time() < deadline:
            try:
                task = self.http.get(f"{self.base_url}/api/tasks/{task_id}").json()
            except (httpx.HTTPError, ValueError):
                time.sleep(POLL_EVERY_S)
                continue
            if task["status"] != last:
                last = task["status"]
                if last in ("queued", "running"):
                    print(f"  Tarea {task_id} [{task['kind']}]: {last}")
            if last in ("success", "error"):
                icon = "OK   " if last == "success" else "ERROR"
                delivered = task["result"].get("delivered")
                suffix = "" if delivered is None else (" | panel: entregado" if delivered else " | panel: NO entregado")
                print(f"  Tarea {task_id} [{task['kind']}] {icon}: {task['summary']}{suffix}")
                return
            time.sleep(POLL_EVERY_S)
        print(f"  Tarea {task_id}: sin respuesta tras {POLL_MAX_S}s")


class PushToTalk:
    """Barra espaciadora = push-to-talk. press()/release() las llama el teclado (o los tests).

    Mientras se procesa una respuesta o suena un aviso (busy) no se graba: el micrófono captaría el parlante.
    """

    def __init__(self, recorder: Recorder, client: Client, min_seconds: float = MIN_SECONDS):
        self.recorder, self.client, self.min_bytes = recorder, client, int(min_seconds * SAMPLE_RATE * 2)
        self.busy = False  # procesando una respuesta o reproduciendo un aviso
        self.held = False  # la tecla está apretada
        self._recording = False
        self._lock = threading.Lock()
        self._jobs: queue.Queue[bytes] = queue.Queue()

    def press(self) -> None:
        with self._lock:
            if self.held:  # el sistema repite la tecla mientras se mantiene apretada
                return
            self.held = True
            if self.busy:
                print("\r  (espere a que termine de hablar el asistente)", end="", flush=True)
                return
            self._recording = True
        self.recorder.start()
        print("\r  Grabando... suelte la barra para enviar", end="", flush=True)

    def release(self) -> None:
        with self._lock:
            if not self.held:
                return
            self.held = False
            if not self._recording:  # la pulsación empezó mientras el asistente hablaba: no grabó nada
                return
            self._recording = False
        pcm = self.recorder.stop()
        if len(pcm) < self.min_bytes:
            print("\r  Muy corto: mantenga la barra apretada mientras habla.      ")
            return
        with self._lock:
            self.busy = True
        self._jobs.put(pcm)

    def claim_audio(self) -> bool:
        """Reserva el parlante para un aviso si nadie está grabando ni escuchando otra respuesta."""
        with self._lock:
            if self.busy or self.held:
                return False
            self.busy = True
            return True

    def release_audio(self) -> None:
        with self._lock:
            self.busy = False

    def process_next(self, timeout: float = 0.2) -> bool:
        """Envía la siguiente grabación pendiente (bloquea hasta que termina de sonar la respuesta)."""
        try:
            pcm = self._jobs.get(timeout=timeout)
        except queue.Empty:
            return False
        print("\r  Procesando...                                  ", end="", flush=True)
        try:
            self.client.send(wav_bytes(pcm))
        finally:
            self.release_audio()
            print("\nListo: mantenga la barra para hablar.")
        return True


def run_push_to_talk(ptt: PushToTalk) -> None:
    from pynput import keyboard

    threading.Thread(target=ptt.client.poll_speech, args=(ptt,), daemon=True).start()
    with keyboard.Listener(
        on_press=lambda key: ptt.press() if key == keyboard.Key.space else None,
        on_release=lambda key: ptt.release() if key == keyboard.Key.space else None,
    ):
        print("Listo. Mantenga presionada la BARRA ESPACIADORA para hablar. Ctrl+C para salir.")
        while True:
            ptt.process_next()


def run_toggle(ptt: PushToTalk) -> None:
    """Respaldo sin teclado global: ENTER en esta terminal inicia y termina la grabación."""
    print("Modo alternar: presione ENTER para empezar a grabar y de nuevo para enviar. Ctrl+C para salir.")
    threading.Thread(target=ptt.client.poll_speech, args=(ptt,), daemon=True).start()
    while True:
        input("  [enter] para grabar... ")
        ptt.press()
        input("")
        ptt.release()
        ptt.process_next(timeout=0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.getenv("BACKEND_URL", "http://localhost:8000"))
    ap.add_argument("--device", default=None, help="índice o nombre del micrófono (ver --list-devices)")
    ap.add_argument("--speaker", default=None, help="índice o nombre del parlante (ver --list-devices)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--test-speaker", action="store_true", help="reproduce un pitido para comprobar el parlante")
    ap.add_argument("--file", help="enviar este WAV en vez de usar el micrófono")
    ap.add_argument("--toggle", action="store_true", help="ENTER inicia/termina en vez de mantener la barra")
    args = ap.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0

    def as_device(value):
        return int(value) if value and value.isdigit() else value

    if args.speaker is not None:
        sd.default.device = (sd.default.device[0], as_device(args.speaker))

    warning = audio_output_warning()
    if warning:
        print(f"\n  *** {warning}\n")

    if args.test_speaker:
        print("Reproduciendo un pitido por el parlante...")
        play(beep())
        print("Si no lo oyó: revise el volumen, que no esté silenciado, o elija otra salida con --speaker (ver --list-devices).")
        return 0

    client = Client(args.url)
    if args.file:
        with open(args.file, "rb") as f:
            for follower in client.send(f.read()):
                follower.join()  # espera a que terminen las tareas en segundo plano
        for _ in range(8):  # y al aviso hablado que suele llegar justo después
            got = client.fetch_speech()
            if got:
                print(f"\n  Asistente (aviso): {got[0]}")
                play(got[1])
                break
            time.sleep(SPEECH_POLL_S)
        return 0

    old_tty = None
    try:
        import termios

        if sys.stdin.isatty() and not args.toggle:  # evita que los espacios se impriman mientras se mantiene la barra
            old_tty = termios.tcgetattr(sys.stdin)
            attrs = termios.tcgetattr(sys.stdin)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, attrs)
    except ImportError:
        pass

    try:
        with Recorder(as_device(args.device)) as rec:
            ptt = PushToTalk(rec, client)
            if args.toggle:
                run_toggle(ptt)
            else:
                try:
                    run_push_to_talk(ptt)
                except ImportError:
                    print("pynput no está disponible; uso el modo --toggle.")
                    run_toggle(ptt)
    except KeyboardInterrupt:
        print("\nHasta luego.")
    finally:
        if old_tty is not None:
            import termios

            termios.tcflush(sys.stdin, termios.TCIFLUSH)
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
    return 0


if __name__ == "__main__":
    sys.exit(main())
