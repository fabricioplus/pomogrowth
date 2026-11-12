# Changelog — PomoGrowth

Historial de cambios por versión. El estado actual del proyecto (arquitectura,
módulos, esquema de datos) se describe en [DOCUMENTACION.md](DOCUMENTACION.md).

---

## v1.0 — 2026-11-04 · Primera versión pública

La 9.4 pasa a ser la **1.0**, la primera que se publica. Las versiones 9.0 a
9.4 (más abajo) fueron internas, de uso diario, y la numeración empieza de
nuevo con el lanzamiento. La 1.0 reúne todo lo construido en ellas:
temporizador con anillo y ventana compacta, tareas propias y de Asana con cola
sin conexión, Plan de hoy y vista «Hoy», recompensas con tienda, temas y
mascotas con IA, modo enfoque con comodín, música, estadísticas, instalador y
la despedida con redes. La versión Comunidad (local) es gratis y lo será
siempre.

Estaba lista el 2026-10-29; antes de publicarla entraron dos lotes más. El
primero (2026-11-02): el modo enfoque funciona con el hosts de solo lectura y
puede reiniciarse como administrador, el modo Libre (sin límite de tiempo),
crear la tarea también en Asana o ClickUp, el saludo con tu nombre y la racha
de días de uso, la valoración de esfuerzo al completar una tarea, un búfer
para las radios en directo que se cortan y la firma «Fabricioplus». El
segundo (2026-11-04): sonidos propios del temporizador, sonidos de ambiente
con grabaciones reales para mezclar con la música y una presentación animada
en el primer arranque.

### Añadido
- **Licencia GPL-3.0 o posterior** (`LICENSE`, el texto oficial de gnu.org): el
  proyecto pasa a ser software libre. El instalador y la versión portable la
  llevan como `LICENSE.txt` junto al exe, y el README explica qué permite y que
  el nombre, la mascota y el logotipo de PomoGrowth siguen siendo de GrowthLab
  (GPL-3.0, sección 7). Antes, «Todos los derechos reservados».
- **Ko-fi en la despedida**, en una fila propia bajo la del repositorio:
  «Apóyanos en Ko-fi» (con el logo de Ko-fi) y la cápsula «☕ Donar», los dos a
  `Config.KOFI_URL` (ko-fi.com/L3Q421O7ZW) y con el mismo estilo que «Dar
  estrella». Junto a «Dar estrella» solo cabía con 9 px de margen: con una
  fuente algo más ancha (Windows 10 sin Segoe UI Variable) las cápsulas se
  habrían comprimido. Logo de Simple Icons v16.30.0 (CC0) en
  `assets/icons/brand-kofi.svg`. La despedida mide ahora 440 × 344 px.
- **Modo Libre**: selector «Pomodoro | Libre» encima del anillo (se recuerda
  en `settings['timer_kind']` y se bloquea con una sesión a medias). En Libre
  el reloj cuenta hacia arriba sin tope (h:mm:ss pasada la hora; el anillo,
  la barra y la mini ventana dan una vuelta por hora), con el rótulo «Libre»
  y el icono infinity, sin descansos ni puntos de ciclo, y el botón de
  reinicio pasa a «Terminar sesión». Desde 5 min
  (`RewardsSystem.FREE_SESSION_MIN_MINUTES`) da 1 XP por minuto y 1 moneda
  cada 5 min y suma al tiempo total y a la racha de Recompensas, pero no a
  los pomodoros; por debajo solo queda el tiempo. Sus tramos llevan
  `mode: 'free'`: cuentan como tiempo (totales, gráficos, heatmap, racha y
  tiempo por tarea) pero no como pomodoros en Estadísticas. Al terminar, en
  Asana el comentario «⏱️ Sesión libre: X min (terminada el dd/mm a las
  HH:MM).» por la misma cola que los pomodoros, el evento n8n
  `free_session_completed` y frases propias de la mascota (evento
  `free_session`, sin hablar de pomodoros ni de minutos). La sesión se parte a
  medianoche para que cada parte cuente en su día, y completar o borrar la
  tarea activa, o salir de la app, la terminan con sus recompensas. Los avisos
  de suspensión y de salida hablan de «la sesión libre».
- **Crear la tarea también en Asana o ClickUp**: en el panel de nueva tarea,
  tras «Etiquetas», dos botones de 28 × 28 px solo con el logo, que se
  encienden y apagan (apagados en gris; encendidos, con el color de la marca)
  y se quedan como los dejes (`task_add_asana`, `asana_create_target`,
  `task_add_clickup`). Sin configurar se ven atenuados («Configura Asana en
  Ajustes») y abren Ajustes en su pestaña. El de Asana abre un menú con «Mis
  tareas (sin proyecto)» y los proyectos marcados en Ajustes; con «Repetir»
  elegido queda deshabilitado (la API de Asana no admite repetición). El alta
  en Asana es la nueva op `'create'` de la cola persistente, con gid
  provisional `local-<id>` y guardada en la misma escritura que la tarea:
  sobrevive a un reinicio, sale enseguida con el sync activo (si no, «Se
  creará en Asana cuando conectes») y, antes de reintentar, busca si ya llegó
  (mismo nombre, proyecto y fecha) para no duplicarla; el sync tampoco la
  importa por segunda vez ni enlaza un gid que ya es de otra tarea. Completar
  o renombrar antes del envío viaja con el alta, borrarla la retira (también
  «Borrar todo» e importar), y los comentarios de sesión esperan detrás de
  ella y salen con el gid real. Mientras espera, la tarea no ofrece repetirse;
  si Asana la rechaza, avisa «No se pudo crear en Asana: <nombre>». ClickUp
  es crear y olvidar en segundo plano, sin cola: avisa del éxito o del error
  y guarda en la tarea su id y su URL. La lógica vive en
  `ui/controllers/task_destinations.py`.
- **«Incluir mis tareas sin proyecto»** en Ajustes → Asana → Sincronización
  (`asana_include_no_project`, apagado por defecto): con proyectos marcados,
  trae también tus tareas sueltas, sin proyecto (no sus subtareas); sin
  proyectos marcados ya llegan todas.
- **Saludo con tu nombre**: al primer arranque, antes del plan o de la
  Bienvenida, «¿Cómo te llamas?» (un campo, «Omitir» y «Guardar»; se pregunta
  una sola vez), y la sección **«Perfil»** al principio de Ajustes → Timer y
  ventana («Tu nombre», hasta 30 caracteres). Con nombre: «Buenos días,
  <nombre>» en la Bienvenida y en «Planifica tu día», «Así va tu progreso,
  <nombre>» en Estadísticas y «Hasta pronto, <nombre>» (solo el primer
  nombre) en la despedida. El nombre va escapado, en negrita y con el acento
  del tema; sin nombre, los textos de siempre. Helpers en `ui/greeting.py`.
- **Racha de días de uso**: `data['activity_days']` apunta cada día en que se
  abre la app (al arrancar y al cambiar de día; se siembra una vez con los
  tramos y el historial, y guarda los últimos 400 días). Una llama con «12
  días seguidos» y la mejor racha en el tooltip (`ui/widgets/streak_badge.py`)
  en la cabecera de la Bienvenida y de «Planifica tu día». Es aparte de la
  racha de Recompensas y de la de Estadísticas, cuyo rótulo dice ahora «Días
  seguidos con tiempo enfocado».
