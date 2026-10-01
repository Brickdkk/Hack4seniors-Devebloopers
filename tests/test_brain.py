from types import SimpleNamespace as NS

import pytest

from app import brain as brain_mod
from app.brain import REFUSAL_REPLY, Brain


def text(t):
    return NS(type="text", text=t)


def tool(name, **inp):
    return NS(type="tool_use", id="toolu_1", name=name, input=inp)


class FakeMessages:
    def __init__(self, owner, beta):
        self.owner, self.beta = owner, beta

    async def create(self, **kwargs):
        self.owner.calls.append((self.beta, kwargs))
        return self.owner.responses.pop(0)


class FakeAnthropic:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.messages = FakeMessages(self, beta=False)
        self.beta = NS(messages=FakeMessages(self, beta=True))


def resp(*content, stop_reason="end_turn", category=None):
    return NS(content=list(content), stop_reason=stop_reason, stop_details=NS(category=category))


async def test_plain_text_reply(settings):
    fake = FakeAnthropic(resp(text("Buenos días, doña Rosa. ¿Cómo amaneció?")))
    result = await Brain(settings, fake).think("hola")
    assert result.reply == "Buenos días, Rosa. ¿Cómo amaneció?"  # el "doña" se quita: se le llama solo por su nombre
    assert result.tool_calls == [] and not result.refused


async def test_tool_call_uses_spoken_phrase_from_tool_input(settings):
    call = tool("buscar_horas", especialidad="Cardiología", fecha_preferida="2026-10-02", horario_preferido="en la mañana",
                detalles="", datos_paciente="", frase_hablada="Ya reviso las horas disponibles.")
    fake = FakeAnthropic(resp(text("texto suelto que se ignora"), call))
    result = await Brain(settings, fake).think("quiero hora con el cardiólogo")
    assert result.reply == "Ya reviso las horas disponibles."
    assert [c.name for c in result.tool_calls] == ["buscar_horas"]
    assert result.tool_calls[0].input["especialidad"] == "Cardiología"


async def test_tool_call_without_phrase_falls_back_to_text_then_default(settings):
    call = tool("avisar_al_cuidador", urgencia="info", mensaje="Pidió algo", frase_hablada="")
    fake = FakeAnthropic(resp(text("Listo, le aviso."), call), resp(call))
    brain = Brain(settings, fake)
    assert (await brain.think("a")).reply == "Listo, le aviso."
    assert (await brain.think("b")).reply == brain_mod.DEFAULT_ACK


async def test_refusal_returns_safe_reply_and_is_not_remembered(settings):
    fake = FakeAnthropic(resp(stop_reason="refusal", category="cyber"), resp(text("Hola")))
    brain = Brain(settings, fake)
    result = await brain.think("algo")
    assert result.refused and result.reply == REFUSAL_REPLY and result.tool_calls == []
    await brain.think("hola")
    assert len(fake.calls[1][1]["messages"]) == 1  # la petición rechazada no quedó en el historial


async def test_history_is_replayed_as_plain_text_and_expires(settings, monkeypatch):
    fake = FakeAnthropic(resp(text("Me alegra.")), resp(text("De nada.")), resp(text("Hola de nuevo.")))
    brain = Brain(settings, fake)
    await brain.think("estoy bien")
    await brain.think("gracias")
    second = fake.calls[1][1]["messages"]
    assert second == [
        {"role": "user", "content": "estoy bien"},
        {"role": "assistant", "content": "Me alegra."},
        {"role": "user", "content": "gracias"},
    ]
    brain._last_turn -= brain_mod.HISTORY_TTL_S + 1
    await brain.think("hola")
    assert len(fake.calls[2][1]["messages"]) == 1


async def test_request_shape_with_and_without_fallback(settings):
    from dataclasses import replace

    fake = FakeAnthropic(resp(text("a")))
    await Brain(settings, fake).think("x")
    is_beta, kw = fake.calls[0]
    assert is_beta and kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert kw["model"] == "claude-sonnet-5-5" and kw["output_config"] == {"effort": "low"}
    assert "tool_choice" not in kw and "thinking" not in kw and "temperature" not in kw
    assert all(t["strict"] for t in kw["tools"])

    fake = FakeAnthropic(resp(text("a")))
    await Brain(replace(settings, refusal_fallback=False), fake).think("x")
    is_beta, kw = fake.calls[0]
    assert not is_beta and "fallbacks" not in kw


def test_system_prompt_has_the_booking_flow_and_the_current_state(settings):
    prompt = brain_mod.build_system_prompt(settings, "Horas disponibles encontradas:\n  1) jueves, 10:30 - Dr. Pérez")
    assert "asistente de voz de Rosa" in prompt and "doña Rosa" not in prompt
    assert settings.caregiver_name in prompt and settings.emergency_number in prompt
    assert "1) jueves, 10:30 - Dr. Pérez" in prompt and "Estado actual:" in prompt
    for step in ("buscar_horas", "proponer_reserva", "reservar_hora", "abandonar_reserva"):
        assert step in prompt
    assert "Solo si responde con claridad que sí" in prompt  # la confirmación explícita está en el prompt (y además la exige el servidor)


