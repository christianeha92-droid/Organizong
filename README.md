# Organizador de Música

Aplicación local para Windows que ordena una biblioteca musical en carpetas
`Artista/Álbum`, descarga letras y consigue portadas. Trabaja con una carpeta de
origen y otra de destino independientes, muestra una vista previa antes de
mover nada y registra cada cambio para poder deshacerlo.

## Carpetas del proyecto

En este equipo se usan tres carpetas separadas:

| Carpeta | Uso |
| --- | --- |
| `D:\Music` | Origen: música sin organizar. La aplicación solo lee de aquí al organizar. |
| `D:\MusicaOrganizada` | Destino: biblioteca ordenada en `Artista/Álbum`. |
| `D:\OrganizadorMusica` | Aplicación, configuración y estado interno. |

El estado, la caché, los informes, el diario y las copias de seguridad viven en
`D:\OrganizadorMusica\.music-organizer`, fuera de la música. Así no se mezclan
con las canciones ni con las carpetas del destino.

## Qué hace

- **Organiza** los audios en `Artista/Álbum` usando `albumartist` por defecto
  (se puede usar el artista de cada pista).
- Renombra las pistas como `01 - Título.flac` o `1-01 - Título.flac` en álbumes
  con varios discos.
- Conserva los archivos acompañantes de una canción: `.lrc` y las imágenes con
  el mismo nombre que el audio.
- Detecta **duplicados idénticos** y **conflictos** sin borrar archivos.
- Si una canción ya existe en el destino, muestra el **formato y el tamaño** del
  origen y del destino antes de decidir.
- Detecta si el origen ya tiene una estructura `Artista/Álbum` y la compara con
  la carpeta organizada.
- **Extrae letras incrustadas** en las etiquetas y, si no existen, las busca en
  LRCLIB y Lyrics.ovh. Las guarda como `.lrc` junto al audio.
- **Extrae la portada incrustada** del audio. Si no hay, la busca en iTunes,
  Deezer y MusicBrainz/Cover Art Archive. La guarda como `cover.jpg` o
  `cover.png` dentro de la carpeta del álbum.
- No modifica las etiquetas de los audios ni borra canciones duplicadas.
- Registra cada sesión en `.music-organizer/journal/` para poder deshacerla.

## Requisitos

- Windows 10 u 11.
- Python 3.10 o superior. En este equipo está en `C:\Python315`.
- Conexión a Internet solo para descargar letras o portadas que falten.

Instala las dependencias desde la carpeta de la aplicación:

```bat
py -3 -m pip install -r requirements.txt
```

## Inicio rápido

### Interfaz gráfica

Haz doble clic en **`Iniciar_GUI.bat`** o ejecuta:

```bat
py -3 gui.py
```

La interfaz arranca en **modo simulación**. Pulsa **Analizar** para ver la vista
previa, los formatos y tamaños de origen y destino, y los conflictos. Para
aplicar cambios, desmarca *«Solo simular (no modifica archivos)»* y usa los
botones de organización, letras o portadas. Siempre se pedirá confirmación
antes de modificar la biblioteca.

### Menú de consola

Haz doble clic en **`Iniciar.bat`**. El menú permite analizar, simular,
organizar, descargar letras y portadas, hacer todo y deshacer.

### Comandos

```bat
:: Analizar origen + biblioteca organizada, sin cambiar nada
py -3 organizador.py --source "D:\Music" --library "D:\MusicaOrganizada" scan

:: Ver qué se movería (simulación; no modifica archivos)
py -3 organizador.py --source "D:\Music" --library "D:\MusicaOrganizada" organize

:: Aplicar la organización (pide confirmación final)
py -3 organizador.py --source "D:\Music" --library "D:\MusicaOrganizada" organize --apply

:: Descargar letras y portadas faltantes en origen y destino
py -3 organizador.py --source "D:\Music" --library "D:\MusicaOrganizada" lyrics --apply
py -3 organizador.py --source "D:\Music" --library "D:\MusicaOrganizada" covers --apply

:: Organizar y después descargar letras y portadas
py -3 organizador.py --source "D:\Music" --library "D:\MusicaOrganizada" all --apply

:: Deshacer la última sesión registrada
py -3 organizador.py --source "D:\Music" --library "D:\MusicaOrganizada" undo
```

`--source` y `--root` son equivalentes; `--library` y `--dest` también. Si se
omiten, se usa `D:\Music` como origen y `D:\MusicaOrganizada` como destino
cuando existen.

`--apply` es obligatorio para modificar archivos. Si se omite, todo se ejecuta
en modo simulación. Aunque se use `--apply`, la organización pide una
confirmación final antes de mover nada. `--yes` elimina esa confirmación y
debe usarse solo con conocimiento de lo que se está ejecutando.

Opciones útiles:

| Opción | Efecto |
| --- | --- |
| `--artist-field artist` | Usa el artista de la pista en vez de `albumartist`. |
| `--conflict ask` | Pregunta canción por canción si el destino existe (valor por defecto). |
| `--conflict overwrite` | Reemplaza el destino; antes guarda una copia de seguridad. |
| `--conflict keep` | Conserva ambos: mueve la canción a `Álbum (2)`. |
| `--conflict skip` | Deja la canción en el origen y no toca el destino. |
| `--clean-empty-dirs` | Elimina carpetas vacías del origen al terminar de organizar. |
| `--overwrite` | En `lyrics`/`covers`, reemplaza archivos existentes (con copia de seguridad). |
| `--limit N` | Procesa solo las primeras N pistas o N álbumes. |
| `--provider lrclib` | Busca letras solo en LRCLIB (`lyricsovh` o `both` también valen). |
| `--country MX` | Cambia el país usado por iTunes para buscar portadas. |
| `--workers 4` | Número de descargas simultáneas (1-8). |
| `--yes` | No pide la confirmación final (solo para ejecuciones supervisadas). |

