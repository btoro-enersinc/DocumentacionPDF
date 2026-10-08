# -*- coding: utf-8 -*-
"""Historial de reportes: N.º (ID en BD), fecha, título, destinatario y si el PDF existe."""
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PDF_DIR = DATA_DIR / "pdfs"
DB_PATH = DATA_DIR / "reportes.db"


def _ruta_absoluta(ruta_guardada: str) -> Path:
    ruta = (ruta_guardada or "").strip()
    if not ruta:
        return None
    for cand in (Path(ruta), DATA_DIR / ruta, BASE_DIR / ruta):
        if cand.is_file():
            return cand
    return PDF_DIR / Path(ruta.replace("\\", "/")).name


def ver_historial_reportes():
    if not DB_PATH.is_file():
        print("No hay base de datos aún. Genera un reporte primero.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    filas = conn.execute(
        """
        SELECT id, fecha_creacion, titulo_reporte, correo_destino, ruta_pdf, estado_envio
        FROM reportes
        ORDER BY id
        """
    ).fetchall()
    conn.close()

    print("\n================ HISTORIAL DE REPORTES ================")
    if not filas:
        print("No hay reportes registrados.")
        return
    existentes = 0
    for rid, fecha, titulo, correo, ruta, estado in filas:
        pdf = _ruta_absoluta(ruta)
        existe = bool(pdf and pdf.is_file())
        existentes += existe
        print(f"N.º {rid:04d} | {fecha} | {correo}")
        print(f"     Título: {titulo}")
        print(f"     Estado: {estado} | Archivo: {'SÍ existe' if existe else 'NO existe'}")
        print(f"     PDF: {pdf if existe else (ruta or '(sin ruta)')}")
        print("-" * 65)
    print(f"Total: {len(filas)} reportes · {existentes} con PDF disponible en {PDF_DIR}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ver_historial_reportes()
