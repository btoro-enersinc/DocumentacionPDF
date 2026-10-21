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
DIAS_VENTANA = 10   # los informes solo usan datos de los últimos 10 días
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
    # XM corrige y completa los últimos días: solo se reutilizan ventanas ya cerradas
    # (p. ej. el año anterior); lo reciente se consulta siempre en vivo.
    cacheable = fin < date.today() - timedelta(days=10)
    if cacheable and path.is_file():
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
    if cacheable:
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
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
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
    ("aportes", re.compile(r"aporte|hidrolog|caudal|\bnino\b|sequia|hidric"),
     _datos_aportes, _dibujar_aportes, "aportes", "Aportes hídricos"),
    ("embalses", re.compile(r"embalse|volumen util|reserva|\bnino\b|sequia"),
     _datos_embalses, _dibujar_embalses, "vol", "Embalses"),
    ("precio", re.compile(r"precio de bolsa|precio bolsa|bolsa nacional|\bbolsa\b|escasez|tarifa"),
     _datos_precio, _dibujar_precio, "bolsa", "Precio de bolsa"),
    ("demanda", re.compile(r"\bdemanda\b"),
     _datos_demanda, _dibujar_demanda, "demanda", "Demanda"),
)


# --------------------------- Resumen numérico --------------------------------
def _prom(serie):
    return sum(v for _, v in serie) / len(serie) if serie else None


def _entre(serie, ini: date, fin: date):
    return [(f, v) for f, v in serie if ini <= f <= fin]


def _valor_en(serie, dia: date, tolerancia: int = 3):
    """Valor del día pedido o del más cercano anterior (hasta `tolerancia` días)."""
    por_dia = dict(serie)
    for k in range(tolerancia + 1):
        d = dia - timedelta(days=k)
        if d in por_dia:
            return d, por_dia[d]
    return None, None


def _fmt(v, dec=1):
    return "s/d" if v is None else f"{v:.{dec}f}"


def _var(a, b):
    return None if a is None or not b else (a / b - 1) * 100


