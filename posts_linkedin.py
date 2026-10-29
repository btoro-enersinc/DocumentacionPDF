"""
Propuestas de publicación para LinkedIn: ideas creativas ya hechas, cada una como
imagen de ejemplo con la plantilla de Enersinc (fondo azul noche, Montserrat/Lato,
logo) y el texto listo para publicar. Se añaden al PDF: galería 2x2 y textos seguidos.

Uso desde generar_reporte_local.py:
    posts = proponer_posts(texto_informe, llm, n=4)       # llm(prompt) -> str
    pdf_bytes = paginas_posts(posts, carpeta_png)          # PDF con las páginas
"""
import io
import json
import re
import textwrap
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
FUENTES_DIR = BASE_DIR / "assets" / "fonts"
LOGO = BASE_DIR / "assets" / "logo-enersinc-dark.png"

# Paleta de la marca (la misma del encabezado del PDF).
FONDO = "#0f172a"
FONDO_2 = "#16213a"
AZUL = "#0284c7"
CELESTE = "#8EC5FF"
BLANCO = "#ffffff"
GRIS = "#94a3b8"

FORMATOS = ("dato", "carrusel", "video", "pregunta")
ETIQUETA = {"dato": "DATO CLAVE", "carrusel": "CARRUSEL", "video": "VIDEO", "pregunta": "PARA DEBATIR"}


# ------------------------------ Propuesta (LLM) -----------------------------
def _prompt_posts(texto_informe: str, n: int) -> str:
    return (
        "Eres el estratega de contenidos de Enersinc, consultora y proveedora de tecnología energética "
        "en Colombia (analítica predictiva, sistemas de gestión energética EMS, consultoría regulatoria, "
        "estudios de conexión y digitalización de redes). A partir del informe de abajo propone "
        f"{n} publicaciones creativas para la página de LinkedIn de la empresa, ya hechas y listas para usar. "
        "Cada una debe partir de un hecho del informe y conectarlo con una solución de la empresa, "
        "con tono de aliado estratégico, cercano y sin sonar a publicidad.\n"
        "Varía los formatos y usa al menos uno de cada: dato, carrusel y video. Formatos posibles: "
        "dato (una cifra protagonista), carrusel (4 láminas), video (video corto de 30 a 90 segundos) "
        "y pregunta (abre debate con la comunidad).\n"
        "Devuelve SOLO un JSON con la clave \"posts\", una lista de objetos con estas claves:\n"
        '- "formato": dato | carrusel | video | pregunta\n'
        '- "titular": frase creativa de máximo 60 caracteres que irá grande en la imagen\n'
        '- "cifra": la cifra protagonista tal como aparece en el informe (por ejemplo 68,4 % o 412 $/kWh), '
        'o "" si no aplica\n'
        '- "apoyo": frase de máximo 90 caracteres que explica la cifra o el titular\n'
        '- "laminas": solo en carrusel, lista de 4 títulos de lámina de máximo 40 caracteres cada uno\n'
        '- "guion": solo en video, lista de 3 o 4 escenas breves (máximo 110 caracteres cada una), '
        "empezando por el gancho de los primeros 5 segundos\n"
        '- "opciones": solo en pregunta, 3 opciones de encuesta de LinkedIn de máximo 30 caracteres cada una\n'
        '- "texto": el texto de la publicación listo para copiar, de 120 a 200 palabras, con el estilo de '
        "Enersinc descrito abajo; separa párrafos y viñetas con saltos de línea (\\n) y usa • para las viñetas\n"
        '- "hashtags": lista de 3 a 5 hashtags; usa siempre #EnergíaColombia y #MercadoEléctrico y '
        "completa con temas del post (por ejemplo #CREG, #Innovación, #Energía)\n"
        "Estilo de Enersinc (tomado de sus publicaciones reales, por ejemplo su comentario sobre el proyecto "
        "de resolución CREG que ajusta el Estatuto de Riesgo de Desabastecimiento): voz técnica y con postura "
        "propia, no publicitaria. Estructura: 1) abre nombrando la norma, el dato o el hecho concreto con su "
        "número o cifra; 2) lo que cambia o los avances, en 3 o 4 viñetas cortas y precisas sobre el mecanismo "
        "(remuneración, despacho, contratos, señales de precio); 3) lo que sigue pendiente o los riesgos que "
        "no se resuelven; 4) la lectura de Enersinc, una propuesta o recomendación técnica concreta; 5) cierre "
        "invitando a medir el impacto en la operación, los contratos o la gestión energética de cada empresa "
        "y a conversar sobre el tema.\n"
        '- "fuente": entidad o medio y fecha del dato (por ejemplo XM, 28/10/2026)\n'
        "Reglas: no inventes cifras, nombres ni citas que no estén en el informe; no uses markdown ni "
        "citas entre corchetes; Enersinc no expide normas ni opera el sistema.\n\n"
        f"Informe:\n{texto_informe[:9000]}"
    )