- **Valoración de esfuerzo** al completar una tarea: el popup «¿Cuánto
  esfuerzo te costó?» con 5 estrellas (de «Muy fácil» a «Muy duro», vista
  previa al pasar el ratón) sustituye al toast «Tarea completada». Un clic o
  las teclas 1-5 guardan; «Omitir», Esc o un clic fuera cierran sin guardar.
  No es modal (`BrandedDialog.popup()`; el velo se retira en `done()`) y en
  modo compacto sale junto a la mini ventana. Se guarda en `effort_ratings`
  con el tiempo de foco del ciclo (las fijas se valoran en cada ciclo), sin
  XP y sin enviarse a Asana; la tarjeta muestra «★ n» y reabrirla lo quita.
  Toggle «Preguntar el esfuerzo al completar una tarea» en la sección
  «Tareas» de Timer y ventana (encendido por defecto). En Estadísticas, la
  sección «Esfuerzo» del periodo (media, valoraciones, minutos de foco por
  punto y reparto 1-5) y «Esfuerzo por proyecto» (`core/effort_stats.py`); al
  exportar, la hoja «Esfuerzo» en el XLSX y `<nombre>_esfuerzo.csv` aparte.
- **Búfer para directos que se cortan** (`services/stream_buffer.py`): tras
  un corte, un directo confirmado (cabeceras icy-* o marcado como directo al
  resolverlo; HLS no) reconecta por un proxy local en 127.0.0.1 con token por
  sesión, que lee el origen, guarda unos segundos por delante y empalma las
  reconexiones sin que el reproductor se entere (MP3 y AAC; Ogg, Opus, FLAC
  y WebM no se empalman). Cada corte de un directo que ya sonaba sube un
  escalón, 5 → 10 → 15 → 20 s (`MusicService.BUFFER_STEPS_S`); la primera
  conexión va siempre directa, y si el origen no da audio el proxy redirige a
  la URL original. «Búfer N s» junto a «En directo» y «Almacenando en búfer…
  (N s)» en Música, y el búfer en el tooltip del mini reproductor. Un
  directo atascado 5 s también cuenta como corte.
- **«Reiniciar como administrador»** en el diálogo del modo enfoque, junto al
  aviso de permisos (también con un bloqueo heredado que sin admin no se
  puede quitar): relanza PomoGrowth elevado (UAC) con `--elevated-restart`
  y, si se acepta, la instancia actual sale sin despedida tras la limpieza
  normal. Con un pomodoro, un descanso o una sesión libre en marcha pregunta
  antes. No suelta el puerto ni el mutex antes de la UAC: la instancia
  elevada espera hasta 10 s a que la anterior cierre
  (`singleton.acquire_waiting`). Si se cancela la UAC, lo dice.
- **Sonidos propios del temporizador** (`services/sound_service.py`, con
  QSoundEffect): tres estilos, «Campana» (cuenco con parciales inarmónicos y
  caída larga), «Marimba» (barra de madera) y «Suave» (tonos de cristal), más
  «Sistema de Windows», los de antes. Cada estilo tiene cuatro avisos: al
  iniciar o reanudar y al pausar, dos notas breves (0,32 s; ascendentes y
  descendentes); al acabar un pomodoro, y una sesión Libre de 5 min o más,
  un arpegio mayor de tres notas (2 s); al acabar un descanso, una quinta
  ascendente de dos notas (1,6 s). Como antes, no suenan al reiniciar, al
  saltar el descanso, en una sesión Libre corta ni al salir. Son 12 WAV (PCM
  de 16 bits, mono, 44,1 kHz; 1,1 MB en total) en
  `assets/sounds/timer/<estilo>/<evento>.wav`, sintetizados por
  `scripts/build_sounds.py` (numpy y semilla fija: salen iguales byte a
  byte), sin clics al empezar ni al acabar y con la sonoridad igualada entre
  estilos; el spec los empaqueta. Los cuatro de un estilo se cargan la
  primera vez que suena ese estilo y quedan en caché; lo pedido mientras
  cargan suena al quedar listo, una sola vez. Sin el WAV, con QSoundEffect
  en error o fuera del hilo de la interfaz, vuelve a winsound. Suenan por la
  salida de audio predeterminada del momento (también si cambia, p. ej. al
  conectar unos auriculares). En Ajustes → Timer y ventana, la sección
  **«Sonidos»** (entre «Ventana» y «Tareas»): interruptor, estilo, volumen (solo el de los
  avisos, con curva cuadrática: el 70 % por defecto queda a −6 dB; «Sistema
  de Windows» no lo aplica) y «Probar», que toca el fin de pomodoro con lo
  elegido aunque no se haya guardado; con el interruptor apagado, lo demás
  queda deshabilitado. Claves `timer_sounds_enabled` (True),
  `timer_sound_volume` (70) y `timer_sound_style` (`'campana'`, `'marimba'`,
  `'suave'` o `'windows'`); el controlador las aplica al arrancar y al
  guardar Ajustes.
- **Sonidos de ambiente** para mezclar con la música: nueve, en dos grupos,
  Naturaleza (Lluvia, Tormenta, Océano, Río, Bosque y Chimenea: grabaciones
  reales) y Ruidos (blanco, rosa y marrón, sintetizados). Están en la entrada
  fija «Sonidos de ambiente», arriba del todo en la columna de listas del
  diálogo de Música: al elegirla, el hueco del panel de la lista lo ocupa el
  mezclador (`ui/dialogs/ambient_mixer.py`), con una tarjeta por sonido en
  tres columnas. Un clic (o Espacio/Intro) enciende o apaga cada uno;
  encendida, la tarjeta lleva el borde de acento y su deslizador de volumen
  (y, mientras suena, unas ondas animadas), y sin su archivo sale
  desactivada con el motivo en el tooltip. Arriba, lo que suena y
  «Reanudar» (la última mezcla) o «Parar todo»; abajo, «Volumen general» y
  un «Solo durante el enfoque» propio, aparte del de la música: el ambiente suena con un pomodoro o una sesión
  Libre en marcha y, en los descansos o al pausar, baja, se pausa y luego
  sigue donde iba. «Ahora suena» sigue siendo la música y los dos suenan a
  la vez. En la cabecera, un punto de 9 px en el botón Música
  (`ui/widgets/title_bar.py`): relleno de acento con ambiente sonando, solo
  el contorno si espera al enfoque, y el tooltip del botón dice qué suena.
  Al abrir la app no suena nada solo. `services/ambient_service.py` hace
  sonar cada sonido con dos QMediaPlayer que se relevan antes del final del
  archivo con un fundido cruzado de 3 s de igual potencia (coseno el que
  sale, seno el que entra), en vez de que un solo reproductor vuelva al
  principio del archivo; entra con un fundido de 1 s y sale con uno de 0,8 s
  (al apagarlo, con «Parar todo», con «Solo durante el enfoque» y al salir
  de la app), siempre
  desde el volumen del momento. El volumen de cada sonido se multiplica por
  el general y se eleva al cuadrado. Un archivo que falla apaga ese sonido,
  lo saca de la última mezcla y lo deja desactivado hasta el siguiente
  arranque, con un aviso. La mezcla se guarda en `settings['ambient']`
  (`volumes` por sonido, `master`, `focus_only` y `last_mix`; los
  deslizadores, con 800 ms de retardo, como el volumen de la música), se
  sanea en `DataHandler._load` con `core/ambient_catalog.py` y se relee al
  importar o borrar los datos. Por defecto, general al 100 %, grabaciones al
  70 % (Chimenea al 80 %) y ruidos al 65 %.
