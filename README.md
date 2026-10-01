# Hack4Seniors: asistente de voz para personas mayores

En esta version se usa el microfono y parlante del dispositivo, la persona **mantiene apretada la barra espaciadora y habla** → la IA le responde **por el parlante de la laptop** de inmediato → en segundo plano un agente trabaja en el portal de la clínica → el resultado le llega por voz y al panel del cuidador.

```
mic_client.py (laptop)                    backend FastAPI (uvicorn)
  barra espaciadora = push-to-talk  ──▶   POST /api/process-audio
  parlante de la laptop ◀── WAV ────────   Groq Whisper → Claude + herramientas → edge-tts
        ▲                                   └─ tareas en segundo plano
        │                                        ├ buscar_horas / reservar_hora / tramitar_en_clinica → Playwright en CLINIC_PORTAL_URL
        └── GET /api/speech/next ◀── aviso        └ avisar_al_cuidador ─────────────────────────────────────────────┐
            hablado al terminar una tarea                    reserva confirmada / avisos / emergencias ─▶ POST {CAREGIVER_PANEL_URL}/api/logs
```

## La demo: agendar una hora conversando

Cada paso es un turno de voz. La IA hace una pregunta por vez y nunca reserva sin un "sí" explícito:

1. **Pide los datos** que faltan: especialidad, día y horario que le acomodan.
2. **Busca** las horas en el portal (`buscar_horas`, segundo plano). Mientras tanto dice "ya reviso las horas".
3. **Dice las horas disponibles** (aviso hablado) y pregunta cuál prefiere.
4. **Pide nombre completo, RUT y teléfono** (de a uno, repitiéndolos) y **pide confirmación**: *"¿Le confirmo la hora de mañana a las diez y media con el doctor Pérez, a nombre de René Soto?"* (`proponer_reserva`).
5. Solo si responde que sí, **reserva** en el portal (`reservar_hora`), le **avisa por voz** que quedó (con el código) y **la reserva aparece en el panel** con su recordatorio.

Si el portal pide un dato que no se tiene (por ejemplo el RUT para mostrar horas), el agente se detiene, la IA se lo pide a la persona y la búsqueda continúa en la misma ventana. Si la persona cambia de idea (otro día, otra hora, "mejor no"), se reutiliza la ventana o se cancela con `abandonar_reserva`.

**Salvaguardas en el servidor** (no dependen solo del prompt): `reservar_hora` no se ejecuta si no hay una propuesta vigente (5 min) para *esa misma* hora; en ese caso la frase se reemplaza por la pregunta de confirmación. Tampoco se propone sin nombre y RUT, ni con un número de hora que no esté en la lista, ni se reserva dos veces a la vez. Un aviso automático del sistema solo puede avisar a la cuidadora, nunca lanzar trámites.

El navegador queda **visible y abierto entre turnos** (se ve la lista de horas mientras la IA pregunta) y se cierra al terminar o a los 10 minutos sin actividad. Si se cerró antes de confirmar, la reserva busca de nuevo la hora elegida.

**Datos guardados:** el nombre completo, RUT y edad de la persona viven en `.env` (`PATIENT_FULL_NAME`, `PATIENT_RUT`, `PATIENT_AGE`; el RUT se acepta con o sin puntos). La IA no se los pide: al reservar dice que rellena todo con sus datos guardados y pide confirmación nombrando solo la hora y el médico (no lee el RUT en voz alta). El teléfono (`PATIENT_PHONE`) es opcional: solo se pide si el portal lo exige. Si dejas esos campos vacíos, la IA se los pide por voz.

## Correrlo (3 terminales)

```bash
cd ~/hack4seniors && source .venv/bin/activate

# una vez: que el audio de la laptop esté activo y elegir la voz
pactl set-sink-mute @DEFAULT_SINK@ 0
python mic_client.py --test-speaker          # debe sonar un pitido
python -m app.tts --audition                 # compara voces; la elegida va en TTS_VOICE del .env

# Terminal A: portal de la clínica en localhost:8080 (el de tu compañero; si no está, el de pruebas)
python -m http.server 8080 --directory tests/fake_portal

# Terminal B: backend (imprime los tiempos de cada turno y los envíos al panel)
uvicorn app.main:app --port 8000

# Terminal C: cliente de voz. MANTÉN la barra espaciadora para hablar, suéltala para enviar
python mic_client.py
```

Guion de la demo: *"Necesito una hora con el cardiólogo para mañana en la mañana"* → *"La segunda"* → *"Sí, confírmala"*. La IA te llama solo por tu nombre (sin "don"), rellena todo con tus datos guardados y reserva solo después de tu "sí".

## Puesta en marcha

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env        # completa ANTHROPIC_API_KEY y GROQ_API_KEY (solo en .env, nunca en .env.example)

