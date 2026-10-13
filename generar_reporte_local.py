import os
import re
import sqlite3
import smtplib
import asyncio
import getpass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path

def _load_dotenv():
    """Carga .env local si existe (SENDER_EMAIL, APP_PASSWORD, GOOGLE_API_KEY)."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val

_load_dotenv()

import markdown
import requests


def _asegurar_bidi():
    """xhtml2pdf importa python-bidi. En este equipo el Control de aplicaciones
    bloquea su DLL nativa; un sustituto basta para texto en español (izquierda a derecha)."""
    import sys
    import types

    try:
        from bidi import get_display as _ok  # noqa: F401
        return
    except (ImportError, OSError):
        pass
    for nombre in list(sys.modules):
        if nombre == "bidi" or nombre.startswith("bidi."):
            del sys.modules[nombre]

    def get_display(str_or_bytes, encoding="utf-8", base_dir=None, debug=False):
        return str_or_bytes

    def get_base_level(text):
        return 0

    mod = types.ModuleType("bidi")
    mod.get_display = get_display
    mod.get_base_level = get_base_level
    sys.modules["bidi"] = mod


_asegurar_bidi()
from xhtml2pdf import pisa

try:
    from graficas_xm import generar_graficas, temas_detectados
except ImportError:       # falta matplotlib: el informe sale igual, sin gráficas
    generar_graficas = None
    temas_detectados = None
import logging
logging.getLogger("xhtml2pdf").setLevel(logging.ERROR)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PDF_DIR = DATA_DIR / "pdfs"
GRAFICAS_DIR = DATA_DIR / "graficas"   # PNG auxiliares; no van a la BD ni al historial
DB_PATH = DATA_DIR / "reportes.db"
ASSETS_DIR = BASE_DIR / "assets"

# Backend (prioridad: --backend > AGENT_BACKEND del .env > "local"):
# - local (default): investigación con fuentes + redacción directa con Gemini en Vertex AI
#   (service account del .env). Es el modo recomendado.
# - ces: además consulta al 'Agente Documentacion' (CES) y usa su respuesta solo como
#   borrador orientativo; el informe final igualmente se basa en fuentes verificables.
# - adk: igual, pero el borrador viene de agent.py (Agente Estratégico ADK).
AGENT_BACKEND = (os.environ.get("AGENT_BACKEND") or "local").strip().lower()
AGENT_UI_URL = (
    os.environ.get("AGENT_UI_URL")
    or "https://genai-app-agenteestratgico-1-1786540461567-86171080709.us-central1.run.app"
).rstrip("/")
CES_PROJECT_ID = os.environ.get("CES_PROJECT_ID") or "automatizaciones-ia-20260420"
CES_LOCATION = os.environ.get("CES_LOCATION") or "us"
CES_APP_ID = os.environ.get("CES_APP_ID") or "e3bac7d6-8adb-43f3-a390-45393648055f"
CES_VERSION_ID = os.environ.get("CES_VERSION_ID") or "7accddf4-b312-4351-8b0f-8047fac8a442"
CES_DEPLOYMENT_ID = os.environ.get("CES_DEPLOYMENT_ID") or "6ce2f38f-5373-45ea-9a23-10d078a0b1e9"
CES_TIMEOUT_SECONDS = int(os.environ.get("CES_TIMEOUT_SECONDS") or "90")
LOCAL_TIMEOUT_SECONDS = int(os.environ.get("LOCAL_TIMEOUT_SECONDS") or "420")


def _ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    GRAFICAS_DIR.mkdir(parents=True, exist_ok=True)


def _logo_path():
    logo = ASSETS_DIR / "logo-enersinc-dark.png"
    return logo if logo.is_file() else None


def _ruta_pdf_relativa(ruta_pdf) -> str:
    """Guarda en BD la ruta relativa a data/ (carpeta del código)."""
    path = Path(ruta_pdf).resolve()
    try:
        return str(path.relative_to(DATA_DIR.resolve())).replace("\\", "/")
    except ValueError:
        try:
            return str(path.relative_to(BASE_DIR.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)


def _ruta_pdf_absoluta(ruta_guardada: str) -> Path:
    path = Path(ruta_guardada)
    if path.is_file():
        return path
    cand = DATA_DIR / ruta_guardada
    if cand.is_file():
        return cand
    cand2 = BASE_DIR / ruta_guardada
    if cand2.is_file():
        return cand2
    return PDF_DIR / Path(ruta_guardada).name




def _mensaje_usuario(prompt: str):
    """ADK exige un Content con role=user, no un str."""
    from google.genai import types
    return types.Content(role="user", parts=[types.Part(text=prompt)])


def _texto_de_evento(evento) -> str:
    partes = []
    content = getattr(evento, "content", None)
    if content is not None:
        for part in getattr(content, "parts", None) or []:
            txt = getattr(part, "text", None)
            if txt:
                partes.append(str(txt))
        if not partes and getattr(content, "text", None):
            partes.append(str(content.text))
    txt = getattr(evento, "text", None)
    if txt:
        partes.append(str(txt))
    return "\n".join(partes).strip()


def _ces_access_token() -> str:
    """Token ADC / service account (GOOGLE_APPLICATION_CREDENTIALS)."""
    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    if not creds.token:
        raise RuntimeError("No se pudo obtener access token de Google Cloud.")
    return creds.token


def _extract_ces_answer(data) -> str:
    texts = []
    for out in data.get("outputs") or []:
        if isinstance(out, dict) and out.get("text"):
            texts.append(str(out["text"]))
    return "\n\n".join(texts).strip()


def consultar_agente_ces(prompt: str) -> str:
    """Llama al Agente Estratégico desplegado vía CES (misma app que Chat / UI)."""
    import uuid

    sid = "docpdf_" + uuid.uuid4().hex[:16]
    session_path = (
        f"projects/{CES_PROJECT_ID}/locations/{CES_LOCATION}/apps/{CES_APP_ID}"
        f"/sessions/{sid}"
    )
    url = f"https://ces.googleapis.com/v1beta/{session_path}:runSession"
    payload = {
        "config": {
            "session": session_path,
            "app_version": (
                f"projects/{CES_PROJECT_ID}/locations/{CES_LOCATION}/apps/{CES_APP_ID}"
                f"/versions/{CES_VERSION_ID}"
            ),
            "deployment": (
                f"projects/{CES_PROJECT_ID}/locations/{CES_LOCATION}/apps/{CES_APP_ID}"
                f"/deployments/{CES_DEPLOYMENT_ID}"
            ),
        },
        "inputs": [{"text": prompt}],
    }
    headers = {
        "Authorization": f"Bearer {_ces_access_token()}",
        "Content-Type": "application/json",
        "x-goog-user-project": CES_PROJECT_ID,
    }
    res = requests.post(url, headers=headers, json=payload, timeout=CES_TIMEOUT_SECONDS)
    if res.status_code != 200:
        raise RuntimeError(
            f"CES HTTP {res.status_code}: {(res.text or '')[:400]}"
        )
    texto = _extract_ces_answer(res.json())
    if not texto:
        raise RuntimeError("CES respondió 200 pero sin texto en outputs.")
    return texto


def _configurar_vertex_local():
    """Sin GOOGLE_API_KEY, usa Vertex AI con la service account (ADC) del .env."""
    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_GENAI_API_KEY"):
        return
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", CES_PROJECT_ID)
        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")


def consultar_agente_local(prompt: str) -> str:
    """Ejecuta agent.py local (Agente Estratégico ADK: regulatorio, actualidad,
    desarrollo de negocios + búsqueda web) y devuelve el texto final."""
    _configurar_vertex_local()
    from google.adk.runners import InMemoryRunner
    from agent import root_agent

    user_id = "usuario_local"
    mensaje = _mensaje_usuario(prompt)

    async def _run():
        runner = InMemoryRunner(agent=root_agent, app_name="DocumentacionPDF")
        session = await runner.session_service.create_session(
            app_name=runner.app_name,
            user_id=user_id,
        )
        eventos = []
        async for evento in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=mensaje,
        ):
            eventos.append(evento)
        return eventos

    try:
        eventos = asyncio.run(asyncio.wait_for(_run(), timeout=LOCAL_TIMEOUT_SECONDS))
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"El agente local no respondió en {LOCAL_TIMEOUT_SECONDS}s (LOCAL_TIMEOUT_SECONDS)."
        )
    textos = []
    for evento in eventos:
        ttxt = _texto_de_evento(evento)
        if not ttxt:
            continue
        author = (getattr(evento, "author", None) or "").lower()
        if author in {"user", "system"}:
            continue
        textos.append(ttxt)
    if textos:
        return "\n\n".join(textos)
    fallback = [x for x in (_texto_de_evento(e) for e in eventos) if x]
    return "\n\n".join(fallback) if fallback else ""


def consultar_agente(prompt: str) -> str:
    """Borrador orientativo del agente según AGENT_BACKEND (ces|adk). En 'local' no hay
    borrador: el informe se redacta directamente a partir de la evidencia."""
    if AGENT_BACKEND in {"ces", "remote", "cloud", "ui"}:
        return consultar_agente_ces(prompt)
    if AGENT_BACKEND == "adk":
        return consultar_agente_local(prompt)
    if AGENT_BACKEND == "local":
        return ""
    raise RuntimeError(
        f"AGENT_BACKEND desconocido: {AGENT_BACKEND!r}. Usa 'local', 'ces' o 'adk'."
    )


def extraer_texto_respuesta(respuesta):
    """Compatibilidad: consultar_agente ya puede devolver str."""
    if respuesta is None:
        return ""
    if isinstance(respuesta, str):
        return respuesta
    return str(respuesta)


def titulo_desde_contenido(texto_md, prompt=""):
    """Toma el primer encabezado markdown o un título corto desde el prompt."""
    for line in (texto_md or "").splitlines():
        m = re.match(r"^#{1,3}\s+(.+)$", line.strip())
        if m:
            t = re.sub(r"\s*\[\d{1,2}\]", "", m.group(1)).strip()
            if 6 <= len(t) <= 140:
                return t
    base = (prompt or "").strip()
    if base:
        corto = re.sub(r"\s+", " ", base)[:90]
        return f"Informe: {corto}"
    return "Informe Estratégico Enersinc"


def inicializar_base_datos():
    _ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS reportes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha_creacion TEXT NOT NULL,
            prompt_usuario TEXT NOT NULL,
            correo_destino TEXT NOT NULL,
            titulo_reporte TEXT NOT NULL,
            contenido_markdown TEXT NOT NULL,
            ruta_pdf TEXT NOT NULL,
            estado_envio TEXT NOT NULL
        )
        """
    )
    # Columnas añadidas con el modo de fuentes (compatibles con BD anteriores).
    columnas = {r[1] for r in cursor.execute("PRAGMA table_info(reportes)").fetchall()}
    for col in ("fuentes", "verificacion"):
        if col not in columnas:
            cursor.execute(f"ALTER TABLE reportes ADD COLUMN {col} TEXT")
    conn.commit()
    conn.close()


def actualizar_fuentes_db(reporte_id, fuentes, verificacion):
    """Guarda la lista de fuentes (JSON) y el resumen de verificación."""
    import json as _json
    datos = [{"n": f["id"], "titulo": f.get("titulo", ""), "url": f.get("url_final") or f.get("url"),
              "publicado": f.get("fecha", ""), "derogada": f.get("derogada", ""),
              "dominio": _dominio(f.get("url_final") or f.get("url") or ""), "tipo": f.get("tipo", ""),
              "enlace": f.get("enlace", ""), "http": f.get("status")} for f in fuentes]
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE reportes SET fuentes = ?, verificacion = ? WHERE id = ?",
                 (_json.dumps(datos, ensure_ascii=False), verificacion, reporte_id))
    conn.commit()
    conn.close()


def guardar_reporte_db(prompt, correo, titulo, contenido_md, ruta_pdf, estado):
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ruta_rel = _ruta_pdf_relativa(ruta_pdf)
    cursor.execute(
        """
        INSERT INTO reportes
        (fecha_creacion, prompt_usuario, correo_destino, titulo_reporte,
         contenido_markdown, ruta_pdf, estado_envio)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (fecha_actual, prompt, correo, titulo, contenido_md, ruta_rel, estado),
    )
    reporte_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return reporte_id


def reservar_reporte_db(prompt, correo, titulo, contenido_md, estado):
    """Inserta la fila primero (ruta vacía) para conocer el ID y numerar los archivos."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        """
        INSERT INTO reportes
        (fecha_creacion, prompt_usuario, correo_destino, titulo_reporte,
         contenido_markdown, ruta_pdf, estado_envio)
        VALUES (?, ?, ?, ?, ?, '', ?)
        """,
        (fecha_actual, prompt, correo, titulo, contenido_md, estado),
    )
    reporte_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return reporte_id


def actualizar_ruta_db(reporte_id, ruta_pdf):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE reportes SET ruta_pdf = ? WHERE id = ?",
                 (_ruta_pdf_relativa(ruta_pdf), reporte_id))
    conn.commit()
    conn.close()


def nombre_archivo_reporte(reporte_id, timestamp_archivo, ext="pdf") -> str:
    """0015_Reporte_20261007_142301.pdf (número = ID en BD, con ceros a la izquierda)."""
    return f"{int(reporte_id):04d}_Reporte_{timestamp_archivo}.{ext}"


def renumerar_archivos_existentes():
    """Renombra PDFs/.md antiguos al esquema NNNN_Reporte_<timestamp> usando su ID en BD
    y actualiza ruta_pdf a 'pdfs/<nuevo nombre>'. Idempotente."""
    inicializar_base_datos()
    conn = sqlite3.connect(str(DB_PATH))
    filas = conn.execute("SELECT id, ruta_pdf FROM reportes ORDER BY id").fetchall()
    usados = set()
    for rid, ruta in filas:
        nombre = Path((ruta or "").replace("\\", "/")).name
        if not nombre:
            print(f"  #{rid:04d}: sin ruta registrada")
            continue
        base = re.sub(r"^\d{4}_", "", nombre)
        nuevo = f"{rid:04d}_{base}"
        actual = PDF_DIR / nombre
        destino = PDF_DIR / nuevo
        if not actual.is_file() and destino.is_file():
            actual = destino
        if not actual.is_file():
            print(f"  #{rid:04d}: archivo no existe ({nombre}); fila sin cambios")
            continue
        if actual != destino:
            actual.rename(destino)
            md_viejo = actual.with_suffix(".md")
            if md_viejo.is_file():
                md_viejo.rename(destino.with_suffix(".md"))
            # Las PNG de gráficas viven en data/graficas/ y no se renumeran con la BD.
        usados.add(destino.name)
        conn.execute("UPDATE reportes SET ruta_pdf = ? WHERE id = ?", (f"pdfs/{nuevo}", rid))
        print(f"  #{rid:04d}: {nuevo} (ya numerado)" if nombre == nuevo
              else f"  #{rid:04d}: {nombre} -> {nuevo}")
    conn.commit()
    conn.close()
    for f in sorted(PDF_DIR.glob("*.pdf")):
        if f.name not in usados:
            print(f"  Aviso: {f.name} no está asociado a ninguna fila de la BD")


def actualizar_estado_db(reporte_id, nuevo_estado):
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE reportes SET estado_envio = ? WHERE id = ?",
        (nuevo_estado, reporte_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Informe PDF confidencial (2 páginas por defecto, parametrizable con MAX_PAGES)
# ---------------------------------------------------------------------------
MAX_PAGES = max(1, int(os.environ.get("MAX_PAGES") or "2"))
PALABRAS_POR_PAGINA = int(os.environ.get("PALABRAS_POR_PAGINA") or "600")
REPORT_TZ = os.environ.get("REPORT_TZ") or "America/Bogota"
# Tamaños de fuente legibles (pt). 9.0 solo se usa para evitar una página final casi vacía
# o, con --max-pages 1, para encajar el contenido; nunca se baja de ahí.
_ESCALAS_FUENTE = (10.0, 9.5)
_FUENTE_MINIMA = 9.0


def _ahora_local():
    """Fecha/hora en America/Bogota (fallback UTC-5 si falta tzdata en Windows)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(REPORT_TZ)), "COT"
    except Exception:
        from datetime import timezone, timedelta
        return datetime.now(timezone(timedelta(hours=-5))), "UTC-5"