- **Audios de ambiente** en `assets/sounds/ambient/` (OGG Vorbis estéreo a
  44,1 kHz, de 45 a 160 s; unos 10,9 MB en total), con `CREDITOS.txt`
  (autor, original, licencia, fuente y tramo de cada uno) y `LEEME.txt`; el
  spec empaqueta la carpeta entera. Las grabaciones son CC0 o de dominio
  público (no exigen atribución, pero se acreditan): Lluvia, «Light Rain
  Distant Thunder July 5th 2016» de kvgarlic (Freesound), CC0 1.0;
  Tormenta, «Thunderstorm after hot summer day, 17 minutes, 01 of 04» de
  stephan (pdsounds.org), dominio público; Océano, «Ocean Waves on a
  Tropical Beach», y Río, «Sanna river rapids», las dos de Jarrod Stanley
  (J. D. Savanyu), CC0 1.0; Bosque, «Forest ambience (20090610_0)» de nille
  (pdsounds.org), dominio público (las cinco, de Wikimedia Commons); y
  Chimenea, «Crackling fireplace» de nielstii, CC0 1.0, de Freesound. Los
  tres ruidos están sintetizados y van con la licencia de la app (GPL-3.0 o
  posterior). Los genera `scripts/build_ambient_sounds.py`, reproducible:
  descarga los originales a una caché, comprueba su SHA-1 y a todos les
  aplica el mismo proceso (tramo, remuestreo a 44,1 kHz, paso alto contra el
  retumbe, sonoridad igualada, limitador de picos, fundidos de 50 ms y
  Vorbis). Sus dependencias (numpy, scipy y soundfile; numpy también para
  `build_sounds.py`) son solo de los scripts: van comentadas en
  `requirements.txt` y el código de la app no las usa.
- **Presentación animada del primer arranque**
  (`ui/dialogs/onboarding_dialog.py`; solo en la app, el instalador no
  cambia): tras «¿Cómo te llamas?» y antes de «Planifica tu día» o de la
  Bienvenida, una tarjeta de 760 px con la mascota, «Hola, <primer nombre>»
  (o «Te damos la bienvenida») y una X, y tres diapositivas: «Una tarea, toda
  tu atención» (temporizador, descansos, aviso sonoro y modo Libre), «Tu
  espacio sin distracciones» (modo enfoque, música y ambiente, tareas de
  Asana y crear tareas en ClickUp) y «Mira cómo creces» (XP y niveles,
  monedas, racha, Estadísticas y la mascota). Cada una lleva «Paso n de 3»,
  título, texto (con los minutos del pomodoro de Ajustes), tres cápsulas y
  una ilustración animada pintada con QPainter, sin imágenes (solo los
  iconos y la mascota): la tarea que pasa a «en curso», el anillo con la
  cuenta atrás, el check, la campana y el descanso; el escudo que hace
  rebotar las webs, la lluvia junto a un ecualizador y una tarea de Asana
  que se sincroniza; la barra de XP hasta «¡Nivel 4!», las monedas, la
  racha, la barra de hoy de las estadísticas y la mascota que salta. Cada
  ilustración corre a unos 60 fps solo mientras su diapositiva está a la
  vista, y el cambio de diapositiva desliza y funde (260 ms). Se navega con
  los puntos, «Saltar», «Atrás» y «Siguiente» («¡Empezar!» con un cohete en
  la última), y con ←/→, Intro y Esc. Cerrarla de cualquier forma guarda
  `settings['onboarding_seen']` True. Solo sale en instalaciones nuevas: un
  archivo de datos con uso previo (tareas, tiempos, historial, días de uso o
  el nombre ya preguntado) cuenta como vista; tampoco sale con la ventana
  oculta o minimizada, en el reinicio como administrador ni con datos que no
  se pudieron leer (en esos casos, salvo con uso previo, queda para el
  siguiente arranque). Para verla de nuevo, «Ver la presentación» en la
  sección «Perfil» de Ajustes → Timer y ventana: se abre encima de Ajustes,
  que sigue abierto al cerrarla. La ilustración se pinta opaca (sin repintar
  en cada fotograma la sombra del diálogo), el tamaño se acota al área útil
  de la pantalla y salir de la app con la presentación abierta la cierra.
- **Iconos nuevos**: infinity y user-round de Lucide (lucide-static v0.460.0,
  ISC, como el resto: `assets/icons/LICENSE-lucide.txt`), `star-fill.svg`
  derivado de su star (relleno), y el logo de ClickUp
  (`brand-clickup.svg`) de Simple Icons v16.30.0 (CC0 1.0, anotado en
  `assets/icons/LICENSE-simple-icons.txt`). Para el ambiente, los sonidos y
  la presentación, otros 13 de Lucide 0.460.0 con el mismo trazo de 1,75:
  cloud-rain, cloud-lightning, waves, droplets, trees, wind, audio-waveform,
  bell, bell-ring, presentation, trending-up, sprout y rocket (wind y sprout
  aún no se usan).

### Cambiado
- **Las capturas de los scripts de humo (`shots/`) ya no se versionan**: algunas
  mostraban datos reales de Asana. Solo quedan los sprites de las mascotas; el
  repositorio público empezó de cero sin ellas ni su historial.
- **Versión 1.0** (`Config.APP_VERSION`; el exe, 1.0.0.0).
- **El exe se llama `PomoGrowth.exe`** (antes `PomoGrowth9.exe`), y con él la
  carpeta del build (`build/release/PomoGrowth/`), el spec (`PomoGrowth.spec`),
  el instalador (`installer/PomoGrowth.iss` → `PomoGrowth_v1.0_instalador.exe`)
  y el zip portable (`PomoGrowth_v1.0_portable.zip`). Al actualizar desde la
  9.4, el instalador borra el `PomoGrowth9.exe` viejo (y avisa si no pudo,
  porque seguía abierto) y rehace el acceso del menú Inicio y, si sigue
  marcado, el del escritorio. Lo que Windows guarda por la ruta del exe no se
  hereda: uno anclado en la barra de tareas hay que desanclarlo y volver a
  anclarlo, y el firewall vuelve a pedir permiso. `Config.APP_USER_MODEL_ID`
  sigue siendo `PomoGrowth.App.v9`: con él Windows agrupa la ventana con sus
  accesos.
- El aviso de segunda instancia dice «PomoGrowth ya está abierto» (sin «v9»),
  que quizá esté en la bandeja y cómo cerrarlo y abrirlo como administrador
  para el modo enfoque; cada registro de sesión empieza con la versión.
- **Firma «Fabricioplus»** en el pie de Configuración (antes «by GrowthLab»;
  el mismo enlace al repositorio), y en la despedida, bajo «¿Disfrutas de
  PomoGrowth?», «Puedes darnos tu apoyo.» (antes «Ayúdanos siguiéndonos en
  nuestras redes sociales.»).
