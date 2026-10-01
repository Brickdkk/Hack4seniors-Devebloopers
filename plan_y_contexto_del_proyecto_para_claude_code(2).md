# ESPECIFICACIÓN TÉCNICA: NÚCLEO DE VOZ, AGENTE Y DESPACHO AL PANEL

## 1. Flujo Exacto del Sistema

1. **El adulto mayor presiona un botón y habla.**
2. **La IA responde por voz inmediatamente** con una frase breve, empática y clara.
3. **La IA ejecuta el trabajo** en segundo plano (agente de navegación o acción solicitada).
4. **Al terminar, despacha el resultado en JSON al Panel del Cuidador** mediante una petición HTTP POST.

---

## 2. División del Equipo (Tu Alcance)

* **Benjamín:** Hardware físico (ESP32-S3, micrófono, parlante, botón arcade).
* **Compañero 1:** Página web de la clínica donde el agente hará el trámite.
* **Compañero 2:** Dashboard / Panel del cuidador (recibe los JSONs por HTTP).
* **Eliecer (TÚ / CLAUDE CODE):** 
  * Backend que recibe el audio y devuelve el audio de respuesta.
  * Transcripción con Groq Whisper.
  * Razonamiento y llamadas a herramientas con Claude.
  * Síntesis de voz con `edge-tts`.
  * Agente ejecutor (Playwright para navegar y hacer el trámite).
  * Despacho HTTP POST al panel del cuidador.
  * Script para probar todo en tu laptop con micrófono y barra espaciadora.

---

## 3. Requerimientos para Claude Code

Claude Code diseñará la estructura del código, los esquemas de herramientas y los endpoints con total autonomía:

### A. Pipeline de Audio y Voz (`FastAPI`)
* **Endpoint de entrada:** `POST /api/process-audio` recibe un archivo de audio WAV.
* **STT (Speech-to-Text):** Transcripción ultra rápida con Groq Whisper (`whisper-large-v3`).
* **LLM (Orquestador):** Claude 3.5 Sonnet con *Tool Calling*.
  * *System Prompt*: tono gerontológico, respetuoso, respuestas de máximo 2 oraciones, sin tecnicismos.
* **TTS (Text-to-Speech):** `edge-tts` genera la voz de respuesta (español de Chile o neutro) y la retorna en la respuesta HTTP para sonar en el parlante o audífonos.

### B. Agente Ejecutor (`Playwright`)
* Si la intención requiere realizar un trámite o reserva, dispara el agente web en segundo plano.
* Utiliza la URL configurada en la variable `CLINIC_PORTAL_URL`.
* Ejecuta en modo visible (`headless=False`) para ver la navegación en pantalla durante las pruebas.

### C. Despacho al Panel del Cuidador
* Cuando el agente termina la tarea (o cuando se registra un evento relevante), envía una petición `HTTP POST` con el payload JSON a la URL del panel: `CAREGIVER_PANEL_URL`.
* El payload debe incluir datos claros: tipo de evento, detalles de la acción, estado (éxito/error) y timestamp.

### D. Probador Local con Micrófono de Laptop (`mic_client.py`)
* Script para probar todo antes de tener la ESP32:
  * **Mantener presionada la BARRA ESPACIADORA:** Graba desde el micrófono de tu computador.
  * **Soltar la barra:** Envía el audio al backend.
  * **Respuesta:** Reproduce el audio devuelto por los parlantes de tu PC y muestra en consola la transcripción y el estado de la tarea.

---

## 4. Variables de Entorno (`.env`)

```env
GROQ_API_KEY=gsk_...
ANTHROPIC_API_KEY=sk-ant-...

# Integraciones de tus compañeros:
CLINIC_PORTAL_URL=http://localhost:8080
CAREGIVER_PANEL_URL=http://localhost:3000/api/events
```

---

## 5. Instrucción para Claude Code CLI

Copia y pega este prompt en tu terminal con `claude`:

```
Lee el archivo PROJECT_CONTEXT_CLAUDE_CODE.md.

Construye el backend en FastAPI con el pipeline completo de voz (Groq Whisper + Claude Tool Calling + edge-tts), el agente de ejecución con Playwright, el despacho de eventos vía HTTP POST al panel del cuidador (CAREGIVER_PANEL_URL), y el script interactivo mic_client.py para probar con el micrófono de la laptop y la barra espaciadora como Push-to-Talk.

No agregues bots de mensajería (ni Telegram ni WhatsApp). No construyas el dashboard ni la web de la clínica, solo deja parametrizadas sus URLs en las variables de entorno.

Diseña la arquitectura, esquemas y código con total libertad. Comienza instalando dependencias y estructurando el proyecto.
```