"""Text-to-Speech con edge-tts. Devuelve WAV (PCM 16 bit mono 24 kHz) listo para reproducir en el parlante de la laptop."""

from __future__ import annotations

import io
import re
import wave
from pathlib import Path

import edge_tts
import miniaudio

from .config import Settings

WAV_SAMPLE_RATE = 24000  # edge-tts entrega MP3 a 24 kHz


class TTSError(RuntimeError):
    pass


def clean_for_speech(text: str) -> str:
    text = re.sub(r"[*_#`>~|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def mp3_to_wav(mp3: bytes) -> bytes:
    decoded = miniaudio.decode(
        mp3,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=WAV_SAMPLE_RATE,
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(WAV_SAMPLE_RATE)
        w.writeframes(decoded.samples.tobytes())
    return buf.getvalue()


class Synthesizer:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def synthesize(self, text: str) -> bytes:
        text = clean_for_speech(text)
        if not text:
            raise TTSError("texto vacío")
        comm = edge_tts.Communicate(text, self.settings.tts_voice, rate=self.settings.tts_rate)
        mp3 = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                mp3 += chunk["data"]
        if not mp3:
            raise TTSError("edge-tts no devolvió audio")
        return mp3_to_wav(bytes(mp3))


# -- escuchar voces (python -m app.tts) ---------------------------------------------------------------------

AUDITION_PHRASE = "Hola, Eliecer. Para mañana viernes hay tres horas con el cardiólogo: a las nueve y media, a las diez y media o a las once y cuarto. ¿Cuál prefiere?"
AUDITION_VOICES = [
    ("es-CL-CatalinaNeural", "chilena, mujer"),
    ("es-CL-LorenzoNeural", "chileno, hombre (la voz anterior)"),
    ("es-MX-DaliaNeural", "mexicana, mujer (neutra)"),
    ("es-US-PalomaNeural", "latina de EE.UU., mujer (neutra)"),
    ("es-AR-ElenaNeural", "argentina, mujer"),
    ("es-ES-ElviraNeural", "española, mujer"),
    ("es-ES-XimenaNeural", "española, mujer (más nueva)"),
    ("en-US-AvaMultilingualNeural", "EXPERIMENTAL: voz multilingüe, más expresiva pero con acento"),
]


def _main() -> None:
    import argparse
    import asyncio
    import dataclasses
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from mic_client import audio_output_warning, play  # el mismo reproductor que usa el asistente

    ap = argparse.ArgumentParser(prog="python -m app.tts", description="Escucha voces de edge-tts por el parlante de la laptop.")
    ap.add_argument("--audition", action="store_true", help="reproduce la misma frase en varias voces (elige una y ponla en TTS_VOICE del .env)")
    ap.add_argument("--voice", help="reproduce una frase con esta voz (ver --list)")
    ap.add_argument("--list", action="store_true", help="lista todas las voces en español disponibles")
    ap.add_argument("--rate", help="velocidad, p. ej. -5%% o +10%% (por defecto, TTS_RATE del .env)")
    ap.add_argument("text", nargs="*", help="frase para --voice (por defecto, la de la demo)")
    args = ap.parse_args()

    async def go() -> None:
        if args.list:
            for v in sorted(await edge_tts.list_voices(), key=lambda v: (v["Locale"], v["ShortName"])):
                if v["Locale"].lower().startswith("es-"):
                    print(f"{v['ShortName']:34} {v['Gender']}")
            return
        warning = audio_output_warning()
        if warning:
            print(f"\n  *** {warning}\n")
        settings = Settings.from_env()
        if args.rate:
            settings = dataclasses.replace(settings, tts_rate=args.rate)
        voices = AUDITION_VOICES if args.audition else [(args.voice or settings.tts_voice, "")]
        phrase = " ".join(args.text) or AUDITION_PHRASE
        for name, note in voices:
            print(f"\n▶ {name}  {note}  (velocidad {settings.tts_rate})", flush=True)
            wav = await Synthesizer(dataclasses.replace(settings, tts_voice=name)).synthesize(phrase)
            await asyncio.get_running_loop().run_in_executor(None, play, wav)
        if args.audition:
            print("\nElige la que más te guste y ponla en el .env:  TTS_VOICE=<nombre>   (y reinicia el backend)")

    asyncio.run(go())


if __name__ == "__main__":
    _main()