- **Modo enfoque**: los fallos al escribir el hosts dan un mensaje según su
  causa (en uso por otro programa, sin administrador, solo lectura que no se
  pudo quitar u otra denegación con el detalle) y dejan en el log el WinError
  y si había admin. Con el hosts en uso (WinError 32/33) reintenta tres
  veces (0,15/0,2/0,3 s) y luego escribe en el sitio. Al activar, avisa si
  no bloquea ningún sitio (lista vacía o todos con comodín) y recuerda que
  las pestañas ya abiertas pueden tardar en bloquearse. Un bloqueo que quedó
  de la sesión anterior se ajusta a la lista al activar, y si no se pudo
  rehacer al arrancar el diálogo lo dice.
- **Sync de Asana por workspace**: un solo listado por workspace (antes uno
  por proyecto), filtrado en el equipo: los proyectos marcados, las sueltas
  si se incluyen y siempre las ya enlazadas (así siguen al día las creadas en
  «Mis tareas»).
- Completar la tarea activa con una sesión en marcha cierra la sesión antes
  de marcarla completada: el último tramo cuenta en el esfuerzo de ese ciclo
  y no también en el siguiente de una tarea fija.
- **Desinstalar con la app abierta** ya no deja su exe y sus DLL (ni, si se
  borraban los datos, la app volviéndolos a escribir): el desinstalador de Inno
  no cierra programas como Setup, así que la app crea un mutex con nombre
  mientras está abierta (`Config.APP_MUTEX`, `utils/win_native.hold_named_mutex`)
  y el desinstalador lo busca y pide cerrarla antes.
- **`scripts/make_portable.py`** hace el zip portable (antes, a mano) y se niega
  si junto al exe del build hay datos del usuario.
- **Los avisos del temporizador ya no dependen del esquema de sonidos de
  Windows**: eran aliases del sistema con winsound (SystemStart al iniciar,
  SystemHand al pausar, SystemAsterisk al acabar el pomodoro y
  SystemQuestion al acabar el descanso), y un evento sin sonido asignado no
  suena: en el esquema predeterminado de Windows 11, «Pregunta»
  (SystemQuestion) no tiene ninguno, así que el fin del descanso no se oía.
  Ahora suenan los WAV propios, también para quien actualiza (estilo
  «Campana» al 70 %); los de antes siguen en el estilo «Sistema de Windows».
- Tests: 631. Eran 324 el 2026-10-29 (despedida con Ko-fi y los SVG de sus
  iconos, nombres sin el «9», `[InstallDelete]` exacto, mutex del
  desinstalador y zip portable); el primer lote de antes de publicar suma
  181: hosts de solo lectura, reintentos, mensajes, «Reiniciar como
  administrador» y singleton (`tests/test_focus_hosts_admin.py`, 34), modo
  Libre (`test_free_mode.py`, 28), nombre y racha (`test_nombre_racha.py`,
  34), esfuerzo (`test_effort.py`, 28), altas en Asana y ClickUp y tareas
  sin proyecto (+37 en `test_asana_outbox.py`, +7 en `test_task_add.py`) y
  el búfer de directos (+12 en `test_music_service.py`, +1 en
  `test_music_ui.py`). El segundo suma 126: sonidos del temporizador
  (`tests/test_timer_sounds.py`, 48: los 12 WAV —formato, duración, pico,
  bordes en cero y que son los que genera el script—, el servicio con
  QSoundEffect y winsound simulados, la sección de Ajustes, cuándo suena
  cada aviso y ajustes rotos en el JSON), ambiente (`test_ambient.py`, 41:
  catálogo y spec, mezcla
  guardada, fundidos, relevo, «Solo durante el enfoque», fallos, «Reanudar»,
  el mezclador (también con teclado, rueda, pegar y soltar), el punto del
  botón Música, importar datos, la salida y el controlador; ningún test crea
  reproductores reales: `tests/ambient_fakes.py` y un fixture de
  `conftest.py`) y presentación (`test_onboarding.py`, 37: cuándo sale y el
  orden, cerrarla de cada forma, navegación, transición, animaciones solo a
  la vista, las escenas en varios temas, el botón de Ajustes y los arreglos
  de la revisión).
- README: versión 1.0, Ko-fi, la versión Comunidad gratis para siempre y la IA
  de la mascota con sus tres proveedores (no solo Claude); y lo de los dos
  lotes de antes de publicar: modo Libre, alta en Asana o ClickUp, nombre y
  racha, esfuerzo, búfer de las radios, la firma «Fabricioplus», los sonidos
  del temporizador y de ambiente (con la procedencia de las grabaciones y
  cómo rehacer los audios) y la presentación.

### Corregido
- **El modo enfoque no bloqueaba nada si otra herramienta había marcado el
  hosts como solo lectura**, y la app lo achacaba a la falta de permisos de
  administrador (todo `PermissionError` daba «Ejecuta PomoGrowth como
  administrador», también siéndolo). Ahora, como administrador, quita la
  marca solo mientras escribe y la repone siempre al terminar, también si la
  escritura falla: el hosts final la conserva. Toda escritura pasa por
  `FocusMode._write_hosts` (bloquear, desbloquear, comodín y sincronía).
- ClickUp: crear una tarea ya no fija `status: 'Open'` (daba 400 en las
  listas con otros estados; ahora se usa el estado inicial de la lista), y
  una fecha sin hora se manda como solo día.

---

> **Historial interno.** De aquí hacia abajo, las versiones 9.0 a 9.4: de uso
> diario y sin publicar, antes de la 1.0. «v8» es la versión anterior, en
> Tkinter.

## v9.4 — 2026-10-28

### Añadido
- **Firma «by GrowthLab» en Configuración**: en el pie, a la izquierda de
  «Cancelar» y «Guardar» y en su misma fila (antes era un hueco), un enlace
  discreto con el icono de GitHub que abre el repositorio del proyecto. Texto
  pequeño atenuado, acento al pasar el ratón y la URL en el tooltip; nunca es
  el botón por defecto (Enter no abre la web). `BrandedDialog.add_buttons`
  admite un widget a la izquierda (`leading`) sin cambiar los demás diálogos.
- **Redes sociales en la despedida**: bajo «Hasta pronto», «¿Disfrutas de
  PomoGrowth? Ayúdanos siguiéndonos en nuestras redes sociales.» y, encima
  de las cuentas, el **repositorio de GitHub** («fabricioplus/pomogrowth»
  con el logo y una cápsula «☆ Dar estrella», los dos abren el repositorio
  para calificarlo con una estrella); debajo, una fila por cuenta,
  **@xgrowthlab** y **@fabricioplus**, con Facebook, Instagram, YouTube,
  TikTok y LinkedIn. Iconos atenuados que al pasar el ratón toman el
  color de su marca (TikTok, el del texto; el acento si un color no se leyera
  sobre el fondo del tema) y tooltip «Facebook · @xgrowthlab». Para poder
  hacer clic, la despedida dura ahora ~8 s con una barra fina de cuenta atrás
  que se pausa con el ratón sobre la tarjeta (si aparece justo bajo el
  cursor, solo cuando se mueve), y se cierra antes con Esc, con «Cerrar» o
  poco después de abrir un enlace. Sigue centrada (440 px) con su fade.
