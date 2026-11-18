# Clips de Twitch

Aplicación local que vigila directos de Twitch, detecta los momentos en los que el chat se dispara y guarda automáticamente un clip de lo que pasó justo antes, ya editado en vertical (1080x1920) para YouTube Shorts. Los clips se revisan desde una interfaz web antes de subirlos.

> Usa la aplicación solo con directos propios o de streamers que te hayan dado permiso para clipear y subir su contenido.

## Requisitos

- Windows 10/11
- [Python 3.10 o superior](https://www.python.org/downloads/) (al instalarlo, marca "Add Python to PATH")
- Conexión a internet

No hace falta instalar ffmpeg ni tener cuenta o claves de Twitch: ffmpeg viene incluido en una dependencia y el chat se lee de forma anónima.

## Instalación (solo la primera vez)

Abre una terminal (PowerShell) en la carpeta del proyecto y ejecuta:

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Opcional: acelerar con una tarjeta gráfica NVIDIA

Si el ordenador tiene una tarjeta NVIDIA con los drivers al día, los clips se generan mucho más rápido (en las pruebas, un clip de 62 s pasó de unos 64 s de proceso a unos 30 s):

- **Codificación del vídeo**: funciona sin instalar nada más.
- **Transcripción de subtítulos**: necesita unas librerías de NVIDIA (unos 2 GB):

```bash
.venv\Scripts\python.exe -m pip install -r requirements-gpu.txt
```

Si la instalación falla con "No space left on device", el disco C: no tiene sitio para los archivos temporales. Usa otro disco para ellos:

```powershell
$env:TEMP = "E:\tmp-pip"; $env:TMP = $env:TEMP; mkdir E:\tmp-pip -Force; .venv\Scripts\python.exe -m pip install --no-cache-dir -r requirements-gpu.txt
```

En **Ajustes → Tarjeta gráfica** se ve si la aplicación la ha detectado y se puede desactivar. Si la gráfica falla en algún momento (por ejemplo, por falta de memoria porque hay un juego abierto), la aplicación sigue con el procesador automáticamente.

## Ejecutar

Doble clic en `iniciar.bat`, o desde la terminal:

```bash
.venv\Scripts\python.exe app.py
```

Se abre la interfaz en el navegador en <http://127.0.0.1:8765>. Para cerrar la aplicación, cierra la ventana de la terminal o pulsa `Ctrl+C`.

Si el puerto 8765 está ocupado, puedes cambiarlo antes de arrancar:

```powershell
$env:PORT = "9000"; .venv\Scripts\python.exe app.py
```

## Cómo se usa

1. Escribe el nombre del canal (o pega el enlace de Twitch) y pulsa **Vigilar canal**. Puedes vigilar varios canales a la vez.
2. Si el canal no está en directo, la aplicación espera y empieza a grabar sola cuando arranque (comprueba cada minuto). Cuando el directo termina, vuelve a esperar al siguiente.
   - La aplicación recuerda los canales vigilados: si la cierras o reinicias el ordenador, al volver a abrirla sigue vigilándolos. Para que deje de vigilar uno, pulsa **Dejar de vigilar**.
   - Solo vigila mientras la aplicación está abierta y el ordenador no está suspendido. Para dejarla esperando muchas horas, desactiva la suspensión en la configuración de energía de Windows.
3. Mientras graba, verás el ritmo del chat en una gráfica. La línea roja discontinua es el umbral a partir del cual se crea un clip.
4. Cuando el chat supera el umbral, se guarda un clip. También puedes pulsar **✂ Clip ahora** para crear uno a mano.
5. Los clips aparecen en **Por revisar**. Desde ahí puedes:
   - **Aprobar**: lo pasa a la pestaña *Aprobados* y mueve su carpeta a `aprobados`.
   - **Descartar**: borra la carpeta del clip del disco (pide confirmación).
   - **Ver original horizontal**: abre la versión sin editar.

## Dónde se guardan los clips

```
clips/
  <streamer>/
    <fecha> <título del directo>/
      por revisar/
        clip_001/
          clip_001.mp4            ← vertical 1080x1920, listo para Shorts
          clip_001_original.mp4   ← original horizontal, sin recodificar
          clip_001.ass            ← texto y tiempos de los subtítulos
          clip_001.json           ← hora, motivo del clip y estado
        clip_002/
          ...
      aprobados/
        clip_003/
          ...
```

Cada clip tiene su propia carpeta con todos sus archivos. Los clips nuevos se guardan en `por revisar`; al pulsar **Aprobar** en la interfaz, la carpeta del clip se mueve a `aprobados` dentro del mismo directo, y con **Volver a revisar** vuelve a `por revisar`. **Descartar** borra la carpeta entera del clip.

Mueve los clips siempre desde la interfaz, no a mano desde el Explorador, para que el estado y la carpeta coincidan. Si tienes clips de una versión anterior con los archivos sueltos, la aplicación los reorganiza sola al arrancar.

## Ajustes

Se cambian en la interfaz, en **Ajustes de detección y clip**, y se guardan en `config.json`. Se aplican al momento, también a los canales que ya se están vigilando.

| Ajuste | Por defecto | Qué hace |
|---|---|---|
| Segundos antes del pico | 45 | Cuánto vídeo anterior al pico entra en el clip |
| Segundos después de calmarse el chat | 15 | Cuánto vídeo se guarda desde que el chat vuelve a la normalidad |
| Duración máxima del clip (s) | 120 | Tope de duración de un clip automático, aunque el chat siga disparado |
| Personas distintas mínimas en el pico | 3 | Evita que una o dos personas haciendo spam disparen un clip |
| Sensibilidad | 3 | Cuántas veces por encima de lo normal debe ir el chat. Más bajo = más clips |
| Mensajes mínimos en el pico | 10 | Evita disparos en chats casi vacíos. Bájalo para canales pequeños |
| Ventana del pico (s) | 10 | Periodo en el que se mide el ritmo actual del chat |
| Espera entre clips (s) | 60 | Tiempo mínimo desde que acaba un clip automático hasta el siguiente |
| Palabras/emotes que cuentan doble | kekw, lul, pog, clip, jaja… | Mensajes que pesan el doble al medir el pico |
| Diseño del clip vertical | Fondo difuminado | Fondo difuminado o cámara arriba + juego abajo (ver más abajo) |
| Tarjeta gráfica | Usar si está disponible | Acelera la transcripción y la codificación con una NVIDIA |
| Subtítulos automáticos | Activados | Incrusta en el clip vertical lo que se dice en el directo |
| Idioma del directo | Español | Idioma en el que se transcribe; "Detectar automáticamente" si varía |
| Calidad de la transcripción | Normal | Más alta = menos errores pero tarda más en generar el clip |

## Diseño del clip: cámara arriba + juego abajo

En **Ajustes → Diseño del clip vertical** puedes elegir entre dos diseños:

- **Vídeo centrado con fondo difuminado**: el directo completo en el centro.
- **Cámara arriba + juego abajo**: la cámara del streamer ocupa la parte superior y el centro de la imagen del directo ocupa la inferior. Los subtítulos salen en la unión entre ambas.

Para el segundo diseño hay que decirle a la aplicación dónde está la cámara de cada streamer (solo una vez por canal):

1. Pulsa **📷 Marcar cámara** en la tarjeta del canal o en cualquiera de sus clips.
2. Arrastra con el ratón sobre la imagen para dibujar un recuadro alrededor de la cámara.
3. Pulsa **Guardar**.

La imagen sale del directo si se está grabando o, si no, del último clip guardado de ese canal. Si un canal no tiene la cámara marcada, sus clips salen con el fondo difuminado. Si el streamer cambia la cámara de sitio, hay que volver a marcarla.

En la parte de abajo se usa el centro de la imagen, así que los bordes izquierdo y derecho del directo quedan fuera.

## Editar un clip: título, texto, recorte y subtítulos

El botón **✏ Editar** de cada clip abre un editor con el vídeo original:

- **Título**: nombre del clip, visible en su tarjeta. Se usará al subirlo.
- **Texto encima del vídeo** (opcional): una frase fija que se ve durante todo el clip, en blanco con contorno, como en muchos Shorts. Con el fondo difuminado va en la franja de arriba, justo encima del vídeo; con el diseño dividido va arriba del todo, sobre la cámara. Si es largo se parte solo en varias líneas, y con Intro puedes forzar un salto de línea. **Usar el título como texto** copia el título. Déjalo vacío para quitarlo. Los emojis salen en blanco y negro, no en color.
- **Recorte**: reproduce el vídeo hasta donde quieras que empiece y pulsa **Inicio = posición actual**; haz lo mismo con el fin. También puedes escribir los segundos a mano. **▶ Ver recorte** reproduce solo el tramo elegido.
- **Subtítulos**: cada línea se puede corregir. Pulsa el tiempo de una línea para saltar a ese momento del vídeo; deja la línea vacía para quitarla. Las líneas atenuadas quedan fuera del recorte.

Al pulsar **Guardar y rehacer clip** se vuelve a generar el vídeo vertical. El original nunca se modifica, así que puedes volver a editar y cambiar el recorte las veces que quieras.

## Rehacer la edición de un clip

El botón **↻ Rehacer edición** vuelve a generar el vídeo vertical a partir del original con los ajustes actuales, conservando el título, el recorte y los subtítulos del clip. Sirve para cambiar un clip de diseño o aplicarle una cámara recién marcada.

Si borras el archivo `clip_001.ass` y rehaces la edición, el clip se vuelve a transcribir desde cero.

## Subtítulos

El clip vertical lleva subtítulos incrustados, generados transcribiendo el audio con Whisper en tu propio ordenador (no se envía nada a internet). El original horizontal se guarda sin subtítulos.

- La primera vez que se genera un clip con subtítulos se descarga el modelo de transcripción (unos 500 MB con la calidad "Normal"), así que ese primer clip tarda más.
- Junto al clip se guarda `clip_001.ass` con el texto y los tiempos de los subtítulos. Se corrigen desde el editor del clip.
- Si en el clip no se detecta voz, sale sin subtítulos.
- La transcripción es automática y puede fallar con nombres propios, jerga o cuando hay música o varias personas hablando a la vez.

"Lo normal" es la media de mensajes por segundo de los 2 minutos anteriores. Por eso la detección automática no empieza hasta que la aplicación lleva unos 30 segundos leyendo el chat.

### Cómo se decide cada clip

- Cuando el chat supera el umbral se abre un **momento** (la tarjeta del canal muestra "🔥 momento en curso"). El momento sigue abierto mientras el chat siga caliente, así que un momento largo o varios picos seguidos dan **un solo clip** en vez de varios.
- El clip empieza los "segundos antes del pico" y termina los "segundos después de calmarse el chat", sin pasar nunca de la duración máxima.
- Cada clip automático guarda una **puntuación** (🔥 ×3.4 = el chat fue 3,4 veces por encima de lo normal). En la lista de clips puedes ordenar por "Mejor puntuación primero" para revisar antes los más prometedores.

**Consejo para ajustar:** si salen demasiados clips, sube la sensibilidad o los mensajes mínimos; si no sale ninguno en un canal pequeño, baja los mensajes mínimos a 4–5.

## Cómo funciona por dentro

1. **Chat** (`clipper/chat.py`): se conecta de forma anónima al IRC de Twitch y recibe cada mensaje.
2. **Detector** (`clipper/detector.py`): compara los mensajes por segundo de la última ventana con la media reciente del canal, y sigue el momento mientras el chat siga caliente.
3. **Grabación** (`clipper/recorder.py`): streamlink descarga el directo y ffmpeg lo trocea en archivos de 2 segundos dentro de `buffer/`. Solo se conservan los últimos minutos.
4. **Clip** (`clipper/session.py`): al saltar el detector, espera los segundos posteriores, une los trozos correspondientes y genera la versión vertical con el vídeo centrado sobre un fondo difuminado.
5. **Interfaz** (`app.py` + `static/index.html`): servidor Flask local, solo accesible desde tu propio ordenador.

## Estructura del proyecto

```
app.py              Servidor e interfaz
iniciar.bat         Arranque con doble clic
requirements.txt    Dependencias
requirements-gpu.txt  Dependencias opcionales para tarjeta gráfica NVIDIA
config.json         Ajustes guardados (se crea al guardar ajustes)
clipper/            Lógica: chat, detector, grabación, sesiones
static/index.html   Interfaz web
clips/              Clips generados
buffer/             Búfer temporal de grabación (se borra solo)
```

## Problemas habituales

- **"python no se reconoce como comando"**: reinstala Python marcando "Add Python to PATH", o usa `py` en lugar de `python`.
- **El canal se queda en "esperando"**: no está en directo, o el nombre está mal escrito.
- **No se crea ningún clip automático**: el chat no llega al mínimo de mensajes; baja "Mensajes mínimos en el pico" o la sensibilidad.
- **Sale un anuncio en el clip**: Twitch puede mostrar un anuncio al entrar al directo y afecta a los clips de los primeros segundos.
- **El clip tarda en aparecer**: tras el pico se esperan los segundos posteriores, se transcribe el audio y se codifica la versión vertical (con tarjeta gráfica NVIDIA, más o menos la mitad de lo que dura el clip; sin ella, algo más de lo que dura el clip). Si quieres que vaya más rápido sin gráfica, baja la calidad de la transcripción o desactiva los subtítulos.
- **Ocupa mucho disco**: cada clip guarda también el original horizontal (unos 15–20 MB por minuto en 1080p). Descarta los que no quieras desde la interfaz.

## Limitaciones actuales

- La subida a YouTube no está implementada: los clips aprobados hay que subirlos a mano.
- La precisión del corte es de unos 2 segundos.
- La detección automática está probada con datos simulados; conviene ajustar la sensibilidad con directos reales.
