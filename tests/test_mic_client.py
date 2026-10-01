"""Push-to-talk con la barra espaciadora y reproducción en la laptop (sin micrófono ni parlante reales)."""

import io
import subprocess
import threading
import time
import wave
from types import SimpleNamespace as NS

import httpx
import pytest

import mic_client as m

SECOND = b"\x01\x02" * m.SAMPLE_RATE  # 1 s de audio PCM 16 bit


class FakeRecorder:
    def __init__(self, pcm=SECOND):
        self.pcm, self.starts, self.stops = pcm, 0, 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1
        return self.pcm


class FakeClient:
    def __init__(self):
        self.sent = []

    def send(self, wav):
        self.sent.append(wav)
        return []


def ptt(pcm=SECOND):
    rec, client = FakeRecorder(pcm), FakeClient()
    return m.PushToTalk(rec, client), rec, client


def test_hold_space_records_and_release_sends_one_wav():
    p, rec, client = ptt()
    p.press()
    p.press(); p.press()  # el sistema repite la tecla mientras se mantiene apretada
    assert rec.starts == 1 and p.held
    p.release()
    assert rec.stops == 1 and not p.held and p.busy  # busy: se está procesando, no se graba otra cosa
    assert p.process_next(timeout=0) is True
    w = wave.open(io.BytesIO(client.sent[0]))
    assert (w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getnframes()) == (1, m.SAMPLE_RATE, 2, m.SAMPLE_RATE)
    assert not p.busy  # listo para la próxima
    assert p.process_next(timeout=0) is False


def test_a_tap_that_is_too_short_is_discarded():
    p, rec, client = ptt(pcm=b"\x00\x00" * 100)
    p.press(); p.release()
    assert p.process_next(timeout=0) is False and client.sent == [] and not p.busy


def test_pressing_while_the_assistant_is_busy_does_not_record():
    p, rec, client = ptt()
    p.press(); p.release()  # primera consulta en proceso
    assert p.busy
    p.press()  # la persona aprieta mientras la IA todavía habla
    assert rec.starts == 1 and p.held
    p.release()
    assert rec.stops == 1  # no se llamó a stop(): esa pulsación no grabó nada
    p.process_next(timeout=0)
    assert len(client.sent) == 1 and not p.busy
    p.press(); p.release()  # y ahora sí, libre
    p.process_next(timeout=0)
    assert rec.starts == 2 and len(client.sent) == 2


def test_press_that_began_busy_and_ended_free_does_not_stop_a_recording_that_never_started():
    p, rec, client = ptt()
    p.press(); p.release()
    p.press()  # busy: no graba
    p.process_next(timeout=0)  # la respuesta termina mientras la barra sigue apretada
    p.release()
    assert rec.stops == 1 and len(client.sent) == 1  # no hubo un segundo envío fantasma


def test_release_without_press_is_ignored():
    p, rec, client = ptt()
    p.release()
    assert rec.stops == 0 and not p.busy


def test_spoken_notices_wait_while_the_bar_is_held_or_the_assistant_talks():
    p, _, _ = ptt()
    assert p.claim_audio() is True and p.busy
    assert p.claim_audio() is False  # ya suena algo
    p.release_audio()
    p.press()  # la persona empieza a hablar
    assert p.claim_audio() is False
    p.release()
    p.process_next(timeout=0)
    assert p.claim_audio() is True


def test_poller_plays_notice_through_the_laptop_speaker_when_idle(monkeypatch):
    played = []
    monkeypatch.setattr(m, "play", lambda wav: played.append(wav))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/speech/next":
            return httpx.Response(200, content=b"WAVDATA", headers={"x-reply": "Hay%20tres%20horas"}) if not played else httpx.Response(204)
        return httpx.Response(404)

    client = m.Client("http://backend", transport=httpx.MockTransport(handler))
    p = m.PushToTalk(FakeRecorder(), client)
    stop = threading.Event()
    poller = threading.Thread(target=client.poll_speech, args=(p, 0.02, stop), daemon=True)
    poller.start()
    try:
        deadline = time.time() + 2
        while not played and time.time() < deadline:
            time.sleep(0.02)
        assert played == [b"WAVDATA"]
        time.sleep(0.1)
        assert not p.busy  # el parlante se libera al terminar
    finally:
        stop.set()
        poller.join(timeout=2)