- **Enlaces en un único sitio** (`core/config.py`): `GITHUB_URL`,
  `GROWTHLAB_URL` y `SOCIAL_LINKS` (cuentas, redes y orden). Se abren con
  `ui/widgets/link_button.py` (`open_url`, `LinkButton`).
- **Logotipos de marca** en `assets/icons/brand-*.svg` (GitHub, Facebook,
  Instagram, YouTube, TikTok y LinkedIn) de **Simple Icons**, licencia **CC0
  1.0**: LinkedIn de la v9.21.0 y el resto de la v16.30.0, tal cual (un solo
  trazado sin color: se tiñen como los de Lucide). Origen, versiones y
  licencia en `assets/icons/LICENSE-simple-icons.txt`; los logotipos son
  marcas de sus dueños.

### Cambiado
- **La X de la cabecera sale de PomoGrowth** (antes ocultaba la ventana en
  la bandeja), preguntando siempre antes: «¿Estás seguro de salir del
  programa?» con «Cancelar» y «Salir». Con un pomodoro o un descanso en
  marcha avisa de que el tiempo en curso se guarda. «Salir» guarda, limpia el
  bloqueo del enfoque y muestra la despedida. Tooltip: «Salir de
  PomoGrowth». Para dejarla en segundo plano, minimizar.
- **«Cerrar» del menú de la bandeja** hace la misma pregunta (antes salía
  directo). Si la ventana está minimizada, la restaura antes para que la
  pregunta salga sobre ella.
- **Alt+F4** (y «Cerrar ventana» de la barra de tareas) pasa por la misma
  pregunta en vez de ocultar la ventana sin más. Nunca hay dos preguntas a
  la vez ni se pregunta con la app ya cerrándose.
- `.gitignore` ignora `.venv/` (el README crea el entorno virtual dentro del
  repositorio).
- Instalador: la guarda de versión compara con la del exe en cuatro números
  también para versiones de dos (la 9.4 es 9.4.0.0; antes solo aceptaba
  x.y.z.0).

---

## v9.3.1 — 2026-10-28

### Añadido
- **Logos de marca** («Identidad Esmeralda», los SVG de `assets/`): la
  mascota sustituye al tomate como logo en la cabecera (26 px), en las fichas
  de la bienvenida y la despedida, y el splash muestra el logo horizontal
  (mascota + «PomoGrowth» con la letra de la marca). Dentro de la app la
  mascota toma el acento del tema (menta en el tema por defecto) y cambia con
  los temas de la tienda; cara blanca y ojos oscuros siempre. El tomate se
  queda donde significa «pomodoro» (modo Enfoque del timer, ventana compacta,
  recompensas).
- **Iconos del exe, del instalador, de la ventana/barra de tareas y de la
  bandeja** con la mascota en el esmeralda oficial `#0BBF85`:
  `assets/icon.ico` (16, 18, 20, 24, 26, 32, 35, 40, 48, 64, 96, 128 y
  256 px, cada tamaño dibujado aparte para que salga nítido) y
  `assets/icon.png` (256). Lleva los tamaños exactos de la bandeja y del
  icono grande que pide Windows a 100, 110, 125 y 150 % (16/32, 18/35, 20/40
  y 24/48) y los de la barra de tareas a 100 y 110 % (24 y ~26): a 110 %
  faltaban 18, 35 y 26, y Qt y Windows reescalaban el de al lado (los ojos
  volvían a ser manchas). Los genera
  `scripts/build_brand_assets.py` a partir de `assets/icono.svg`; de 16 a
  26 px la mascota va con menos margen, hasta 35 px con los ojos ajustados a
  píxeles enteros (a 32 px eran 1 px oscuro con un halo gris) y de 16 a
  20 px sin el detalle del temporizador, para que se lea la cara.
- **Instalador** (`installer/PomoGrowth9.iss`, Inno Setup 6): por usuario y
  sin administrador en `%LOCALAPPDATA%\Programs\PomoGrowth` (la app guarda
  los datos junto al exe), en español, con acceso en el menú Inicio y
  opcional en el escritorio (con el `AppUserModelID` de la app, para que la
  barra de tareas agrupe la ventana con el acceso anclado). Abre PomoGrowth
  al terminar y cierra la app abierta al actualizar o desinstalar. Nunca
  instala ni sobrescribe datos; al desinstalar pregunta si borrarlos (por
  defecto se conservan; la música lofi y lo exportado, nunca). Imágenes del
  asistente con la marca oficial en 7 escalas: la mascota sobre «PomoGrowth»
  («Pomo» blanco, «Growth» esmeralda) en `#0F1720`, y la mascota sola en la
  cabecera; las genera también `scripts/build_brand_assets.py`. Solo se
  compila con un build de su misma versión: si el exe de `build/release`
  lleva otra o ninguna, ISCC para con un error en vez de empaquetar un build
  viejo con la etiqueta nueva.
- **Versión en el exe** (`PomoGrowth9.spec`): recurso de versión con
  `Config.APP_VERSION` (Propiedades > Detalles; el Administrador de tareas
  lo muestra como «PomoGrowth»). Es lo que compara el instalador.

### Cambiado
- **Modo compacto, dentro de la cápsula** de los botones de ventana, entre
  minimizar y cerrar (antes era una acción más de la cabecera): mismo tamaño
  y hover que sus vecinos, con el icono a 14 px para que mida y pese lo que
  la «X». La cabecera recogida pide ahora ~424 px (antes ~446); el mínimo de
  la ventana sigue en 460.

### Corregido
- En el exe, el icono de la ventana se buscaba junto al exe, pero los
  recursos van en `_internal`: no se encontraba. La ventana y la bandeja (que
  lo sacaba de la ruta del propio módulo) lo buscan ahora con
  `utils/paths.app_icon_path()` (dentro de `resource_base()`). El spec
  empaqueta además los SVG de marca que usa la app (`icono.svg` y el logo
  horizontal monocromo).
- El comando de build documentado (`pyinstaller PomoGrowth9.spec
  --noconfirm`, salida en `dist/PomoGrowth9`) habría borrado la instalación
  diaria con sus datos: PyInstaller vacía la carpeta de salida entera y, con
  `--noconfirm`, sin preguntar. Ahora el spec se niega a compilar sobre una
  carpeta con los JSON del usuario, y el único comando documentado compila
  en `build/release`.
- Con la lista de tareas vacía (lo primero que se ve tras instalar), el
  campo «Añadir una tarea…» crecía hasta media columna: al ocultarse la
  lista, el alto sobrante se repartía entre el aviso y el campo. Ahora se lo
  queda el aviso y el campo conserva su línea.

---

## v9.3 — 2026-10-27