def test_prompt_uses_saved_data_and_the_bare_name(settings):
    from dataclasses import replace

    prompt = brain_mod.build_system_prompt(replace(settings, elder_name="don Eliecer"), "Esos son sus datos GUARDADOS en el sistema.")
    assert "rellenas todo con sus datos guardados" in prompt and "NO se los pidas" in prompt
    assert "asistente de voz de Eliecer" in prompt and "don Eliecer" not in prompt  # aunque el .env diga "don", se le llama solo por su nombre
    assert 'nunca le antepongas "don" ni "doña"' in prompt
    assert "recordatorio" not in prompt.lower() and "alarma" not in prompt.lower()


@pytest.mark.parametrize("said,expected", [
    ("Don Eliecer, ya reviso las horas.", "Eliecer, ya reviso las horas."),
    ("Claro, don Eliecer. ¿Qué día le acomoda?", "Claro, Eliecer. ¿Qué día le acomoda?"),
    ("Perfecto, doña Eliecer.", "Perfecto, Eliecer."),
    ("Perfecto, Eliecer. Mañana hay hora.", "Perfecto, Eliecer. Mañana hay hora."),
    ("Don Pedro llamó por usted.", "Don Pedro llamó por usted."),  # otros "don" no se tocan
])
def test_drop_honorific_only_touches_the_persons_name(said, expected):
    assert brain_mod.drop_honorific(said, "Eliecer") == expected
    assert brain_mod.drop_honorific(said, "don Eliecer") == expected


async def test_replies_never_say_don_even_if_the_model_does(settings):
    from dataclasses import replace

    s = replace(settings, elder_name="Eliecer")
    call = tool("buscar_horas", especialidad="Cardiología", fecha_preferida="2026-10-02", horario_preferido="", detalles="", datos_paciente="",
                frase_hablada="Un momento, don Eliecer, ya reviso las horas.")
    fake = FakeAnthropic(resp(text("Hola, don Eliecer. ¿En qué le ayudo?")), resp(call), resp(text("Don Eliecer, hay tres horas.")))
    brain = Brain(s, fake)
    assert (await brain.think("hola")).reply == "Hola, Eliecer. ¿En qué le ayudo?"
    assert (await brain.think("quiero hora")).reply == "Un momento, Eliecer, ya reviso las horas."
    assert (await brain.follow_up("La búsqueda terminó", "ctx")).reply == "Eliecer, hay tres horas."
    assert all("don" not in m["content"].lower() for m in fake.calls[2][1]["messages"] if m["role"] == "assistant")  # ni siquiera queda en el historial


def test_tools_are_strict_and_each_carries_the_spoken_phrase():
    assert [t["name"] for t in brain_mod.TOOLS] == [
        "buscar_horas", "proponer_reserva", "reservar_hora", "abandonar_reserva", "tramitar_en_clinica", "avisar_al_cuidador",
    ]
    for t in brain_mod.TOOLS:
        schema = t["input_schema"]
        assert t["strict"] is True and schema["additionalProperties"] is False
        assert "frase_hablada" in schema["properties"] and set(schema["required"]) == set(schema["properties"])
    propose = next(t for t in brain_mod.TOOLS if t["name"] == "proponer_reserva")["input_schema"]["properties"]
    assert propose["opcion"]["type"] == "integer" and {"nombre_completo", "rut", "telefono"} <= set(propose)
    consult = next(t for t in brain_mod.TOOLS if t["name"] == "tramitar_en_clinica")["input_schema"]["properties"]["tramite"]
    assert consult["enum"] == ["cancelar_hora", "consultar_horas"]  # agendar tiene su propio flujo con confirmación


async def test_follow_up_marks_the_message_as_an_automatic_notice_and_keeps_it_in_history(settings):
    fake = FakeAnthropic(resp(text("Hay dos horas: ¿cuál prefiere?")), resp(text("La segunda, perfecto.")))
    brain = Brain(settings, fake)
    result = await brain.follow_up("La búsqueda terminó: hay 2 horas.", "Horas disponibles encontradas:\n  1) a\n  2) b")
    assert result.reply == "Hay dos horas: ¿cuál prefiere?"
    sent = fake.calls[0][1]
    assert sent["messages"][-1]["content"].startswith("[Aviso automático del sistema; no lo dijo la persona]")
    assert "  2) b" in sent["system"]

    await brain.think("la segunda")
    history = fake.calls[1][1]["messages"]
    assert history[0]["content"].startswith("[Aviso automático") and history[1] == {"role": "assistant", "content": "Hay dos horas: ¿cuál prefiere?"}
    assert history[2] == {"role": "user", "content": "la segunda"}