uvicorn app.main:app --port 8000          # terminal 1: backend (imprime los tiempos de cada turno)
python mic_client.py                      # terminal 2: mantén ESPACIO para hablar, suelta para enviar
python -m pytest                          # tests (no necesitan claves ni micrófono)
```

Voz de la IA: se cambia con `TTS_VOICE` / `TTS_RATE` en `.env`. Para compararlas de oído: `python -m app.tts --audition` (la misma frase en 8 voces) o `python -m app.tts --voice es-MX-DaliaNeural "Hola, Eliecer"`; `--list` muestra las 45 voces en español.

`mic_client.py`:
- `--test-speaker`: reproduce un pitido para comprobar el parlante (sin backend). Si el audio de la laptop está silenciado, lo avisa al arrancar.
- `--speaker N` / `--device N`: elige parlante / micrófono (`--list-devices` los muestra).
- `--toggle`: ENTER inicia y termina la grabación, por si no hay teclado global (p. ej. Wayland).
- `--file x.wav`: envía un WAV sin usar el micrófono.

Mientras la IA habla (o suena un aviso) la barra no graba, para que el micrófono no capte el parlante. Los avisos del sistema ("encontré estas horas…") suenan solos cuando nadie está hablando.

## API del backend

`POST /api/process-audio`: cuerpo = WAV crudo (`Content-Type: audio/wav`). Responde el audio de la voz (WAV PCM 16 bit mono 24 kHz). Texto y tareas viajan en headers, con el texto en %-encoding UTF-8: `X-Transcript` (lo que dijo), `X-Reply` (lo que se le respondió), `X-Task-Ids` (ids separados por coma), `X-Error` (solo si algo falló; igual viene audio con una disculpa).

`GET /api/speech/next`: `200` = WAV de un aviso del sistema (texto en `X-Reply`), `204` = nada pendiente. Es como la persona se entera de las horas disponibles y de que su hora quedó reservada.

`GET /api/tasks` · `GET /api/tasks/{id}` (`queued|running|success|error`) · `GET /health`.

## Panel del cuidador (`CAREGIVER_PANEL_URL`)

El panel desplegado recibe `POST {url}/api/logs` con su propio esquema (`static/datalogger.js` del panel). Si `CAREGIVER_PANEL_URL` es solo el sitio (`https://panel-cuidador.vercel.app/`), se agrega `/api/logs`; si trae una ruta, se usa tal cual. El adulto mayor se asocia por nombre (`ELDER_NAME`): debe existir en el panel. La IA le habla solo por su nombre; si en `ELDER_NAME` pones "don Eliecer", el "don" se quita.

| Qué pasó | `category` / `status` | Detalle |
|---|---|---|
| **Reserva confirmada** | `cita_medica` / `ok` | `title` "Hora médica de Cardiología", `due_at` de la cita y `create_reminder: true` (el panel crea el recordatorio); `metadata`: especialidad, profesional, fecha, hora, código |
| Reserva o búsqueda fallida | `tramite` / `alerta` | |
| Cancelar o consultar horas | `tramite` / `ok` o `alerta` | |
| La persona pidió avisar algo | `mensaje` / `ok` | "Recado para {CAREGIVER_NAME}" |
| Aviso importante / emergencia | `alerta` / `alerta` | "🚨 Emergencia de …" |
| Falla de transcripción, razonamiento o tarea | `alerta` / `alerta` | `metadata.stage` |

Siempre `source: "agente_llm"`, `actor: "Asistente de voz"`. La búsqueda de horas y las propuestas **no** generan logs: al panel solo llega lo que ya pasó.

**El panel es público y no tiene autenticación**, así que nunca se le envía RUT, teléfono, nombre completo de la persona (los datos guardados incluidos) ni rutas locales: solo el código de confirmación (no el texto de la pantalla del portal) y un filtro oculta RUT y teléfonos en cualquier texto que salga, más los datos exactos que la persona dictó (`[dato oculto]`).

Todo evento queda también en `data/outbox.jsonl` (el evento completo, lo enviado al panel, `delivered` y el `panel_log_id`); con el panel caído no se pierde nada. Los envíos se reintentan 3 veces ante errores 5xx o de red.

## Cómo funciona el agente del portal

No usa selectores fijos: en cada paso Claude recibe la URL, el texto visible y una lista numerada de elementos interactivos de la página, y responde con acciones (`click`, `type_text`, `select_option`, `press_key`, `go_to`). Trabaja por **fases** sobre una sesión que sigue abierta entre turnos: la búsqueda termina con `report_slots` (las horas, sin reservar nada), la reserva con `finish`, y si el portal pide un dato que falta, con `need_info`. Funciona con la web que haga el Compañero 1 siempre que sea una página normal (enlaces, formularios, botones). Solo puede navegar dentro del origen de `CLINIC_PORTAL_URL`, tiene tope de pasos y de tiempo, y trata el contenido de la página como datos, no como instrucciones.

## Notas

- Las variables del LLM se llaman `LLM_*` (no `CLAUDE_*`) porque Claude Code define `CLAUDE_EFFORT` en su entorno y pisaría la configuración.
- `.env.example` es para compartir: **no pongas claves reales ahí**. Van en `.env` (permisos `600`, en `.gitignore`).
- `tests/fake_portal/` es un portal mínimo solo para los tests del agente; no es la web de la clínica. Los tests no leen tu `.env`.