def _esc(texto) -> str:
    return (
        str(texto or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _presupuesto_palabras(max_pages=None, grounded=False):
    """(objetivo, mínimo, máximo) de palabras del cuerpo para N páginas.
    2 páginas -> ~1150 palabras (1000-1250) a 10 pt."""
    n = max(1, int(max_pages or MAX_PAGES))
    objetivo = PALABRAS_POR_PAGINA * n - (PALABRAS_RESERVA_FUENTES if grounded else 0) - 50 * n
    if n == 1:
        objetivo -= 80          # la cabecera, los metadatos y la consulta ocupan ~1/4 de la página 1
    objetivo = max(350, objetivo)
    return objetivo, int(objetivo * 0.88), int(objetivo * 1.08)


def construir_prompt_agente(pregunta: str, max_pages=None, grounded=False) -> str:
    """Envuelve la pregunta con instrucciones para un REPORTE PDF explicativo.

    Las instrucciones explícitas del usuario tienen prioridad absoluta sobre
    el formato por defecto (conteos, secciones, páginas, longitud, etc.).
    """
    n = max(1, int(max_pages or MAX_PAGES))
    palabras_objetivo, palabras_min, palabras_max = _presupuesto_palabras(n, grounded)
    esc = palabras_objetivo / 1100.0
    w_res = max(80, int(120 * esc))
    w_ana = max(220, int(520 * esc))
    w_pts = max(150, int(300 * esc))
    w_cie = max(50, int(80 * esc))
    n_sub = "3 o 4" if n >= 2 else "2 o 3"
    n_pts = "4 o 5" if n >= 2 else "3 o 4"
    paginas_txt = "1 página" if n == 1 else f"{n} páginas"
    meses = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
             "septiembre", "octubre", "noviembre", "diciembre")
    hoy, _ = _ahora_local()
    fecha_hoy = f"{hoy.day} de {meses[hoy.month - 1]} de {hoy.year}"
    if grounded:
        encabezado = f"PREGUNTA DEL USUARIO:\n"
    elif AGENT_BACKEND in {"local", "adk"}:
        encabezado = (
            "Solicitud de informe técnico Enersinc.\n"
            f"FECHA ACTUAL: {fecha_hoy} (Colombia). 'Reciente', 'últimas semanas' o "
            "'actualizaciones' se refieren a esta fecha: busca primero información de "
            f"{hoy.year} y {hoy.year - 1} y di explícitamente si no encuentras nada más reciente.\n"
            "Usa tus especialistas y la búsqueda web. Para cada norma, noticia o cifra indica "
            "fecha y fuente (entidad o medio, p. ej. CREG, XM, MinEnergía). No inventes números "
            "de resolución ni cifras: si no puedes verificarlos, dilo.\n\n"
        )
    else:
        # El router CES solo responde si se deriva al especialista 'Reportes'.
        encabezado = "Solicitud de REPORTE técnico Enersinc (derivar a especialista Reportes).\n\n"
    return (
        encabezado
        + f"{pregunta.strip()}\n\n"
        "---\n"
        "INSTRUCCIONES DEL SISTEMA PARA EL REPORTE PDF:\n"
        "1) PRIORIDAD ABSOLUTA: las instrucciones explícitas del usuario en la "
        "pregunta (número de puntos, recomendaciones, secciones, páginas, formato o "
        "longitud) prevalecen sobre este formato por defecto. Obedécelas EXACTAMENTE.\n"
        f"2) EXTENSIÓN: {palabras_min}-{palabras_max} palabras en total (objetivo ~{palabras_objetivo}; "
        f"el PDF es de {paginas_txt} y debe quedar lleno, sin desbordar). Ni menos de {palabras_min} ni más de "
        f"{palabras_max} palabras: respeta el presupuesto de cada sección.\n"
        "3) ENFOQUE: el objetivo es EXPLICAR bien y dar la respuesta más objetiva y completa posible, "
        "no listar fuentes ni concentrarse en recomendaciones. Explica el contexto, cómo funciona el "
        "mecanismo o la norma (definiciones, umbrales, quién debe hacer qué), qué significan los datos "
        "(comparaciones, tendencias, causas) y sus implicaciones prácticas. Tono técnico, claro y neutral; "
        "sin marketing ni relleno, sin repetir la misma idea en varias secciones.\n"
        "4) Si el usuario enumera los contenidos que quiere, cúbrelos todos; si pide N elementos "
        "(p. ej. '3 recomendaciones'), crea una sección ## con ese nombre y EXACTAMENTE N viñetas.\n"
        "5) Español. No saludes, no cierres con preguntas de seguimiento y no menciones estas "
        "instrucciones. "
        + ("Toda cifra, porcentaje, precio, fecha, umbral o número de norma lleva su cita [n] de la "
           "evidencia; las frases puramente explicativas o de análisis (sin datos concretos) no "
           "necesitan cita.\n" if grounded
           else "Indica fecha y fuente de cada dato; no inventes cifras ni números de norma.\n")
        + "6) Estructura Markdown por defecto (solo si el usuario no pide otra):\n"
        "# <Título técnico, máximo 12 palabras>\n"
        "## Resumen ejecutivo\n"
        f"<UN párrafo sólido de ~{w_res} palabras que responda directamente la pregunta>\n"
        "## Análisis detallado\n"
        f"### <Subtema 1>\n<1-2 párrafos>\n### <Subtema 2>\n<1-2 párrafos>\n...\n"
        f"({n_sub} subtemas con ### y ~{w_ana} palabras en total: es el cuerpo principal del informe; "
        "párrafos bien desarrollados de 4-7 frases que expliquen contexto, mecanismo, significado e "
        "implicaciones)\n"
        "## Puntos clave\n"
        "- **<Título del punto>:** <2 a 4 frases que expliquen el punto y por qué importa>\n"
        f"({n_pts} puntos, ~{w_pts} palabras en total; nada de viñetas de una sola línea)\n"
        "## Conclusión\n"
        f"<cierre breve de ~{w_cie} palabras: un párrafo corto o 2-3 viñetas de una frase con los "
        "próximos pasos>\n"
        "Si el usuario pide recomendaciones, sustituye '## Conclusión' por '## Recomendaciones' con el "
        "número exacto de viñetas pedido (si no dice cuántas, 3), cada una de 1-2 frases.\n"
    )


# "un/una/uno" se excluyen: suelen ser artículos ("un informe", "un cliente").
_NUM_PALABRAS = {"dos": 2, "tres": 3, "cuatro": 4,
                 "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10}
_UNIDADES_LONGITUD = {"pagina", "paginas", "palabra", "palabras", "hoja", "hojas",
                      "parrafo", "parrafos", "linea", "minuto", "minutos", "anos", "meses",
                      "dias", "horas", "por"}
_ADJ_ANTEPUESTOS = r"(?:principales|mejores|grandes|claves?|concretas?|concretos?|nuevas?|nuevos?|primeras?|primeros?)"


def _sin_acentos(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn").lower()


def _conteos_pedidos(pregunta: str):
    """[(n, sustantivo)] p. ej. '3 principales líneas de servicio' -> (3, 'lineas')."""
    q = _sin_acentos(pregunta)
    nums = r"(\d+|" + "|".join(_NUM_PALABRAS) + r")"
    pat = re.compile(r"\b" + nums + r"\s+(?:" + _ADJ_ANTEPUESTOS + r"\s+)?([a-zn]{4,})")
    out = []
    for m in pat.finditer(q):
        n_txt, noun = m.group(1), m.group(2)
        n = int(n_txt) if n_txt.isdigit() else _NUM_PALABRAS.get(n_txt)
        if not n or n > 20 or noun in _UNIDADES_LONGITUD:
            continue
        out.append((n, noun))
    return out


def _contar_items_seccion(texto_md: str, raiz: str):
    """Cuenta viñetas de primer nivel (o sub-encabezados) bajo el primer encabezado
    cuyo texto contiene la raíz. None si no hay encabezado."""
    lineas = (texto_md or "").splitlines()
    candidatos = []
    for i, line in enumerate(lineas):
        m = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if m and raiz in _sin_acentos(m.group(2)):
            candidatos.append((i, len(m.group(1))))
    if not candidatos:
        return None
    # El título (H1) suele mencionar todo ("Análisis y Recomendaciones"): preferir secciones.
    min_nivel = min(n for _, n in candidatos)
    secciones = [c for c in candidatos if c[1] > min_nivel] if len(candidatos) > 1 else []
    if min_nivel == 1 and not secciones:
        secciones = [c for c in candidatos if c[1] > 1]
    for i, nivel in (secciones or candidatos)[:1]:
        items, subs = 0, 0
        for nxt in lineas[i + 1:]:
            mh = re.match(r"^(#{1,6})\s+", nxt.strip())
            if mh:
                if len(mh.group(1)) <= nivel:
                    break
                subs += 1
                continue
            if re.match(r"^([-*+]|\d+[.)])\s+", nxt):  # sin sangría = primer nivel
                items += 1
        return items if items else subs
    return None


def _verificar_conteos(pregunta: str, texto_md: str):
    """Lista de incumplimientos de conteos explícitos del usuario."""
    problemas = []
    for n, noun in _conteos_pedidos(pregunta):
        raiz = noun[:5]
        got = _contar_items_seccion(texto_md, raiz)
        if got is None:
            problemas.append(f"falta una sección '## ...{noun}...' con exactamente {n} viñetas")
        elif got != n:
            problemas.append(f"la sección de '{noun}' tiene {got} elementos y deben ser exactamente {n}")
    return problemas


def _contar_palabras(texto: str) -> int:
    return len(re.findall(r"\w+", texto or "", flags=re.UNICODE))


def _parece_rechazo_router(texto: str) -> bool:
    """Detecta respuestas del clasificador CES que no son el informe."""
    t = (texto or "").lower()
    claves = (
        "no tengo la capacidad",
        "subagente",
        "derivar el mensaje",
        "especialista adecuado",
        "clasificar, filtrar",
        "manuales, contratos o reportes",
    )
    return any(k in t for k in claves)


def _separar_titulo(texto_md: str, titulo: str) -> str:
    """Quita del cuerpo el encabezado que ya se usa como título del documento."""
    lineas = (texto_md or "").splitlines()
    for i, line in enumerate(lineas):
        m = re.match(r"^#{1,3}\s+(.+)$", line.strip())
        if m:
            if re.sub(r"\s*\[\d{1,2}\]", "", m.group(1)).strip() == (titulo or "").strip():
                del lineas[i]
            break
    return "\n".join(lineas).strip()


def _normalizar_md(texto_md: str) -> str:
    """Asegura líneas en blanco antes/después de encabezados y antes de listas
    (el agente CES suele devolver todo pegado)."""
    out = []
    for line in (texto_md or "").splitlines():
        st = line.strip()
        es_titulo = bool(re.match(r"^#{1,6}\s", st))
        es_item = bool(re.match(r"^([-*+]|\d+[.)])\s", st))
        prev = out[-1].strip() if out else ""
        prev_item = bool(re.match(r"^([-*+]|\d+[.)])\s", prev))
        if out and prev and (es_titulo or (es_item and not prev_item)
                             or re.match(r"^#{1,6}\s", prev)):
            out.append("")
        out.append(line.rstrip())
    return "\n".join(out).strip()


def _limpiar_respuesta(texto_md: str) -> str:
    """Quita preguntas de seguimiento finales y compacta citas largas del agente."""
    lineas = (texto_md or "").rstrip().splitlines()
    # preguntas de cierre (¿Deseas...?) al final
    while lineas and (not lineas[-1].strip() or (
            lineas[-1].strip().startswith("¿") and lineas[-1].strip().endswith("?"))):
        lineas.pop()
    texto = "\n".join(lineas)
    # LaTeX inline corto del modelo ($G$, $CU_t$) -> texto plano
    texto = re.sub(r"\$([A-Za-z][A-Za-z0-9_{}]{0,10})\$", lambda m: m.group(1).replace("{", "").replace("}", ""), texto)

    def _cita(m):
        doc = re.sub(r"\.pdf$", "", m.group(1).strip(), flags=re.I)
        pag = (m.group(2) or "").strip()
        etiqueta = f"{doc}, p. {pag}" if pag else doc
        return f' <span class="cita">[{_esc(etiqueta)}]</span>'

    texto = re.sub(
        r"\s*\[Documento:\s*([^|\]]+?)\s*(?:\|\s*P[áa]gina\s*([^\]]+?))?\s*\]",
        _cita, texto)
    return texto.strip()


def _recortar_inteligente(bloques):
    """Índice del bloque a eliminar cuando el contenido desborda mucho el objetivo:
    solo párrafos del análisis/detalle (nunca el resumen, los puntos clave, la
    conclusión/recomendaciones ni el único párrafo de un subtema). None si no hay
    candidato seguro (en ese caso el contenido fluye a una página extra)."""
    def _nivel(b):
        m = re.match(r"^(#{1,6})\s", b.strip())
        return len(m.group(1)) if m and "\n" not in b.strip() else 0
    prioridad = re.compile(r"detalle|contexto|an[áa]lisis|desarrollo|discusi|explicaci", re.I)
    # Secciones ## (o del nivel superior presente) que son de análisis.
    niveles = [_nivel(b) for b in bloques]
    tops = [n for n in niveles if n]
    if not tops:
        return None
    top = min(tops)
    grupos, en_analisis, actual = [], False, None
    for i, b in enumerate(bloques):
        nv = niveles[i]
        if nv == top:
            en_analisis = bool(prioridad.search(b))
            actual = None
            if en_analisis:
                actual = [i, []]
                grupos.append(actual)
            continue
        if not en_analisis:
            continue
        if nv:                      # subtema ### dentro del análisis
            actual = [i, []]
            grupos.append(actual)
        elif actual is not None and not re.match(r"^\s*([-*+]|\d+[.)])\s", b):
            actual[1].append(i)
    candidatos = [g for g in grupos if len(g[1]) > 1]
    if not candidatos:
        return None
    # del subtema con más párrafos, el último párrafo
    g = max(candidatos, key=lambda g: (len(g[1]), g[0]))
    return g[1][-1]


def _bloques_md(texto_md: str):
    """Divide markdown en bloques (párrafos / listas / tablas) separados por línea en blanco."""
    bloques, actual = [], []
    for line in (texto_md or "").splitlines():
        if line.strip():
            actual.append(line)
        elif actual:
            bloques.append("\n".join(actual))
            actual = []
    if actual:
        bloques.append("\n".join(actual))
    return bloques


def _crear_marca_agua_enersinc(ruta: Path) -> Path:
    """PNG transparente: ENERSINC + CONFIDENCIAL pequeños y casi invisibles."""
    from PIL import Image, ImageDraw, ImageFont

    w, h = 1400, 1800
    img = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    capa = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    draw = ImageDraw.Draw(capa)
    try:
        font_e = ImageFont.truetype("arial.ttf", 28)
        font_c = ImageFont.truetype("arial.ttf", 16)
    except OSError:
        font_e = ImageFont.load_default()
        font_c = font_e
    # Gris muy tenue (alpha ~6%): se nota al mirar de reojo, no tapa el texto.
    color = (100, 116, 139, 16)
    for y in range(-80, h + 80, 320):
        for x in range(-120, w + 120, 420):
            draw.text((x, y), "ENERSINC", font=font_e, fill=color)
            draw.text((x + 8, y + 32), "CONFIDENCIAL", font=font_c, fill=color)
    rota = capa.rotate(28, expand=False, fillcolor=(255, 255, 255, 0))
    img = Image.alpha_composite(img, rota)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    img.save(ruta, "PNG")
    return ruta


def _marca_agua_path():
    if (os.environ.get("PDF_MARCA_AGUA", "1").strip().lower() in {"0", "false", "no"}):
        return None
    preferida = ASSETS_DIR / "marca-agua-enersinc.png"
    # Siempre regenerar si se pide forzar, o crear si falta.
    forzar = os.environ.get("PDF_MARCA_AGUA_FORZAR", "").strip().lower() in {"1", "true", "si", "sí"}
    if preferida.is_file() and not forzar:
        return preferida
    try:
        return _crear_marca_agua_enersinc(preferida)
    except Exception:
        return None


def _font_paths():
    """Rutas a TTF locales (Montserrat títulos + Lato cuerpo)."""
    fonts = ASSETS_DIR / "fonts"
    return {
        "lato_regular": fonts / "Lato-Regular.ttf",
        "lato_bold": fonts / "Lato-Bold.ttf",
        "lato_italic": fonts / "Lato-Italic.ttf",
        "mont_regular": fonts / "Montserrat-Regular.ttf",
        "mont_bold": fonts / "Montserrat-Bold.ttf",
    }


def _font_faces_css() -> str:
    """@font-face para xhtml2pdf (TTF locales con acentos españoles)."""
    fp = _font_paths()
    parts = []
    mapping = [
        ("Lato", fp["lato_regular"], "normal", "normal"),
        ("Lato", fp["lato_bold"], "bold", "normal"),
        ("Lato", fp["lato_italic"], "normal", "italic"),
        ("Montserrat", fp["mont_regular"], "normal", "normal"),
        ("Montserrat", fp["mont_bold"], "bold", "normal"),
    ]
    for family, path, weight, style in mapping:
        if path.is_file():
            parts.append(
                f"@font-face {{ font-family: '{family}'; src: url('{path.as_uri()}'); "
                f"font-weight: {weight}; font-style: {style}; }}"
            )
    return "\n  ".join(parts)


def _register_pdf_fonts():
    """Registra TTF en ReportLab para que xhtml2pdf las incruste de forma fiable."""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except Exception:
        return
    fp = _font_paths()
    pairs = [
        ("Lato", fp["lato_regular"]),
        ("Lato-Bold", fp["lato_bold"]),
        ("Lato-Italic", fp["lato_italic"]),
        ("Montserrat", fp["mont_regular"]),
        ("Montserrat-Bold", fp["mont_bold"]),
    ]
    for name, p in pairs:
        if not p.is_file():
            continue
        try:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, str(p)))
        except Exception:
            pass
    try:
        from reportlab.pdfbase.pdfmetrics import registerFontFamily
        registerFontFamily(
            "Lato",
            normal="Lato",
            bold="Lato-Bold",
            italic="Lato-Italic",
            boldItalic="Lato-Bold",
        )
        registerFontFamily(
            "Montserrat",
            normal="Montserrat",
            bold="Montserrat-Bold",
            italic="Montserrat",
            boldItalic="Montserrat-Bold",
        )
    except Exception:
        pass


def _ids_citados(texto_md: str):
    return {int(n) for n in re.findall(r"\[(\d{1,2})\]", texto_md or "")}


_ENTIDADES = (
    ("gestornormativo.creg.gov.co", "CREG – Gestor Normativo"), ("creg.gov.co", "CREG"),
    ("xm.com.co", "XM"), ("upme.gov.co", "UPME"), ("minenergia.gov.co", "Ministerio de Minas y Energía"),
    ("superservicios.gov.co", "Superservicios"), ("secretariasenado.gov.co", "Secretaría del Senado"),
    ("funcionpublica.gov.co", "Función Pública"), ("suin-juriscol.gov.co", "SUIN-Juriscol"),
    ("presidencia.gov.co", "Presidencia"), ("dnp.gov.co", "DNP"), ("dian.gov.co", "DIAN"),
    ("ideam.gov.co", "IDEAM"), ("banrep.gov.co", "Banco de la República"), ("sic.gov.co", "SIC"),
    ("larepublica.co", "La República"), ("portafolio.co", "Portafolio"), ("eltiempo.com", "El Tiempo"),
    ("elcolombiano.com", "El Colombiano"), ("elespectador.com", "El Espectador"),
    ("valoraanalitik.com", "Valora Analitik"), ("infobae.com", "Infobae"), ("semana.com", "Semana"),
    ("bloomberglinea.com", "Bloomberg Línea"), ("reuters.com", "Reuters"), ("bnamericas.com", "BNamericas"),
    ("elheraldo.co", "El Heraldo"), ("energiaestrategica.com", "Energía Estratégica"),
    ("pv-magazine-latam.com", "pv magazine Latam"), ("asocodis.org.co", "Asocodis"),
    ("andeg.org", "Andeg"), ("acolgen.org.co", "Acolgen"), ("ser-colombia.org", "SER Colombia"),
    ("fedesarrollo.org.co", "Fedesarrollo"), ("iea.org", "IEA"), ("irena.org", "IRENA"),
)


def _entidad_fuente(url: str) -> str:
    host = _dominio(url)
    for dom, nombre in _ENTIDADES:
        if host == dom or host.endswith("." + dom):
            return nombre
    return host


def _titulo_referencia(f, entidad: str) -> str:
    """Título limpio para la lista de referencias (sin coletillas del portal)."""
    t = re.sub(r"\s+", " ", f.get("titulo") or "").strip()
    t = re.sub(r"\s*[–-]\s*(?:Gestor Normativo\s*/\s*)?texto oficial\s*$", "", t, flags=re.I)
    t = re.sub(r"^\s*Informe de XM sobre", "Informe sobre", t)
    # quita el nombre del medio/entidad al final: "… | Portafolio", "… - La República"
    for sep in ("|", " - ", " – ", " — "):
        if sep in t:
            cabeza, cola = t.rsplit(sep, 1)
            if len(cola) <= 40 and (_sin_acentos(cola.strip())[:6] in _sin_acentos(entidad + " " + _dominio(f.get("url_final") or ""))
                                    or len(cola.split()) <= 3):
                t = cabeza.strip()
    if not t:
        t = entidad
    if len(t) > 60:
        t = t[:57].rsplit(" ", 1)[0] + "…"
    return t


def _html_fuentes(fuentes, verificacion="") -> str:
    """Bloque 'Referencias': lista numerada compacta (título — entidad · fecha — enlace).
    La verificación automática NO se imprime (queda en BD, .md y consola)."""
    if not fuentes:
        return ""
    filas = []
    for f in fuentes:
        url = f.get("url_final") or f.get("url") or ""
        entidad = _entidad_fuente(url)
        titulo = _titulo_referencia(f, entidad) + (" (derogada)" if f.get("derogada") else "")
        fecha = ""
        if f.get("fecha"):
            try:
                fecha = datetime.strptime(f["fecha"], "%Y-%m-%d").strftime("%d/%m/%Y")
            except ValueError:
                fecha = f["fecha"]
        meta = entidad + (f", {fecha}" if fecha else "")
        # la fila debe caber en una línea (~128 caracteres a 7.5 pt)
        presupuesto = max(40, 126 - len(titulo) - len(meta) - 8)
        visible = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
        if len(visible) > presupuesto:
            host, _, ruta = visible.partition("/")
            ultimo = ruta.rsplit("/", 1)[-1]
            espacio = max(12, presupuesto - len(host) - 3)
            if len(ultimo) > espacio:
                ultimo = ultimo[:espacio - 1] + "…"
            visible = f"{host}/…/{ultimo}" if "/" in ruta else f"{host}/{ultimo}"
        filas.append(
            f'<tr><td class="r-n">[{f["id"]}]</td>'
            f'<td class="r-t">{_esc(titulo)} — <span class="r-e">{_esc(meta)}</span> — '
            f'<a href="{_esc(url)}">{_esc(visible)}</a></td></tr>'
        )
    return ('<div class="refs"><div class="r-tit">Referencias</div>'
            '<table class="r-tab">' + "".join(filas) + "</table></div>")


def _html_graficas(graficas) -> str:
    """Una gráfica por fila, con aire entre ellas (evita el bloque 2x2 apretado)."""
    if not graficas:
        return ""
    imagenes = graficas.get("imagenes") or [{
        "ruta": graficas["ruta"], "ancho_pt": graficas["ancho_pt"],
        "alto_pt": graficas["alto_pt"], "titulo": "",
    }]
    desde = graficas["desde"].strftime("%d/%m/%Y")
    hasta = graficas["hasta"].strftime("%d/%m/%Y")
    consultado = graficas["consultado"].strftime("%d/%m/%Y")
    nota = (f"Fuente: XM, Sinergox (API XM, servapibi.xm.com.co). Series del {desde} al {hasta}; "
            f"consultado el {consultado}. Elaboración: Enersinc.")
    if graficas.get("preliminares_omitidos"):
        nota += " Se omiten los últimos días de demanda por datos preliminares incompletos."
    filas = []
    for img in imagenes:
        filas.append(
            '<tr><td class="g-cell">'
            f'<img src="{Path(img["ruta"]).resolve().as_uri()}" '
            f'width="{img["ancho_pt"]}" height="{img["alto_pt"]}" />'
            "</td></tr>"
        )
    return (
        '<div class="graf"><div class="r-tit">Indicadores del mercado (datos XM · Sinergox)</div>'
        '<table class="g-tab">' + "".join(filas) + "</table>"
        f'<div class="cita">{_esc(nota)}</div></div>'
    )


def _html_informe(cuerpo_md, titulo, prompt_usuario, correo_destino, fecha_txt,
                  referencia, max_pages, font_pt, nota_recorte="", fuentes=None,
                  verificacion="", graficas=None):
    """HTML del PDF. `nota_recorte` y `verificacion` se aceptan por compatibilidad
    pero ya no se imprimen (quedan en BD/.md/consola)."""
    fuentes = fuentes or []
    urls = {f["id"]: (f.get("url_final") or f.get("url") or "") for f in fuentes}
    # Citas [n] -> marcador neutro para que Markdown no las trate como enlaces.
    cuerpo_md = re.sub(r"\[(\d{1,2})\]", "\u27e6\\1\u27e7", cuerpo_md or "")
    cuerpo_html = markdown.markdown(
        cuerpo_md, extensions=["tables", "fenced_code", "sane_lists"]
    )

    def _ref(m):
        n = int(m.group(1))
        if urls.get(n):
            return f'<a class="ref" href="{_esc(urls[n])}">[{n}]</a>'
        return f'<span class="ref">[{n}]</span>'
    cuerpo_html = re.sub("\u27e6(\\d{1,2})\u27e7", _ref, cuerpo_html)
    cuerpo_html = re.sub(r"<em>\s*\(no verificado\)\s*</em>|\(no verificado\)",
                         '<span class="nv">(no verificado)</span>', cuerpo_html)
    fuentes_html = _html_fuentes(fuentes)
    logo = _logo_path()
    if logo:
        logo_p1 = f'<img src="{logo.as_uri()}" style="height: 17px; width: auto;" />'
        logo_p2 = f'<img src="{logo.as_uri()}" style="height: 11px; width: auto;" />'
    else:
        logo_p1 = '<span style="color:#7dd3fc;font-weight:bold;font-size:11pt;">ENERSINC</span>'
        logo_p2 = '<span style="color:#7dd3fc;font-weight:bold;font-size:8pt;">ENERSINC</span>'

    small = font_pt - 1.5
    # Título del header: tamaño adaptable y recorte suave para no desbordar la banda.
    titulo_hdr = re.sub(r"\s+", " ", titulo or "").strip()
    if len(titulo_hdr) > 95:
        titulo_hdr = titulo_hdr[:92].rsplit(" ", 1)[0] + "…"
    hdr_title_pt = 11 if len(titulo_hdr) <= 52 else (10 if len(titulo_hdr) <= 62 else 9)
    marca = _marca_agua_path()
    marca_css = f"background-image: url('{marca.as_uri()}');" if marca else ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  @page {{
    size: letter;
    margin: 2.1cm 1.8cm 1.85cm 1.8cm;
    {marca_css}
    @frame header_frame {{
      -pdf-frame-content: headerContent;
      left: 1.8cm; right: 1.8cm; top: 0.6cm; height: 1.35cm;
    }}
    @frame footer_frame {{
      -pdf-frame-content: footerContent;
      left: 1.8cm; right: 1.8cm; bottom: 0.55cm; height: 1.05cm;
    }}
  }}
  @page siguientes {{
    size: letter;
    margin: 1.75cm 1.8cm 1.85cm 1.8cm;
    {marca_css}
    @frame header_frame2 {{
      -pdf-frame-content: headerContent2;
      left: 1.8cm; right: 1.8cm; top: 0.6cm; height: 0.9cm;
    }}
    @frame footer_frame2 {{
      -pdf-frame-content: footerContent;
      left: 1.8cm; right: 1.8cm; bottom: 0.55cm; height: 1.05cm;
    }}
  }}
  {_font_faces_css()}
  body {{ font-family: Lato, Helvetica, Arial, sans-serif; color: #1e293b;
          font-size: {font_pt}pt; line-height: 1.4; }}
  .hdr {{ width: 100%; background-color: #0f172a; }}
  .hdr td {{ vertical-align: middle; padding: 6px 10px 3px 10px; }}
  .hdr td.stripe {{ background-color: #8EC5FF; padding: 0; font-size: 1pt; height: 2px; }}
  .hdr-title {{ color: #ffffff; font-size: {hdr_title_pt}pt; font-weight: bold;
               font-family: Montserrat, Helvetica, sans-serif; text-align: left;
                padding-left: 8px; border-left: 0.5px solid #334155; }}
  .hdr2 {{ width: 100%; background-color: #0f172a; }}
  .hdr2 td {{ vertical-align: middle; padding: 4px 9px 1px 9px; }}
  .hdr2 td.stripe {{ background-color: #8EC5FF; padding: 0; font-size: 1pt; height: 1px; }}
  .hdr2-title {{ color: #cbd5e1; font-size: 7.5pt; text-align: right;
                 font-family: Montserrat, Helvetica, sans-serif; }}
  .meta {{ width: 100%; background-color: #f1f5f9; border: 0.5px solid #cbd5e1;
           margin-bottom: 7px; }}
  .meta td {{ padding: 4px 8px 2px 8px; font-size: 8pt; vertical-align: top; }}
  .lbl {{ color: #64748b; font-size: 6.5pt; text-transform: uppercase; }}
  .val {{ color: #0f172a; font-weight: bold; }}
  .q {{ background-color: #eff6ff; border-left: 3px solid #0284c7;
        padding: 5px 9px 3px 9px; margin-bottom: 6px; font-size: {font_pt - 0.5}pt; }}
  .q-lbl {{ color: #0369a1; font-size: 7.5pt; font-weight: bold; letter-spacing: 0.5px; }}
  h1 {{ color: #0f172a; font-size: {font_pt + 2}pt; margin: 6px 0 2px 0;
       font-family: Montserrat, Helvetica, sans-serif; }}
  h2 {{ color: #0284c7; font-size: {font_pt + 1.5}pt; margin: 9px 0 3px 0;
        padding-bottom: 1px; border-bottom: 0.5px solid #cbd5e1;
        font-family: Montserrat, Helvetica, sans-serif; -pdf-keep-with-next: true; }}
  h3 {{ color: #1e3a5f; font-size: {font_pt + 0.5}pt; margin: 6px 0 1px 0;
       font-family: Montserrat, Helvetica, sans-serif; -pdf-keep-with-next: true; }}
  p {{ margin: 2px 0 5px 0; text-align: justify; }}
  ul, ol {{ margin: 2px 0 4px 0; padding-left: 14px; }}
  li {{ margin: 0 0 4px 0; text-align: justify; }}
  table {{ width: 100%; margin: 4px 0 6px 0; }}
  th, td {{ padding: 3px 6px; text-align: left; font-size: {small}pt; }}
  .contenido th {{ background-color: #0f172a; color: #ffffff; border: 0.5px solid #0f172a; }}
  .contenido td {{ border: 0.5px solid #cbd5e1; }}
  blockquote {{ background-color: #f0f9ff; border-left: 3px solid #0284c7;
                padding: 4px 9px; margin: 4px 0; }}
  code {{ font-family: Courier; font-size: {small}pt; }}
  .cita {{ color: #64748b; font-size: {small - 0.5}pt; }}
  a.ref, span.ref {{ color: #0369a1; font-size: {font_pt - 3}pt; text-decoration: none; }}
  .nv {{ color: #b45309; font-size: {small - 0.5}pt; font-style: italic; }}
  .graf {{ margin-top: 10px; }}
  table.g-tab {{ width: 100%; margin: 4px 0 2px 0; border: none; }}
  td.g-cell {{ padding: 8px 0 10px 0; border: none; text-align: center;
               -pdf-keep-with-next: false; }}
  .refs {{ margin-top: 8px; }}
  .r-tit {{ color: #0284c7; font-size: 8.5pt; font-weight: bold; padding-bottom: 1px;
            border-bottom: 0.5px solid #cbd5e1; margin-bottom: 2px;
            font-family: Montserrat, Helvetica, sans-serif; -pdf-keep-with-next: true; }}
  table.r-tab {{ width: 100%; margin: 0; }}
  table.r-tab td {{ font-size: 7.5pt; line-height: 1.2; color: #334155; padding: 0 2px 1px 0;
                    vertical-align: top; border: none; }}
  td.r-n {{ width: 4%; color: #0369a1; font-weight: bold; }}
  td.r-t {{ width: 96%; }}
  .r-e {{ color: #64748b; }}
  table.r-tab a {{ color: #0369a1; text-decoration: none; }}
  .ftr {{ width: 100%; border-top: 0.5px solid #cbd5e1; }}
  .ftr td {{ font-size: 7pt; color: #64748b; padding-top: 4px; }}
</style></head>
<body>
  <div id="headerContent">
    <table class="hdr"><tr>
      <td style="width: 21%;">{logo_p1}</td>
      <td style="width: 79%;"><div class="hdr-title">{_esc(titulo_hdr)}</div></td>
    </tr>
    <tr><td class="stripe" colspan="2">&nbsp;</td></tr></table>
  </div>

  <div id="headerContent2">
    <table class="hdr2"><tr>
      <td style="width: 25%;">{logo_p2}</td>
      <td style="width: 75%;"><div class="hdr2-title">{_esc(titulo_hdr)}</div></td>
    </tr>
    <tr><td class="stripe" colspan="2">&nbsp;</td></tr></table>
  </div>

  <div id="footerContent">
    <table class="ftr"><tr>
      <td style="width: 72%;">CONFIDENCIAL · Uso exclusivo de {_esc(correo_destino)}. Prohibida su reproducción o distribución. · Enersinc · Ref. {_esc(referencia)}</td>
      <td style="width: 28%; text-align: right;">Página <pdf:pagenumber /> de <pdf:pagecount /></td>
    </tr></table>
  </div>

  <pdf:nexttemplate name="siguientes" />

  <table class="meta"><tr>
    <td style="width: 25%;"><span class="lbl">Fecha y hora</span><br/><span class="val">{_esc(fecha_txt)}</span></td>
    <td style="width: 29%;"><span class="lbl">Destinatario</span><br/><span class="val">{_esc(correo_destino)}</span></td>
    <td style="width: 31%;"><span class="lbl">Referencia</span><br/><span class="val">{_esc(referencia)}</span></td>
    <td style="width: 15%;"><span class="lbl">Clasificación</span><br/><span class="val" style="color:#1e5a9a;">Confidencial</span></td>
  </tr></table>

  <div class="q">
    <span class="q-lbl">CONSULTA AL AGENTE</span><br/>{_esc(prompt_usuario)}
  </div>

  <div class="contenido">
    {cuerpo_html}
  </div>
  {_html_graficas(graficas)}
  {fuentes_html}
</body></html>"""


def _render_pdf_bytes(html: str) -> bytes:
    import io
    _register_pdf_fonts()
    buf = io.BytesIO()
    status = pisa.CreatePDF(html, dest=buf, encoding="utf-8")
    if getattr(status, "err", 0):
        raise RuntimeError("No se pudo generar el PDF (xhtml2pdf).")
    return buf.getvalue()


def _contar_paginas(pdf_bytes: bytes):
    try:
        import io
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:
        return None


def _lineas_ultima_pagina(pdf_bytes: bytes) -> int:
    """Líneas de contenido en la última página (sin encabezado ni pie). Página llena ≈ 50."""
    try:
        import io
        from pypdf import PdfReader
        txt = PdfReader(io.BytesIO(pdf_bytes)).pages[-1].extract_text() or ""
    except Exception:
        return 999
    txt = re.sub(r"CONFIDENCIAL · Uso exclusivo.*?(Ref\.\s*\S+)", " ", txt, flags=re.S)
    txt = re.sub(r"Página\s+\d+\s+de\s+\d+", " ", txt)
    return max(0, sum(1 for ln in txt.splitlines() if ln.strip()) - 1)


def markdown_a_pdf(texto_md, titulo_documento, prompt_usuario, correo_destino,
                   ruta_salida_pdf, max_pages=None, referencia="", fuentes=None,
                   verificacion="", graficas=None):
    """
    PDF confidencial: header delgado (logo + título), meta (fecha/hora Bogotá,
    destinatario, referencia, clasificación), consulta, informe, Referencias y
    footer con página X de N. Encabezado mínimo en páginas 2+.

    Ajuste a max_pages (objetivo, no tope rígido) sin notas en el PDF:
      1) 10 pt; si no cabe, 9.5 pt.
      2) Si aún desborda, se acepta UNA página extra (nunca se corta texto con sentido).
         Si la página extra queda a medias (≤30 líneas) se prueba 9.0 pt para reabsorberla.
      3) Solo si se pasaría de max_pages + 1 se quitan párrafos finales de subtemas del
         análisis (nunca resumen, puntos clave ni cierre). El .md y la BD conservan todo.
    Devuelve dict con paginas, font_pt, recortado.
    """
    max_pages = max(1, int(max_pages or MAX_PAGES))
    ahora, tz_lbl = _ahora_local()
    fecha_txt = ahora.strftime("%d/%m/%Y %I:%M %p") + f" {tz_lbl}"
    titulo = titulo_documento or "Informe Técnico Enersinc"
    referencia = referencia or ("ENR-" + ahora.strftime("%Y%m%d-%H%M"))
    cuerpo = _normalizar_md(_limpiar_respuesta(_separar_titulo(texto_md, titulo)))

    fuentes = fuentes or []

    def _intentar(cuerpo_md, font_pt, con_graficas=False):
        # Solo se listan las fuentes citadas en el texto que realmente entra en el PDF.
        citadas = _ids_citados(cuerpo_md)
        visibles = [f for f in fuentes if f["id"] in citadas]
        html = _html_informe(cuerpo_md, titulo, prompt_usuario, correo_destino,
                             fecha_txt, referencia, max_pages, font_pt, "", visibles,
                             graficas=graficas if con_graficas else None)
        data = _render_pdf_bytes(html)
        return data, _contar_paginas(data) or 1

    # El tamaño de letra y el recorte se deciden solo con el texto (como siempre);
    # las gráficas se suman después, así el informe no pierde contenido por ellas.
    intentos = {}
    elegido = None
    for font_pt in _ESCALAS_FUENTE:
        intentos[font_pt] = _intentar(cuerpo, font_pt)
        if intentos[font_pt][1] <= max_pages:
            elegido = font_pt
            break
    if elegido is None:
        # Con 1 página pedida se admite bajar a 9.0 pt para encajar.
        if max_pages == 1:
            intentos[_FUENTE_MINIMA] = _intentar(cuerpo, _FUENTE_MINIMA)
        # Mismo número de páginas -> la fuente más grande (más legible).
        elegido = min(intentos, key=lambda pt: (intentos[pt][1], -pt))
        data, paginas = intentos[elegido]
        # Página final casi vacía: probar 9.0 pt para reabsorberla.
        if paginas > max_pages and _FUENTE_MINIMA not in intentos and _lineas_ultima_pagina(data) <= 30:
            intentos[_FUENTE_MINIMA] = _intentar(cuerpo, _FUENTE_MINIMA)
            if intentos[_FUENTE_MINIMA][1] < paginas:
                elegido = _FUENTE_MINIMA
    data, paginas = intentos[elegido]
    font_usado, recortado = elegido, False
    cuerpo_final = cuerpo

    if paginas > max_pages + 1:
        bloques = _bloques_md(cuerpo)
        while paginas > max_pages + 1:
            idx = _recortar_inteligente(bloques)
            if idx is None:
                break             # sin candidato seguro: se deja fluir antes que cortar sentido
            del bloques[idx]
            cuerpo_final = "\n\n".join(bloques)
            data, paginas = _intentar(cuerpo_final, font_usado)
            recortado = True

    if graficas:
        data, paginas = _intentar(cuerpo_final, font_usado, con_graficas=True)

    with open(ruta_salida_pdf, "wb") as fh:
        fh.write(data)
    return {"paginas": paginas, "font_pt": font_usado, "recortado": recortado,
            "fecha": fecha_txt, "referencia": referencia,
            "palabras_pdf": _contar_palabras(cuerpo)}


# ---------------------------------------------------------------------------
# INFORMES CON FUENTES VERIFICABLES (comportamiento por defecto y permanente)
#   1) Investigación: Gemini en Vertex con Google Search (grounding) orientado a
#      fuentes oficiales + descarga directa de páginas oficiales (Gestor Normativo
#      CREG, XM, Secretaría del Senado, ...) y de las URLs que devuelve el grounding.
#   2) Redacción: el modelo solo puede usar la EVIDENCIA numerada y debe poner [n]
#      en cada dato; lo que no tenga fuente se marca "(no verificado)".
#   3) Validación en código: citas inexistentes, frases con cifras/fechas/normas sin
#      cita, cifras que no aparecen en la fuente citada, enlaces caídos. Una ronda de
#      corrección con el modelo y, si persiste, se marca "(no verificado)".
# ---------------------------------------------------------------------------
import json
import html as _html_mod
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

GROUNDED_MODEL = os.environ.get("GROUNDED_MODEL") or "gemini-3.1-pro-preview"
# La investigación con búsqueda web usa un modelo rápido. La redacción sigue en GROUNDED_MODEL.
RESEARCH_MODEL = os.environ.get("RESEARCH_MODEL") or "gemini-2.5-flash"
# La corrección tras la validación reescribe todo el informe; con el modelo pro superaba el timeout.
CORRECTION_MODEL = os.environ.get("CORRECTION_MODEL") or RESEARCH_MODEL
CORRECTION_TIMEOUT_SECONDS = int(os.environ.get("CORRECTION_TIMEOUT_SECONDS") or "90")
FAST_REPORT = os.environ.get("FAST_REPORT", "").strip().lower() in {"1", "true", "si", "sí"}
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION") or "global"
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT_SECONDS") or "20")
LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS") or "150")
RESEARCH_TIMEOUT_SECONDS = int(os.environ.get("RESEARCH_TIMEOUT_SECONDS") or "70")
MAX_FUENTES_EVIDENCIA = int(os.environ.get("MAX_FUENTES_EVIDENCIA") or "24")
MAX_FUENTES_INFORME = int(os.environ.get("MAX_FUENTES_INFORME") or "12")
# Palabras que se reservan en el PDF para el bloque "Referencias".
PALABRAS_RESERVA_FUENTES = int(os.environ.get("PALABRAS_RESERVA_FUENTES") or "50")
CREG_DOCS = "https://gestornormativo.creg.gov.co/gestor/entorno/docs/"
CREG_NOVEDADES = ("https://gestornormativo.creg.gov.co/gestor/entorno/"
                  "novedades_regulacion_servicio_energia_electrica.html")
XM_NOTICIAS = "https://www.xm.com.co/noticias"

_HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "es-CO,es;q=0.9,en;q=0.6",
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
}

DOMINIOS_OFICIALES = (
    "creg.gov.co", "xm.com.co", "upme.gov.co", "minenergia.gov.co",
    "superservicios.gov.co", "secretariasenado.gov.co", "funcionpublica.gov.co",
    "suin-juriscol.gov.co", "presidencia.gov.co", "dnp.gov.co", "dian.gov.co",
    "sic.gov.co", "ideam.gov.co", "ungrd.gov.co", "minhacienda.gov.co",
    "anh.gov.co", "anla.gov.co", "cra.gov.co", "corteconstitucional.gov.co",
    "consejodeestado.gov.co", "imprenta.gov.co", "senado.gov.co", "camara.gov.co",
    "minambiente.gov.co", "mincit.gov.co", "datos.gov.co", "ipse.gov.co",
)
DOMINIOS_REPUTADOS = (
    "larepublica.co", "portafolio.co", "eltiempo.com", "elcolombiano.com",
    "elespectador.com", "valoraanalitik.com", "semana.com", "bloomberglinea.com",
    "reuters.com", "bnamericas.com", "infobae.com", "elheraldo.co",
    "energiaestrategica.com", "pv-magazine-latam.com", "asocodis.org.co",
    "andeg.org", "acolgen.org.co", "naturgas.com.co", "andesco.org.co",
    "ser-colombia.org", "serColombia.org", "fedesarrollo.org.co", "banrep.gov.co",
    "dinero.com", "cambio.com.co", "lasillavacia.com", "iea.org", "irena.org",
)

_DIGITOS_VERIF = re.compile(r"\d+(?:[.,]\d+)*")


def _log(msg):
    print(msg, flush=True)


def _dominio(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _tipo_dominio(url: str) -> str:
    host = _dominio(url)
    if not host:
        return "otro"
    if any(host == d or host.endswith("." + d) for d in DOMINIOS_OFICIALES) or host.endswith(".gov.co"):
        return "oficial"
    if any(host == d.lower() or host.endswith("." + d.lower()) for d in DOMINIOS_REPUTADOS):
        return "medio"
    return "otro"


def _normalizar_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
        q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "gclid", "fbclid"))]
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(q), ""))
    except Exception:
        return url.strip()


# ---------------------------- Cliente Gemini --------------------------------
_GENAI_CLIENT = None
_GENAI_LOCK = threading.Lock()


def _genai_client():
    """Vertex AI con la service account (GOOGLE_APPLICATION_CREDENTIALS) si existe;
    si no, API key de Gemini."""
    global _GENAI_CLIENT
    with _GENAI_LOCK:
        if _GENAI_CLIENT is not None:
            return _GENAI_CLIENT
        from google import genai
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            guardadas = {k: os.environ.pop(k) for k in ("GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY")
                         if k in os.environ}
            try:
                _GENAI_CLIENT = genai.Client(
                    vertexai=True,
                    project=os.environ.get("GOOGLE_CLOUD_PROJECT") or CES_PROJECT_ID,
                    location=VERTEX_LOCATION,
                )
            finally:
                os.environ.update(guardadas)
        else:
            key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_GENAI_API_KEY")
            if not key:
                raise RuntimeError("Se requiere GOOGLE_APPLICATION_CREDENTIALS (Vertex) o GOOGLE_API_KEY.")
            _GENAI_CLIENT = genai.Client(api_key=key)
        return _GENAI_CLIENT


def _llm(prompt: str, model=None, buscar=False, temperature=0.2, intentos=2, timeout_s=None):
    """Llamada a Gemini con reintentos. buscar=True activa Google Search grounding."""
    from google.genai import types
    espera = int(timeout_s or LLM_TIMEOUT_SECONDS)
    cfg = dict(
        temperature=temperature,
        http_options=types.HttpOptions(timeout=espera * 1000),
    )
    cfg["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
    if buscar:
        cfg["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    ultimo = None
    for i in range(intentos):
        try:
            return _genai_client().models.generate_content(
                model=model or GROUNDED_MODEL, contents=prompt,
                config=types.GenerateContentConfig(**cfg))
        except Exception as exc:  # errores transitorios (429/503/red)
            ultimo = exc
            _log(f"   [LLM] intento {i + 1}/{intentos} falló: {type(exc).__name__}: {str(exc)[:160]}")
            _time.sleep(4 * (i + 1))
    raise RuntimeError(f"Gemini no respondió tras {intentos} intentos: {ultimo}")


def _texto_respuesta_llm(resp) -> str:
    try:
        if resp.text:
            return resp.text
    except Exception:
        pass
    partes = []
    for c in getattr(resp, "candidates", None) or []:
        for p in getattr(getattr(c, "content", None), "parts", None) or []:
            if getattr(p, "text", None):
                partes.append(p.text)
    return "\n".join(partes)


# ---------------------------- Descarga de páginas ---------------------------
def _decodificar(raw: bytes, encoding_hdr: str = "") -> str:
    try:  # la mayoría de portales son UTF-8 aunque el encabezado HTTP diga otra cosa
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    enc = None
    m = re.search(rb'<meta[^>]+charset=["\']?([A-Za-z0-9_-]+)', raw[:4000], re.I)
    if m:
        enc = m.group(1).decode("ascii", "ignore")
    enc = enc or encoding_hdr or "utf-8"
    try:
        s = raw.decode(enc, errors="replace")
    except LookupError:
        s = raw.decode("utf-8", errors="replace")
    return s


def _html_a_texto(raw: bytes, encoding_hdr: str = "") -> (str, str):
    s = _decodificar(raw, encoding_hdr)
    mt = re.search(r"<title[^>]*>(.*?)</title>", s, re.I | re.S)
    titulo = _html_mod.unescape(re.sub(r"\s+", " ", mt.group(1))).strip() if mt else ""
    s = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|table|section|article)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html_mod.unescape(s)
    lineas = [re.sub(r"[ \t\r\f\v\xa0]+", " ", ln).strip() for ln in s.split("\n")]
    texto = "\n".join(ln for ln in lineas if len(ln) > 2)
    return texto, titulo


_MESES_NUM = {m: i for i, m in enumerate(
    ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
     "septiembre", "octubre", "noviembre", "diciembre"), 1)}


def _fecha_publicacion(html_txt: str) -> str:
    """Fecha de publicación (YYYY-MM-DD) desde metadatos de la página, si existe."""
    if not html_txt:
        return ""
    pats = [
        r'article:published_time"\s+content="(\d{4}-\d{2}-\d{2})',
        r'content="(\d{4}-\d{2}-\d{2})[^"]*"\s+property="article:published_time"',
        r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})',
        r'<meta[^>]+name="(?:date|publishdate|pubdate|DC\.date[^"]*)"[^>]+content="(\d{4}-\d{2}-\d{2})',
    ]
    for p in pats:
        m = re.search(p, html_txt, re.I)
        if m:
            return m.group(1)
    m = re.search(r'<time[^>]+datetime="([^"]+)"', html_txt, re.I)
    if m:
        v = m.group(1)
        mi = re.match(r"(\d{4}-\d{2}-\d{2})", v)
        if mi:
            return mi.group(1)
        me = re.match(r"(\d{1,2}) de (\w+) de (\d{4})", _sin_acentos(v))
        if me and me.group(2) in _MESES_NUM:
            return f"{me.group(3)}-{_MESES_NUM[me.group(2)]:02d}-{int(me.group(1)):02d}"
    return ""


def _pdf_a_texto(raw: bytes) -> str:
    try:
        import io
        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(raw))
        return "\n".join((pg.extract_text() or "") for pg in rd.pages[:20])
    except Exception:
        return ""


def _descargar(url: str, max_bytes=4_000_000, guardar_html=False) -> dict:
    """GET con redirecciones. Devuelve estado del enlace + texto plano."""
    out = {"url": url, "url_final": url, "status": None, "ok": False, "texto": "",
           "titulo": "", "error": "", "fecha": ""}

    def _get(verify=True):
        r = requests.get(url, headers=_HTTP_HEADERS, timeout=HTTP_TIMEOUT,
                         allow_redirects=True, stream=True, verify=verify)
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > max_bytes:
                break
        r.close()
        return r, data

    try:
        try:
            r, data = _get()
        except requests.exceptions.SSLError:
            # Algunos portales .gov.co (Función Pública, SUIN) tienen cadena TLS incompleta.
            if not _dominio(url).endswith(".gov.co"):
                raise
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r, data = _get(verify=False)
        out["status"] = r.status_code
        out["url_final"] = r.url
        out["ok"] = r.status_code < 400
        ctype = (r.headers.get("content-type") or "").lower()
        if out["ok"]:
            if "pdf" in ctype or r.url.lower().split("?")[0].endswith(".pdf"):
                out["texto"] = _pdf_a_texto(data)
            else:
                out["texto"], out["titulo"] = _html_a_texto(data, r.encoding or "")
                crudo = _decodificar(data, r.encoding or "")
                out["fecha"] = _fecha_publicacion(crudo[:400_000])
                if guardar_html:
                    out["html"] = crudo
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return out


def _resolver_redireccion(uri: str) -> str:
    """Los enlaces de grounding de Vertex son redirecciones temporales
    (vertexaisearch.cloud.google.com/grounding-api-redirect/...): se resuelven a la URL final."""
    if "grounding-api-redirect" not in (uri or ""):
        return uri
    for metodo in ("head", "get"):
        try:
            r = getattr(requests, metodo)(uri, headers=_HTTP_HEADERS, timeout=HTTP_TIMEOUT,
                                          allow_redirects=False)
            loc = r.headers.get("Location") or r.headers.get("location")
            if loc:
                return loc
        except Exception:
            continue
    return ""


def _limpiar_titulo(titulo: str) -> str:
    t = re.sub(r"\s+", " ", titulo or "").strip()
    t = re.sub(r"^Alejandr[ií]a\s*-\s*", "", t)
    t = re.sub(r"\b(\d{3})_(\d{1,3})\b", lambda m: f"{m.group(1)} {int(m.group(2)):03d}", t)
    t = re.sub(r"\s*[|–-]\s*(Portal XM|XM S\.A\. E\.S\.P\.)\s*$", "", t)
    return t


_RE_LISTADO = re.compile(
    r"[?&](page|items_per_page|field_[a-z_]+|find2?)=|/(comunicados|noticias|sala-de-prensa)/?$"
    r"|norma_error|/error|^https?://[^/]+/?(?:[a-z]{2}/?)?(?:index\.\w+)?$", re.I)


def _estado_enlace(info: dict) -> str:
    st = info.get("status")
    if st is None:
        return "sin respuesta"
    if st < 400:
        return "activo"
    if st in (401, 403, 429):
        return f"acceso restringido ({st})"
    return f"caído ({st})"


# ---------------------------- Selección de extractos ------------------------
_STOP = set("""
para como entre sobre desde hasta segun cuales cuales cual donde porque tiene tienen esta
estas este estos sector colombia colombiano colombiana empresa empresas puede pueden ultimas
ultimos semanas reciente recientes relevantes explica resume posible impacto implicaciones
marco recomendaciones tambien informe energia electrica electrico
""".split())


def _terminos(texto: str, maximo=40):
    palabras = re.findall(r"[a-zñ0-9]{5,}", _sin_acentos(texto))
    vistos, out = set(), []
    for p in palabras:
        raiz = p[:7]
        if p in _STOP or raiz in vistos:
            continue
        vistos.add(raiz)
        out.append(raiz)
    return out[:maximo]


_RE_MECANISMO = re.compile(
    r"sera considerad|se entiende|se define|se reconocer|se valorar|se liquida|creditos de energia|"
    r"precio promedio|participacion|(menor|mayor|igual)[^.]{0,25}\d[\d.,]*\s*(mw|kw)\b")


def _extracto(texto: str, terminos, max_chars=3200) -> str:
    """Cabecera de la norma (si aplica) + párrafos con más coincidencias, en orden."""
    if not texto:
        return ""
    lineas = [ln for ln in texto.split("\n") if len(ln) > 25]
    if not lineas:
        return texto[:max_chars]
    bajo = [_sin_acentos(ln) for ln in lineas]
    # raíces de 6 letras además de las de 7 ("generac" no casa con "generador")
    terminos = list(dict.fromkeys(list(terminos) + [t[:6] for t in terminos if len(t) >= 6 and not t.isdigit()]))
    cabecera = ""
    for i, ln in enumerate(bajo):
        if "por la cual" in ln or "por medio de la cual" in ln or "por el cual" in ln:
            cabecera = "\n".join(lineas[max(0, i - 2): i + 2])[:900]
            break
    puntajes, mecanismo = [], []
    for i, ln in enumerate(bajo):
        s = sum(ln.count(t) for t in terminos)
        if re.search(r"modificad|derogad|adicionad|sustituid|vigencia", ln):
            s += 1.5
        # definiciones, umbrales y reglas de liquidación: explican el mecanismo
        if _RE_MECANISMO.search(ln):
            ms = (s + (3 if re.search(r"sera considerad|se entiende por|se define", ln) else 0)
                  + (3 if re.search(r"creditos de energia|\bmcm\b|permuta", ln) else 0)
                  + (1.5 if re.search(r"se valorar|se liquida|se reconocer", ln) else 0)
                  + (1.5 if re.search(r"\d[\d.,]*\s*(mw|kw)\b", ln) else 0))
            mecanismo.append((ms, i))
            s += 2.5
        if re.search(r"\d", ln):
            s += 0.5
        if s > 0:
            puntajes.append((s, i))
    elegidos, total = set(), len(cabecera)
    # ~45 % del extracto se reserva para líneas de mecanismo (si las hay)
    for s, i in sorted(mecanismo, reverse=True):
        largo = min(len(lineas[i]), 900)
        if total + largo > max_chars * 0.45:
            continue
        elegidos.add(i)
        total += largo
    for s, i in sorted(puntajes, reverse=True):
        if i in elegidos:
            continue
        largo = min(len(lineas[i]), 900)
        if total + largo > max_chars:
            continue
        elegidos.add(i)
        total += largo
    cuerpo = []
    for i in sorted(elegidos):
        ln = lineas[i]
        if len(ln) > 900:
            pos = max(0, min((_sin_acentos(ln).find(t) for t in terminos if t in _sin_acentos(ln)), default=0) - 300)
            ln = "…" + ln[pos:pos + 880] + "…"
        cuerpo.append(ln)
    if not cuerpo and not cabecera:
        return "\n".join(lineas)[:max_chars]
    return (cabecera + ("\n…\n" if cabecera and cuerpo else "") + "\n".join(cuerpo)).strip()


# ---------------------------- Normas citadas --------------------------------
_RE_NORMAS = [
    ("creg", re.compile(r"CREG\s*(?:No\.?\s*)?(\d{3}\s?\d{3}|\d{1,3})\s*(?:de|del|/|-)\s*((?:19|20)\d{2})", re.I)),
    ("creg", re.compile(r"Resoluci[oó]n(?:es)?\s+(?:CREG\s+)?(?:No\.?\s*)?(\d{3}\s\d{3})\s*(?:de|del)\s*(20\d{2})", re.I)),
    ("ley", re.compile(r"Ley\s+(?:No\.?\s*)?(\d{2,4})\s*(?:de|del)\s*((?:19|20)\d{2})", re.I)),
    ("decreto", re.compile(r"Decreto\s+(?:No\.?\s*)?(\d{2,4})\s*(?:de|del)\s*((?:19|20)\d{2})", re.I)),
    ("circular", re.compile(r"Circular\s+(?:CREG\s+)?(?:No\.?\s*)?(\d{1,4})\s*(?:de|del)\s*(20\d{2})", re.I)),
]


def _normas_en_texto(texto: str):
    vistos, out = set(), []
    for tipo, rx in _RE_NORMAS:
        for m in rx.finditer(texto or ""):
            num, anio = re.sub(r"\D", "", m.group(1)), m.group(2)
            clave = (tipo, num.lstrip("0") or "0", anio)
            if clave not in vistos:
                vistos.add(clave)
                out.append(clave)
    return out


def _urls_norma(tipo, num, anio):
    n = int(num)
    if tipo == "creg":
        if len(num) == 6:
            return [f"{CREG_DOCS}resolucion_creg_{num[:3]}-{int(num[3:])}_{anio}.htm"]
        return [f"{CREG_DOCS}resolucion_creg_{n:04d}_{anio}.htm"]
    if tipo == "ley":
        return [f"http://www.secretariasenado.gov.co/senado/basedoc/ley_{n:04d}_{anio}.html",
                f"{CREG_DOCS}ley_{n}_{anio}.htm"]
    if tipo == "decreto":
        return [f"{CREG_DOCS}decreto_{n}_{anio}.htm",
                f"https://normas.cra.gov.co/gestor/docs/decreto_{n}_{anio}.htm",
                f"http://www.secretariasenado.gov.co/senado/basedoc/decreto_{n:04d}_{anio}.html"]
    if tipo == "circular":
        return [f"{CREG_DOCS}circular_creg_{n:04d}_{anio}.htm"]
    return []


def _etiqueta_norma(tipo, num, anio):
    if tipo == "creg":
        txt = f"{num[:3]} {num[3:]}" if len(num) == 6 else num
        return f"Resolución CREG {txt} de {anio}"
    return f"{tipo.capitalize()} {num} de {anio}"


# ---------------------------- Semillas oficiales ----------------------------
_RE_TEMA_REGULATORIO = re.compile(r"creg|resoluci|regulac|normativ|norma|\bley\b|decreto|circular|marco|reglament|autogener|generacion distribuida", re.I)
_RE_TEMA_ACTUALIDAD = re.compile(r"noticia|actualidad|ultimas semanas|ultimos meses|reciente", re.I)
# Normas troncales que se descargan siempre del texto oficial cuando la pregunta trata el tema
# (la redacción sigue limitada a lo que diga el texto descargado).
_SEMILLAS_NORMAS = (
    (re.compile(r"autogener|generacion distribuida|\bagpe\b|\bagge\b|excedentes|creditos de energia", re.I),
     [("creg", "174", "2021"), ("creg", "101072", "2025"), ("ley", "1715", "2014"), ("ley", "2099", "2021")]),
)
_RE_TEMA_MERCADO = re.compile(r"precio|bolsa|embalse|hidrolog|aporte|demanda|noticia|actualidad|mercado|\bxm\b|nino|generacion|tarifa|escasez|racionamiento", re.I)


def _novedades_creg(pregunta: str):
    """Lista cronológica de normas recientes (Gestor Normativo CREG) y las más relevantes."""
    info = _descargar(CREG_NOVEDADES, max_bytes=3_000_000, guardar_html=True)
    if not info["ok"]:
        return None, []
    anio = _ahora_local()[0].year
    raw = info.get("html", "")
    entradas = []
    for m in re.finditer(r'<div[^>]+class="opcion-nueva"><a href="([^"]+)".*?class="id-documento">(.*?)</div>'
                         r'.*?class="descripcion-documento">(.*?)</div>', raw, re.S):
        ident = _html_mod.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(2))).strip()
        desc = _html_mod.unescape(re.sub(r"<[^>]+>", " ", m.group(3)))
        desc = re.sub(r"\s+", " ", desc).strip()
        if not re.search(rf"\b({anio}|{anio - 1})\b", ident):
            continue
        if re.search(r"Resoluci.n\s+50\d\s", ident):
            continue   # resoluciones particulares (una empresa): se omiten del listado
        entradas.append((ident, desc, m.group(1)))
        if len(entradas) >= 80:
            break
    if not entradas:
        return None, []
    genericos = {"resoluc", "recient", "impact", "implica", "colomb", "energia", "electri", "creg"}
    terms = [t for t in _terminos(pregunta) if t not in genericos] + ["comercializ", "usuario", "bolsa", "mercado",
                                                                       "nino", "escasez", "precio"]
    puntuadas = []
    for pos, (ident, desc, href) in enumerate(entradas):
        d = _sin_acentos(desc)
        s = sum(2 for t in terms if t in d)
        if re.search(r"Resoluci.n 101 ", ident):
            s += 2           # resoluciones generales
        if re.search(r"Proyecto", ident):
            s -= 3           # proyectos de resolución (no vigentes)
        s -= pos * 0.03      # recencia
        puntuadas.append((s, ident))
    relevantes = [i for s, i in sorted(puntuadas, reverse=True) if s > 0][:6]
    texto = "\n".join(f"- {i}: {d[:260]}" for i, d, _ in entradas[:45])
    fuente = {"url": CREG_NOVEDADES, "url_final": CREG_NOVEDADES, "status": info["status"], "ok": True,
              "titulo": "CREG – Gestor Normativo: Novedades de regulación de energía eléctrica",
              "texto": texto, "extracto": texto[:6000], "origen": "semilla"}
    normas = []
    for ident in relevantes:
        mm = re.search(r"(Resoluci.n|Circular)\s+(\d{3}\s?\d{3}|\d{1,4})\s+de\s+(20\d{2})", ident)
        if mm:
            tipo = "creg" if mm.group(1).lower().startswith("resol") else "circular"
            normas.append((tipo, re.sub(r"\D", "", mm.group(2)), mm.group(3)))
    return fuente, normas


def _noticias_xm(pregunta: str, paginas=5, maximo=4):
    """Últimos informes/comunicados de XM sobre variables del mercado."""
    rx_util = re.compile(r"informe|variables|mercado|embalse|aporte|demanda|precio|hidrolog|nino|generaci|escasez|consumo|reservas", re.I)
    vistos, cand = set(), []
    for p in range(paginas):
        info = _descargar(f"{XM_NOTICIAS}?page={p}", max_bytes=1_500_000, guardar_html=True)
        if not info["ok"]:
            continue
        raw = info.get("html", "")
        for m in re.finditer(r'field-tags">([^<]*)</span>.*?href="(/noticias/\d+[^"]*)".*?class="title">([^<]*)</span>'
                             r'.*?datetime="([^"]+)"', raw, re.S):
            tag, href, titulo, fecha = m.groups()
            if href in vistos or "Agentes" in tag:
                continue
            vistos.add(href)
            if rx_util.search(_sin_acentos(titulo)):
                cand.append((fecha, "https://www.xm.com.co" + href, titulo.strip()))
        if len(cand) >= maximo:
            break
    cand.sort(reverse=True)
    return [(u, t) for _, u, t in cand[:maximo]]


# ---------------------------- Investigación ---------------------------------
def _fecha_hoy_txt():
    meses = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
             "septiembre", "octubre", "noviembre", "diciembre")
    hoy, _ = _ahora_local()
    return f"{hoy.day} de {meses[hoy.month - 1]} de {hoy.year}"


def _prompt_investigacion(pregunta: str, enfoque: str, contexto: str = "") -> str:
    fecha = _fecha_hoy_txt()
    max_busquedas = 4 if FAST_REPORT else 6
    rango_hechos = "6 y 10" if FAST_REPORT else "8 y 14"
    oficiales = ("gestornormativo.creg.gov.co y creg.gov.co (resoluciones, circulares, proyectos), "
                 "xm.com.co (informes mensuales del mercado: precio de bolsa, embalses, aportes, generación, demanda), "
                 "upme.gov.co, minenergia.gov.co, superservicios.gov.co, y para leyes y decretos "
                 "secretariasenado.gov.co, funcionpublica.gov.co o suin-juriscol.gov.co")
    if enfoque == "oficial":
        guia = (f"Busca SOLO en fuentes oficiales colombianas: {oficiales}. Usa consultas con el operador "
                "site: (p. ej. site:gestornormativo.creg.gov.co, site:xm.com.co). Incluye las normas vigentes "
                "con sus modificaciones posteriores y los datos oficiales más recientes.")
    else:
        guia = ("Busca hechos recientes (últimas semanas y meses) en fuentes oficiales y en medios o gremios "
                "reconocidos (La República, Portafolio, El Tiempo, El Colombiano, Valora Analitik, Asocodis, "
                "Andeg, Acolgen, SER Colombia). Prefiere siempre la fuente oficial cuando exista.")
    return (
        f"FECHA ACTUAL: {fecha} (Colombia). 'Reciente' significa los últimos 12 meses respecto a esta fecha.\n"
        f"{guia}\n\nPREGUNTA DEL USUARIO: {pregunta.strip()}\n"
        + (f"\nPistas (no verificadas, solo para orientar la búsqueda):\n{contexto[:2500]}\n" if contexto else "")
        + f"\nHaz como máximo {max_busquedas} búsquedas web y detente. "
        f"Devuelve entre {rango_hechos} HECHOS verificables, "
        "uno por línea: la norma o el dato más reciente, su contexto inmediato y la causa o el efecto "
        "que la fuente señale. Formato exacto:\n"
        "- <hecho concreto con la cifra, fecha o número de norma tal como aparece en la fuente> | "
        "<periodo o fecha del dato> | <entidad o medio> | <URL exacta de la página consultada>\n"
        "No inventes nada: si no encuentras un dato con fuente, no lo incluyas."
    )


def _investigar_grounded(pregunta: str, enfoque: str, contexto: str = ""):
    """Una búsqueda con grounding. Devuelve (texto, [(uri, titulo)], [(segmento, [idx])])."""
    t0 = _time.time()
    try:
        resp = _llm(
            _prompt_investigacion(pregunta, enfoque, contexto),
            model=RESEARCH_MODEL, buscar=True, intentos=2, timeout_s=RESEARCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _log(f"   [investigación {enfoque}] error: {exc}")
        return "", [], []
    texto = _texto_respuesta_llm(resp)
    chunks, soportes = [], []
    try:
        gm = resp.candidates[0].grounding_metadata
        for ch in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(ch, "web", None)
            if web and getattr(web, "uri", None):
                chunks.append((web.uri, getattr(web, "title", "") or ""))
        for s in (getattr(gm, "grounding_supports", None) or []):
            seg = getattr(getattr(s, "segment", None), "text", "") or ""
            soportes.append((seg, list(getattr(s, "grounding_chunk_indices", None) or [])))
        consultas = getattr(gm, "web_search_queries", None) or []
        _log(f"   [investigación {enfoque}] {len(chunks)} fuentes de grounding · "
             f"{len(consultas)} búsquedas · {round(_time.time() - t0)} s")
    except Exception:
        pass
    return texto, chunks, soportes


def recopilar_evidencia(pregunta: str, borrador: str = ""):
    """Reúne fuentes reales (URL final verificada + extracto). Lista de dicts."""
    t0 = _time.time()
    fuentes = {}          # url_normalizada -> fuente
    notas_por_url = {}    # url_normalizada -> [texto de apoyo]

    def _agregar(info, origen, titulo_hint="", notas=()):
        if not info or not info.get("ok"):
            return None
        if origen in ("grounding", "modelo") and _RE_LISTADO.search(info.get("url_final", "")):
            return None   # redirigió a portada, listado o página de error
        clave = _normalizar_url(info["url_final"])
        f = fuentes.get(clave)
        if f is None:
            f = dict(info)
            f["origen"] = origen
            if origen == "norma" and titulo_hint:
                f["titulo"] = (f"{titulo_hint} – Gestor Normativo / texto oficial" if "gestornormativo" in info["url_final"]
                               else f"{titulo_hint} – texto oficial")
            else:
                f["titulo"] = _limpiar_titulo(info.get("titulo") or titulo_hint or _dominio(info["url_final"]))
            fuentes[clave] = f
        for n in notas:
            if n:
                notas_por_url.setdefault(clave, []).append(n)
        return f

    # 1) Búsquedas con grounding, más semillas oficiales.
    # En modo rápido se usa una sola investigación oficial; conserva trazabilidad y
    # evita pagar el tiempo de una segunda búsqueda general.
    tema_reg = bool(_RE_TEMA_REGULATORIO.search(_sin_acentos(pregunta))
                    or _RE_TEMA_ACTUALIDAD.search(_sin_acentos(pregunta)))
    tema_mer = bool(_RE_TEMA_MERCADO.search(_sin_acentos(pregunta)))
    with ThreadPoolExecutor(max_workers=4) as ex:
        fut_of = ex.submit(_investigar_grounded, pregunta, "oficial", borrador)
        fut_ge = None if FAST_REPORT else ex.submit(_investigar_grounded, pregunta, "general", borrador)
        fut_nov = ex.submit(_novedades_creg, pregunta) if tema_reg else None
        fut_xm = ex.submit(_noticias_xm, pregunta) if tema_mer else None
        res_of = fut_of.result()
        res_ge = fut_ge.result() if fut_ge else ("", [], [])
        nov_fuente, nov_normas = fut_nov.result() if fut_nov else (None, [])
        xm_urls = fut_xm.result() if fut_xm else []

    textos_inv = res_of[0] + "\n" + res_ge[0]
    # 2) URLs candidatas: grounding (redirecciones resueltas) + URLs escritas por el modelo
    #    (se descartan si no responden) + noticias XM + normas mencionadas.
    candidatas = {}   # url -> (origen, titulo_hint, [notas])
    for texto, chunks, soportes in (res_of, res_ge):
        notas_chunk = {}
        for seg, idxs in soportes:
            for i in idxs:
                notas_chunk.setdefault(i, []).append(seg)
        resueltas = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            for i, final in zip(range(len(chunks)), ex.map(lambda c: _resolver_redireccion(c[0]), chunks)):
                resueltas[i] = final
        for i, (uri, titulo) in enumerate(chunks):
            final = resueltas.get(i)
            if final and not _RE_LISTADO.search(final):
                candidatas.setdefault(final, ("grounding", titulo, []))[2].extend(notas_chunk.get(i, []))
        for linea in texto.splitlines():
            for u in re.findall(r"https?://[^\s|)\]>\"'<]+", linea):
                u = u.rstrip(".,;:")
                if "grounding-api-redirect" in u or "vertexaisearch" in u or _RE_LISTADO.search(u):
                    continue
                candidatas.setdefault(u, ("modelo", "", []))[2].append(linea)
    for u, t in xm_urls:
        candidatas.setdefault(u, ("semilla", t, []))

    # Prioridad: normas de la pregunta > 4 más relevantes de las novedades CREG >
    # normas que más menciona la investigación > resto.
    from collections import Counter
    menc = Counter()
    for n in _normas_en_texto(textos_inv):
        menc[n] += len(re.findall(re.escape(n[1][-3:]), textos_inv))
    investigadas = [n for n, _ in menc.most_common()]
    normas = []
    semillas = [n for rx, lst in _SEMILLAS_NORMAS if rx.search(_sin_acentos(pregunta)) for n in lst]
    for n in (_normas_en_texto(pregunta) + semillas + nov_normas[:4] + investigadas + nov_normas[4:]
              + _normas_en_texto(borrador)):
        if n not in normas:
            normas.append(n)
    normas = normas[:16]

    terms_base = _terminos(pregunta)

    def _bajar_candidata(item):
        url, (origen, titulo, notas) = item
        return url, origen, titulo, notas, _descargar(url)

    def _bajar_norma(n):
        for u in _urls_norma(*n):
            info = _descargar(u)
            if info["ok"] and len(info["texto"]) > 500 and "no existe" not in info["texto"][:2000].lower():
                return n, info
        return n, None

    cand_items = list(candidatas.items())[:40]
    with ThreadPoolExecutor(max_workers=10) as ex:
        res_cand = list(ex.map(_bajar_candidata, cand_items))
        res_norm = list(ex.map(_bajar_norma, normas))

    descartadas = 0
    if nov_fuente:
        _agregar(nov_fuente, "semilla")
    for n, info in res_norm:
        if info:
            lineas = [ln for ln in textos_inv.splitlines() if re.search(re.escape(n[1][-3:].lstrip("0") or n[1]), ln)]
            _agregar(info, "norma", _etiqueta_norma(*n), lineas)
    for url, origen, titulo, notas, info in res_cand:
        if info["ok"]:
            _agregar(info, origen, titulo, notas)
        elif origen == "grounding" and info.get("status") in (401, 403, 429) and notas:
            # Página existe pero bloquea bots: se conserva con el texto de apoyo del grounding.
            info = dict(info, ok=True, texto="", titulo=titulo or _dominio(url), restringida=True)
            _agregar(info, origen, titulo, notas)
        else:
            descartadas += 1

    # 3) Extractos relevantes por fuente.
    lista = []
    for clave, f in fuentes.items():
        notas = notas_por_url.get(clave, [])
        f["notas"] = notas
        if not f.get("extracto"):
            terms = terms_base + _terminos(" ".join(notas), 25)
            terms += [d for d in re.findall(r"\d{3,}", " ".join(notas))][:15]
            limite = 8000 if f.get("origen") == "norma" else 3600
            f["extracto"] = _extracto(f.get("texto", ""), terms, limite)
        if not f["extracto"] and notas:
            f["extracto"] = "Fragmentos respaldados por la búsqueda:\n" + "\n".join(notas[:6])
        if not f["extracto"]:
            continue
        f["tipo"] = _tipo_dominio(f["url_final"])
        f["enlace"] = "acceso restringido" if f.get("restringida") else _estado_enlace(f)
        if f.get("origen") == "norma" or "gestornormativo" in f["url_final"] or "basedoc" in f["url_final"]:
            f["derogada"] = _nota_derogacion(f.get("texto", ""))
            if f["derogada"]:
                _log(f"     (norma derogada {f['derogada'][:80]}: {f['titulo'][:60]})")
        lista.append(f)
    prioridad = {"oficial": 0, "medio": 1, "otro": 2}
    origen_p = {"norma": 0, "semilla": 1, "grounding": 2, "modelo": 3}
    lista.sort(key=lambda f: (prioridad.get(f["tipo"], 3), origen_p.get(f.get("origen"), 4)))
    lista = lista[:MAX_FUENTES_EVIDENCIA]
    for i, f in enumerate(lista, 1):
        f["id"] = i
    of = sum(1 for f in lista if f["tipo"] == "oficial")
    _log(f"   Evidencia: {len(lista)} fuentes ({of} oficiales) · {descartadas} URLs descartadas "
         f"(caídas o inexistentes) · {round(_time.time() - t0)} s")
    for f in lista:
        _log(f"     [{f['id']}] {f['tipo']:7s} {f['enlace']:10s} {f['url_final'][:110]}")
    return lista


def _nota_derogacion(texto: str) -> str:
    """'por el artículo 31 de la Resolución 174 de 2021' si la norma está derogada en su totalidad."""
    cab = re.sub(r"\s+", " ", (texto or "")[:6000])
    m = re.search(r"NOTA DE VIGENCIA:\s*(?:Resoluci[oó]n|Ley|Decreto|Circular)\s+derogad[ao]\s+(por [^>]{3,160})>", cab, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"^.{0,1500}?\b(?:Resoluci[oó]n|Ley|Decreto|Circular)\s+derogad[ao]\s+(por (?:el|la|los)?\s*[^.;>]{3,160})", cab, re.I)
    return m.group(1).strip() if m else ""


def _bloque_evidencia(fuentes) -> str:
    partes = []
    for f in fuentes:
        fecha = ""
        if f.get("fecha"):
            fecha = f"FECHA DE PUBLICACIÓN: {f['fecha']}"
            try:
                dias = (_ahora_local()[0].date() - datetime.strptime(f["fecha"], "%Y-%m-%d").date()).days
                if dias > 180:
                    fecha += f" (ANTIGUA: hace {dias} días; no la uses como dato reciente)"
            except ValueError:
                pass
            fecha += "\n"
        estado = ""
        if f.get("derogada"):
            estado = (f"ESTADO: NORMA DEROGADA {f['derogada']}. Úsala SOLO como antecedente histórico, en pasado "
                      "('la derogada Resolución … establecía …'); NO presentes sus definiciones, umbrales ni "
                      "procedimientos como vigentes.\n")
        partes.append(
            f"[{f['id']}] ({f['tipo']}) {f['titulo'][:150]}\nURL: {f['url_final']}\n{fecha}{estado}EXTRACTO:\n{f['extracto']}\n"
        )
    return "\n".join(partes)


# ---------------------------- Redacción con citas ---------------------------
def construir_prompt_redaccion(pregunta, fuentes, max_pages=None, borrador="") -> str:
    reglas = (
        f"Eres analista senior del mercado eléctrico colombiano en Enersinc. FECHA ACTUAL: {_fecha_hoy_txt()}.\n"
        "Redacta el informe que responde la PREGUNTA usando EXCLUSIVAMENTE la EVIDENCIA numerada de abajo.\n\n"
        "REGLAS DE FUENTES (obligatorias, sin excepción):\n"
        "a) Cada dato concreto —cifra, porcentaje, precio, fecha, número de resolución/ley/decreto/circular, nombre "
        "de entidad o empresa y cualquier afirmación fáctica— lleva al final de la frase la cita [n] (o [n][m]) de la "
        "fuente de la EVIDENCIA que lo respalda TEXTUALMENTE.\n"
        "b) Usa solo números [n] que existan en la EVIDENCIA. No cites de memoria, no escribas URLs.\n"
        "c) Si algo relevante no está en la evidencia, no lo afirmes; si es imprescindible mencionarlo, añade "
        "'(no verificado)'. Nunca inventes cifras, fechas, umbrales ni números de norma.\n"
        "d0) Ubica cada dato en el tiempo con la FECHA DE PUBLICACIÓN de su fuente: una nota publicada en 2025 "
        "que dice 'en octubre' habla de octubre de 2025, no del año actual. No presentes datos viejos como recientes.\n"
        "d) Copia las cifras EXACTAMENTE como aparecen en la fuente (mismos decimales y unidad) e indica el periodo "
        "del dato (p. ej. 'en agosto de 2026, según XM [3]'). No redondees ni combines cifras de periodos distintos.\n"
        "e) Prioriza fuentes (oficial) sobre (medio)/(otro); usa medios solo para hechos noticiosos o si no hay "
        "fuente oficial, atribuyéndolo ('según La República [5]').\n"
        "f) Normas: describe su objeto como lo dice la fuente, sin extrapolar; si la evidencia indica que fue "
        "modificada, derogada o tiene un periodo/umbral específico, dilo con su cita. Las fuentes marcadas "
        "'NORMA DEROGADA' solo sirven como antecedente (en pasado); las definiciones y umbrales vigentes deben "
        "salir de la norma vigente. Escribe los números con "
        "espacio (p. ej. 'Resolución CREG 101 124 de 2026', no '101_124').\n"
        "g) Frases de explicación, análisis o implicaciones sin datos concretos pueden ir sin cita (no hace falta "
        "citar cada frase); las recomendaciones también, salvo que mencionen una norma, cifra o fecha (entonces "
        "llevan [n]). Usa la evidencia a fondo para EXPLICAR: definiciones, mecanismo, antecedentes, causas y "
        "efectos; aprovecha varias fuentes y no te limites a enumerar datos.\n"
        "h) Responde a lo que se pregunta con los datos MÁS RECIENTES de la evidencia: si la pregunta menciona "
        "una variable (p. ej. precio de bolsa, embalses, demanda), incluye su último valor disponible con su "
        "periodo; si hay varios periodos, usa el más reciente. En preguntas normativas incluye las definiciones, "
        "umbrales y modificaciones vigentes que aparezcan en la evidencia (p. ej. límites de capacidad, cómo se "
        "liquidan o remuneran los excedentes, con el umbral de capacidad de cada caso). En preguntas sobre precios de bolsa incluye el último precio promedio "
        "mensual de bolsa disponible, su variación y los factores que lo explican (participación hidráulica/térmica "
        "en la generación total, embalses, aportes, demanda) si están en la evidencia. Describe cada porcentaje con "
        "su base correcta (p. ej. 'del total de la generación' frente a 'de la generación renovable').\n"
        f"i) Usa como máximo {MAX_FUENTES_INFORME} fuentes distintas. No incluyas sección de fuentes ni bibliografía "
        "(el sistema la agrega).\n\n"
    )
    formato = construir_prompt_agente(pregunta, max_pages, grounded=True)
    return (
        reglas + formato
        + ("\nBORRADOR PREVIO DEL AGENTE (NO es fuente; úsalo solo como guía de temas y "
           "verifica todo contra la evidencia):\n" + borrador[:3000] + "\n" if borrador else "")
        + "\n=== EVIDENCIA ===\n" + _bloque_evidencia(fuentes) + "\n=== FIN EVIDENCIA ===\n"
        "\nEscribe ahora el informe completo en Markdown con citas [n], respetando la extensión pedida."
    )


# ---------------------------- Validación ------------------------------------
_RE_CITA = re.compile(r"\[(\d{1,2}(?:\s*[,;–-]\s*\d{1,2})*)\]")
_RE_MARCADOR = re.compile(
    r"\d|%|\$|"
    r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b", re.I)
_RE_NO_VERIF = re.compile(r"no verificad", re.I)
_ABREV = re.compile(r"(?:\b[A-ZÁÉÍÓÚÑ]|\b(?:Res|No|N\.º|art|Art|arts|núm|Núm|p|ej|Sr|etc|aprox|vs|Dr|Ing|lit|num|"
                    r"inc|par|Par|S\.A|E\.S\.P|S\.A\.S|Ltda|Cía|pág|págs|Dec|min|máx|mín|aprox))\.$")


_RE_PASADO = re.compile(
    r"derog|\bfue\b|\bfueron\b|\bera\b|\beran\b|establecia|regulaba|definia|fijaba|permitia|exigia|"
    r"inicialmente|anterior|antes de|origen|historic|antecedente|expidio|regulo|establecio|reglamento|"
    r"quedo|sustituy|reemplaz|hasta (su|la) derogatoria|en su momento|primer")


def _normalizar_citas(texto: str) -> str:
    def _r(m):
        nums = re.split(r"\s*[,;]\s*", m.group(1))
        out = []
        for n in nums:
            if re.match(r"^\d+\s*[–-]\s*\d+$", n):
                a, b = [int(x) for x in re.split(r"\s*[–-]\s*", n)]
                out.extend(range(a, b + 1) if 0 < b - a < 6 else [a, b])
            else:
                out.append(int(n))
        return "".join(f"[{n}]" for n in out)
    return _RE_CITA.sub(_r, texto or "")


def _frases(linea: str):
    """Divide una línea en frases sin cortar abreviaturas; las citas sueltas al inicio
    de una frase se devuelven a la anterior."""
    partes, inicio = [], 0
    for m in re.finditer(r"[.!?](?=\s+(?:[¿¡(\"“A-ZÁÉÍÓÚÑ]|\*\*))", linea):
        antes = linea[inicio:m.end()]
        if _ABREV.search(antes.rstrip()):
            continue
        partes.append(linea[inicio:m.end()])
        inicio = m.end()
    partes.append(linea[inicio:])
    out = []
    for p in partes:
        mcita = re.match(r"^(\s*(?:\[\d{1,2}\])+)", p)
        if mcita and out:
            out[-1] += mcita.group(1)
            p = p[mcita.end():]
        if p.strip():
            out.append(p)
        elif p and out:
            out[-1] += p
    return out


def _unir_miles(t: str) -> str:
    """'24 447 techos' -> '24447' (miles separados por espacio), salvo números de norma
    ('Resolución CREG 101 124')."""
    def _r(m):
        antes = t[max(0, m.start() - 30):m.start()]
        if re.search(r"(resoluci[oó]n|creg|circular|decreto|ley|proyecto)\s*(?:no\.?\s*)?$", antes, re.I):
            return m.group(0)
        return m.group(1) + m.group(2).replace(" ", "")
    return re.sub(r"(?<!\d)(\d{1,3})((?: \d{3})+)(?![\d.,])", _r, t)


def _numeros_verificables(frase: str):
    limpio = _RE_CITA.sub(" ", frase)
    limpio = _unir_miles(limpio)
    limpio = re.sub(r"https?://\S+", " ", limpio)
    nums = []
    for tok in _DIGITOS_VERIF.findall(limpio):
        dig = re.sub(r"\D", "", tok)
        if not dig:
            continue
        try:
            val = float(tok.replace(".", "").replace(",", "."))
        except ValueError:
            val = 0
        if re.fullmatch(r"(19[89]\d|20[0-3]\d)", tok):   # años: no se exigen
            continue
        if val < 10 and len(dig) == 1:
            continue
        nums.append(tok)
    return nums


def _candidatos_numero(tok: str):
    dig = re.sub(r"\D", "", tok)
    c = {dig.lstrip("0") or "0"}
    grupos = re.split(r"[.,]", tok)
    if len(grupos) > 1 and len(grupos[-1]) in (1, 2):
        ent = "".join(grupos[:-1])
        c.add(ent.lstrip("0") or "0")
    return c


def _numeros_de_fuente(f) -> set:
    if "_nums" not in f:
        texto = (f.get("texto") or "") + "\n" + (f.get("extracto") or "") + "\n" + "\n".join(f.get("notas") or [])
        s = set()
        for tok in _DIGITOS_VERIF.findall(texto):
            s |= _candidatos_numero(tok)
        # números separados por espacio/guion/guion bajo (p. ej. 101 124, 101-124, 101_124)
        for a, b in re.findall(r"(\d{3})[\s_-](\d{2,3})\b", texto):
            s.add(a + b.zfill(3))
        f["_nums"] = s
    return f["_nums"]


def _seccion_de(lineas, idx):
    for j in range(idx, -1, -1):
        m = re.match(r"^#{1,6}\s+(.+)$", lineas[j].strip())
        if m:
            return _sin_acentos(m.group(1))
    return ""


def validar_informe(texto_md: str, fuentes, verificar_numeros=True):
    """Analiza el informe. Devuelve dict con problemas por frase y estadísticas."""
    por_id = {f["id"]: f for f in fuentes}
    texto_md = _normalizar_citas(texto_md)
    lineas = texto_md.splitlines()
    problemas, stats = [], {"datos": 0, "con_fuente": 0, "no_verificados": 0, "citas_invalidas": 0}
    for i, linea in enumerate(lineas):
        st = linea.strip()
        if not st or re.match(r"^#{1,6}\s", st) or re.match(r"^\|?\s*-{3,}", st):
            continue
        for fr in _frases(re.sub(r"^\s*([-*+]|\d{1,2}[.)])\s+", "", linea)):
            if not re.search(r"\w", _RE_CITA.sub("", fr)):
                continue
            citas = [int(c) for c in re.findall(r"\[(\d{1,2})\]", fr)]
            invalidas = [c for c in citas if c not in por_id]
            validas = [c for c in citas if c in por_id]
            stats["citas_invalidas"] += len(invalidas)
            sin_vineta = re.sub(r"^\s*([-*+]|\d{1,2}[.)])\s+", "", _RE_CITA.sub(" ", fr))
            # un año o rango de años suelto ("El Niño 2026-2027") no convierte la frase en dato
            sin_vineta = re.sub(r"\b(?:19|20)\d{2}(?:\s*[-–/]\s*(?:19|20)?\d{2,4})?\b", " ", sin_vineta)
            tiene_marcador = bool(_RE_MARCADOR.search(sin_vineta))
            if not tiene_marcador and not validas:
                continue
            stats["datos"] += 1
            if _RE_NO_VERIF.search(fr):
                stats["no_verificados"] += 1
                continue
            motivo = ""
            if invalidas and not validas:
                motivo = f"cita inexistente {invalidas}"
            elif not validas:
                motivo = "dato sin cita"
            elif verificar_numeros:
                faltan = []
                terms = [t for t in _terminos(_RE_CITA.sub(" ", fr)) if not t.isdigit()]
                for tok in _numeros_verificables(fr):
                    cands = _candidatos_numero(tok)
                    # la cifra debe estar en una fuente citada y cerca de algún término de la frase
                    respaldada = any(_numero_en_contexto(por_id[c], tok, terms, minimo=1) for c in validas)
                    if not respaldada and not any(por_id[c].get("restringida") for c in validas):
                        otras = [f["id"] for f in fuentes if cands & _numeros_de_fuente(f)]
                        faltan.append(f"{tok}" + (f" (aparece en [{']['.join(map(str, otras[:3]))}])" if otras else " (no aparece en ninguna fuente)"))
                if faltan:
                    motivo = "cifra no encontrada en la fuente citada: " + ", ".join(faltan)
            if (not motivo and validas and all(por_id[c].get("derogada") for c in validas)
                    and not _RE_PASADO.search(_sin_acentos(fr))):
                motivo = (f"se apoya solo en una norma DEROGADA {['[%d]' % c for c in validas]} y está redactado "
                          "como vigente: preséntalo como antecedente (en pasado, indicando que fue derogada) o "
                          "usa la norma vigente de la evidencia")
            if not motivo and validas:
                fechas = [por_id[c].get("fecha") for c in validas]
                periodos = [(int(a), _MESES_NUM[m.lower()]) for m, a in re.findall(
                    r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)"
                    r"\s+(?:de\s+|del\s+)?(20\d{2})", _sin_acentos(fr), re.I)]
                proyeccion = bool(re.search(r"proyect|proyecc|estim|preve|prevision|pronostic|se espera|podria|"
                                            r"caera|llegara|alcanzara|sera\b|esperad|escenario", _sin_acentos(fr)))
                if periodos and fechas and all(fechas) and not proyeccion:
                    a, m = max(periodos)
                    if max(fechas) < f"{a:04d}-{m:02d}-01":
                        motivo = (f"el periodo {m:02d}/{a} es posterior a la publicación de la fuente "
                                  f"({max(fechas)}): el dato probablemente es de otro año")
                elif fechas and all(fechas) and not re.search(r"\b(19|20)\d{2}\b", fr):
                    # Mes sin año citando una fuente antigua ("en octubre..." de una nota de 2025).
                    mes_suelto = re.search(r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|"
                                           r"octubre|noviembre|diciembre)\b", _sin_acentos(fr))
                    try:
                        dias = (_ahora_local()[0].date()
                                - datetime.strptime(max(fechas), "%Y-%m-%d").date()).days
                    except ValueError:
                        dias = 0
                    if mes_suelto and dias > 120:
                        motivo = (f"menciona '{mes_suelto.group(1)}' sin año y la fuente se publicó el "
                                  f"{max(fechas)}: indica el año real del dato (no es reciente)")
            if motivo:
                problemas.append({"linea": i, "frase": fr.strip(), "motivo": motivo,
                                  "seccion": _seccion_de(lineas, i)})
            else:
                stats["con_fuente"] += 1
    return {"texto": texto_md, "problemas": problemas, "stats": stats}


def _marcar_no_verificado(texto_md: str, problemas) -> str:
    """Último recurso: añade '(no verificado)' a las frases que siguen sin respaldo
    y quita las citas inexistentes."""
    lineas = texto_md.splitlines()
    for p in problemas:
        i = p["linea"]
        frase = p["frase"]
        if i >= len(lineas) or frase not in lineas[i]:
            continue
        # Sin respaldo -> se quitan sus citas (no deben parecer fuente del dato).
        nueva = _RE_CITA.sub("", frase).rstrip()
        m = re.search(r"([.!?]?)\s*$", nueva)
        cuerpo = nueva[:m.start()] if m else nueva
        nueva = cuerpo.rstrip() + " *(no verificado)*" + (m.group(1) if m else "")
        lineas[i] = lineas[i].replace(frase, nueva, 1)
    return "\n".join(lineas)


def _quitar_citas_invalidas(texto_md: str, ids_validos) -> str:
    return re.sub(r"\[(\d{1,2})\]", lambda m: m.group(0) if int(m.group(1)) in ids_validos else "", texto_md)


def _quitar_seccion_fuentes(texto_md: str) -> str:
    return re.split(r"\n#{1,6}\s*(?:Fuentes|Referencias|Bibliograf[ií]a)\b.*", texto_md or "", maxsplit=1, flags=re.I)[0].rstrip()


def renumerar_fuentes(texto_md: str, fuentes):
    """Deja solo las fuentes citadas, numeradas 1..k por orden de aparición."""
    por_id = {f["id"]: f for f in fuentes}
    orden = []
    for m in re.finditer(r"\[(\d{1,2})\]", texto_md):
        n = int(m.group(1))
        if n in por_id and n not in orden:
            orden.append(n)
    mapa = {old: new for new, old in enumerate(orden, 1)}
    nuevo = re.sub(r"\[(\d{1,2})\]", lambda m: f"[{mapa[int(m.group(1))]}]" if int(m.group(1)) in mapa else "", texto_md)
    nuevo = re.sub(r"(\[\d{1,2}\])(?:\1)+", r"\1", nuevo)
    lista = []
    for old in orden:
        f = dict(por_id[old])
        f["id"] = mapa[old]
        lista.append(f)
    return nuevo, lista


def _numero_en_contexto(f, tok, terms, ventana=250, minimo=2) -> bool:
    """True si la cifra aparece en la fuente rodeada de al menos `minimo` términos de la
    frase (evita coincidencias casuales de números en páginas no relacionadas)."""
    texto = f.get("texto") or f.get("extracto") or ""
    cands = _candidatos_numero(tok)
    # número de la norma en el título o la URL de la propia fuente (p. ej. ley_1715_2014)
    ident = f"{f.get('titulo', '')} {f.get('url_final') or f.get('url') or ''}"
    if any(_candidatos_numero(x) & cands for x in re.findall(r"\d+", ident)):
        return True
    for m in _DIGITOS_VERIF.finditer(texto):
        if _candidatos_numero(m.group()) & cands:
            win = _sin_acentos(texto[max(0, m.start() - ventana): m.end() + ventana])
            if sum(1 for t in terms if t in win) >= minimo:
                return True
    return False


def _preferir_oficiales(texto_md: str, fuentes) -> str:
    """Si una frase solo cita medios/otras fuentes pero TODAS sus cifras aparecen también
    en una fuente oficial de la evidencia, se añade la cita oficial (corroboración)."""
    por_id = {f["id"]: f for f in fuentes}
    oficiales = [f for f in fuentes if f.get("tipo") == "oficial"]
    lineas = texto_md.splitlines()
    for i, linea in enumerate(lineas):
        if re.match(r"^\s*#", linea):
            continue
        nueva = linea
        for fr in _frases(linea):
            citas = [int(c) for c in re.findall(r"\[(\d{1,2})\]", fr)]
            if not citas or any(por_id.get(c, {}).get("tipo") == "oficial" for c in citas):
                continue
            nums = [t for t in _numeros_verificables(fr) if len(re.sub(r"\D", "", t)) >= 3]
            if not nums:
                continue
            terms = [t for t in _terminos(_RE_CITA.sub(" ", fr)) if not t.isdigit()]
            for f in oficiales:
                if all(_numero_en_contexto(f, t, terms) for t in nums):
                    m = re.search(r"\[\d{1,2}\]", fr)
                    nueva = nueva.replace(fr, fr[:m.start()] + f"[{f['id']}]" + fr[m.start():], 1)
                    break
        lineas[i] = nueva
    return "\n".join(lineas)


def resumen_verificacion(stats, fuentes) -> str:
    of = sum(1 for f in fuentes if f.get("tipo") == "oficial")
    activos = sum(1 for f in fuentes if f.get("enlace") == "activo")
    return (f"Verificación automática: {stats['con_fuente']} datos con fuente · "
            f"{stats['no_verificados']} no verificados · {len(fuentes)} fuentes ({of} oficiales) · "
            f"enlaces activos {activos}/{len(fuentes)}")


def fuentes_a_markdown(fuentes, verificacion: str) -> str:
    lineas = ["## Fuentes", "", f"> {verificacion}", ""]
    hoy = _ahora_local()[0].strftime("%Y-%m-%d")
    for f in fuentes:
        publ = (f" · publicado {f['fecha']}" if f.get("fecha") else "") + (" · DEROGADA" if f.get("derogada") else "")
        lineas.append(f"[{f['id']}] [{f['titulo'][:140]}]({f['url_final']}) — {_dominio(f['url_final'])} · "
                      f"{f.get('tipo', 'otro')}{publ} · enlace {f.get('enlace', 's/d')} (consultado {hoy})  ")
    return "\n".join(lineas)


def fuentes_desde_markdown(texto_md: str):
    """Para --desde-md: separa el cuerpo y la lista de fuentes guardada en el .md."""
    partes = re.split(r"\n#{1,6}\s*Fuentes\s*\n", texto_md or "", maxsplit=1)
    cuerpo = partes[0].rstrip()
    fuentes = []
    if len(partes) > 1:
        for m in re.finditer(r"^\[(\d+)\]\s*\[(.+?)\]\((\S+?)\)(?:\s*—\s*(.*))?$", partes[1], re.M):
            meta = m.group(4) or ""
            mf = re.search(r"publicado (\d{4}-\d{2}-\d{2})", meta)
            fuentes.append({"id": int(m.group(1)), "titulo": m.group(2), "url": m.group(3),
                            "fecha": mf.group(1) if mf else "",
                            "url_final": m.group(3), "tipo": _tipo_dominio(m.group(3)),
                            "texto": "", "extracto": "", "notas": []})
    return cuerpo, fuentes


def _comprobar_enlaces(fuentes):
    """Comprueba que cada enlace responda (200/3xx); marca caídos."""
    def _chk(f):
        info = _descargar(f["url_final"], max_bytes=200_000)
        f["status"] = info.get("status")
        f["enlace"] = _estado_enlace(info)
        return f
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_chk, fuentes))


_VARIABLES_MERCADO = (
    # (patrón en la pregunta, nombre, patrón que debe aparecer con una cifra en el informe/evidencia)
    (r"precios? de (la )?bolsa|precio spot", "precio promedio de bolsa",
     r"(precio[^.\n]{0,40}bolsa|bolsa[^.\n]{0,40}precio)[^.\n]{0,120}\d[\d.,]*\s*(cop|\$|pesos)"),
    (r"embalse", "nivel de embalses", r"embalse[^.\n]{0,120}\d[\d.,]*\s*%"),
    (r"\bdemanda\b", "demanda de energía", r"demanda[^.\n]{0,120}\d[\d.,]*\s*(gwh|%)"),
    (r"aportes", "aportes hídricos", r"aportes[^.\n]{0,120}\d[\d.,]*\s*%"),
)


def _variables_faltantes(pregunta, texto_md, fuentes):
    """Variables que la pregunta menciona y cuyo valor numérico falta en el informe
    aunque sí está en la evidencia."""
    q = _sin_acentos(pregunta)
    t = _sin_acentos(texto_md)
    out = []
    for rx_q, nombre, rx_v in _VARIABLES_MERCADO:
        if not re.search(rx_q, q) or re.search(rx_v, t):
            continue
        ids = [f["id"] for f in fuentes
               if re.search(rx_v, _sin_acentos((f.get("extracto") or "") + " " + (f.get("texto") or "")[:200000]))]
        if ids:
            out.append(f"falta el último valor disponible de '{nombre}' (la pregunta lo pide): inclúyelo con su "
                       f"periodo exacto, su variación si la fuente la da y su cita (aparece en "
                       f"[{']['.join(map(str, ids[:4]))}])")
    return out


def generar_informe_con_fuentes(pregunta: str, max_pages=None, borrador: str = ""):
    """Investigación -> redacción con citas -> validación (+1 corrección) -> marcas.
    Devuelve (texto_md_sin_fuentes, fuentes_citadas, stats)."""
    t0 = _time.time()
    _log("   a) Investigando fuentes oficiales y verificables…")
    fuentes = recopilar_evidencia(pregunta, borrador)
    if not fuentes:
        raise RuntimeError("No se encontró ninguna fuente verificable (revisa conexión / Vertex AI).")
    _log(f"   b) Redactando con citas ({GROUNDED_MODEL})…")
    prompt = construir_prompt_redaccion(pregunta, fuentes, max_pages, borrador)
    texto = _quitar_seccion_fuentes(_limpiar_respuesta(_texto_respuesta_llm(_llm(prompt)).strip()))
    if not texto:
        raise RuntimeError("El modelo no devolvió texto en la redacción.")
    val = validar_informe(texto, fuentes)
    objetivo, minimo, _maximo = _presupuesto_palabras(max_pages, grounded=True)
    extra = [f"falta: {p}" for p in _verificar_conteos(pregunta, val["texto"])]
    n_pal = _contar_palabras(val["texto"])
    _log(f"   Borrador: {n_pal} palabras (objetivo {objetivo}, rango {minimo}-{_maximo})")
    corto = n_pal < minimo
    if n_pal > int(_maximo * 1.10):
        extra.append(f"el texto es demasiado largo ({n_pal} palabras; máximo {_maximo}): condénsalo a ~{objetivo} "
                     "palabras eliminando repeticiones y detalles accesorios (sobre todo en 'Puntos clave' y en "
                     "párrafos que repiten el análisis), sin perder cifras clave, definiciones ni citas")
    extra += _variables_faltantes(pregunta, val["texto"], fuentes)
    if corto:
        extra.append(f"el texto es corto ({_contar_palabras(val['texto'])} palabras; mínimo {minimo}, objetivo "
                     f"~{objetivo}): amplía sobre todo el 'Análisis detallado' explicando contexto, mecanismo e "
                     "implicaciones con la evidencia, y desarrolla cada punto clave en 2-4 frases")
    _log(f"   c) Validación: {val['stats']} · {len(val['problemas'])} problemas"
         + (f" · {len(extra)} de formato" if extra else ""))
    for p in val["problemas"][:20]:
        _log(f"      - {p['motivo']}: {p['frase'][:140]}")
    for e in extra:
        _log(f"      - formato: {e[:160]}")
    solo_extension = (not val["problemas"]) and extra and all(
        e.startswith("el texto es demasiado largo") or e.startswith("el texto es corto") for e in extra
    )
    if solo_extension:
        _log("   d) Extensión fuera de rango; el PDF ajusta las páginas, sin otra llamada al modelo.")
        extra = []
    if FAST_REPORT and (val["problemas"] or extra):
        _log("   d) Modo rápido: se omite la segunda reescritura; los datos sin respaldo se marcarán.")
    if (val["problemas"] or extra) and not FAST_REPORT:
        _log(f"   d) Pidiendo una corrección al modelo ({CORRECTION_MODEL})…")
        lista = "\n".join(f"- \"{p['frase'][:300]}\" -> {p['motivo']}" for p in val["problemas"][:30])
        lista += ("\n" + "\n".join(f"- {e}" for e in extra)) if extra else ""
        prompt_fix = (
            prompt + "\n\n=== TU BORRADOR ===\n" + val["texto"] + "\n=== FIN BORRADOR ===\n\n"
            "CORRECCIÓN OBLIGATORIA. La validación automática encontró estos problemas:\n" + lista + "\n\n"
            "Reescribe el informe COMPLETO corrigiéndolos: para cada frase, cita la fuente correcta de la "
            "EVIDENCIA, copia la cifra exacta que aparece en ella, elimina el dato o márcalo '(no verificado)'. "
            "Mantén lo que ya estaba bien y respeta la estructura; la extensión final debe quedar entre "
            f"{minimo} y {_maximo} palabras."
        )
        try:
            resp_fix = _llm(prompt_fix, model=CORRECTION_MODEL, intentos=1,
                            timeout_s=CORRECTION_TIMEOUT_SECONDS)
            texto2 = _quitar_seccion_fuentes(_limpiar_respuesta(_texto_respuesta_llm(resp_fix).strip()))
            val2 = validar_informe(texto2, fuentes) if texto2 else None
        except Exception as exc:
            _log(f"      Corrección omitida: {exc}")
            val2 = None
        def _puntaje(v):  # problemas de fuentes + penalización si queda corto o muy largo
            w = _contar_palabras(v["texto"])
            return (len(v["problemas"]) + (6 if w < minimo else 0) + (4 if w > int(_maximo * 1.15) else 0)
                    + 3 * len(_variables_faltantes(pregunta, v["texto"], fuentes)))
        if val2 and (_puntaje(val2) <= _puntaje(val)):
            _log(f"      Tras la corrección: {val2['stats']} · {len(val2['problemas'])} problemas")
            val = val2
        else:
            _log("      La corrección no mejoró; se conserva el borrador.")
    texto = val["texto"]
    if val["problemas"]:
        _log(f"   e) Marcando {len(val['problemas'])} frase(s) como '(no verificado)'.")
        texto = _marcar_no_verificado(texto, val["problemas"])
    texto = _quitar_citas_invalidas(texto, {f["id"] for f in fuentes})
    texto = _preferir_oficiales(texto, fuentes)
    texto, citadas = renumerar_fuentes(texto, fuentes)
    final = validar_informe(texto, citadas)
    _log(f"   f) Final: {final['stats']} · {len(citadas)} fuentes citadas · {round(_time.time() - t0)} s")
    return texto, citadas, final["stats"]


_RE_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _lista_correos(texto: str):
    """'a@x.com, b@y.com; c@z.com' -> ['a@x.com', 'b@y.com', 'c@z.com']"""
    return [c.strip() for c in re.split(r"[,;\s]+", texto or "") if c.strip()]


def _guardar_en_env(valores: dict):
    env_path = BASE_DIR / ".env"
    lineas = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    pendientes = dict(valores)
    for i, raw in enumerate(lineas):
        key = raw.partition("=")[0].strip()
        if key in pendientes:
            lineas[i] = f"{key}={pendientes.pop(key)}"
    lineas += [f"{k}={v}" for k, v in pendientes.items()]
    env_path.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def _probar_smtp(sender_email: str, app_password: str):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender_email, app_password)


def _pedir_credenciales_smtp() -> bool:
    """Pide Gmail + App Password, las prueba y opcionalmente las guarda en .env."""
    print("\nNo hay credenciales de correo configuradas para el envío automático.")
    print("   Usa una cuenta Gmail con App Password (https://myaccount.google.com/apppasswords).")
    while True:
        sender = input("   Gmail remitente (Enter = solo generar el PDF): ").strip()
        if not sender:
            return False
        password = getpass.getpass("   App Password (16 caracteres, no se muestra): ").replace(" ", "")
        try:
            _probar_smtp(sender, password)
        except Exception as exc:
            print(f"   No se pudo iniciar sesión en Gmail: {exc}")
            continue
        os.environ["SENDER_EMAIL"] = sender
        os.environ["APP_PASSWORD"] = password
        print("   Credenciales verificadas.")
        if input("   ¿Guardarlas en .env para próximas ejecuciones? [s/N]: ").strip().lower() in {"s", "si", "sí", "y"}:
            _guardar_en_env({"SENDER_EMAIL": sender, "APP_PASSWORD": password})
            print("   Guardadas en .env.")
        return True


def _es_placeholder_sendgrid(api_key: str) -> bool:
    k = (api_key or "").strip()
    if not k:
        return True
    bajos = k.lower()
    # Clave de Google (AQ....) no es SendGrid: modo prueba / no enviar real
    if bajos.startswith("aq."):
        return True
    if not bajos.startswith("sg."):
        return True
    return (
        bajos.startswith("sg.reemplazar")
        or "tu_api_key" in bajos
        or "placeholder" in bajos
        or k == "SG.xxx"
    )



def enviar_correo_sendgrid(destinatario, ruta_pdf, prompt, titulo, api_key, from_email, from_name="Enersinc AI"):
    """Envía el PDF con SendGrid (API key real)."""
    import base64

    if _es_placeholder_sendgrid(api_key):
        return False, "placeholder"
    with open(ruta_pdf, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("ascii")
    payload = {
        "personalizations": [{"to": [{"email": c} for c in _lista_correos(destinatario)]}],
        "from": {"email": from_email, "name": from_name},
        "subject": f"Enersinc · {titulo}",
        "content": [
            {
                "type": "text/plain",
                "value": (
                    f"Adjunto el informe \"{titulo}\" generado por Enersinc AI.\n\n"
                    f"Consulta: {prompt}\n"
                ),
            }
        ],
        "attachments": [
            {
                "content": content_b64,
                "type": "application/pdf",
                "filename": Path(ruta_pdf).name,
                "disposition": "attachment",
            }
        ],
    }
    res = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if res.status_code in (200, 202):
        return True, "sendgrid"
    raise RuntimeError(f"SendGrid HTTP {res.status_code}: {(res.text or '')[:300]}")


def enviar_correo_smtp(destinatario, ruta_pdf, prompt, titulo, sender_email, app_password):
    """Envío real por Gmail SMTP (cuenta de prueba / App Password)."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = f"Enersinc · {titulo}"
    msg["From"] = sender_email
    msg["To"] = ", ".join(_lista_correos(destinatario))
    msg.set_content(
        f'Adjunto el informe "{titulo}" generado por Enersinc AI.\n\n'
        f"Consulta: {prompt}\n"
    )
    with open(ruta_pdf, "rb") as f:
        data = f.read()
    msg.add_attachment(
        data,
        maintype="application",
        subtype="pdf",
        filename=Path(ruta_pdf).name,
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(sender_email, app_password)
        smtp.send_message(msg)
    return True, "smtp"



def _gateway_config():
    url = (os.environ.get("API_GATEWAY_DATOS_URL") or "").strip().rstrip("/")
    key = (os.environ.get("API_GATEWAY_DATOS_KEY") or "").strip()
    sender = (os.environ.get("EMAIL_SENDER") or "contacto@enersinc.com").strip()
    return url, key, sender


def _asunto_correo(titulo: str) -> str:
    """Una sola línea. El gateway agrega la firma HTML; no debe ir en el asunto."""
    limpio = re.sub(r"[\r\n\t]+", " ", titulo or "")
    limpio = re.sub(r"\s{2,}", " ", limpio).strip().strip("-–—|")
    if not limpio:
        limpio = "Informe Enersinc"
    return f"Enersinc - {limpio}"[:180]


def _cuerpo_correo_html(titulo: str, prompt: str) -> str:
    """HTML corto. La firma de Enersinc la agrega el API Gateway al final."""
    return (
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#333333;">'
        f"<p>Adjunto el informe <strong>{_esc(titulo)}</strong>, generado por Enersinc AI.</p>"
        f"<p>Consulta: {_esc(prompt)}</p>"
        "</div>"
    )


def enviar_correo_gateway(destinatario, ruta_pdf, prompt, titulo):
    """Envía el PDF por el API Gateway de Enersinc (POST /email/send)."""
    url, key, sender = _gateway_config()
    if not url or not key:
        raise RuntimeError("Faltan API_GATEWAY_DATOS_URL o API_GATEWAY_DATOS_KEY en .env.")
    to_emails = _lista_correos(destinatario)
    pdf_path = Path(ruta_pdf)
    payload = {
        "sender": sender,
        "recipients": ",".join(to_emails),
        "cc_recipients": "",
        "bcc_recipients": "",
        "subject": _asunto_correo(titulo),
        "content": _cuerpo_correo_html(titulo, prompt),
        "content_subtype": "html",
    }
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    res = requests.post(
        f"{url}/email/send",
        headers={"x-api-key": key},
        data=payload,
        files=[("attachments", (pdf_path.name, pdf_bytes, "application/pdf"))],
        timeout=60,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"API Gateway HTTP {res.status_code}: {(res.text or '')[:300]}")
    return True, "gateway"


def enviar_correo(destinatario, ruta_pdf, prompt, titulo):
    """
    Envío real: API Gateway de Enersinc (contacto@enersinc.com);
    si no está configurado, SendGrid y luego Gmail SMTP.
    """
    url, key, sender = _gateway_config()
    if url and key:
        print(f"   Enviando vía API Gateway (from={sender})...")
        return enviar_correo_gateway(destinatario, ruta_pdf, prompt, titulo)

    api_key = (os.environ.get("SENDGRID_API_KEY") or "").strip()
    from_email = (
        os.environ.get("SENDGRID_FROM_EMAIL")
        or os.environ.get("SENDER_EMAIL")
        or "noreply@enersinc.com"
    ).strip()
    if not _es_placeholder_sendgrid(api_key):
        print(f"   Enviando vía SendGrid (from={from_email})...")
        return enviar_correo_sendgrid(
            destinatario, ruta_pdf, prompt, titulo, api_key, from_email
        )

    sender = (os.environ.get("SENDER_EMAIL") or "").strip()
    password = (os.environ.get("APP_PASSWORD") or "").strip()
    if sender and password:
        print(f"   Enviando vía Gmail SMTP (from={sender})...")
        return enviar_correo_smtp(
            destinatario, ruta_pdf, prompt, titulo, sender, password
        )

    raise RuntimeError(
        "No hay credenciales de correo. En .env configura:\n"
        "  API_GATEWAY_DATOS_URL y API_GATEWAY_DATOS_KEY (Passbolt, API GATEWAY KEY EMAIL)\n"
        "  EMAIL_SENDER=contacto@enersinc.com"
    )


def ejecutar_flujo_completo(prompt, correo_destino, remitente_email="", password_app="",
                            enviar=True, max_pages=None, texto_md=None, con_graficas=True):
    """
    Flujo: investigación con fuentes -> redacción con citas [n] -> validación ->
    PDF confidencial (~N páginas, con bloque Referencias) -> BD -> correo (opcional).
    enviar=False genera solo el PDF (modo prueba / --no-email).
    texto_md: usa un markdown ya existente (revalida sus citas y enlaces).
    """
    t_inicio = _time.time()
    max_pages = max(1, int(max_pages or MAX_PAGES))
    if texto_md is None and not (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                                 or os.environ.get("GOOGLE_API_KEY")
                                 or os.environ.get("GOOGLE_GENAI_API_KEY")):
        raise RuntimeError(
            "Falta GOOGLE_APPLICATION_CREDENTIALS en .env (service account con Vertex AI) "
            "o GOOGLE_API_KEY: se necesitan para investigar y redactar con fuentes."
        )

    if remitente_email:
        os.environ["SENDER_EMAIL"] = remitente_email
    if password_app:
        os.environ["APP_PASSWORD"] = password_app

    inicializar_base_datos()
    _ensure_dirs()
    ahora, _ = _ahora_local()
    timestamp_archivo = ahora.strftime("%Y%m%d_%H%M%S")

    # Las gráficas salen solo de la pregunta y se bajan en paralelo con la
    # investigación, así el PDF no espera segundos extra al final.
    fut_graficas = None
    _ex_g = None
    ruta_png_tmp = GRAFICAS_DIR / f"_tmp_graficas_{timestamp_archivo}.png"
    if con_graficas and generar_graficas:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        print("   Gráficas XM · Sinergox: se preparan en paralelo (según la pregunta)…")
        _ex_g = _TPE(max_workers=1)
        fut_graficas = _ex_g.submit(generar_graficas, prompt, ruta_png_tmp, ahora.date())
    elif con_graficas:
        print("   Gráficas omitidas: instala matplotlib (py -m pip install matplotlib).")

    if texto_md is None:
        print(f"\n1. Informe con fuentes verificables · backend {AGENT_BACKEND} · "
              f"máx. {max_pages} página(s)...")
        print(f"   Pregunta: '{prompt}'")
        borrador = ""
        if AGENT_BACKEND != "local":
            try:
                print(f"   Borrador orientativo del agente ({AGENT_BACKEND})...")
                borrador = extraer_texto_respuesta(
                    consultar_agente(construir_prompt_agente(prompt, max_pages))).strip()
                if _parece_rechazo_router(borrador):
                    borrador = ""
            except Exception as exc:
                print(f"   Borrador omitido ({exc}); se continúa solo con la investigación.")
                borrador = ""
        texto_respuesta, fuentes, stats = generar_informe_con_fuentes(prompt, max_pages, borrador)
        print(f"   Palabras del informe: {_contar_palabras(texto_respuesta)}")
    else:
        print("\n1. Usando markdown existente (se revalidan citas y enlaces)...")
        cuerpo, fuentes = fuentes_desde_markdown((texto_md or "").strip())
        if fuentes:
            _comprobar_enlaces(fuentes)
        val = validar_informe(cuerpo, fuentes, verificar_numeros=False)
        texto_respuesta = val["texto"]
        if val["problemas"]:
            texto_respuesta = _marcar_no_verificado(texto_respuesta, val["problemas"])
        texto_respuesta = _quitar_citas_invalidas(texto_respuesta, {f["id"] for f in fuentes})
        stats = validar_informe(texto_respuesta, fuentes, verificar_numeros=False)["stats"]
    if not texto_respuesta:
        if _ex_g is not None:
            fut_graficas.cancel()
            _ex_g.shutdown(wait=False, cancel_futures=True)
        raise RuntimeError("No se obtuvo texto para el informe.")
    verificacion = resumen_verificacion(stats, fuentes)
    md_completo = texto_respuesta.rstrip() + "\n\n" + fuentes_a_markdown(fuentes, verificacion) + "\n"
    print("   " + verificacion)

    print("\n" + "-" * 24 + " INFORME (markdown con fuentes) " + "-" * 24)
    print(md_completo)
    print("-" * 80)

    titulo_reporte = titulo_desde_contenido(texto_respuesta, prompt)
    estado_inicial = "Generado" if enviar else "Generado (sin envío)"
    # Primero la fila en BD: su ID numera los archivos (0015_Reporte_<timestamp>.pdf).
    reporte_id = reservar_reporte_db(
        prompt, correo_destino, titulo_reporte, md_completo, "Generando PDF"
    )
    actualizar_fuentes_db(reporte_id, fuentes, verificacion)
    numero = f"{reporte_id:04d}"
    ruta_pdf = PDF_DIR / nombre_archivo_reporte(reporte_id, timestamp_archivo, "pdf")
    ruta_md = PDF_DIR / nombre_archivo_reporte(reporte_id, timestamp_archivo, "md")
    actualizar_ruta_db(reporte_id, ruta_pdf)
    ruta_md.write_text(
        f"<!-- Pregunta: {prompt} | Destinatario: {correo_destino} -->\n\n{md_completo}",
        encoding="utf-8",
    )

    graficas = None
    if fut_graficas is not None:
        try:
            graficas = fut_graficas.result(timeout=90)
        except Exception as exc:
            print(f"   Gráficas omitidas: {type(exc).__name__}: {exc}")
        finally:
            _ex_g.shutdown(wait=False)
        if graficas:
            # PNG solo en data/graficas/ (auxiliares del PDF). La BD guarda únicamente el PDF.
            destino_base = GRAFICAS_DIR / nombre_archivo_reporte(reporte_id, timestamp_archivo, "png")
            for img in graficas.get("imagenes") or []:
                origen = Path(img["ruta"])
                if origen == ruta_png_tmp:
                    nuevo = destino_base
                else:
                    suf = origen.stem.replace(ruta_png_tmp.stem, "").lstrip("_")
                    nuevo = destino_base.with_name(f"{destino_base.stem}_{suf}{destino_base.suffix}")
                if origen != nuevo and origen.is_file():
                    if nuevo.is_file():
                        nuevo.unlink()
                    origen.rename(nuevo)
                img["ruta"] = nuevo
            graficas["ruta"] = Path(graficas["imagenes"][0]["ruta"])
            print(f"   Gráficas de XM · Sinergox: {', '.join(graficas['titulos'])} "
                  f"(archivos en data/graficas/, no en BD)")
        else:
            print("   Sin gráficas: la pregunta no las pidió o XM no devolvió datos.")

    print(f"2. Generando PDF confidencial N.º {numero} ({max_pages} página(s) máx.)...")
    try:
        info = markdown_a_pdf(
            texto_respuesta, titulo_reporte, prompt, correo_destino, str(ruta_pdf),
            max_pages=max_pages,
            referencia=f"ENR-{numero}-" + timestamp_archivo.replace("_", "-"),
            fuentes=fuentes, verificacion=verificacion, graficas=graficas,
        )
    except Exception as exc:
        actualizar_estado_db(reporte_id, f"Error PDF: {exc}")
        raise
    print(f"   Páginas: {info['paginas']} (objetivo {max_pages}) · fuente {info['font_pt']}pt · "
          f"{info.get('palabras_pdf', 0)} palabras"
          + (" · se omitieron párrafos del análisis en el PDF (el .md conserva todo)" if info["recortado"] else ""))

    ruta_abs = str(ruta_pdf.resolve())
    actualizar_estado_db(reporte_id, estado_inicial)
    print(f"   PDF y BD OK · Reporte N.º {numero} (ID #{reporte_id})")
    print(f"   PDF guardado en: {ruta_abs}")
    print(f"   Informe .md (con fuentes): {ruta_md.resolve()}")
    print(f"   BD: {DB_PATH.resolve()}")

    print(f"   Tiempo total: {round(_time.time() - t_inicio)} s")
    if not enviar:
        print("3. Envío de correo omitido (--no-email).")
        _imprimir_resultado(numero, ruta_abs)
        return reporte_id, ruta_abs

    try:
        print(f"3. Enviando correo real a {correo_destino}...")
        ok, modo = enviar_correo(correo_destino, ruta_abs, prompt, titulo_reporte)
        estado = f"Enviado ({modo})"
        actualizar_estado_db(reporte_id, estado)
        print(f"   {estado} a {correo_destino}.")
    except Exception as e:
        actualizar_estado_db(reporte_id, f"Error Envío: {e}")
        print(f"   Falló el envío: {e}")
        print(f"   El PDF quedó en: {ruta_abs}")
        _imprimir_resultado(numero, ruta_abs)
        raise

    _imprimir_resultado(numero, ruta_abs)
    return reporte_id, ruta_abs


def _imprimir_resultado(numero, ruta_abs):
    print("\n" + "=" * 60)
    print(f"   REPORTE N.º {numero}")
    print(f"   PDF: {ruta_abs}")
    print("=" * 60)


def _credenciales_correo_ok() -> bool:
    url, key, _sender = _gateway_config()
    if url and key:
        return True
    sg = (os.environ.get("SENDGRID_API_KEY") or "").strip()
    smtp_ok = bool((os.environ.get("SENDER_EMAIL") or "").strip()
                   and (os.environ.get("APP_PASSWORD") or "").strip())
    return (not _es_placeholder_sendgrid(sg)) or smtp_ok


if __name__ == "__main__":
    import argparse
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Documentación PDF · Enersinc")
    ap.add_argument("--pregunta", help="Pregunta para el agente")
    ap.add_argument("--correo", help="Correo destinatario (aparece en el PDF)")
    ap.add_argument("--no-email", action="store_true",
                    help="Solo generar PDF (no enviar correo). También SEND_EMAIL=0")
    ap.add_argument("--sin-graficas", action="store_true",
                    help="No incluir gráficas de XM · Sinergox en el PDF. También GRAFICAS=0")
    ap.add_argument("--max-pages", type=int, default=None,
                    help=f"Páginas objetivo del informe (default MAX_PAGES={MAX_PAGES}); la extensión se escala")
    ap.add_argument("--desde-md", help="Ruta a un .md existente: genera PDF sin llamar al agente")
    ap.add_argument("--backend", choices=["local", "ces", "adk"],
                    help="local (default si .env no define AGENT_BACKEND): investigación con fuentes "
                         "oficiales + Gemini en Vertex. ces / adk: añaden un borrador orientativo del "
                         "Agente Documentacion (CES) o de agent.py. En todos los casos el informe "
                         "lleva citas [n] y bloque de Fuentes verificadas.")
    ap.add_argument("--renumerar", action="store_true",
                    help="Renombra los PDFs existentes a NNNN_Reporte_<fecha>.pdf según su ID y sale")
    args = ap.parse_args()
    if args.backend:
        AGENT_BACKEND = args.backend
    if args.renumerar:
        print("Renumerando archivos de data/pdfs según el ID en la BD...")
        renumerar_archivos_existentes()
        raise SystemExit(0)

    print("=" * 60)
    print("   DOCUMENTACIÓN PDF · ENERSINC")
    print("=" * 60)

    PREGUNTA = (args.pregunta or "").strip() or input("\n1. Pregunta para el agente: ").strip()
    if not PREGUNTA:
        raise SystemExit("Necesitas la pregunta.")
    CORREO = (args.correo or "").strip()
    while True:
        if not CORREO:
            CORREO = input("2. ¿A qué correo(s) se envía el PDF? (separa varios con coma): ").strip()
        destinos = _lista_correos(CORREO)
        invalidos = [c for c in destinos if not _RE_CORREO.match(c)]
        if destinos and not invalidos:
            break
        print(f"   Correo(s) no válido(s): {', '.join(invalidos) or '(vacío)'}")
        if args.correo:
            raise SystemExit(1)
        CORREO = ""
    CORREO = ", ".join(destinos)

    paginas = args.max_pages
    if paginas is None and sys.stdin.isatty():
        while True:
            raw = input("3. ¿Cuántas páginas? (Enter = 2, mínimo 2): ").strip()
            if not raw:
                paginas = 2
                break
            if raw.isdigit() and int(raw) >= 2:
                paginas = int(raw)
                break
            print("   Escribe un entero de 2 o más, o Enter para dejar 2.")
    paginas = max(2, int(paginas or MAX_PAGES))

    enviar = not args.no_email and (os.environ.get("SEND_EMAIL", "1").strip() not in {"0", "false", "no"})
    if enviar and not _credenciales_correo_ok():
        enviar = sys.stdin.isatty() and _pedir_credenciales_smtp()
        if not enviar:
            print("Aviso: sin credenciales de correo -> se genera solo el PDF (sin envío).")
    print(f"\nDestino: {CORREO}" + ("" if enviar else " (solo aparecerá en el PDF, no se enviará)"))
    print(f"Páginas: {paginas}")
    con_graficas = not args.sin_graficas and (
        os.environ.get("GRAFICAS", "1").strip().lower() not in {"0", "false", "no"})
    if con_graficas and temas_detectados:
        series = temas_detectados(PREGUNTA)
        if series:
            print(f"Sinergox: sí, gráficas de {', '.join(series)}")
        else:
            print("Sinergox: no. La pregunta no pide precio, embalses, aportes ni demanda.")
            con_graficas = False

    texto_md = None
    if args.desde_md:
        texto_md = Path(args.desde_md).read_text(encoding="utf-8")
        texto_md = re.sub(r"^<!--.*?-->\s*", "", texto_md, flags=re.S)

    ejecutar_flujo_completo(PREGUNTA, CORREO, enviar=enviar,
                            max_pages=paginas, texto_md=texto_md,
                            con_graficas=con_graficas)
