# Blender + Claude Code (MCP)

Dos archivos:

| Archivo | Dónde corre | Qué hace |
|---|---|---|
| `blender_mcp.py` | Dentro de Blender (add-on) | Abre un socket local y ejecuta los comandos en Blender |
| `blender_mcp_server.py` | Lo lanza Claude Code | Servidor MCP: expone las herramientas a Claude y habla con el add-on |

```
Claude Code ──MCP/stdio──► blender_mcp_server.py ──TCP 127.0.0.1:9876──► blender_mcp.py (Blender)
```

## Instalación

1. **Add-on:** Blender > Edit > Preferences > Add-ons > Install > `blender_mcp.py` (desinstala antes cualquier versión anterior) y actívalo.
2. **Conectar:** en la vista 3D pulsa `N` > pestaña **BlenderMCP** > **Connect to MCP server**.
3. **Servidor en Claude Code** (necesita [uv](https://docs.astral.sh/uv/); instala las dependencias solo). Sustituye la ruta por la de tu copia del repositorio:

```bash
claude mcp add blender -- uv run /ruta/absoluta/a/blender_mcp_server.py
```

4. Comprueba la conexión (con Blender abierto y conectado):

```bash
uv run /ruta/absoluta/a/blender_mcp_server.py --check
```

## Herramientas

| Herramienta | Para qué |
|---|---|
| `blender_status` | Diagnóstico: conexión, versión, archivo, si el código está permitido |
| `scene_overview` | Resumen de escena: render, unidades, conteo por tipo, colecciones, selección |
| `list_objects` | Objetos con filtros (tipo, nombre, colección, selección) y paginación |
| `get_object_details` | Estado completo de un objeto: transformación, modificadores, materiales, malla, cámara, luz |
| `viewport_screenshot` | Captura del viewport como imagen para que Claude verifique su trabajo |
| `render_image` | Render a archivo (restaura los ajustes de la escena al terminar) |
| `execute_blender_code` | Python corto dentro de Blender (crea un paso de Ctrl+Z antes) |
| `run_blender_script` | Ejecuta un `.py` de tu repo, con nombre de archivo y línea reales en los errores |
| `save_blend`, `export_scene` | Guardar y exportar (`.glb .fbx .obj .stl .ply .usd`) a tu proyecto |
| `polyhaven_*`, `sketchfab_*` | Búsqueda, vista previa e importación de assets |

También hay un recurso `blender://scene` y un prompt `blender_task` que arranca el flujo recomendado:
mirar la escena, trabajar en pasos pequeños y verificar con capturas.

**Flujo recomendado en Claude Code:** pídele que escriba scripts en tu repo (`scenes/rocas.py`) y los ejecute con
`run_blender_script`. El código queda versionado en git y se puede editar y volver a lanzar.

## Configuración (variables de entorno)

Se pasan con `-e` al registrar el servidor, por ejemplo `claude mcp add blender -e BLENDER_PORT=9999 -- uv run ...`.

| Variable | Por defecto | |
|---|---|---|
| `BLENDER_HOST` / `BLENDER_PORT` | `127.0.0.1` / `9876` | Debe coincidir con el puerto del panel |
| `BLENDER_MCP_TOKEN` | vacío | Mismo valor que "Auth Token" en las preferencias del add-on |
| `BLENDER_TIMEOUT` | `30` | Segundos por comando (cada herramienta larga acepta `timeout_s`) |
| `BLENDER_MCP_READONLY` | desactivado | `1` = no registra las herramientas que modifican la escena o escriben archivos |
| `BLENDER_MCP_LOG` | `INFO` | Los logs van a stderr |

## Seguridad

- El socket solo escucha en `localhost`. Con token configurado, cada comando debe llevarlo (se compara en tiempo constante).
- `execute_blender_code` y `run_blender_script` ejecutan Python arbitrario con tus permisos. Puedes apagarlos con
  **Allow code execution** en el panel de Blender. Las herramientas de inspección también lo usan, así que con eso
  apagado solo funcionan el resumen básico, las capturas y los assets.
- `BLENDER_MCP_READONLY=1` es una comodidad para que Claude no vea las herramientas de escritura, **no** un sandbox:
  el límite real es el interruptor del panel.
- Las rutas se resuelven en la máquina de Blender. Si Claude Code corre en otra (contenedor, WSL), las capturas y
  exportaciones necesitan un sistema de archivos compartido.

## Limitaciones conocidas

- Blender ejecuta cada comando en su hilo de interfaz: renders, importaciones grandes y descargas lo congelan hasta terminar.
- No incluye los generadores de IA (Hyper3D Rodin, Hunyuan3D); el add-on sí los soporta, falta añadir las herramientas.

## Créditos y licencia

- `blender_mcp.py` es una versión modificada del add-on de [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp)
  (© 2025 Siddharth Ahuja, licencia MIT). Su aviso de copyright y licencia están en
  [`LICENSE-blender-mcp.txt`](LICENSE-blender-mcp.txt). Los cambios respecto al original están listados en la
  cabecera del archivo.
- `blender_mcp_server.py` y este README son de este repositorio.
