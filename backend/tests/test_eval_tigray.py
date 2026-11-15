"""Tigray evaluation tooling on an invented workbook (same layout as the real one)."""

import openpyxl
import pytest

from eval import evaluar_tigray, tigray

HEADER = ["Partida", "Orden", "Fecha", "Factura", "Emisor", "Concepto", "Local", "EUR"]
INC = ["Incidencia 1", "Comentarios Incidencia 1", "Incidencia 2", "Comentarios Incidencia 2"]


@pytest.fixture
def workbook(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.title = "Revisor"  # sheet without comments: must be skipped
    wb.active.append(["Incidencia 1"])
    ws = wb.create_sheet("Respuestas")
    ws.append([None])
    ws.append(["Relación incidencias detectadas:"])
    ws.append([])
    ws.append(["Partida", None, None, None, None, None, None, None, *INC])
    rows = [
        (
            "A.I",
            1.0,
            "F-1",
            "Sello/diligencia en factura incompleto",
            "Incidencia aceptada",
            None,
            None,
        ),
        (
            "A.I",
            2.0,
            "F-2",
            "Fecha de la factura no coincide con documentación aportada",
            "Coincide la fecha con la factura",
            "No se aporta contrato de los trabajadores",
            "Se cambiarán los documentos",
        ),
        (
            "A.V",
            3.0,
            "F-3",
            "Fecha e importe del pago no coinciden",
            "Pendiente de recibir",
            None,
            None,
        ),
        ("A.V", 4.0, "F-4", "Concepto de factura no coincide", "Consultado con el socio", None, None),
    ]
    for partida, order, invoice, inc1, com1, inc2, com2 in rows:
        ws.append(
            [partida, order, None, invoice, "Proveedor", "x", 1.0, 1.0, inc1, com1, inc2, com2]
        )
    path = tmp_path / "resumen.xlsx"
    wb.save(path)
    return path


def test_categories_and_verdicts():
    assert tigray.categorize("Sello/diligencia en factura incompleto") == "DONOR_STAMP_MISSING"
    assert tigray.categorize("Fecha e importe del pago no coinciden") == (
        "DATE_MISMATCH|AMOUNT_MISMATCH"
    )
    assert tigray.categorize("No se aporta contrato de los trabajadores") == "DOC_INCOMPLETE"
    assert tigray.categorize("Descripción factura no coincide") == tigray.NO_RULE
    assert tigray.verdict("Incidencia aceptada") == tigray.REAL
    assert tigray.verdict("Se cambiarán los documentos") == tigray.REAL
    assert tigray.verdict("Coincide la fecha") == tigray.FALSE_POSITIVE
    assert tigray.verdict("Es legible") == tigray.FALSE_POSITIVE
    assert tigray.verdict("Consultado con el socio") == tigray.UNDECIDED
    assert tigray.verdict(None) == tigray.UNDECIDED


def test_workbook_to_csv_roundtrip(workbook, tmp_path):
    rows = tigray.read_workbook(workbook)
    assert len(rows) == 5
    assert [r.orden for r in rows] == ["1", "2", "2", "3", "4"]
    assert rows[0].factura == "F-1" and rows[0].categoria == "DONOR_STAMP_MISSING"
    out = tmp_path / "gt.csv"
    assert tigray.write_csv(rows, out) == 5
    assert tigray.read_csv(out) == rows


def test_evaluation_counts_detected_and_avoided(workbook):
    truth = tigray.read_workbook(workbook)
    # we flag the stamp of expense 1 (real) and the date of expense 2 (a reviewer false positive)
    ours = {"1": {"DONOR_STAMP_MISSING"}, "2": {"DATE_MISMATCH"}}
    results = tigray.evaluate(truth, ours)
    stamp = results["DONOR_STAMP_MISSING"]
    assert (stamp.reales, stamp.detectadas, stamp.recall) == (1, 1, 1.0)
    date = results["DATE_MISMATCH"]
    assert (date.falsos_positivos, date.evitados) == (1, 0)
    docs = results["DOC_INCOMPLETE"]
    assert (docs.reales, docs.detectadas) == (1, 0)  # a real one we missed
    both = results["DATE_MISMATCH|AMOUNT_MISMATCH"]
    assert (both.reales, both.detectadas) == (1, 0)
    assert results[tigray.NO_RULE].indeterminadas == 1
    # raising the date finding on expense 2 is not a hit for anything else: avoided when silent
    silent = tigray.evaluate(truth, {"1": {"DONOR_STAMP_MISSING"}})
    assert silent["DATE_MISMATCH"].evitados == 1


def test_cli_with_findings_csv(workbook, tmp_path, capsys):
    truth = tmp_path / "gt.csv"
    tigray.write_csv(tigray.read_workbook(workbook), truth)
    findings = tmp_path / "ours.csv"
    findings.write_text("order_no,rule_code\n1.0,DONOR_STAMP_MISSING\n", encoding="utf-8")
    code = evaluar_tigray.main(["--hallazgos", str(findings), "--verdades", str(truth)])
    assert code == 0
    out = capsys.readouterr().out
    assert "DONOR_STAMP_MISSING" in out and "TOTAL" in out