### Añadido
- **Música personalizada** (botón «Música», el primero de la cabecera): listas
  propias con **radios en directo** (Icecast/SHOUTcast y HLS), **enlaces
  directos** a archivos de audio y **archivos y carpetas del equipo**, con
  calidad de hasta **MP3 a 320 kbps** (y lo que entienda FFmpeg: AAC, OGG,
  Opus, FLAC, WAV…). Se añaden pegando un enlace (Enter o «Añadir»), con
  «Archivos…»/«Carpeta…» o soltándolos sobre el diálogo; un enlace a una lista
  `.m3u`, `.pls` o `.xspf` trae todas sus pistas. YouTube, Spotify,
  SoundCloud, Deezer, Apple Music y Tidal se rechazan con un aviso claro (no
  dejan sacar su audio), igual que una página web en vez de un stream. La
  carpeta lofi sigue ahí como lista de solo lectura («Abrir carpeta»,
  «Releer»).
- El diálogo muestra las listas a la izquierda y los elementos a la derecha
  (reordenar arrastrando, doble clic o Enter reproduce, Supr quita; al pasar
  el ratón, reproducir/renombrar/quitar) con aleatorio y repetir (lista, pista
  o sin repetir). Abajo, **«Ahora suena»**: título, canción que emite la
  radio (metadatos ICY), «Pista 3 de 12», insignias de calidad («MP3 · 320
  kbps») y «En directo», estado (cargando, reconectando, error), controles,
  progreso con salto (sin barra en directo) y volumen.
- **«Solo durante el enfoque»**: la música suena con un pomodoro en marcha y
  se pausa sola en los descansos, al pausar o parar el timer; pulsar
  reproducir con el timer parado la deja preparada («Esperando al enfoque»).
- **Mini reproductor** en la fila de estado del timer (icono, título con
  «En directo» o la calidad, reproducir/pausar y siguiente): solo aparece con
  algo cargado y un clic en el título abre Música. No ocupa alto propio, así
  que no mueve el anillo.
- Las radios se **reconectan solas** si se corta la señal (esperas de 2, 5,
  10, 20 y 30 s, sin saltar de emisora); una pista que falla se salta y, si
  fallan todas, se para con un aviso. Con el diálogo cerrado los errores
  llegan como un toast.
- Menú de la bandeja: **«Reproducir la música» / «Pausar la música»**.
- Volumen, aleatorio, repetir, «solo durante el enfoque» y lo último que sonó
  se guardan en `pomogrowth_data.json` (`music`); al arrancar no suena nada
  hasta pulsar reproducir. Importar un backup sin música conserva la actual y
  «Borrar todos los datos» también la conserva.
- **Comodín del modo enfoque** (bandera blanca junto a cada sitio): deja libre
  un sitio bloqueado (y sus subdominios) solo hasta cerrar PomoGrowth, para
  casos excepcionales como consultar el WhatsApp del trabajo. Pide
  confirmación, se aplica al momento con el enfoque activo y nunca se guarda:
  al volver a abrir la app, o al día siguiente, el sitio vuelve a estar
  bloqueado. Volver a pulsar la bandera lo bloquea de nuevo sin preguntar.

### Cambiado
- **El timer mide siempre lo mismo**: al ocultar las tareas la ventana encaja
  justo en él (460 px, el ancho mínimo) y al mostrarlas crece por su lado sin
  ensancharlo ni moverlo. Antes el divisor repartía 440:640 en proporción y
  el timer cambiaba de ancho en cada clic (plegado podía medir ~700 px). El
  ancho mínimo con las tareas pasa de 760 a **900 px** (timer + ~430 px de
  tareas).
- **Minimizar y cerrar** van a la misma altura que el resto de iconos de la
  cabecera (antes, pegados arriba al estilo de Windows), juntos en una cápsula
  con fondo y borde sutiles; cerrar sigue en rojo al pasar el ratón.
- Con poco ancho (el mínimo, con el panel de tareas a un lado) la fila «Ordenar»
  pasa a iconos con tooltip y solo el orden elegido conserva el texto; antes
  se recortaban («Manu», «Proyec»).
- **Recompensas** pierde la pestaña Música (quedan Tienda, Inventario y
  Progreso) y su botón pasa a llamarse solo «Recompensas».
- El ancho mínimo de la ventana con el panel de tareas oculto pasa de 420 a
  **460 px**: la cabecera tiene una acción más. Una posición guardada más
  estrecha se ensancha al mínimo en vez de descartarse.
- Regresión: `tests/test_music_library.py`, `tests/test_music_service.py`
  (servidor HTTP local en 127.0.0.1, audio en silencio) y
  `tests/test_music_ui.py`.

### Corregido (revisión de la música y del comodín)
- **Comodín del enfoque**: si la app moría con el enfoque activo y un sitio
  libre, al volver a abrirla ese sitio seguía libre aunque se mostraba
  bloqueado; ahora el bloque heredado se rehace con la lista entera. Quitar
  un dominio con comodín vuelve a bloquear al momento sus subdominios de la
  lista; si al cambiar de día el hosts no se deja reescribir, los sitios se
  siguen viendo libres (lo están), se avisa una vez y se reintenta cada
  minuto. El hosts se reescribe sin dejarlo nunca a medias y sin cambiar sus
  finales de línea. En el diálogo, un dominio largo ya no esconde la bandera
  ni la papelera, la insignia dice «Libre», el recuento «6 · 2 libres» y la
  bandera de un subdominio nombra el comodín real.
- **Red de la música**: resolver un enlace y leer el título de la radio
  tienen un plazo total (un servidor que gotea ya no los mantiene vivos), van
  en hilos propios (no ocupan los de Asana) y no retienen el cierre de la app
  ni el puerto de instancia única.
- **Seguridad**: una lista HLS (.m3u/.m3u8) de tu equipo que apunta a rutas
  de red (`\\servidor\...`), a otras unidades o fuera de su carpeta ya no se
  da a FFmpeg (podía filtrar el hash NTLM de Windows); también se revisa
  justo antes de sonar.
- Un MP3 servido sin `Content-Length` ya no se repite sin fin como si fuera
  una radio: al acabar pasa a la siguiente. Una pista por enlace en pausa (o
  en un descanso con «solo durante el enfoque») ya no se da por fallida al
  reanudar: se recarga y sigue donde iba. Con un solo elemento, «siguiente»
  y «anterior» ya no cortan la radio.
- Una playlist con URLs larguísimas ya no infla `pomogrowth_data.json`
  (tope de texto por lista y total) y cambiar de pista ya no reescribe el
  JSON entero cada vez (se guarda con retardo, como el volumen).
- Títulos ICY y nombres de radio en Windows-1252 se leen bien («Don’t Stop
  Me Now – Queen»); fuera los controles C1 y las marcas de dirección.
- Diálogo de Música: en la carpeta lofi también se puede pegar un enlace (y
  Ctrl+V con un enlace en el portapapeles lo añade); Enter dos veces no
  muestra «Eso ya estaba en la lista»; un enlace que tarda no saca de la
  lista abierta; «Añadidas N pistas» ya no se borra al cambiar de lista; el
  estado vacío no se aplasta en pantallas de 720 px; «Esperando al enfoque»
  ya no aparenta sonar (en el mini: reloj, «Al enfocar» y la pausa atenuada);
  el motivo de un error se lee entero y sigue al reabrir; en directo,
  «● En directo» en vez de un contador y una barra vacía; tooltip en nombres
  de lista recortados (elididos por el medio), ruta de la lofi elidida por el
  medio, orden de Tab visual y «Añadida 1 radio» al añadir una emisora.
- Regresión: `tests/test_focus_session_pass.py` y más casos en los tres
  archivos de música.

