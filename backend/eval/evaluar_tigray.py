"""Measure our checks against the Tigray reviewer: real incidences found, false positives avoided.

Usage (from backend/):
    python eval/evaluar_tigray.py --proyecto ID [--verdades eval/tigray_ground_truth.csv]
    python eval/evaluar_tigray.py --hallazgos hallazgos.csv

Our findings come from the database (`--proyecto`: the Finding rows of that project, keyed by the
expense's order number) or from a CSV with the columns `order_no,rule_code`. For each category
(our rule codes) it prints:

- real incidences (EC accepted them) and how many of those we detect;
- reviewer false positives (EC refuted them) and how many we avoid (we raise nothing for them).

`SIN_REGLA` incidences have no rule of ours: they are listed but cannot be detected. Rows whose EC
answer is unclear (`indeterminado`) are counted apart and excluded from the percentages.
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from eval import tigray  # noqa: E402

DEFAULT_TRUTH = BACKEND / "eval" / "tigray_ground_truth.csv"


def findings_from_csv(path: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            out[tigray.order_key(row["order_no"])].add(row["rule_code"])
    return out


def findings_from_db(project_id: int) -> dict[str, set[str]]:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Expense, Finding, Project

    out: dict[str, set[str]] = defaultdict(set)
    with SessionLocal() as session:
        if session.get(Project, project_id) is None:
            raise SystemExit(f"No existe el proyecto {project_id}")
        query = (
            select(Expense.order_no, Finding.rule_code)
            .join(Expense, Finding.expense_id == Expense.id)
            .where(Finding.project_id == project_id)
        )
        for order_no, code in session.execute(query):
            out[tigray.order_key(order_no)].add(code)
    return out


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def render(results: dict[str, tigray.CategoryResult]) -> str:
    head = (
        f"{'categoría':<42}{'reales':>7}{'detect.':>8}{'recall':>8}"
        f"{'FP rev.':>9}{'evitados':>9}{'%':>6}{'indet.':>7}"
    )
    total = tigray.CategoryResult("TOTAL (categorías con regla)")
    lines = [head, "-" * len(head)]

    def line(r: tigray.CategoryResult) -> str:
        return (
            f"{r.categoria:<42}{r.reales:>7}{r.detectadas:>8}{_pct(r.recall):>8}"
            f"{r.falsos_positivos:>9}{r.evitados:>9}{_pct(r.evitados_pct):>6}"
            f"{r.indeterminadas:>7}"
        )

    for result in results.values():
        lines.append(line(result))
        if result.categoria != tigray.NO_RULE:
            for name in ("reales", "detectadas", "falsos_positivos", "evitados", "indeterminadas"):
                setattr(total, name, getattr(total, name) + getattr(result, name))
    lines += ["-" * len(head), line(total)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--proyecto", type=int, help="id del proyecto Tigray en la BD")
    source.add_argument("--hallazgos", type=Path, help="CSV order_no,rule_code")
    ap.add_argument("--verdades", type=Path, default=DEFAULT_TRUTH)
    args = ap.parse_args(argv)

    if not args.verdades.is_file():
        print(f"Falta {args.verdades}: genéralo con eval/convertir_tigray.py", file=sys.stderr)
        return 1
    truth = tigray.read_csv(args.verdades)
    findings = (
        findings_from_db(args.proyecto)
        if args.proyecto is not None
        else findings_from_csv(args.hallazgos)
    )
    if not findings:
        print("No hay incidencias nuestras que comparar: ¿se ejecutó el pipeline?", file=sys.stderr)
        return 1
    print(render(tigray.evaluate(truth, findings)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