def resumen_mercado(hoy: date = None):
    """
    Indicadores del mercado mayorista con datos de la API de XM (últimos DIAS_VENTANA días y
    las mismas fechas del año anterior como referencia). Devuelve dict con `texto` (tabla legible que
    el informe usa como evidencia oficial), `hasta` (último día con datos) o None.
    """
    hoy = hoy or date.today()
    fin = hoy - timedelta(days=1)
    ini = fin - timedelta(days=DIAS_VENTANA - 1)
    ini_a, fin_a = ini - timedelta(days=365), fin - timedelta(days=365)
    tareas = {
        "bolsa": (serie_horaria_agregada, "PrecBolsNaci", ini, fin, "promedio"),
        "bolsa_a": (serie_horaria_agregada, "PrecBolsNaci", ini_a, fin_a, "promedio"),
        "escasez": (serie_diaria, "PrecEscaAct", ini, fin),
        "contrato": (serie_diaria, "PrecPromCont", ini, fin),
        "cont_reg": (serie_horaria_agregada, "PrecPromContRegu", ini, fin, "promedio"),
        "cont_noreg": (serie_horaria_agregada, "PrecPromContNoRegu", ini, fin, "promedio"),
        "comp_bolsa": (serie_horaria_agregada, "CompBolsNaciEner", ini, fin, "suma", 1e-6),
        "comp_cont": (serie_horaria_agregada, "CompContEner", ini, fin, "suma", 1e-6),
        "embalse": (serie_diaria, "PorcVoluUtilDiar", ini, fin, 100.0),
        "embalse_a": (serie_diaria, "PorcVoluUtilDiar", ini_a, fin_a, 100.0),
        "aportes": (serie_diaria, "AporEner", ini, fin, 1e-6),
        "media": (serie_diaria, "AporEnerMediHist", ini, fin, 1e-6),
        "demanda": (serie_horaria_agregada, "DemaReal", ini, fin, "suma", 1e-6),
        "demanda_a": (serie_horaria_agregada, "DemaReal", ini_a, fin_a, "suma", 1e-6),
        "potencia": (serie_diaria, "DemaMaxPot", ini, fin, 1e-3),
    }

    def _cargar(item):
        clave, (fn, *args) = item
        try:
            return clave, fn(*args)
        except (requests.RequestException, ValueError, KeyError):
            return clave, []

    with ThreadPoolExecutor(max_workers=8) as ex:
        s = dict(ex.map(_cargar, tareas.items()))
    s["demanda"], _ = _sin_preliminares(s["demanda"])
    if not (s["bolsa"] or s["embalse"] or s["demanda"]):
        return None

    lineas = []
    ultimo_dia = max(f for k in ("bolsa", "embalse", "aportes", "demanda") for f, _ in s[k] or [(ini, 0)])

    # Precio de bolsa
    if s["bolsa"]:
        f_ult, v_ult = s["bolsa"][-1]
        u7 = s["bolsa"][-5:]
        p7 = s["bolsa"][:-5]
        fechas = {f for f, _ in s["bolsa"]}
        pa = _prom([(f, v) for f, v in s["bolsa_a"] if f + timedelta(days=365) in fechas])
        lineas += [
            "PRECIO DE BOLSA NACIONAL (promedio diario, COP/kWh):",
            f"- Último día con dato ({f_ult:%d/%m/%Y}): {_fmt(v_ult)} COP/kWh",
            f"- Promedio últimos 5 días ({u7[0][0]:%d/%m}–{u7[-1][0]:%d/%m/%Y}): {_fmt(_prom(u7))} COP/kWh",
            f"- Promedio de los días anteriores de la ventana: {_fmt(_prom(p7))} COP/kWh "
            f"(variación frente a esos días {_fmt(_var(_prom(u7), _prom(p7)))} %)",
            f"- Promedio últimos {len(s['bolsa'])} días ({s['bolsa'][0][0]:%d/%m}–{f_ult:%d/%m/%Y}): "
            f"{_fmt(_prom(s['bolsa']))} COP/kWh; mínimo {_fmt(min(v for _, v in s['bolsa']))}, "
            f"máximo {_fmt(max(v for _, v in s['bolsa']))}",
        ]
        if pa:
            lineas.append(f"- Mismas fechas del año anterior ({s['bolsa'][0][0] - timedelta(days=365):%d/%m/%Y}–"
                          f"{f_ult - timedelta(days=365):%d/%m/%Y}): "
                          f"{_fmt(pa)} COP/kWh (variación anual {_fmt(_var(_prom(s['bolsa']), pa))} %)")
    if s["bolsa"] and len(s["bolsa"]) > 2:
        media = _prom(s["bolsa"])
        desv = (sum((v - media) ** 2 for _, v in s["bolsa"]) / (len(s["bolsa"]) - 1)) ** 0.5
        lineas.append(f"- Volatilidad: desviación estándar del precio diario en el periodo {_fmt(desv)} COP/kWh "
                      f"({_fmt(desv / media * 100)} % del promedio)")
    if s["escasez"]:
        esc = s["escasez"][-1][1]
        lineas.append(f"- Precio de escasez de activación vigente ({s['escasez'][-1][0]:%d/%m/%Y}): "
                      f"{_fmt(esc)} COP/kWh")
        if s["bolsa"]:
            sobre = sum(1 for _, v in s["bolsa"] if v > esc)
            lineas.append(f"- Días del periodo con precio de bolsa por encima del precio de escasez de activación: "
                          f"{sobre} de {len(s['bolsa'])}; el último precio diario equivale al "
                          f"{_fmt(s['bolsa'][-1][1] / esc * 100)} % del precio de escasez")

    # Contratos y exposición
    if s["contrato"] or s["cont_reg"]:
        lineas.append("PRECIO PROMEDIO DE CONTRATOS (COP/kWh):")
        for clave, nombre in (("contrato", "Total contratos"), ("cont_reg", "Mercado regulado"),
                              ("cont_noreg", "Mercado no regulado")):
            if s[clave]:
                lineas.append(f"- {nombre}: último dato ({s[clave][-1][0]:%d/%m/%Y}) {_fmt(s[clave][-1][1])}; "
                              f"promedio últimos 5 días {_fmt(_prom(s[clave][-5:]))}")
        if s["bolsa"] and s["contrato"]:
            dif = _prom(s["bolsa"][-5:]) - _prom(s["contrato"][-5:])
            lineas.append(f"- Diferencia bolsa menos contrato (promedio últimos 5 días): {_fmt(dif)} COP/kWh; "
                          f"la bolsa está {_fmt(_var(_prom(s['bolsa'][-5:]), _prom(s['contrato'][-5:])))} % "
                          "por encima del precio promedio de contratos")
    if s["comp_bolsa"] and s["comp_cont"]:
        b7 = sum(v for _, v in s["comp_bolsa"][-5:])
        c7 = sum(v for _, v in s["comp_cont"][-5:])
        if b7 + c7:
            lineas.append(f"- Compras en bolsa como porcentaje de las compras totales del sistema "
                          f"(bolsa + contratos, últimos 5 días): {_fmt(b7 / (b7 + c7) * 100)} %")

    # Embalses
    if s["embalse"]:
        f_ult, v_ult = s["embalse"][-1]
        f7, v7 = _valor_en(s["embalse"], f_ult - timedelta(days=5))
        f30, v30 = s["embalse"][0]
        fa, va = _valor_en(s["embalse_a"], f_ult - timedelta(days=365))
        lineas += [
            "EMBALSES DEL SIN (volumen útil, % de la capacidad útil):",
            f"- {f_ult:%d/%m/%Y}: {_fmt(v_ult, 2)} %",
        ]
        if v7 is not None:
            lineas.append(f"- {f7:%d/%m/%Y}: {_fmt(v7, 2)} % (cambio en 5 días {_fmt(v_ult - v7, 2)} puntos)")
        lineas.append(f"- {f30:%d/%m/%Y}: {_fmt(v30, 2)} % (cambio en los últimos 10 días {_fmt(v_ult - v30, 2)} puntos)")
        if va is not None:
            lineas.append(f"- Mismo día del año anterior ({fa:%d/%m/%Y}): {_fmt(va, 2)} % "
                          f"(diferencia {_fmt(v_ult - va, 2)} puntos)")

    # Aportes
    if s["aportes"] and s["media"]:
        media = dict(s["media"])
        par = [(f, v, media[f]) for f, v in s["aportes"] if media.get(f)]
        if par:
            def _pct(trozo):
                return sum(v for _, v, _ in trozo) / sum(m for _, _, m in trozo) * 100
            lineas += [
                "APORTES HÍDRICOS AL SIN (energía, % de la media histórica):",
                f"- Último día ({par[-1][0]:%d/%m/%Y}): {_fmt(par[-1][1])} GWh, {_fmt(_pct(par[-1:]))} % de la media",
                f"- Últimos 5 días: {_fmt(_pct(par[-5:]))} % de la media histórica",
                f"- Últimos {len(par)} días: {_fmt(_pct(par))} % de la media histórica",
            ]

    # Demanda
    if s["demanda"]:
        f_ult, v_ult = s["demanda"][-1]
        u7 = s["demanda"][-5:]
        ant = dict(s["demanda_a"])
        par_a = [(v, ant.get(f - timedelta(days=364))) for f, v in u7]   # mismo día de la semana
        par_a = [(v, a) for v, a in par_a if a]
        lineas += [
            "DEMANDA REAL DE ENERGÍA DEL SIN (GWh/día):",
            f"- Último día completo ({f_ult:%d/%m/%Y}): {_fmt(v_ult, 2)} GWh",
            f"- Promedio últimos 5 días ({u7[0][0]:%d/%m}–{f_ult:%d/%m/%Y}): {_fmt(_prom(u7), 2)} GWh/día",
            f"- Máximo de los últimos {len(s['demanda'])} días: {_fmt(max(v for _, v in s['demanda']), 2)} GWh",
        ]
        if par_a:
            lineas.append(f"- Variación de los últimos 5 días frente a los mismos días de la semana del año "
                          f"anterior: {_fmt(_var(sum(v for v, _ in par_a), sum(a for _, a in par_a)))} %")
    if s["potencia"] and s["potencia"][-1][0] >= fin - timedelta(days=10):   # XM la publica con rezago
        f_max, v_max = max(s["potencia"], key=lambda x: x[1])
        lineas.append(f"- Demanda máxima de potencia: último dato ({s['potencia'][-1][0]:%d/%m/%Y}) "
                      f"{_fmt(s['potencia'][-1][1], 0)} MW; máximo del periodo {_fmt(v_max, 0)} MW ({f_max:%d/%m/%Y})")

    encabezado = (f"Datos oficiales de XM (API pública de Sinergox, servapibi.xm.com.co) consultados el "
                  f"{hoy:%d/%m/%Y}. Cifras calculadas por Enersinc a partir de las series diarias.")
    return {"texto": encabezado + "\n" + "\n".join(lineas), "hasta": ultimo_dia, "desde": ini}


def temas_detectados(pregunta: str):
    """
    Decide solo con la pregunta si hay que entrar a Sinergox.
    Entra cuando nombra una serie que XM publica (precio, embalses, aportes, demanda).
    Una pregunta solo normativa (resolución, ley, decreto) no entra.
    """
    t = _sin_acentos(pregunta)
    if re.search(r"\bsin graficas?\b|\bno incluy", t):
        return []
    return [c[0] for c in _CATALOGO if c[1].search(t)][:MAX_GRAFICAS]


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