---

## v9.2.2 — 2026-10-27 (salió junto con la v9.3)

### Corregido
- **Suspender el equipo ya no infla el tiempo enfocado.** Con el timer en
  marcha y el PC suspendido, al despertar el reloj había saltado: el pomodoro
  se daba por completado y la suspensión se registraba como trabajo (en los
  datos reales, tramos de 96,9 y 73,8 min en pomodoros de 35: ~100 min
  fantasma). Ahora el bucle de la UI late cada 250 ms
  (`PomodoroModel.heartbeat`); si pasan más de 60 s sin latir, el timer se
  pausa en el último latido, solo cuenta el tiempo real (fechado en ese
  momento, no al despertar: una suspensión nocturna ya no pasa el trabajo al
  día siguiente) y avisa con una notificación de Windows, que queda en el
  centro de notificaciones, más un toast si la ventana está a la vista.
  Además, ningún tramo puede superar lo que quedaba de la sesión, y atrasar el
  reloj ya no borra el tiempo trabajado. Regresión:
  `tests/test_timer_suspension.py`.
- **Los cambios hacia Asana ya no se pierden ni se revierten.** Antes salían
  en un hilo suelto, sin reintento: sin red se perdían (comentarios de
  pomodoro), completar una tarea con el sync apagado se revertía en el
  siguiente sync, renombrarla también, y la marca `asana_push_pending` nunca
  se limpiaba tras un envío con éxito (4 tareas atascadas en los datos reales,
  que además ocultaban cambios hechos en Asana). Ahora completar/reabrir y
  renombrar una tarea de Asana entran **siempre** en una **cola persistente**
  (`asana_outbox` en el JSON) antes de enviarse, también con el sync apagado;
  el comentario de pomodoro, solo con el sync permitido (como antes), pero ya
  no se pierde sin red. La cola se envía al momento si el sync está
  permitido, y si no al encenderlo, al sincronizar (antes del fetch), al
  guardar la configuración o al arrancar, con reintentos automáticos
  (1 → 15 min) tras fallos de red, 401, 429, 451 o 5xx. Lo que Asana rechaza
  de forma definitiva (403/404…) se descarta y se avisa con un toast. El
  merge no pisa los campos con un cambio en cola. Un comentario cuyo envío
  quedó en duda (timeout, 5xx, cierre de la app a mitad) se busca en Asana
  antes de reintentarlo, para no publicarlo dos veces. Un solo trabajo de red
  a la vez: lo que se pide durante un sync o un envío espera su turno, y
  apagar el switch anula el sync que esperaba. Regresión:
  `tests/test_asana_outbox.py`.
- **Migración de la marca heredada** al arrancar: si coincide con el estado
  local (el cambio casi seguro llegó, como en las 4 tareas atascadas) no se
  reenvía nada y el primer sync deja mandar a Asana; si ese sync le cambia el
  estado (alguien la reabrió allí, o el envío de entonces falló), una
  notificación lo avisa para revisarla. Si no coincide (el último cambio se
  hizo con el sync apagado y nunca salió), se encola el estado local.
- **Los tags añadidos a mano a una tarea de Asana sobreviven al sync**
  (`local_tags`, sembrado también para los que ya existían, sin confundir un
  proyecto renombrado con un tag propio); antes cada sync los borraba.
- La casilla «Integración con Asana activada» desmarcada (`active_integrations`
  vacío) contaba como activada: ahora detiene de verdad el sync y los envíos.
- Importar un backup conserva la cola actual; de la del archivo solo añade
  los comentarios, que se buscan en Asana antes de publicarse (sin
  duplicados), y no reaplica estados ni nombres viejos. «Borrar todos los
  datos» tampoco descarta los cambios en cola.
- Editar una tarea de Asana sin cambiar su nombre ya no la renombra en Asana
  (espacios en los extremos, o un renombrado que llegó por sync con el
  diálogo abierto). Completar una tarea marcada «eliminada en Asana» no
  intenta enviarla.

### Cambiado
- El panel de tareas solo se acopla a la **izquierda o a la derecha** del
  timer (el botón alterna entre las dos); se quitaron las posiciones arriba y
  abajo, que no quedaban bien. Quien las tuviera guardadas pasa a izquierda
  (arriba) o derecha (abajo).
- El comentario de pomodoro en Asana lleva la fecha y hora de la sesión
  («🍅 Sesión Pomodoro completada el 23/10 a las 10:35. Tiempo enfocado: 25
  minutos.»): si sale con retraso no se confunde con el día del envío, y cada
  comentario es único (la comprobación anti-duplicados compara el texto
  exacto).
- El diálogo «Editar tarea» de una tarea de Asana avisa que el nombre también
  cambia en Asana y que los tags añadidos son solo de PomoGrowth.
- El estado de la línea de sync muestra el resultado de los envíos
  («✅ Asana actualizado», «⏳ N cambios en cola… se reintentará solo»,
  «⚠ Asana rechazó…»).
- `python -m pytest tests -q` ejecuta todos los tests de pytest
  (`tests/conftest.py` excluye el script de paridad, que se sigue lanzando
  con `python tests/test_core_parity.py`).

---

## v9.2.1 — 2026-07-21

### Añadido
- **Edición del ciclo de repetición**: el diálogo «Editar tarea» incluye el
  selector «Repetición» — convertir una tarea normal en fija, cambiarle el
  ciclo o quitárselo. Solo tareas locales (las de Asana las gobierna Asana).
- **Filtros avanzados colapsables**: la fila de orden (Manual/Proyecto/Fecha/
  Nombre/Creada) vive tras el botón «Filtros avanzados ▾» con colapso animado.
  Colapsado muestra el orden activo («Filtros avanzados · Fecha») y el estado
  se recuerda entre sesiones (`advanced_filters_open`).
- **Auto-equipado de la primera mascota**: comprarla la equipa y la muestra al
  instante (antes exigía el paso extra Inventario → Equipar y parecía que la
  compra "no hacía nada"). Una segunda compra no desplaza a la equipada.
- **Build .exe actualizado** (`dist/PomoGrowth9/`, ~209 MB): incluye el SDK
  `anthropic` (hiddenimport) para que la mascota IA funcione empaquetada.
  Convención de rebuild: respaldar datos junto al exe en
  `dist/datos_previos_<fecha>/` (el build borra la carpeta).

### Cambiado
- **Títulos de los gráficos de estadísticas en verde de acento** (antes negros:
  con `loc='left'` matplotlib usa `ax._left_title`, que el estilado no pintaba).
  Siguen el tema equipado.
- El sync manual respeta también la casilla «Integración con Asana activada»
  de ⚙ Configuración (antes solo la comprobaba el auto-sync del arranque).

---

## v9.2 — 2026-07-20

### Añadido
- **Ciclos de repetición en tareas fijas**: al añadir, combo «Repetición» con
  Diaria / Semanal / Mensual / Trimestral (antes solo diaria implícita). La
  reapertura respeta el ciclo: +1 día, +7 días, +1/+3 meses de **calendario**
  con clamp de fin de mes (31 ene → 28/29 feb). Badge de la tarjeta y toast de
  completado indican el ciclo. Compat: `recurring=True` heredado = diaria.

