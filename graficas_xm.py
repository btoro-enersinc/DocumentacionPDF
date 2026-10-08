# -*- coding: utf-8 -*-
"""
Gráficas con datos oficiales de XM (los mismos que publica Sinergox) para el PDF.

Sinergox no exporta sus visores como imagen; las series salen de la API pública
servapibi.xm.com.co y aquí se dibujan una por una. Solo se generan las que la
pregunta pide explícitamente (o menciona con claridad).
"""
import hashlib
import json
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import requests  # noqa: E402

API_XM = "https://servapibi.xm.com.co"
DIAS_VENTANA = 30
MAX_GRAFICAS = 4
TIMEOUT_S = 18
DPI = 150
CACHE_DIR = Path(__file__).resolve().parent / "data" / "cache_xm"

AZUL = "#0284c7"
AZUL_OSCURO = "#0f172a"
GRIS = "#64748b"
AMBAR = "#d97706"
ROJO = "#dc2626"

ANCHO_PULG = 6.6
ALTO_PULG = 2.55

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "EnersincDocumentacion/1.0", "Accept": "application/json"})
_LOCK = threading.Lock()


def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").lower()


# ------------------------------- Datos XM -----------------------------------
def _cache_path(periodo: str, metrica: str, entidad: str, ini: date, fin: date) -> Path:
    clave = f"{periodo}|{metrica}|{entidad}|{ini}|{fin}"
    return CACHE_DIR / (hashlib.sha1(clave.encode()).hexdigest() + ".json")


def _consultar(periodo: str, metrica: str, entidad: str, ini: date, fin: date):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(periodo, metrica, entidad, ini, fin)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    r = _SESSION.post(
        f"{API_XM}/{periodo}",
        json={"MetricId": metrica, "StartDate": ini.isoformat(), "EndDate": fin.isoformat(),
              "Entity": entidad, "Filter": []},
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    items = r.json().get("Items") or []
    try:
        path.write_text(json.dumps(items), encoding="utf-8")
    except OSError:
        pass
    return items


def _flotante(valor):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def serie_diaria(metrica: str, ini: date, fin: date, escala: float = 1.0, entidad: str = "Sistema"):
    salida = []
    for it in _consultar("daily", metrica, entidad, ini, fin):
        for e in it.get("DailyEntities") or []:
            v = _flotante(e.get("Value"))
            if e.get("Id") == entidad and v is not None:
                salida.append((datetime.strptime(it["Date"], "%Y-%m-%d").date(), v * escala))
    return salida


def serie_horaria_agregada(metrica: str, ini: date, fin: date, modo: str, escala: float = 1.0,
                           entidad: str = "Sistema"):
    salida = []
    for it in _consultar("hourly", metrica, entidad, ini, fin):
        for e in it.get("HourlyEntities") or []:
            if e.get("Id") != entidad:
                continue
            horas = [_flotante(v) for k, v in (e.get("Values") or {}).items() if k.startswith("Hour")]
            horas = [h for h in horas if h is not None]
            if len(horas) < 20:
                continue
            total = sum(horas)
            valor = total / len(horas) if modo == "promedio" else total
            salida.append((datetime.strptime(it["Date"], "%Y-%m-%d").date(), valor * escala))
    return salida


# ------------------------------- Gráficas -----------------------------------
def _estilo(ax, titulo: str, unidad: str):
    ax.set_title(titulo, fontsize=9, fontweight="bold", color=AZUL_OSCURO, loc="left", pad=8)
    ax.set_ylabel(unidad, fontsize=7.5, color=GRIS)
    ax.tick_params(labelsize=7, colors=GRIS, length=2)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=5))
    ax.grid(axis="y", color="#e2e8f0", linewidth=0.6)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    for lado in ("left", "bottom"):
        ax.spines[lado].set_color("#cbd5e1")


def _etiqueta_final(ax, serie, color, formato):
    f, v = serie[-1]
    ax.annotate(formato(v), xy=(f, v), xytext=(5, 0), textcoords="offset points",
                fontsize=7, color=color, fontweight="bold", va="center")


