"""DOC_INCOMPLETE: the expense's file has the supporting documents its type requires."""

import re
from typing import Any

from app.lectura.classify import normalize
from app.pipeline.checks.context import (
    CheckContext,
    ExpenseFacts,
    Seg,
    _finding,
)
from app.rules import (
    DOC_INCOMPLETE,
    partida_matches,
)
from app.schemas import FindingIn


def _requirement_applies(req: dict[str, Any], exp: ExpenseFacts) -> bool:
    if not partida_matches(exp.partida_code, req.get("applies_to_partidas") or []):
        return False
    keywords = req.get("keywords") or []
    if not keywords:
        return True
    text = normalize(f"{exp.concept or ''} {exp.issuer or ''}")
    return any(re.search(rf"\b{re.escape(normalize(k))}", text) for k in keywords)


def _has(item: dict[str, Any], bundle: list[Seg]) -> bool:
    types, flags = set(item.get("segment_types") or []), item.get("flags") or []
    for seg in bundle:
        if seg.tipo in types:
            return True
        for flag in flags:
            if seg.prueba_pago if flag == "prueba_pago" else seg.extra.get(flag):
                return True
    return False


def check_file_completeness(ctx: CheckContext) -> list[FindingIn]:
    """DOC_INCOMPLETE: the expense's file has the supporting documents its type requires."""
    findings: list[FindingIn] = []
    for case in ctx.cases:
        exp = case.expense
        rule = ctx.rules.applies(DOC_INCOMPLETE, exp.partida_code)
        if rule is None or not case.linked or exp.cofinanced:
            continue
        for req in (rule.params or {}).get("requirements") or []:
            if not _requirement_applies(req, exp):
                continue
            missing = [item for item in req.get("requires") or [] if not _has(item, case.bundle)]
            if not missing:
                continue
            present = sorted({s.label for s in case.bundle})
            findings.append(
                _finding(
                    rule,
                    case,
                    f"Al expediente del gasto {exp.ref} ({req.get('label', req['name'])}) le "
                    f"falta: {', '.join(i['label'] for i in missing)}. "
                    f"En sus documentos solo hay: {', '.join(present) or 'nada'}.",
                    seg=case.linked[0],
                    requisito=req["name"],
                    falta=[i["id"] for i in missing],
                    documentos_presentes=present,
                )
            )
    return findings