def _limpio(s, maximo=None) -> str:
    s = re.sub(r"\[\d{1,2}\]|\*\*|__|`", "", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    if maximo and len(s) > maximo:
        s = s[: maximo - 1].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return s


def _mayuscula(s: str) -> str:
    return s[:1].upper() + s[1:]


def _normalizar(post: dict, texto_informe: str):
    formato = _limpio(post.get("formato")).lower()
    if formato not in FORMATOS:
        formato = "dato" if post.get("cifra") else "pregunta"
    titular = _limpio(post.get("titular"), 70)
    if not titular:
        return None
    cifra = _limpio(post.get("cifra"), 16)
    # Solo se dibuja una cifra que esté en el informe (sin espacios ni separadores).
    plano = re.sub(r"[\s.,]", "", texto_informe)
    numero = re.search(r"\d[\d.,]*", cifra)
    if not numero or re.sub(r"[.,]", "", numero.group(0)) not in plano:
        cifra = ""
    if formato == "dato" and not cifra:
        formato = "pregunta"
    return {
        "formato": formato,
        "titular": titular,
        "cifra": cifra,
        "apoyo": _limpio(post.get("apoyo"), 100),
        "laminas": [_limpio(x, 44) for x in (post.get("laminas") or []) if _limpio(x)][:4],
        # Sin prefijos tipo "Gancho:" o "Escena 2:"; la página ya numera las escenas.
        "guion": [_mayuscula(_limpio(re.sub(r"^\s*(gancho|escena\s*\d*|cierre)\s*[:.-]\s*", "", str(x), flags=re.I), 120))
                  for x in (post.get("guion") or []) if _limpio(x)][:4],
        "opciones": [_limpio(x, 34) for x in (post.get("opciones") or []) if _limpio(x)][:3],
        # Conserva párrafos y viñetas: se limpia línea por línea.
        "texto": "\n".join(ln for ln in (_limpio(x) for x in str(post.get("texto") or "").split("\n")) if ln)[:1600],
        "hashtags": [("#" + _limpio(h).lstrip("#")).replace(" ", "") for h in (post.get("hashtags") or [])
                     if _limpio(h)][:5],
        "fuente": _limpio(post.get("fuente"), 60),
    }


def proponer_posts(texto_informe: str, llm, n: int = 4):
    """Pide las publicaciones al modelo y las limpia. Lista vacía si no hay JSON útil."""
    crudo = llm(_prompt_posts(texto_informe, n)) or ""
    m = re.search(r"\{.*\}", crudo, re.S)
    if not m:
        return []
    datos = json.loads(m.group(0))
    posts = [_normalizar(p, texto_informe) for p in (datos.get("posts") or []) if isinstance(p, dict)]
    return [p for p in posts if p][:n]


# ------------------------------ Imagen (plantilla) --------------------------
# Cada formato tiene su propio tema para que el feed se vea variado pero de la misma marca.
TEMAS = {
    "dato": {"fondo": ("#0b1224", "#123a63"), "texto": BLANCO, "suave": "#a5b4cc", "acento": CELESTE},
    "carrusel": {"fondo": ("#f4f8fd", "#dbeafe"), "texto": FONDO, "suave": "#475569", "acento": AZUL},
    "video": {"fondo": ("#050a17", "#1b2a4a"), "texto": BLANCO, "suave": "#cbd5e1", "acento": "#38bdf8"},
    "pregunta": {"fondo": ("#0369a1", "#0ea5e9"), "texto": BLANCO, "suave": "#e0f2fe", "acento": FONDO},
}


def _fuentes_mpl():
    from matplotlib.font_manager import FontProperties
    def fp(nombre, respaldo="DejaVu Sans", peso="normal"):
        ruta = FUENTES_DIR / nombre
        return FontProperties(fname=str(ruta)) if ruta.is_file() else FontProperties(family=respaldo, weight=peso)
    return {
        "titulo": fp("Montserrat-Bold.ttf", peso="bold"),
        "sub": fp("Montserrat-Regular.ttf"),
        "texto": fp("Lato-Regular.ttf"),
        "texto_b": fp("Lato-Bold.ttf", peso="bold"),
    }


def _envolver(texto, ancho):
    return "\n".join(textwrap.wrap(texto, width=ancho)) if texto else ""


def _porcentaje(cifra: str):
    """68,4 % -> 68.4 si es un porcentaje entre 0 y 100 (para dibujar el anillo)."""
    m = re.match(r"\s*(\d{1,3}(?:[.,]\d+)?)\s*%", cifra or "")
    if not m:
        return None
    valor = float(m.group(1).replace(",", "."))
    return valor if 0 < valor <= 100 else None


def _fondo(ax, tema):
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap
    x, y = np.meshgrid(np.linspace(0, 1, 300), np.linspace(0, 1, 300))
    mapa = LinearSegmentedColormap.from_list("f", list(tema["fondo"]))
    ax.imshow((x * 0.6 + (1 - y) * 0.4), extent=(0, 100, 0, 100), origin="lower", cmap=mapa,
              aspect="auto", zorder=0, interpolation="bicubic")


def _brillo(ax, x, y, radio, color, pasos=10, alfa=0.035):
    from matplotlib.patches import Circle
    for k in range(pasos):
        ax.add_patch(Circle((x, y), radio * (1 - k / pasos), color=color, alpha=alfa, lw=0, zorder=1))


def _puntos(ax, x0, y0, filas, columnas, color, paso=2.6, alfa=0.35):
    xs = [x0 + c * paso for c in range(columnas) for _ in range(filas)]
    ys = [y0 + f * paso for _ in range(columnas) for f in range(filas)]
    ax.scatter(xs, ys, s=3, color=color, alpha=alfa, lw=0, zorder=1)


def _etiqueta(ax, f, texto, x, y, fondo, color):
    from matplotlib.patches import FancyBboxPatch
    ancho = 3.2 + len(texto) * 1.32
    ax.add_patch(FancyBboxPatch((x, y), ancho, 5.6, boxstyle="round,pad=0,rounding_size=2.8",
                                color=fondo, lw=0, zorder=4))
    ax.text(x + 1.8, y + 2.85, texto, color=color, fontproperties=f["texto_b"], fontsize=9.5,
            va="center", zorder=5)


def _logo(ax, plt, x, y, alto, con_placa=False):
    from matplotlib.patches import FancyBboxPatch
    if not LOGO.is_file():
        return
    import numpy as np
    img = plt.imread(str(LOGO))[..., :3]
    # El PNG trae fondo azul noche: se vuelve transparente para que el logo flote sobre el degradado.
    distancia = np.abs(img - img[2, 2]).sum(axis=2)
    img = np.dstack([img, np.clip(distancia / 0.25, 0, 1)])
    ancho = alto * img.shape[1] / img.shape[0]
    if con_placa:   # el logo es para fondo oscuro: sobre fondo claro va en una placa
        ax.add_patch(FancyBboxPatch((x - 1.5, y - 1.2), ancho + 3, alto + 2.4,
                                    boxstyle="round,pad=0,rounding_size=1.8", color=FONDO, lw=0, zorder=5))
    ax.imshow(img, extent=(x, x + ancho, y, y + alto), zorder=6, interpolation="bicubic")


def _dato(ax, f, post, t):
    from matplotlib.patches import Wedge
    _brillo(ax, 78, 50, 38, t["acento"])
    _puntos(ax, 72, 82, 5, 9, t["acento"])
    ax.text(7, 80, _envolver(post["titular"], 24), color=t["texto"], fontproperties=f["titulo"],
            fontsize=20, va="top", linespacing=1.12, zorder=5)
    pct = _porcentaje(post["cifra"])
    if pct is not None:
        cx, cy, r = 70, 40, 19
        ax.add_patch(Wedge((cx, cy), r, 0, 360, width=4.2, color="#ffffff", alpha=0.12, zorder=3))
        ax.add_patch(Wedge((cx, cy), r, 90 - 3.6 * pct, 90, width=4.2, color=t["acento"], zorder=4))
        ax.text(cx, cy + 1, post["cifra"].replace(" ", ""), color=t["texto"], fontproperties=f["titulo"],
                fontsize=24, ha="center", va="center", zorder=5)
        ax.text(7, 42, _envolver(post["apoyo"], 24), color=t["suave"], fontproperties=f["sub"],
                fontsize=11.5, va="center", linespacing=1.35, zorder=5)
    else:
        tam = 66 if len(post["cifra"]) <= 6 else (50 if len(post["cifra"]) <= 9 else 38)
        ax.text(6.5, 46, post["cifra"], color=t["acento"], fontproperties=f["titulo"], fontsize=tam,
                va="center", zorder=5)
        ax.plot([7, 25], [33.5, 33.5], color=t["acento"], lw=4, solid_capstyle="round", zorder=5)
        ax.text(7, 29, _envolver(post["apoyo"], 44), color=t["suave"], fontproperties=f["sub"],
                fontsize=11.5, va="top", linespacing=1.35, zorder=5)


def _carrusel(ax, f, post, t):
    from matplotlib.patches import FancyBboxPatch
    # Tarjetas apiladas detrás de la portada: se lee como carrusel de un vistazo.
    for k, alfa in ((2, 0.25), (1, 0.5)):
        ax.add_patch(FancyBboxPatch((10 + k * 3.2, 15 - k * 2.4), 78, 72, boxstyle="round,pad=0,rounding_size=3",
                                    color=AZUL, alpha=alfa, lw=0, zorder=2))
    ax.add_patch(FancyBboxPatch((7, 17), 78, 72, boxstyle="round,pad=0,rounding_size=3",
                                color=BLANCO, ec="#bfdbfe", lw=1.2, zorder=3))
    ax.add_patch(FancyBboxPatch((7, 82), 78, 7, boxstyle="round,pad=0,rounding_size=3",
                                color=FONDO, lw=0, zorder=4))
    ax.text(10.5, 85.5, "CARRUSEL", color=CELESTE, fontproperties=f["texto_b"], fontsize=9,
            va="center", zorder=5)
    ax.text(81.5, 85.5, "1 / 4", color=BLANCO, fontproperties=f["texto_b"], fontsize=9,
            va="center", ha="right", zorder=5)
    ax.text(11, 77, _envolver(post["titular"], 24), color=FONDO, fontproperties=f["titulo"],
            fontsize=19, va="top", linespacing=1.12, zorder=5)
    laminas = post["laminas"] or [post["apoyo"]]
    for i, lam in enumerate(laminas[:4]):
        y = 47 - i * 7.2
        ax.text(11, y, f"{i + 1:02d}", color=AZUL, fontproperties=f["titulo"], fontsize=12,
                va="center", zorder=5)
        ax.text(19, y, _limpio(lam, 40), color="#334155", fontproperties=f["texto"], fontsize=10,
                va="center", zorder=5)
        ax.plot([11, 81], [y - 3.6, y - 3.6], color="#e2e8f0", lw=0.8, zorder=4)
    ax.text(81, 21, "Desliza  →", color=AZUL, fontproperties=f["texto_b"], fontsize=10.5,
            ha="right", va="center", zorder=5)


def _video(ax, f, post, t):
    from matplotlib.patches import Circle, FancyBboxPatch, Polygon
    _brillo(ax, 50, 56, 34, t["acento"], alfa=0.04)
    # Marco de grabación en las esquinas.
    for (x, y, dx, dy) in ((6, 94, 1, -1), (94, 94, -1, -1), (6, 10, 1, 1), (94, 10, -1, 1)):
        ax.plot([x, x + 6 * dx], [y, y], color=t["suave"], lw=1.6, alpha=0.7, zorder=4)
        ax.plot([x, x], [y, y + 6 * dy], color=t["suave"], lw=1.6, alpha=0.7, zorder=4)
    ax.add_patch(Circle((11.5, 88.5), 1.3, color="#ef4444", zorder=5))
    ax.text(14, 88.5, "REC · VIDEO 60 s", color=t["suave"], fontproperties=f["texto_b"], fontsize=9,
            va="center", zorder=5)
    ax.text(50, 79, _envolver(post["titular"], 28), color=t["texto"], fontproperties=f["titulo"],
            fontsize=18.5, va="top", ha="center", linespacing=1.12, zorder=5)
    ax.add_patch(Circle((50, 47), 10.5, color=t["acento"], alpha=0.25, zorder=4))
    ax.add_patch(Circle((50, 47), 8, color=t["acento"], zorder=5))
    ax.add_patch(Polygon([(47.3, 51.6), (47.3, 42.4), (55, 47)], color=BLANCO, zorder=6))
    gancho = post["guion"][0] if post["guion"] else post["apoyo"]
    lineas = textwrap.wrap(gancho, 40)[:2]
    alto = 4.6 * len(lineas) + 2.6
    ax.add_patch(FancyBboxPatch((14, 30.5 - alto), 72, alto, boxstyle="round,pad=0,rounding_size=1.5",
                                color="#000000", alpha=0.55, lw=0, zorder=5))
    ax.text(50, 30.5 - alto / 2, "\n".join(lineas), color=BLANCO, fontproperties=f["texto_b"],
            fontsize=10.5, ha="center", va="center", linespacing=1.3, zorder=6)
    ax.plot([14, 86], [15.5, 15.5], color=BLANCO, alpha=0.18, lw=3, solid_capstyle="round", zorder=5)
    ax.plot([14, 33], [15.5, 15.5], color=t["acento"], lw=3, solid_capstyle="round", zorder=6)
    ax.add_patch(Circle((33, 15.5), 1.1, color=BLANCO, zorder=7))


def _pregunta(ax, f, post, t):
    from matplotlib.patches import FancyBboxPatch
    ax.text(62, 40, "?", color=BLANCO, alpha=0.1, fontproperties=f["titulo"], fontsize=330,
            ha="center", va="center", zorder=1)
    _puntos(ax, 8, 88, 3, 12, BLANCO, alfa=0.3)
    _etiqueta(ax, f, "PARA DEBATIR", 7, 77, FONDO, BLANCO)
    ax.text(7, 70, _envolver(post["titular"], 24), color=t["texto"], fontproperties=f["titulo"],
            fontsize=21, va="top", linespacing=1.12, zorder=5)
    opciones = post.get("opciones") or []
    if opciones:
        for i, op in enumerate(opciones[:3]):
            y = 36 - i * 7.6
            ax.add_patch(FancyBboxPatch((7, y - 2.9), 62, 5.8, boxstyle="round,pad=0,rounding_size=2.9",
                                        color=BLANCO, alpha=0.95, lw=0, zorder=4))
            ax.text(10, y, op, color=FONDO, fontproperties=f["texto_b"], fontsize=10, va="center", zorder=5)
        ax.text(7, 11.5 + 2, "Encuesta · vota y comenta", color=t["suave"], fontproperties=f["texto"],
                fontsize=8.5, va="center", zorder=5)
    else:
        ax.text(7, 34, _envolver(post["apoyo"], 40), color=t["suave"], fontproperties=f["sub"],
                fontsize=12, va="top", linespacing=1.35, zorder=5)


def dibujar_post(post: dict, ruta: Path, numero: int = 1) -> Path:
    """Imagen cuadrada 1080x1080 lista para LinkedIn."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    f = _fuentes_mpl()
    t = TEMAS[post["formato"]]
    fig = plt.figure(figsize=(6, 6), dpi=180)
    ax = fig.add_axes([0, 0, 1, 1])
    _fondo(ax, t)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")

    if post["formato"] == "dato":
        _etiqueta(ax, f, "DATO CLAVE", 7, 88, AZUL, BLANCO)
        _dato(ax, f, post, t)
    elif post["formato"] == "carrusel":
        _carrusel(ax, f, post, t)
    elif post["formato"] == "video":
        _video(ax, f, post, t)
    else:
        _pregunta(ax, f, post, t)

    claro = post["formato"] == "carrusel"
    _logo(ax, plt, 7 if not claro else 8.5, 3.2, 4.4, con_placa=claro)
    if post["fuente"]:
        ax.text(93, 5.4, "Fuente: " + post["fuente"], color=t["suave"], fontproperties=f["texto"],
                fontsize=7.5, ha="right", va="center", zorder=6)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(ruta, dpi=180)
    plt.close(fig)
    return ruta


# ------------------------------ Páginas del PDF -----------------------------
def _registrar_fuentes_pdf():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    nombres = {}
    for nombre, archivo, respaldo in (("Lato", "Lato-Regular.ttf", "Helvetica"),
                                      ("Lato-Bold", "Lato-Bold.ttf", "Helvetica-Bold"),
                                      ("Montserrat-Bold", "Montserrat-Bold.ttf", "Helvetica-Bold")):
        ruta = FUENTES_DIR / archivo
        try:
            if nombre not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(nombre, str(ruta)))
            nombres[nombre] = nombre
        except Exception:
            nombres[nombre] = respaldo
    return nombres


def paginas_posts(posts, carpeta: Path, prefijo: str = "post") -> bytes:
    """PDF (bytes): una galería con las imágenes juntas (2x2) y después los textos listos
    para publicar, seguidos, cada uno con su miniatura."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    fn = _registrar_fuentes_pdf()
    buf = io.BytesIO()
    lienzo = canvas.Canvas(buf, pagesize=A4)
    ancho, alto = A4
    margen = 40
    piso = 48
    rutas = [dibujar_post(p, Path(carpeta) / f"{prefijo}_{i + 1}.png", i + 1) for i, p in enumerate(posts)]

    def _encabezado(titulo, sub):
        lienzo.setFillColorRGB(0.008, 0.518, 0.780)
        lienzo.setFont(fn["Montserrat-Bold"], 12)
        lienzo.drawString(margen, alto - 46, titulo)
        lienzo.setFillColorRGB(0.39, 0.45, 0.55)
        lienzo.setFont(fn["Lato"], 8)
        lienzo.drawString(margen, alto - 58, sub)
        return alto - 74

    # 1) Galería: las cuatro piezas juntas, como se verían en el feed.
    y = _encabezado("Propuestas de publicación para LinkedIn",
                    "Imágenes de ejemplo con la plantilla de Enersinc. Revise cifras y fuentes antes de publicar.")
    hueco = 14
    lado = (ancho - 2 * margen - hueco) / 2
    for i, (post, ruta) in enumerate(zip(posts, rutas)):
        col, fila = i % 2, i // 2
        x = margen + col * (lado + hueco)
        y_img = y - lado - fila * (lado + 34)
        lienzo.drawImage(str(ruta), x, y_img, width=lado, height=lado)
        lienzo.setFillColorRGB(0.008, 0.518, 0.780)
        lienzo.setFont(fn["Lato-Bold"], 7.5)
        lienzo.drawString(x, y_img - 11, f"IDEA {i + 1} · {ETIQUETA[post['formato']]}")
        lienzo.setFillColorRGB(0.06, 0.09, 0.16)
        lienzo.setFont(fn["Montserrat-Bold"], 8.5)
        titular = post["titular"]
        while stringWidth(titular, fn["Montserrat-Bold"], 8.5) > lado and " " in titular:
            titular = titular.rsplit(" ", 1)[0]
        lienzo.drawString(x, y_img - 22, titular + ("…" if titular != post["titular"] else ""))

    # 2) Textos seguidos: miniatura + texto; salta de página solo cuando no cabe.
    lienzo.showPage()
    y = _encabezado("Textos listos para publicar", "Copie el texto, adjunte la imagen correspondiente y publique.")
    mini = 74
    x_txt = margen + mini + 14

    def _parrafos(texto, fuente, tam, interlinea, y, x, w):
        lienzo.setFont(fuente, tam)
        for parrafo in str(texto).split("\n"):
            linea = ""
            for palabra in parrafo.split() + [None]:
                prueba = (linea + " " + palabra).strip() if palabra else None
                if palabra and stringWidth(prueba, fuente, tam) <= w:
                    linea = prueba
                    continue
                if y < piso:
                    lienzo.showPage()
                    y = alto - 50
                    lienzo.setFont(fuente, tam)
                lienzo.drawString(x, y, linea)
                y -= interlinea
                linea = palabra or ""
            y -= interlinea * 0.3
        return y

    for i, (post, ruta) in enumerate(zip(posts, rutas)):
        if y < piso + mini + 60:
            lienzo.showPage()
            y = alto - 50
        lienzo.drawImage(str(ruta), margen, y - mini, width=mini, height=mini)
        y_cab = y - 9
        lienzo.setFillColorRGB(0.008, 0.518, 0.780)
        lienzo.setFont(fn["Lato-Bold"], 7.5)
        lienzo.drawString(x_txt, y_cab, f"IDEA {i + 1} · {ETIQUETA[post['formato']]}")
        lienzo.setFillColorRGB(0.06, 0.09, 0.16)
        y_cab = _parrafos(post["titular"], fn["Montserrat-Bold"], 10.5, 13, y_cab - 14, x_txt,
                          ancho - margen - x_txt)
        extra = []
        if post["formato"] == "carrusel" and post["laminas"]:
            extra = [f"Lámina {k + 1}: {s}" for k, s in enumerate(post["laminas"])]
        elif post["formato"] == "video" and post["guion"]:
            extra = [f"Escena {k + 1}: {s}" for k, s in enumerate(post["guion"])]
        elif post["formato"] == "pregunta" and post.get("opciones"):
            extra = ["Encuesta: " + " / ".join(post["opciones"])]
        if extra:
            lienzo.setFillColorRGB(0.28, 0.33, 0.41)
            y_cab = _parrafos("\n".join(extra), fn["Lato"], 8, 10.5, y_cab, x_txt, ancho - margen - x_txt)
        y = min(y_cab, y - mini) - 8
        lienzo.setFillColorRGB(0.12, 0.16, 0.23)
        cuerpo = post["texto"] + ("\n" + " ".join(post["hashtags"]) if post["hashtags"] else "")
        y = _parrafos(cuerpo, fn["Lato"], 9, 12, y, margen, ancho - 2 * margen)
        if i < len(posts) - 1:
            lienzo.setStrokeColorRGB(0.85, 0.89, 0.94)
            lienzo.setLineWidth(0.6)
            lienzo.line(margen, y + 2, ancho - margen, y + 2)
            y -= 16
    lienzo.save()
    return buf.getvalue()
