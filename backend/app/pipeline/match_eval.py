"""Validation of a matching against the manual numbering (same metric as the conciliador's
``--validar``).

Ground truth = the order number written in each file name (project filename rules) versus the
order number of each expense. Only expenses WITH an order number are evaluable. Per expense, with
`expected` = the documents whose name states its order, and `got` = the primary document assigned:

- ``correcto``: got in expected;
- ``correcto_sin_documento``: nothing expected and nothing assigned;
- ``incorrecto``: another document;
- ``no_encontrado``: expected but nothing assigned;
- ``falso_positivo``: nothing expected but something assigned.

Exactitud = (correcto + correcto_sin_documento) / evaluable.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.pipeline.match_content import ExpenseView


@dataclass
class Validation:
    evaluable: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    # confidence -> [hits, misses] over assigned expenses
    by_confidence: dict[str, list[int]] = field(default_factory=dict)

    @property
    def correct(self) -> int:
        return self.counts["correcto"] + self.counts["correcto_sin_documento"]

    @property
    def accuracy(self) -> float:
        return self.correct / self.evaluable if self.evaluable else 0.0

    @property
    def assigned(self) -> int:
        return self.counts["correcto"] + self.counts["incorrecto"] + self.counts["falso_positivo"]

    @property
    def precision(self) -> float:
        return self.counts["correcto"] / self.assigned if self.assigned else 0.0


def truth_by_expense(pairs: Iterable[tuple[int, str]]) -> dict[int, set[str]]:
    out: dict[int, set[str]] = defaultdict(set)
    for expense_id, doc_key in pairs:
        out[expense_id].add(doc_key)
    return out


def validate(
    expenses: Iterable[ExpenseView],
    assigned: Mapping[int, str | None],
    truth: Mapping[int, set[str]],
    confidence: Mapping[int, str] | None = None,
) -> Validation:
    """`assigned`: expense id -> primary document key (None / missing = none)."""
    v = Validation()
    for e in expenses:
        if not e.order_key:
            continue
        v.evaluable += 1
        expected = truth.get(e.id, set())
        got = assigned.get(e.id)
        if not expected:
            verdict = "correcto_sin_documento" if got is None else "falso_positivo"
        elif got is None:
            verdict = "no_encontrado"
        elif got in expected:
            verdict = "correcto"
        else:
            verdict = "incorrecto"
        v.counts[verdict] += 1
        if got is not None and verdict in ("correcto", "incorrecto", "falso_positivo"):
            level = (confidence or {}).get(e.id, "")
            hits = v.by_confidence.setdefault(level, [0, 0])
            hits[0 if verdict == "correcto" else 1] += 1
    return v
