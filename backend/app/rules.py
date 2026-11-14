"""Donor check rules, declared in `backend/rules/<donor>.yaml`.

Each rule has a code, a Spanish description, the partidas it applies to, its normative
reference, a severity and free-form `params` read by the check that implements it
(app/pipeline/checks.py). Nothing donor-specific lives in the checks' code: thresholds, tolerances,
document requirements per expense type... are all here.

    donor: xunta
    rules:
      - code: CASH_OVER_LIMIT
        description: ...
        kind: deterministic            # deterministic | doc_requirement | llm
        applies_to_partidas: []        # empty = every partida; "A.V" also covers "A.V.2"
        reference: "Orden ... art. 19.5"
        severity: high                 # high | medium | low
        active: true
        params: {limit_eur: 1000}
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Donor, RuleKind, Severity
from app.models import Rule
from app.schemas import RuleIn

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"

AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
DATE_MISMATCH = "DATE_MISMATCH"
DONOR_STAMP_MISSING = "DONOR_STAMP_MISSING"
NOT_AN_INVOICE = "NOT_AN_INVOICE"
PAYMENT_EVIDENCE_MISSING = "PAYMENT_EVIDENCE_MISSING"
CASH_OVER_LIMIT = "CASH_OVER_LIMIT"
ILLEGIBLE = "ILLEGIBLE"
DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
DOC_INCOMPLETE = "DOC_INCOMPLETE"

CHECK_CODES = (
    AMOUNT_MISMATCH,
    DATE_MISMATCH,
    DONOR_STAMP_MISSING,
    NOT_AN_INVOICE,
    PAYMENT_EVIDENCE_MISSING,
    CASH_OVER_LIMIT,
    ILLEGIBLE,
    DUPLICATE_INVOICE,
    DOC_INCOMPLETE,
)


class RulesError(ValueError):
    """A rules file is missing, malformed or incomplete."""


def partida_matches(code: str | None, patterns: list[str]) -> bool:
    """`patterns` empty = every partida; "A.V" covers "A.V" and "A.V.2" (not "A.VI")."""
    if not patterns:
        return True
    if not code:
        return False
    return any(code == p or code.startswith(p + ".") for p in patterns)


@dataclass
class RuleSet:
    donor: Donor
    rules: dict[str, RuleIn] = field(default_factory=dict)

    def get(self, code: str) -> RuleIn | None:
        """The rule when it exists AND is active, else None (the check is skipped)."""
        rule = self.rules.get(code)
        return rule if rule is not None and rule.active else None

    def applies(self, code: str, partida_code: str | None) -> RuleIn | None:
        rule = self.get(code)
        if rule is None or not partida_matches(partida_code, rule.applies_to_partidas or []):
            return None
        return rule

    def params(self, code: str) -> dict[str, Any]:
        rule = self.rules.get(code)
        return dict(rule.params or {}) if rule else {}


def _parse_rule(raw: Any, donor: Donor, source: Path) -> RuleIn:
    if not isinstance(raw, dict):
        raise RulesError(f"{source.name}: every rule must be a mapping, got {raw!r}")
    missing = {"code", "description", "severity", "reference"} - raw.keys()
    if missing:
        raise RulesError(f"{source.name}: rule {raw.get('code', '?')} lacks {sorted(missing)}")
    code = raw["code"]
    if code not in CHECK_CODES:
        raise RulesError(f"{source.name}: unknown rule code {code!r}")
    try:
        severity = Severity(raw["severity"])
        kind = RuleKind(raw.get("kind", "deterministic"))
    except ValueError as exc:
        raise RulesError(f"{source.name}: rule {code}: {exc}") from exc
    partidas = raw.get("applies_to_partidas") or []
    if not isinstance(partidas, list) or not all(isinstance(p, str) for p in partidas):
        raise RulesError(f"{source.name}: rule {code}: applies_to_partidas must be a list of text")
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise RulesError(f"{source.name}: rule {code}: params must be a mapping")
    return RuleIn(
        donor=donor,
        code=code,
        description=str(raw["description"]).strip(),
        kind=kind,
        applies_to_partidas=partidas or None,
        reference=str(raw["reference"]).strip(),
        severity=severity,
        params=params,
        active=bool(raw.get("active", True)),
    )


def load_rules(donor: Donor | str, directory: Path | None = None) -> RuleSet:
    """Load and validate `rules/<donor>.yaml`. Every check code must be declared (an inactive
    rule is declared with `active: false`), so a typo can never silently disable a check."""
    donor = Donor(donor)
    path = (directory or RULES_DIR) / f"{donor.value}.yaml"
    if not path.is_file():
        raise RulesError(f"No rules file for donor '{donor.value}': {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RulesError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or data.get("donor") != donor.value:
        raise RulesError(f"{path.name}: the top-level 'donor' must be '{donor.value}'")
    rules: dict[str, RuleIn] = {}
    for raw in data.get("rules") or []:
        rule = _parse_rule(raw, donor, path)
        if rule.code in rules:
            raise RulesError(f"{path.name}: duplicated rule {rule.code}")
        rules[rule.code] = rule
    absent = [c for c in CHECK_CODES if c not in rules]
    if absent:
        raise RulesError(f"{path.name}: missing rules {absent}")
    return RuleSet(donor=donor, rules=rules)


def sync_rules(session: Session, donor: Donor | str) -> int:
    """Upsert the donor's YAML rules into the `rule` table (so the UI can list them).
    Returns how many rows were created or changed."""
    rule_set = load_rules(donor)
    existing = {
        r.code: r for r in session.scalars(select(Rule).where(Rule.donor == rule_set.donor))
    }
    changed = 0
    for code, rule in rule_set.rules.items():
        row = existing.get(code)
        values = rule.model_dump(exclude={"donor", "code"})
        if row is None:
            session.add(Rule(donor=rule.donor, code=code, **values))
            changed += 1
            continue
        if any(getattr(row, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(row, k, v)
            changed += 1
    session.flush()
    return changed
