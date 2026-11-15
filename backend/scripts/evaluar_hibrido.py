"""Measure the matching against the manual numbering: conciliador alone vs content alone vs hybrid.

    python scripts/evaluar_hibrido.py [--only xunta|aecid] [--workers W]

Same metric as the conciliador's ``--validar`` (app/pipeline/match_eval.py): per expense with a
manual order number, is the assigned document the one whose NAME states that order? Nothing is
written to the database; PDFs are read through the lectura cache (CACHE_DIR).
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.adapters import AecidAdapter, XuntaAdapter  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.lectura.pipeline import process_pdfs  # noqa: E402
from app.pipeline.conciliador import NameReconciler  # noqa: E402
from app.pipeline.ingest import scan_documents  # noqa: E402
from app.pipeline.match import match_name  # noqa: E402
from app.pipeline.match_config import MatchConfig  # noqa: E402
from app.pipeline.match_content import (  # noqa: E402
    ExpenseView,
    SegmentView,
    compute_name_claims,
    expense_view,
)
from app.pipeline.match_eval import Validation, truth_by_expense, validate  # noqa: E402
from app.pipeline.match_hybrid import HybridOptions, HybridResult, hybrid_match  # noqa: E402
from app.samples import PARAGUAY_MATCH_CONFIG, UGANDA_MATCH_CONFIG  # noqa: E402

PROJECTS = {
    "xunta": dict(
        title="Uganda · Adjumani · Xunta 2024",
        folder="DocJustificativa/24-UG-226 Adjumani - Xunta 2024",
        report="Informe_economico_final_Adjuamni_def 14.10.25 v1.xls",
        adapter=XuntaAdapter,
        config=UGANDA_MATCH_CONFIG,
    ),
    "aecid": dict(
        title="Paraguay · AECID",
        folder="DocJustificativa/22-PY-136 - AECID",
        report="Informe seguimiento y final_31_10.xlsx",
        adapter=AecidAdapter,
        config=PARAGUAY_MATCH_CONFIG,
    ),
}


def load(spec: dict, data_dir: Path, workers: int | None):
    base = data_dir / spec["folder"]
    config: MatchConfig = spec["config"]
    expenses: list[ExpenseView] = []
    for i, exp in enumerate(spec["adapter"]().parse_expenses(base / spec["report"]), 1):
        view = expense_view(exp, config)
        view.id = i
        expenses.append(view)

    scan = scan_documents(base, ignore=config.get("ignore_files", ()))
    paths: dict[str, list[str]] = defaultdict(list)  # sha256 -> relative paths (all files)
    for d in scan.documents:
        paths[d.sha256].append(d.path)
    names = {k: sorted({match_name(p, config) for p in v}) for k, v in paths.items()}

    pdfs, seen = [], set()
    for d in scan.documents:
        if d.path.lower().endswith(".pdf") and "::" not in d.path and d.sha256 not in seen:
            seen.add(d.sha256)
            pdfs.append(base / d.path)
    print(f"  leyendo {len(pdfs)} PDF (caché)…", file=sys.stderr)
    results = process_pdfs(
        pdfs,
        get_settings(),
        config.get("amount_convention", "auto"),
        config.get("default_currency"),
        workers=workers,
    )
    segments: list[SegmentView] = []
    for res in results:
        if res.error:
            continue
        for seg in res.segments:
            segments.append(
                SegmentView(
                    id=len(segments) + 1,
                    doc_key=res.sha256,
                    tipo=seg.tipo.value,
                    emisor=seg.emisor,
                    nif=seg.nif,
                    numero_factura=seg.numero_factura,
                    fecha=seg.fecha,
                    importe=seg.importe,
                    importes=tuple(seg.importes),
                    moneda=seg.moneda,
                    numero_orden=seg.numero_orden,
                    legible=seg.legible,
                    pagina_inicio=seg.pagina_inicio,
                    pagina_fin=seg.pagina_fin,
                )
            )
    return expenses, segments, names


def numbered_config(config: MatchConfig) -> MatchConfig:
    """Only the filename rules that read an ORDER NUMBER (the manual numbering)."""
    rules = [r for r in config["filename_rules"] if r["kind"] != "issuer"]
    return {**config, "filename_rules": rules}


def row(label: str, v: Validation) -> str:
    c = v.counts

    def level(name: str) -> str:
        hits, misses = v.by_confidence.get(name, [0, 0])
        return f"{hits}/{hits + misses}" if hits + misses else "-"

    exact = f"{v.correct}/{v.evaluable}  {v.accuracy:.1%}"
    return (
        f"  {label:<44}{exact:>16}{c['correcto']:>11}{c['incorrecto']:>13}"
        f"{c['no_encontrado']:>13}{c['falso_positivo']:>9}{v.precision:>11.1%}"
        f"{level('high'):>10}{level('medium'):>9}{level('low'):>9}"
    )


def run(spec: dict, data_dir: Path, workers: int | None) -> int:
    print("=" * 128)
    print(spec["title"])
    print("=" * 128)
    expenses, segments, names = load(spec, data_dir, workers)
    config: MatchConfig = spec["config"]
    truth = truth_by_expense(compute_name_claims(expenses, names, numbered_config(config)))
    evaluable = sum(1 for e in expenses if e.order_key)
    print(
        f"  gastos: {len(expenses)} · evaluables (con nº de orden): {evaluable} · documentos: "
        f"{len(names)} · segmentos leídos: {len(segments)}"
    )
    reconciler = NameReconciler(config["name_reconciler"]) if "name_reconciler" in config else None

    def hybrid(rec, options: HybridOptions) -> HybridResult:
        return hybrid_match(expenses, segments, names, rec, options=options)

    rows: list[tuple[str, Validation]] = []

    def add(label: str, res: HybridResult) -> None:
        primary = {e.id: res.primary_doc(e.id) for e in expenses}
        conf = {lk.expense_id: lk.confidence.value for lk in res.links if lk.primary}
        rows.append((label, validate(expenses, primary, truth, conf)))

    if reconciler is not None:
        solo = reconciler.propose(expenses, names)
        primary = {e.id: p.doc_key for e, p in ((e, solo[e.id]) for e in expenses)}
        conf = {i: p.confidence.value for i, p in solo.items() if p.assigned and p.confidence}
        rows.append(("conciliador solo (por nombre)", validate(expenses, primary, truth, conf)))
    off = HybridOptions(reassign=False, disambiguate_low=False, content_fill=False)
    add("solo contenido", hybrid(None, HybridOptions()))
    if reconciler is not None:
        add("híbrido: solo verificar", hybrid(reconciler, off))
        add("híbrido + desempate de dudosos", hybrid(reconciler, HybridOptions(False, True, False)))
        add(
            "híbrido + reasignar con evidencia fuerte",
            hybrid(reconciler, HybridOptions(True, False, False)),
        )
        add(
            "híbrido + asignar por contenido", hybrid(reconciler, HybridOptions(False, False, True))
        )
        full = hybrid(reconciler, HybridOptions())
        add("HÍBRIDO COMPLETO", full)

    print(
        f"\n  {'variante':<44}{'exactitud':>16}{'correctos':>11}{'incorrectos':>13}"
        f"{'no encontr.':>13}{'falsos+':>9}{'precisión':>11}"
        f"{'alta':>10}{'media':>9}{'baja':>9}"
    )
    for label, v in rows:
        print(row(label, v))

    if reconciler is not None:
        links = [lk for lk in full.links if lk.primary]
        verified = sum(lk.verified for lk in links)
        print(
            f"\n  Híbrido completo: {len(links)} gastos con documento · verificados por contenido: "
            f"{verified} · propuestos por nombre: {sum(lk.origin == 'name' for lk in links)} · "
            f"asignados por contenido: {sum(lk.origin == 'content' for lk in links)} · "
            f"contradichos: {sum(lk.contradicted for lk in links)} · "
            f"conflictos: {len(full.conflicts)} (reasignados: "
            f"{sum(c.reassigned for c in full.conflicts)})"
        )
    if not config.get("carpeta_completa", True):
        print(
            "\n  OJO: carpeta_completa = false (la carpeta es solo una muestra). La exactitud\n"
            "  cuenta como acierto un gasto sin documento esperado y sin documento asignado,\n"
            "  así que no es comparable: mira «correctos» (gastos con documento bien asignado)\n"
            "  y «no encontr.»."
        )
    baseline = rows[0][1].correct if reconciler is not None else None
    if baseline is not None:
        final = rows[-1][1].correct
        print(
            f"  Conciliador solo {baseline}/{evaluable} → híbrido {final}/{evaluable} "
            f"({'OK' if final >= baseline else 'POR DEBAJO'})"
        )
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=get_settings().data_dir)
    parser.add_argument("--only", choices=sorted(PROJECTS))
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args()
    if args.data_dir is None or not args.data_dir.is_dir():
        print("DATA_DIR no está definido o no existe (usa --data-dir o la variable DATA_DIR).")
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for key, spec in PROJECTS.items():
        if args.only and args.only != key:
            continue
        run(spec, args.data_dir, args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
