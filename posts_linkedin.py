"""
Propuestas de publicación para LinkedIn: ideas creativas ya hechas, cada una como
imagen de ejemplo con la plantilla de Enersinc (fondo azul noche, Montserrat/Lato,
logo) y el texto listo para publicar. Se añaden al PDF en páginas propias.

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
        '- "texto": el texto de la publicación listo para copiar, de 60 a 110 palabras, con una primera '
        "línea que enganche, el dato, la conexión con la solución de Enersinc y una pregunta o llamado a "
        "la acción al cierre\n"
        '- "hashtags": lista de 3 a 5 hashtags\n'
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
        "guion": [_limpio(re.sub(r"^\s*(gancho|escena\s*\d*|cierre)\s*[:.-]\s*", "", str(x), flags=re.I), 120)
                  for x in (post.get("guion") or []) if _limpio(x)][:4],
        "texto": _limpio(post.get("texto"), 900),
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


def dibujar_post(post: dict, ruta: Path, numero: int = 1) -> Path:
    """Imagen cuadrada 1080x1080 lista para LinkedIn."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, FancyBboxPatch, Polygon, Rectangle

    f = _fuentes_mpl()
    fig = plt.figure(figsize=(6, 6), dpi=180)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 100, 100, color=FONDO))
    # Arco decorativo y franja de acento (identidad de la plantilla).
    ax.add_patch(Circle((96, 92), 30, color=FONDO_2))
    ax.add_patch(Circle((96, 92), 18, color=FONDO))
    ax.add_patch(Rectangle((0, 0), 100, 1.2, color=CELESTE))

    formato = post["formato"]
    etiqueta = ETIQUETA[formato] + (" · 1/4" if formato == "carrusel" else "")
    ax.add_patch(FancyBboxPatch((7, 86.5), 4 + len(etiqueta) * 1.3, 6, boxstyle="round,pad=0,rounding_size=3",
                                color=AZUL))
    ax.text(9, 89.5, etiqueta, color=BLANCO, fontproperties=f["texto_b"], fontsize=10, va="center")

    if formato == "dato":
        ax.text(7, 76, _envolver(post["titular"], 30), color=BLANCO, fontproperties=f["titulo"],
                fontsize=19, va="top", linespacing=1.15)
        tam = 64 if len(post["cifra"]) <= 7 else 46
        ax.text(7, 44, post["cifra"], color=CELESTE, fontproperties=f["titulo"], fontsize=tam, va="center")
        ax.text(7, 27, _envolver(post["apoyo"], 46), color=GRIS, fontproperties=f["sub"],
                fontsize=12, va="top", linespacing=1.3)
    elif formato == "carrusel":
        ax.text(7, 76, _envolver(post["titular"], 26), color=BLANCO, fontproperties=f["titulo"],
                fontsize=23, va="top", linespacing=1.12)
        laminas = post["laminas"] or [post["apoyo"]]
        for i, lam in enumerate(laminas[:4]):
            x = 7 + i * 22
            ax.add_patch(FancyBboxPatch((x, 13), 19.5, 22, boxstyle="round,pad=0,rounding_size=2",
                                        color=FONDO_2, ec=AZUL, lw=1.2))
            ax.text(x + 2, 31.5, f"{i + 1:02d}", color=CELESTE, fontproperties=f["titulo"], fontsize=13, va="top")
            ax.text(x + 2, 25.5, _envolver(lam, 16), color=BLANCO, fontproperties=f["texto"],
                    fontsize=7.5, va="top", linespacing=1.2)
        ax.text(93, 40, "Desliza  →", color=CELESTE, fontproperties=f["texto_b"], fontsize=11, ha="right")
    elif formato == "video":
        ax.add_patch(Circle((50, 50), 11, color=AZUL))
        ax.add_patch(Polygon([(46.5, 56), (46.5, 44), (56.5, 50)], color=BLANCO))
        ax.text(7, 80, _envolver(post["titular"], 30), color=BLANCO, fontproperties=f["titulo"],
                fontsize=19, va="top", linespacing=1.15)
        gancho = post["guion"][0] if post["guion"] else post["apoyo"]
        ax.text(50, 30, _envolver(gancho, 44), color=GRIS, fontproperties=f["sub"],
                fontsize=11, va="top", ha="center", linespacing=1.3)
        ax.add_patch(Rectangle((7, 12.5), 86, 1, color=FONDO_2))
        ax.add_patch(Rectangle((7, 12.5), 24, 1, color=CELESTE))
        ax.text(93, 15.5, "0:00 / 1:00", color=GRIS, fontproperties=f["texto"], fontsize=8, ha="right")
    else:  # pregunta
        ax.text(7, 80, "“", color=AZUL, fontproperties=f["titulo"], fontsize=90, va="top")
        ax.text(7, 60, _envolver(post["titular"], 24), color=BLANCO, fontproperties=f["titulo"],
                fontsize=24, va="top", linespacing=1.12)
        ax.text(7, 25, _envolver(post["apoyo"], 46), color=GRIS, fontproperties=f["sub"],
                fontsize=12, va="top", linespacing=1.3)

    if post["fuente"]:
        ax.text(93, 4.5, "Fuente: " + post["fuente"], color=GRIS, fontproperties=f["texto"],
                fontsize=7.5, ha="right", va="center")
    if LOGO.is_file():
        logo = plt.imread(str(LOGO))
        alto = 5.2
        ancho = alto * logo.shape[1] / logo.shape[0]
        ax.imshow(logo, extent=(7, 7 + ancho, 2.2, 2.2 + alto), zorder=5)
        ax.set_xlim(0, 100)
        ax.set_ylim(0, 100)
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(ruta, dpi=180, facecolor=FONDO)
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


