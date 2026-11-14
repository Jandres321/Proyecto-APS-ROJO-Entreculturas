"""Print a per-partida summary of what the donor adapters read from the pilot reports.

Usage (from backend/, or inside the api container with `make inspect`):
    python scripts/inspect_ingest.py [--data-dir DIR] [--only xunta|aecid]

Needs no database: it runs the adapters on the Excel reports under DATA_DIR and compares, for
each partida, the sum of the expense list with the figures the report itself declares.
"""

import argparse
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.adapters import AecidAdapter, DonorAdapter, XuntaAdapter  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas import BudgetLineIn, ExpenseIn  # noqa: E402

PROJECTS = {
    "xunta": (
        "Uganda · Adjumani · Xunta 2024 (24-UG-226)",
        XuntaAdapter,
        "DocJustificativa/24-UG-226 Adjumani - Xunta 2024/"
        "Informe_economico_final_Adjuamni_def 14.10.25 v1.xls",
    ),
    "aecid": (
        "Paraguay · AECID (22-PY-136)",
        AecidAdapter,
        "DocJustificativa/22-PY-136 - AECID/Informe seguimiento y final_31_10.xlsx",
    ),
}
ZERO = Decimal(0)


def money(value: Decimal | None, width: int = 13) -> str:
    return " " * (width - 1) + "-" if value is None else f"{value:>{width},.2f}"


def sort_key(code: str) -> list[tuple[int, str]]:
    # "A.I.6.1" < "A.I.10" < "A.II.1" < "B": numbers numerically, roman/letters as text.
    return [(0, f"{int(p):06d}") if p.isdigit() else (1, p) for p in code.split(".")]


def report(title: str, adapter: DonorAdapter, path: Path) -> None:
    expenses: list[ExpenseIn] = adapter.parse_expenses(path)
    budget: list[BudgetLineIn] = adapter.parse_budget(path)
    leaves = {b.partida_code: b for b in budget if b.is_partida}
    catalog = {p.code for p in adapter.partida_catalog()}

    count: dict[str, int] = defaultdict(int)
    eur: dict[str, Decimal] = defaultdict(Decimal)
    donor: dict[str, Decimal] = defaultdict(Decimal)
    for e in expenses:
        code = e.partida_code or "(sin partida)"
        count[code] += 1
        eur[code] += e.amount_eur or ZERO
        donor[code] += e.amount_donor or ZERO

    print("=" * 118)
    print(f"{title}\n{path.name}")
    print("=" * 118)
    print(
        f"{'Partida':<10}{'Nombre':<34}{'Gastos':>7}{'Σ importe EUR':>14}{'Σ imputado':>14}"
        f"{'Aprobado':>14}{'Ejecutado inf.':>15}{'Σ - inf.':>10}{'% ejec.':>8}"
    )
    print("-" * 118)
    for code in sorted(set(leaves) | set(count), key=sort_key):
        line = leaves.get(code)
        spent, executed = donor.get(code, ZERO), line.executed_donor if line else None
        diff = None if executed is None else spent - executed
        pct = (
            f"{100 * spent / line.approved_donor:7.1f}%"
            if line and line.approved_donor
            else f"{'-':>8}"
        )
        flag = ""
        if line is None:
            flag = "  <- no está en el presupuesto"
        elif diff is not None and abs(diff) > Decimal("1"):
            flag = "  <- NO CUADRA"
        if code not in catalog and code != "(sin partida)":
            flag += "  <- sin ficha en el catálogo"
        name = (line.partida_name if line and line.partida_name else "")[:32]
        print(
            f"{code:<10}{name:<34}{count.get(code, 0):>7}{money(eur.get(code, ZERO), 14)}"
            f"{money(spent, 14)}{money(line.approved_donor if line else None, 14)}"
            f"{money(executed, 15)}{money(diff, 10)}{pct}{flag}"
        )
    print("-" * 118)
    total_line = next((b for b in budget if b.partida_code == "TOTAL_GENERAL"), None)
    print(
        f"{'TOTAL':<10}{'':<34}{len(expenses):>7}{money(sum(eur.values(), ZERO), 14)}"
        f"{money(sum(donor.values(), ZERO), 14)}"
        f"{money(total_line.approved_donor if total_line else None, 14)}"
        f"{money(total_line.executed_donor if total_line else None, 15)}"
    )

    dates = [e.date for e in expenses if e.date]
    no_key = sum(e.order_key is None for e in expenses)
    print(
        f"\nGastos: {len(expenses)} | partidas con gasto: {len(count)} | "
        f"fechas {min(dates)} → {max(dates)} | sin nº de orden utilizable: {no_key} | "
        f"fecha ilegible: {sum(e.date is None for e in expenses)} | "
        f"sin importe imputado al donante: {sum(e.amount_donor is None for e in expenses)}"
    )
    non_partidas = [b.partida_code for b in budget if not b.is_partida]
    print(f"Filas de presupuesto que no son partida: {', '.join(non_partidas)}")
    print("Topes del donante:")
    for limit in adapter.limits():
        what = []
        if limit.max_pct is not None:
            what.append(f"{limit.max_pct:.0%} ({limit.base.value})")
        if limit.max_amount_eur is not None:
            what.append(f"{limit.max_amount_eur:,.0f} EUR ({limit.scope.value})")
        print(f"  - {limit.code}: {' / '.join(what)} · partidas {limit.partida_codes or 'todas'}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=get_settings().data_dir)
    parser.add_argument("--only", choices=sorted(PROJECTS))
    args = parser.parse_args()
    if args.data_dir is None or not args.data_dir.is_dir():
        print("DATA_DIR no está definido o no existe (usa --data-dir o la variable DATA_DIR).")
        return 1

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    status = 0
    for key, (title, adapter_cls, relative) in PROJECTS.items():
        if args.only and args.only != key:
            continue
        path = args.data_dir / relative
        if not path.is_file():
            print(f"[{key}] informe no encontrado: {path}")
            status = 1
            continue
        report(title, adapter_cls(), path)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