## Conflictos y seguridad

La aplicación nunca borra canciones para resolver un conflicto. Si el archivo
de destino existe y es distinto:

1. **Sobrescribir**: mueve la versión del origen al destino. La versión anterior
   se guarda antes en `.music-organizer/backups/` y se puede recuperar con
   `undo`.
2. **Conservar ambos**: deja el destino intacto y mueve la versión del origen a
   una carpeta nueva del álbum, por ejemplo `Álbum (2)`.
3. **Omitir**: no mueve esa canción y conserva tanto el origen como el destino.

Antes de decidir se muestran el formato, el tamaño y, cuando está disponible, la
duración y el bitrate de ambas versiones. Los acompañantes `.lrc` e imágenes
siguen la misma decisión que su canción.

Los duplicados idénticos se detectan comparando tamaño y SHA-256. Se dejan en su
sitio y no se borran. La detección de origen ya organizado compara las carpetas
padre relativas al origen y al destino.

## Deshacer

- Cada ejecución crea un diario JSONL en `.music-organizer/journal/`.
- **Deshacer** recupera los movimientos, elimina solo los archivos creados que
  sigan intactos y restaura las copias de seguridad.
- Se deshace la última sesión registrada; si ya fue deshecha, no se vuelve a
  aplicar.
- El comando `all` realiza varias sesiones (organización, portadas y letras).
  Para revertirlas hay que ejecutar `undo` varias veces, empezando por la última.
- Los informes CSV de las simulaciones y ejecuciones se guardan en
  `.music-organizer/reports/`.

## Cómo se eligen los datos

1. Lee las etiquetas del audio (`artist`, `albumartist`, `album`, `title`,
   `tracknumber`, `discnumber`).
2. Si falta el título, lo deduce del nombre del archivo.
3. Si faltan artista o álbum, intenta deducirlos de la ruta y de otras pistas de
   la biblioteca con el mismo título y duración parecida.
4. Para organizar usa `albumartist`; si no existe, usa `artist`.
5. Las letras y portadas se buscan por artista, título, álbum y duración cuando
   es posible.
6. `scan`, `lyrics` y `covers` leen tanto el origen como la biblioteca ya
   organizada. `organize` solo planifica archivos que están en el origen.

## Estructura del proyecto

```text
OrganizadorMusica/
├── Iniciar.bat
├── Iniciar_GUI.bat
├── organizador.py          # Lanzador de consola
├── gui.py                  # Lanzador de interfaz gráfica
├── requirements.txt
├── music_organizer/
│   ├── cli.py              # Comandos y menú
│   ├── gui.py              # Interfaz Tkinter
│   ├── config.py           # Configuración de origen, destino y estado
│   ├── models.py           # Modelos de datos
│   ├── scanner.py          # Lectura de etiquetas y raíces de biblioteca
│   ├── planner.py          # Plan de movimientos y conflictos
│   ├── operations.py       # Ejecución, decisiones y deshacer
│   ├── journal.py          # Diario y copias de seguridad
│   ├── providers.py        # APIs externas y puntuación
│   ├── lyrics.py           # Servicio de letras
│   ├── covers.py           # Servicio de portadas
│   └── utils.py            # Utilidades
└── tests/
```

La carpeta de estado `.music-organizer` se crea dentro de
`D:\OrganizadorMusica` y no dentro de la música.

## Pruebas

```bat
py -3 -m unittest discover -s tests -v
```

Las pruebas usan carpetas temporales y simulan las APIs; no descargan archivos
ni tocan la biblioteca real.

## Limitaciones

- Los servicios de letras y portadas dependen de Internet y de sus catálogos.
  Una canción puede quedar sin letra o sin portada si no hay una coincidencia
  suficientemente buena.
- LRCLIB puede responder `503` de forma temporal; Lyrics.ovh se usa como
  respaldo.
- Las letras descargadas pueden ser sincronizadas (`.lrc` con tiempos) o texto
  plano, según lo que ofrezca el proveedor.
- No se escriben metadatos nuevos dentro de los audios. Las portadas se guardan
  como archivo en la carpeta del álbum.
- El límite de ruta se mantiene por debajo de 240 caracteres para evitar
  problemas de Windows, acortando nombres si hace falta. En álbumes con títulos
  muy largos, varias pistas pueden compartir una carpeta truncada.

## Primer uso recomendado

1. Cierra cualquier programa que esté escribiendo en la biblioteca.
2. Ejecuta `scan` y revisa los avisos.
3. Ejecuta `organize` sin `--apply` y revisa el informe CSV y la vista previa de
   conflictos.
4. Confirma que el destino está vacío o que ya contiene lo que esperas.
5. Aplica la organización con `organize --apply` y confirma el resumen.
6. Descarga letras y portadas con `lyrics --apply` y `covers --apply`, o usa
   `all --apply`.
7. Si algo no ha ido bien, ejecuta `undo` para revertir la última sesión.
