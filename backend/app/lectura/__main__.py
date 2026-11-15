"""CLI: read PDFs, segment them, store the segments and print them per PDF.

    python -m app.lectura <carpeta|pdf> [--max N] [--convencion dot_thousands|comma_thousands|auto]
        [--moneda PYG] [--llm | --llm-modo off|dudosas|todas] [--llm-max-paginas N]
        [--llm-min-confianza X] [--no-guardar] [--workers N]

Examples (from backend/):
    python -m app.lectura "$DATA_DIR/DocJustificativa/22-PY-136 - AECID" --convencion dot_thousands
    python -m app.lectura "$DATA_DIR/DocJustificativa/24-UG-226 Adjumani - Xunta 2024" --max 20
"""

import argparse
import re
import sys
import time
from pathlib import Path

from app.config import get_settings
from app.lectura import fields
from app.lectura.pipeline import PdfResult, list_pdfs, process_pdfs
from app.lectura.vision import LLM_MODES, LlmReadOptions


def evenly_spaced[T](items: list[T], n: int) -> list[T]:
    """`n` items spread uniformly over the list (a representative sample, not just the head)."""
    return [items[i * len(items) // n] for i in range(n)]


def _fmt(value: object) -> str:
    return "-" if value in (None, "") else str(value)


def print_result(result: PdfResult, base: Path | None) -> None:
    try:
        name = str(result.path.relative_to(base)) if base else result.path.name
    except ValueError:
        name = result.path.name
    if result.error:
        print(f"\n## {name}  [ERROR: {result.error}]")
        return
    fuentes = {p.source for p in result.pages}
    errores = [p.n for p in result.pages if p.error]
    extra = f" · páginas con error: {errores}" if errores else ""
    print(
        f"\n## {name}  ({result.n_pages} pág., {'+'.join(sorted(fuentes))}, "
        f"{len(result.segments)} segmentos){extra}"
    )
    for i, s in enumerate(result.segments, 1):
        rango = (
            f"{s.pagina_inicio}"
            if s.pagina_inicio == s.pagina_fin
            else (f"{s.pagina_inicio}-{s.pagina_fin}")
        )
        sello = "sí" if s.sello_donante else "no"
        if s.sello_donante:
            sello += f" (exp={_fmt(s.sello_expediente)}, {_fmt(s.sello_porcentaje)}%)"
        print(
            f"  {i}. pág {rango:<6} {s.tipo.value:<16} emisor={_fmt(s.emisor)[:40]} · "
            f"NIF={_fmt(s.nif)} · fact={_fmt(s.numero_factura)} · fecha={_fmt(s.fecha)} · "
            f"importe={_fmt(s.importe)} {_fmt(s.moneda)} · orden={_fmt(s.numero_orden)} · "
            f"sello={sello} · pago={'sí' if s.prueba_pago else 'no'} · "
            f"legible={'sí' if s.legible else 'no'}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.lectura", description=__doc__)
    ap.add_argument("ruta", type=Path, help="carpeta con PDFs (recursivo) o un PDF")
    ap.add_argument(
        "--max",
        type=int,
        default=None,
        help="procesar solo N PDFs, repartidos de forma uniforme por la lista",
    )
    ap.add_argument(
        "--excluir",
        default=None,
        help="regex sobre el nombre de archivo a ignorar (p. ej. normativa)",
    )
    ap.add_argument(
        "--convencion",
        choices=fields.CONVENTIONS,
        default="auto",
        help="formato de importes: dot_thousands (Paraguay), comma_thousands, auto",
    )
    ap.add_argument("--moneda", default=None, help="moneda por defecto si no aparece (PYG, UGX…)")
    ap.add_argument(
        "--llm",
        action="store_true",
        help="atajo de --llm-modo todas (exige ALLOW_EXTERNAL_LLM=true)",
    )
    ap.add_argument(
        "--llm-modo",
        choices=LLM_MODES,
        default=None,
        help="páginas que van al LLM con visión: off, dudosas (OCR flojo o sin importe/fecha) o "
        "todas. Exige ALLOW_EXTERNAL_LLM=true salvo con el proveedor fake",
    )
    ap.add_argument("--llm-max-paginas", type=int, default=50, help="tope de páginas enviadas")
    ap.add_argument(
        "--llm-min-confianza",
        type=float,
        default=60.0,
        help="confianza OCR (0-100) por debajo de la cual una página es dudosa",
    )
    ap.add_argument("--no-guardar", action="store_true", help="no escribir en la tabla segmento")
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args(argv)

    settings = get_settings()
    pdfs = list_pdfs(args.ruta) if args.ruta.is_dir() else [args.ruta]
    if args.excluir:
        skip = re.compile(args.excluir, re.IGNORECASE)
        pdfs = [p for p in pdfs if not skip.search(p.name)]
    if args.max and args.max < len(pdfs):
        pdfs = evenly_spaced(pdfs, args.max)
    if not pdfs:
        print(f"No hay PDFs en {args.ruta}", file=sys.stderr)
        return 1

    base = args.ruta if args.ruta.is_dir() else args.ruta.parent
    t0 = time.time()

    def progress(done: int, total: int) -> None:
        if done % 10 == 0 or done == total:
            print(f"\r  páginas leídas: {done}/{total}", end="", file=sys.stderr, flush=True)

    print(f"Leyendo {len(pdfs)} PDFs…", file=sys.stderr)
    mode = args.llm_modo or ("todas" if args.llm else "off")
    options = LlmReadOptions(mode, args.llm_max_paginas, args.llm_min_confianza)
    results = process_pdfs(
        pdfs,
        settings,
        args.convencion,
        args.moneda,
        workers=args.workers,
        progress=progress,
        llm=options,
    )
    print(f"\r  hecho en {time.time() - t0:.0f} s{' ' * 20}", file=sys.stderr)

    for result in results:
        print_result(result, base)

    n_pages = sum(r.n_pages for r in results)
    n_segs = sum(len(r.segments) for r in results)
    print(f"\nTotal: {len(results)} PDFs, {n_pages} páginas, {n_segs} segmentos")

    if not args.no_guardar:
        from app.db import SessionLocal
        from app.lectura.store import save_segments

        with SessionLocal() as session:
            written = sum(save_segments(session, r, base) for r in results)
            session.commit()
        print(f"Guardados {written} segmentos en la tabla segmento")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