def test_poller_does_not_play_while_the_bar_is_held(monkeypatch):
    played = []
    monkeypatch.setattr(m, "play", lambda wav: played.append(wav))
    client = m.Client("http://backend", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"X", headers={"x-reply": "hola"})))
    p = m.PushToTalk(FakeRecorder(), client)
    p.press()
    stop = threading.Event()
    poller = threading.Thread(target=client.poll_speech, args=(p, 0.02, stop), daemon=True)
    poller.start()
    try:
        time.sleep(0.3)
        assert played == []
        p.release(); p.process_next(timeout=0)
        deadline = time.time() + 2
        while not played and time.time() < deadline:
            time.sleep(0.02)
        assert played
    finally:
        stop.set()
        poller.join(timeout=2)


def test_send_posts_raw_wav_plays_the_reply_and_follows_the_task(monkeypatch, capsys):
    played, polls = [], []
    monkeypatch.setattr(m, "play", lambda wav: played.append(wav))
    monkeypatch.setattr(m, "POLL_EVERY_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/process-audio":
            assert request.headers["content-type"] == "audio/wav" and request.content == b"MYWAV"
            return httpx.Response(200, content=b"REPLYWAV", headers={
                "content-type": "audio/wav", "x-transcript": "quiero%20una%20hora", "x-reply": "Ya%20reviso%20las%20horas.", "x-task-ids": "t1"})
        if request.url.path == "/api/tasks/t1":
            polls.append(1)
            status = "running" if len(polls) < 2 else "success"
            return httpx.Response(200, json={"id": "t1", "kind": "buscar_horas", "status": status, "summary": "3 horas encontradas", "result": {}})
        return httpx.Response(404)

    client = m.Client("http://backend", transport=httpx.MockTransport(handler))
    for follower in client.send(b"MYWAV"):
        follower.join(timeout=5)
    out = capsys.readouterr().out
    assert played == [b"REPLYWAV"]
    assert "Usted dijo : quiero una hora" in out and "Asistente  : Ya reviso las horas." in out
    assert "buscar_horas" in out and "OK" in out and "3 horas encontradas" in out


def test_send_reports_backend_errors_without_crashing(monkeypatch, capsys):
    monkeypatch.setattr(m, "play", lambda wav: pytest.fail("no debe reproducir nada"))
    client = m.Client("http://backend", transport=httpx.MockTransport(lambda r: httpx.Response(502, text="No se pudo generar la voz")))
    assert client.send(b"x") == []
    assert "502" in capsys.readouterr().out

    def down(request):
        raise httpx.ConnectError("refused")

    assert m.Client("http://backend", transport=httpx.MockTransport(down)).send(b"x") == []
    assert "No pude hablar con el backend" in capsys.readouterr().out


@pytest.mark.parametrize("pactl_output,expected", [("Mute: sí\n", True), ("Mute: yes\n", True), ("Mute: no\n", False), ("Mute: no", False)])
def test_warns_when_the_laptop_audio_is_muted(monkeypatch, pactl_output, expected):
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: NS(stdout=pactl_output))
    assert (m.audio_output_warning() is not None) is expected


def test_no_warning_when_pactl_is_not_available(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(m.subprocess, "run", missing)
    assert m.audio_output_warning() is None


def test_beep_is_a_valid_wav():
    w = wave.open(io.BytesIO(m.beep(0.2)))
    assert w.getnchannels() == 1 and w.getsampwidth() == 2 and abs(w.getnframes() / w.getframerate() - 0.2) < 0.01