def _sin_preliminares(serie, umbral: float = 0.6):
    if len(serie) < 7:
        return serie, 0
    mediana = sorted(v for _, v in serie)[len(serie) // 2]
    n = len(serie)
    while n and serie[n - 1][1] < umbral * mediana:
        n -= 1
    return serie[:n], len(serie) - n


def _datos_aportes(ini, fin):
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(serie_diaria, "AporEner", ini, fin, 1e-6)
        f2 = ex.submit(serie_diaria, "AporEnerMediHist", ini, fin, 1e-6)
        return {"aportes": f1.result(), "media": f2.result()}


def _dibujar_aportes(ax, d):
    f, v = zip(*d["aportes"])
    ax.fill_between(f, v, color=AZUL, alpha=0.12)
    ax.plot(f, v, color=AZUL, linewidth=1.8, label="Aportes reales")
    if d["media"]:
        fm, vm = zip(*d["media"])
        ax.plot(fm, vm, color=AMBAR, linewidth=1.3, linestyle="--", label="Media histórica")
    _estilo(ax, "Aportes hídricos al SIN", "GWh/día")
    _etiqueta_final(ax, d["aportes"], AZUL, lambda x: f"{x:,.0f}".replace(",", "."))
    ax.legend(fontsize=7, frameon=False, loc="upper left", ncol=2)
    ax.margins(x=0.06)
    ax.set_ylim(0, max([max(v)] + [x for _, x in d["media"]]) * 1.22)


def _datos_embalses(ini, fin):
    return {"vol": serie_diaria("PorcVoluUtilDiar", ini, fin, 100.0)}


def _dibujar_embalses(ax, d):
    f, v = zip(*d["vol"])
    ax.fill_between(f, v, color=AZUL, alpha=0.12)
    ax.plot(f, v, color=AZUL, linewidth=1.8)
    _estilo(ax, "Volumen útil de embalses del SIN", "% del volumen útil")
    _etiqueta_final(ax, d["vol"], AZUL, lambda x: f"{x:.1f} %".replace(".", ","))
    ax.margins(x=0.06)
    ax.set_ylim(0, 100)


def _datos_precio(ini, fin):
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(serie_horaria_agregada, "PrecBolsNaci", ini, fin, "promedio")
        f2 = ex.submit(serie_diaria, "PrecEscaAct", ini, fin)
        return {"bolsa": f1.result(), "escasez": f2.result()}


def _dibujar_precio(ax, d):
    f, v = zip(*d["bolsa"])
    ax.plot(f, v, color=AZUL, linewidth=1.8, label="Precio de bolsa (promedio diario)")
    if d["escasez"]:
        fe, ve = zip(*d["escasez"])
        ax.plot(fe, ve, color=ROJO, linewidth=1.3, linestyle="--", label="Precio de escasez de activación")
    _estilo(ax, "Precio de bolsa nacional", "COP/kWh")
    _etiqueta_final(ax, d["bolsa"], AZUL, lambda x: f"{x:,.0f}".replace(",", "."))
    ax.legend(fontsize=7, frameon=False, loc="upper left")
    ax.margins(x=0.06, y=0.22)


def _datos_demanda(ini, fin):
    serie, omitidos = _sin_preliminares(serie_horaria_agregada("DemaReal", ini, fin, "suma", 1e-6))
    return {"demanda": serie, "_omitidos": omitidos}


def _dibujar_demanda(ax, d):
    f, v = zip(*d["demanda"])
    ax.bar(f, v, color=AZUL, alpha=0.88, width=0.72)
    _estilo(ax, "Demanda real de energía del SIN", "GWh/día")
    ax.set_ylim(0, max(v) * 1.12)
    ax.margins(x=0.03)


# clave, patrón (sobre pregunta sin acentos), carga, dibujo, serie mínima, título
_CATALOGO = (
    ("aportes", re.compile(r"aporte|hidrolog|caudal"),
     _datos_aportes, _dibujar_aportes, "aportes", "Aportes hídricos"),
    ("embalses", re.compile(r"embalse|volumen util|reserva(?:s)? hidric"),
     _datos_embalses, _dibujar_embalses, "vol", "Embalses"),
    ("precio", re.compile(r"precio de bolsa|precio bolsa|bolsa nacional|precio de escasez|\bescasez\b"),
     _datos_precio, _dibujar_precio, "bolsa", "Precio de bolsa"),
    ("demanda", re.compile(r"\bdemanda\b"),
     _datos_demanda, _dibujar_demanda, "demanda", "Demanda"),
)


def temas_detectados(pregunta: str):
    """
    Solo lee la pregunta. Genera gráficas si:
      a) pide gráficas/indicadores y nombra series, o
      b) pide ver/mostrar la evolución de una serie (sin decir 'gráfica').
    Una resolución que solo menciona embalses en el análisis NO activa gráficas.
    """
    t = _sin_acentos(pregunta)
    pide_grafica = bool(re.search(
        r"\bgraficas?\b|\bcharts?\b|\bindicadores?\b|"
        r"\bincluy[ea].{0,40}graf|\bcon graf|\bagrega.{0,40}graf|\banexa.{0,40}graf|"
        r"\bmuestr[ae].{0,40}(graf|evolucion|serie|tendencia)|"
        r"\bevolucion\b|\btendencia\b|\bcomo van\b|\bcomo esta(?:n)?\b",
        t,
    ))
    if not pide_grafica:
        return []
    nombradas = [c[0] for c in _CATALOGO if c[1].search(t)]
    # "incluye gráficas" sin nombrar series -> las 4 de mercado
    return (nombradas or [c[0] for c in _CATALOGO])[:MAX_GRAFICAS]


def _guardar_una(clave, dibujar, datos, ruta: Path):
    fig, ax = plt.subplots(1, 1, figsize=(ANCHO_PULG, ALTO_PULG))
    dibujar(ax, datos)
    fig.tight_layout(pad=0.55)
    with _LOCK:
        fig.savefig(ruta, dpi=DPI, facecolor="white", bbox_inches="tight", pad_inches=0.18)
        plt.close(fig)
    return ruta


def generar_graficas(pregunta: str, ruta_base, hoy: date = None):
    """
    Genera una PNG por gráfica (espaciadas en el PDF) según lo pedido en `pregunta`.
    `ruta_base` es la ruta del PNG principal (…_Reporte_….png); las demás se
    nombran …_aportes.png, …_embalses.png, etc.

    Devuelve dict con `imagenes` [{ruta, ancho_pt, alto_pt, titulo}, …] o None.
    """
    claves = temas_detectados(pregunta)
    if not claves:
        return None
    hoy = hoy or date.today()
    ini, fin = hoy - timedelta(days=DIAS_VENTANA), hoy
    elegidas = [c for c in _CATALOGO if c[0] in claves]
    base = Path(ruta_base)
    base.parent.mkdir(parents=True, exist_ok=True)

    def _una(c):
        clave, _pat, cargar, dibujar, serie_min, titulo = c
        try:
            datos = cargar(ini, fin)
        except (requests.RequestException, ValueError, KeyError):
            return None
        if not datos or not datos.get(serie_min):
            return None
        destino = base if len(elegidas) == 1 else base.with_name(f"{base.stem}_{clave}{base.suffix}")
        _guardar_una(clave, dibujar, datos, destino)
        fechas = [f for serie in datos.values() if isinstance(serie, list) for f, _ in serie]
        return {
            "clave": clave,
            "titulo": titulo,
            "ruta": destino,
            "ancho_pt": 490,
            "alto_pt": 190,
            "desde": min(fechas) if fechas else ini,
            "hasta": max(fechas) if fechas else fin,
            "omitidos": datos.get("_omitidos", 0),
        }

    with ThreadPoolExecutor(max_workers=min(4, len(elegidas))) as ex:
        resultados = [f.result() for f in as_completed([ex.submit(_una, c) for c in elegidas])]
    # Mantener el orden del catálogo (no el de finalización).
    por_clave = {r["clave"]: r for r in resultados if r}
    imagenes = [por_clave[c[0]] for c in elegidas if c[0] in por_clave]
    if not imagenes:
        return None

    return {
        "imagenes": imagenes,
        "titulos": [i["clave"] for i in imagenes],
        "desde": min(i["desde"] for i in imagenes),
        "hasta": max(i["hasta"] for i in imagenes),
        "consultado": hoy,
        "preliminares_omitidos": sum(i.get("omitidos", 0) for i in imagenes),
        # Compatibilidad con el nombre antiguo (una sola ruta).
        "ruta": imagenes[0]["ruta"],
        "ancho_pt": 490,
        "alto_pt": 190 * len(imagenes),
    }
