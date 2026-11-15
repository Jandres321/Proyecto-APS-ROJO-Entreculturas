"""Compare three ways of matching expenses with documents, against the manual numbering.

    python scripts/comparar_cruce.py [--only xunta|aecid] [--max-pdfs N] [--seed S] [--workers W]

Variants (document-level pairs expense <-> PDF):

  (a) solo nombre     name-only reconciler with the order numbers HIDDEN (words of the name vs
                      concept/issuer, invoice no. written in the name).
  (b) solo contenido  `match_content.match_segments` over the segments read from the PDFs,
                      without any file name.
  (c) ambos           (b) + the name reconciler as the weak signal (weight 0.06).
  (b) renombrados     (b) after the PDFs were renamed at random (names shuffled between files).
  (c) renombrados     (c) with those wrong names.

Ground truth = the manual numbering: the order number written in each file name (the project's
filename rules, "33-39 ...", "69.A ...", "CG14", ...) versus the expense list's order numbers.
Nothing is written to the database; PDFs are read through the lectura cache (CACHE_DIR), so the
first run reads everything with OCR and the next ones are instant.
"""

import argparse
import random
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.adapters import AecidAdapter, XuntaAdapter  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.lectura.pipeline import list_pdfs, process_pdfs  # noqa: E402
from app.pipeline.conciliador import NameReconciler  # noqa: E402
from app.pipeline.match_config import MatchConfig  # noqa: E402
from app.pipeline.match_content import (  # noqa: E402
    ExpenseView,
    MatchResult,
    SegmentView,
    compute_name_claims,
    expense_view,
    match_segments,
)
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

Pair = tuple[int, str]  # (expense id, document key = sha256)


def conciliador_name_pairs(
    expenses: list[ExpenseView], names: dict[str, list[str]], config: MatchConfig
) -> set[Pair]:
    """Pairs proposed by the name-based conciliador (numbers hidden); empty when the project has
    no ``name_reconciler`` configuration (Paraguay). See scripts/evaluar_hibrido.py for the
    comparison against the hybrid."""
    if "name_reconciler" not in config:
        return set()
    proposals = NameReconciler(config["name_reconciler"]).propose(expenses, names)
    return {(i, p.doc_key) for i, p in proposals.items() if p.doc_key}


@dataclass
class Metrics:
    predicted: int
    correct: int
    truth: int
    expenses_total: int  # expenses that have a true document
    expenses_exact: int  # ... whose predicted documents are exactly the true ones
    expenses_found: int  # ... for which at least one true document was found
    extra_expenses: int  # expenses WITHOUT true document that got a document anyway

    @property
    def precision(self) -> float:
        return self.correct / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.correct / self.truth if self.truth else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def evaluate(predicted: set[Pair], truth: set[Pair]) -> Metrics:
    by_exp_pred: dict[int, set[str]] = defaultdict(set)
    by_exp_true: dict[int, set[str]] = defaultdict(set)
    for e, d in predicted:
        by_exp_pred[e].add(d)
    for e, d in truth:
        by_exp_true[e].add(d)
    exact = sum(1 for e, docs in by_exp_true.items() if by_exp_pred.get(e) == docs)
    found = sum(1 for e, docs in by_exp_true.items() if by_exp_pred.get(e, set()) & docs)
    extra = sum(1 for e in by_exp_pred if e not in by_exp_true)
    return Metrics(
        predicted=len(predicted),
        correct=len(predicted & truth),
        truth=len(truth),
        expenses_total=len(by_exp_true),
        expenses_exact=exact,
        expenses_found=found,
        extra_expenses=extra,
    )


def doc_pairs(result: MatchResult, segments: list[SegmentView]) -> dict[Pair, str]:
    """Document-level pairs of a content result -> best confidence of its segment links."""
    doc_of = {s.id: s.doc_key for s in segments}
    order = {"low": 0, "medium": 1, "high": 2}
    best: dict[Pair, str] = {}
    for lk in result.links:
        key = (lk.expense_id, doc_of[lk.segment_id])
        if key not in best or order[lk.confidence.value] > order[best[key]]:
            best[key] = lk.confidence.value
    return best


