"""Convert the Tigray reviewer workbook into `eval/tigray_ground_truth.csv`.

Usage (from backend/):
    python eval/convertir_tigray.py [--xlsx RUTA] [--salida eval/tigray_ground_truth.csv]

Without `--xlsx` it uses DATA_DIR/DocJustificativa/22_ET_217 .../Documentos e informe con
incidencias/Resumen incidencias_ Respuestas EC.xlsx. The CSV holds real data (git-ignored).
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402
from eval import tigray  # noqa: E402

DEFAULT_RELATIVE = (
    "DocJustificativa/22_ET_217 Crisis Tigray Addis - Xunta de Galicia/"
    "Documentos e informe con incidencias/Resumen incidencias_ Respuestas EC.xlsx"
)
DEFAULT_OUTPUT = BACKEND / "eval" / "tigray_ground_truth.csv"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xlsx", type=Path, default=None)
    ap.add_argument("--salida", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args(argv)

    xlsx = args.xlsx
    if xlsx is None:
        data_dir = get_settings().data_dir
        if data_dir is None:
            print("DATA_DIR no está definido: usa --xlsx RUTA", file=sys.stderr)
            return 1
        xlsx = data_dir / DEFAULT_RELATIVE
    if not xlsx.is_file():
        print(f"No existe {xlsx}", file=sys.stderr)
        return 1

    rows = tigray.read_workbook(xlsx)
    count = tigray.write_csv(rows, args.salida)
    print(f"{count} incidencias escritas en {args.salida}")
    print("Veredicto:", dict(Counter(r.veredicto for r in rows)))
    print("Categoría:", dict(Counter(r.categoria for r in rows).most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