### Cambiado
- **Switch de sincronización reubicado**: del header del panel de tareas a la
  fila «● Asana conectado» encima del timer — icono de sync con color de
  estado + interruptor + línea de progreso auto-ocultable, todo junto. El
  header de TAREAS vuelve a título + contador + posición.

---

## v9.1 — 2026-07-19

Lote mayor de 11 mejoras + auditoría adversarial multi-agente (40 agentes)
con 27 correcciones verificadas.

### Añadido
- **Tareas fijas (repetitivas)** para actividades fuera de Asana/ClickUp:
  checkbox al crear, badge 🔁, filtro «Fijas», reapertura automática al día
  siguiente de completarlas (al arrancar y al cambiar de día con la app
  abierta). Base de los ciclos de v9.2.
- **Fecha de creación** en toda tarea (`created_at`; en Asana viene de la API,
  con fallback al id-timestamp): chip «＋ fecha» en la tarjeta con tooltip
  completo, y orden nuevo «🕒 Creada».
- **Ciclos completos como métrica**: el pomodoro que cierra el ciclo marca su
  chunk (`cycle_completed`), otorga bonus (+50 XP/+15 🪙, `total_cycles`),
  tarjeta CICLOS en Estadísticas y fila en Recompensas → Progreso.
- **Mascota pixel 8-bit** 🐾: gato, perro y rata dibujados por código (sin
  assets), animados, comprables en la tienda (300–350 🪙, categoría y slot
  nuevos). Burbuja con frases locales por evento (pomodoro/ciclo/nivel/saludo)
  y **consejos personalizados vía la API de Claude** al hacerle clic
  (pestaña ⚙ → 🐾 Mascota IA: API key + modelo; SDK `anthropic` opcional).
- **Switch de sincronización** (`sync_enabled`, widget `ToggleSwitch` nuevo)
  con icono de estado por color (apagado/reposo/sincronizando/conectado/sin
  conexión). Apagado, frena sync manual, auto-sync, webhooks entrantes y
  escrituras a Asana.
- **Detección de tareas completadas fuera de la app**: la verificación del
  sync consulta el estado real en Asana y marca localmente las cerradas con
  PomoGrowth apagado (antes quedaban pendientes para siempre).
- Barra de XP/monedas del header con **degradado dorado** fijo
  (`ProgressGradient.set_custom_colors`).
- Toast de alta + desplazamiento automático a la tarea recién creada.

### Cambiado
- **Las tareas manuales nuevas se insertan ARRIBA** de la lista (antes al final).
- **Arranque siempre en Pendientes + orden 📅 Fecha** (pedido explícito).
- **Candado de tarea durante el pomodoro**: con el conteo iniciado (corriendo
  o pausado a mitad) seleccionar otra tarea no roba el foco del timer; se
  libera al terminar, reiniciar o quedar sin tarea. El sync tampoco limpia ya
  la selección ni fuerza el filtro «Todas».
- **Al redimensionar la ventana solo crece el panel de tareas** (el timer
  conserva su ancho; el divisor manual sigue operativo).
- **Ventana compacta**: muestra TRABAJO/DESCANSO con colores, botón de saltar
  descanso, y se actualiza en todas las transiciones — el ciclo completo sin
  maximizar (tamaño 240×132).

### Corregido (auditoría adversarial — 27 hallazgos confirmados)
Los más importantes:
- Pausar o reiniciar durante un descanso registraba ese tiempo como TRABAJO
  (vestigio `is_pomodoro_mode` de v8) — inflaba tarjeta, «hoy» y stats.
- Pausar y luego completar un pomodoro duplicaba el tiempo registrado (se
  sumaba la duración completa encima de los tramos ya logueados); ahora el
  chunk final registra solo el tramo final. Saltar un descanso registra la
  parte transcurrida.
- La compacta abría con estado obsoleto («25:00/TRABAJO» en pleno descanso):
  el sync corría antes de `show()` y el guard de visibilidad lo anulaba.
- Un fallo total de red se reportaba como sincronización exitosa (verde +
  «conectado»); ahora propaga al camino de error → estado «sin conexión».
- Las tareas de Asana borradas localmente resucitaban en cada sync
  (tombstones en el merge).
- El sync podía revertir un completado/reabierto local con el PUT a Asana aún
  en vuelo o fallido (flag `asana_push_pending`).
- Completar la tarea activa a mitad del 4º pomodoro borraba el progreso del
  ciclo (`reset(keep_cycle=True)`); eliminarla en sesión dejaba el timer
  corriendo sin dueño y saltándose el candado.
- Guardar configuración con una sesión pausada reiniciaba el contador.
- El nivel ganado por el bonus de ciclo no se notificaba.
- Zona horaria: chip de creación y orden «Creada» mezclaban hora local naive
  con UTC de Asana (día equivocado cerca de medianoche; orden incorrecto).
- Estadísticas leían la clave equivocada del proyecto en los chunks
  (`project_name` vs `project`): tiempo caía en «Sin proyecto».
- Refresh de lista durante un arrastre dejaba el drag roto (RuntimeError).
- Sync que solo marca borradas/completadas (fetch vacío) no refrescaba la UI.
- Doble escritura completa del JSON por cada mutación (historial duplicaba el
  guardado); toasts/diálogos anclados a la ventana oculta en modo compacto;
  saludo de la mascota expiraba durante el splash; consejo IA truncado con
  `claude-sonnet-5` (thinking desactivado para la burbuja); gating incompleto
  de `sync_enabled` (webhook/PUTs/comentarios); apagar el switch no cancelaba
  el sync en vuelo; y otros menores.

### Infraestructura
- Suite offscreen ampliada (11 → 27 tests: candado, ciclos, chunks, merge,
  filtros, mascota). Test de paridad corregido (no compara settings contra
  defaults: los datos reales están personalizados). `requirements.txt` suma
  `anthropic`.

---

## v9.0 — 2026-07-09

Reescritura completa de la capa visual de v8 (Tkinter) sobre **PySide6
(Qt 6)** reutilizando el motor, en 6 fases:

1. **Timer + tareas**: ventana frameless con snap/resize nativo, panel de
   tareas con updates in-place, drag & drop, timer de 48 pt con crossfade
   trabajo↔descanso, barra de progreso con gradiente animado.
2. **Asana + settings**: sync multi-conexión con merge en lotes (la UI no se
   congela), verificación de borradas en el hilo de red, diálogo de
   configuración de 5 pestañas.
3. **Estadísticas**: 5 gráficos matplotlib re-tematizados + tarjetas resumen +
   tablas por proyecto/tarea + export CSV/XLSX.
4. **Gamificación**: XP/niveles/monedas/racha, tienda de 16 items con temas
   re-tematizables EN CALIENTE, música lofi con QtMultimedia.
5. **Enfoque + tray + compacto**: bloqueo de webs por hosts (UAC), bandeja del
   sistema, mini ventana compacta, bienvenida/despedida.
6. **Empaquetado**: PyInstaller onedir (~194 MB), singleton por puerto fijo,
   datos junto al exe, convivencia verificada con v8.

Lecciones de Qt documentadas en DOCUMENTACION.md §12 (parents explícitos,
`Signal(object)` para ids >2³¹, QSS sin font-size, SVG en vez de emoji).