def load_project(spec: dict, data_dir: Path, max_pdfs: int | None, workers: int | None):
    base = data_dir / spec["folder"]
    config: MatchConfig = spec["config"]
    expenses_in = spec["adapter"]().parse_expenses(base / spec["report"])
    expenses: list[ExpenseView] = []
    for i, exp in enumerate(expenses_in, 1):
        view = expense_view(exp, config)
        view.id = i
        expenses.append(view)

    ignore = [re.compile(rx, re.IGNORECASE) for rx in config.get("ignore_files", ())]
    pdfs = [p for p in list_pdfs(base) if not any(rx.search(p.name) for rx in ignore)]
    if max_pdfs:
        pdfs = pdfs[:max_pdfs]
    t0 = time.time()
    print(f"  leyendo {len(pdfs)} PDFs…", file=sys.stderr)
    results = process_pdfs(
        pdfs,
        get_settings(),
        config.get("amount_convention", "auto"),
        config.get("default_currency"),
        workers=workers,
        progress=lambda d, t: print(f"\r  páginas {d}/{t}", end="", file=sys.stderr),
    )
    print(f"\r  lectura hecha en {time.time() - t0:.0f} s{' ' * 20}", file=sys.stderr)

    segments: list[SegmentView] = []
    names: dict[str, list[str]] = {}
    seen: set[str] = set()
    for res in results:
        if res.error:
            print(f"  ! {res.path.name}: {res.error}", file=sys.stderr)
            continue
        names.setdefault(res.sha256, []).append(str(res.path.relative_to(base)))
        if res.sha256 in seen:  # exact duplicate file: same content, only its name is new
            continue
        seen.add(res.sha256)
        for seg in res.segments:
            segments.append(
                SegmentView(
                    id=len(segments) + 1,
                    doc_key=res.sha256,
                    filename=res.path.name,
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
    """Only the rules that read an ORDER NUMBER from the name (the manual numbering)."""
    return {
        **config,
        "filename_rules": [r for r in config["filename_rules"] if r["kind"] != "issuer"],
    }


def shuffle_names(names: dict[str, list[str]], segments: list[SegmentView], seed: int):
    """Rename the PDFs at random: names are permuted between documents."""
    keys = sorted(names)
    shuffled = keys[:]
    random.Random(seed).shuffle(shuffled)
    new_names = {k: names[v] for k, v in zip(keys, shuffled, strict=True)}
    new_filename = {k: Path(new_names[k][0]).name for k in keys}
    renamed = [
        SegmentView(**{**s.__dict__, "filename": new_filename.get(s.doc_key)}) for s in segments
    ]
    return new_names, renamed


def fmt(m: Metrics) -> str:
    return (
        f"{m.predicted:>6} {m.correct:>6} {m.precision:>7.1%} {m.recall:>7.1%} {m.f1:>6.1%}  "
        f"{m.expenses_exact:>4}/{m.expenses_total:<4} {m.expenses_found:>4}/{m.expenses_total:<4} "
        f"{m.extra_expenses:>6}"
    )


def run_project(key: str, spec: dict, data_dir: Path, args) -> None:
    print("=" * 112)
    print(f"{spec['title']}")
    print("=" * 112)
    expenses, segments, names = load_project(spec, data_dir, args.max_pdfs, args.workers)
    config = spec["config"]
    truth = compute_name_claims(expenses, names, numbered_config(config))
    docs = len(names)
    print(
        f"  gastos: {len(expenses)} · documentos: {docs} · segmentos: {len(segments)} · "
        f"pares verdaderos (numeración manual): {len(truth)}"
    )

    # (a) name only, numbers hidden
    pred_a = conciliador_name_pairs(expenses, names, config)

    # (b) content only: no file name enters the engine
    plain = [SegmentView(**{**s.__dict__, "filename": None}) for s in segments]
    res_b = match_segments(expenses, plain, use_name=False)
    pred_b = doc_pairs(res_b, plain)

    # (c) both
    res_c = match_segments(expenses, segments, name_hits=pred_a, use_name=True)
    pred_c = doc_pairs(res_c, segments)

    # renamed at random
    names_r, segments_r = shuffle_names(names, segments, args.seed)
    plain_r = [SegmentView(**{**s.__dict__, "filename": None}) for s in segments_r]
    res_br = match_segments(expenses, plain_r, use_name=False)
    pred_br = doc_pairs(res_br, plain_r)
    pred_a_r = conciliador_name_pairs(expenses, names_r, config)
    res_cr = match_segments(expenses, segments_r, name_hits=pred_a_r, use_name=True)
    pred_cr = doc_pairs(res_cr, segments_r)

    print(
        f"\n  {'variante':<28}{'pares':>6} {'bien':>6} {'precis.':>7} {'cobert.':>7} {'F1':>6}  "
        f"{'gastos exactos':<10} {'con algún acierto':<10}{'extras':>7}"
    )
    rows = [
        ("(a) solo nombre", set(pred_a)),
        ("(b) solo contenido", set(pred_b)),
        ("(c) ambos", set(pred_c)),
        ("(b) renombrados al azar", set(pred_br)),
        ("(c) renombrados al azar", set(pred_cr)),
    ]
    for label, pairs in rows:
        print(f"  {label:<28}{fmt(evaluate(pairs, truth))}")
    same = pred_b == pred_br
    print(
        "\n  (b) con los PDFs renombrados da exactamente los mismos pares: "
        + ("sí" if same else "NO")
    )

    print("\n  Precisión por confianza (pares de documento):")
    for label, pred in (("(b) contenido", pred_b), ("(c) ambos", pred_c)):
        parts = []
        for level in ("high", "medium", "low"):
            sel = {p for p, c in pred.items() if c == level}
            ok = len(sel & truth)
            parts.append(f"{level}: {ok}/{len(sel)}" + (f" ({ok / len(sel):.0%})" if sel else ""))
        print(f"    {label:<14}" + " · ".join(parts))
    print(
        f"  Ambigüedades (b): {len(res_b.ambiguities)} · desajustes de nº de orden (b): "
        f"{len(res_b.order_mismatches)} · enlaces por suma de gastos (b): "
        f"{sum(1 for lk in res_b.links if lk.group)}"
    )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=get_settings().data_dir)
    parser.add_argument("--only", choices=sorted(PROJECTS))
    parser.add_argument("--max-pdfs", type=int, default=None, help="limit PDFs per project")
    parser.add_argument("--seed", type=int, default=7, help="seed of the random renaming")
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
        run_project(key, spec, args.data_dir, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