def _lineas(texto, fuente, tam, ancho):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    salida = []
    for parrafo in str(texto).split("\n"):
        linea = ""
        for palabra in parrafo.split():
            prueba = (linea + " " + palabra).strip()
            if stringWidth(prueba, fuente, tam) <= ancho:
                linea = prueba
            else:
                salida.append(linea)
                linea = palabra
        salida.append(linea)
    return salida


def paginas_posts(posts, carpeta: Path, prefijo: str = "post") -> bytes:
    """PDF (bytes) con dos publicaciones por página: imagen a la izquierda, texto a la derecha."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    fn = _registrar_fuentes_pdf()
    buf = io.BytesIO()
    lienzo = canvas.Canvas(buf, pagesize=A4)
    ancho, alto = A4
    margen = 40
    lado = 215
    x_txt = margen + lado + 16
    ancho_txt = ancho - margen - x_txt
    bloque = (alto - 110) / 2

    def _encabezado():
        lienzo.setFillColorRGB(0.008, 0.518, 0.780)
        lienzo.setFont(fn["Montserrat-Bold"], 11)
        lienzo.drawString(margen, alto - 46, "Propuestas de publicación para LinkedIn")
        lienzo.setFillColorRGB(0.39, 0.45, 0.55)
        lienzo.setFont(fn["Lato"], 8)
        lienzo.drawString(margen, alto - 58, "Imágenes de ejemplo con la plantilla de Enersinc y texto listo "
                                             "para publicar. Revise cifras y fuentes antes de publicar.")

    for i, post in enumerate(posts):
        if i % 2 == 0:
            if i:
                lienzo.showPage()
            _encabezado()
        y_top = alto - 74 - (i % 2) * bloque
        ruta = dibujar_post(post, Path(carpeta) / f"{prefijo}_{i + 1}.png", i + 1)
        lienzo.drawImage(str(ruta), margen, y_top - lado, width=lado, height=lado)

        y = y_top - 10
        lienzo.setFillColorRGB(0.008, 0.518, 0.780)
        lienzo.setFont(fn["Lato-Bold"], 8)
        lienzo.drawString(x_txt, y, f"IDEA {i + 1} · {ETIQUETA[post['formato']]}")
        y -= 15
        lienzo.setFillColorRGB(0.06, 0.09, 0.16)
        lienzo.setFont(fn["Montserrat-Bold"], 10.5)
        for ln in _lineas(post["titular"], fn["Montserrat-Bold"], 10.5, ancho_txt)[:3]:
            lienzo.drawString(x_txt, y, ln)
            y -= 13

        extra = []
        if post["formato"] == "carrusel" and post["laminas"]:
            extra = [f"Lámina {k + 1}: {t}" for k, t in enumerate(post["laminas"])]
        elif post["formato"] == "video" and post["guion"]:
            extra = [f"Escena {k + 1}: {t}" for k, t in enumerate(post["guion"])]
        limite = y_top - bloque + 18
        lienzo.setFont(fn["Lato"], 8)
        lienzo.setFillColorRGB(0.2, 0.25, 0.33)
        for item in extra:
            for ln in _lineas(item, fn["Lato"], 8, ancho_txt):
                if y < limite:
                    break
                lienzo.drawString(x_txt, y - 2, ln)
                y -= 10.5
        y -= 6
        lienzo.setFillColorRGB(0.12, 0.16, 0.23)
        lienzo.setFont(fn["Lato"], 8.5)
        cuerpo = post["texto"] + ("\n" + " ".join(post["hashtags"]) if post["hashtags"] else "")
        for ln in _lineas(cuerpo, fn["Lato"], 8.5, ancho_txt):
            if y < limite:
                break
            lienzo.drawString(x_txt, y, ln)
            y -= 11.5
    lienzo.save()
    return buf.getvalue()
