"""Donor adapter interface.

Each donor (Xunta, AECID...) has its own report layout, budget lines (partidas) and
limits. An adapter turns the donor-specific files into the common schemas.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from app.enums import Donor
from app.schemas import BudgetLineIn, ExpenseIn, Limit, Partida


class DonorAdapter(ABC):
    donor: ClassVar[Donor]

    @abstractmethod
    def parse_expenses(self, path: Path) -> list[ExpenseIn]:
        """Read the expense list (listado de gastos) from the donor report spreadsheet."""

    @abstractmethod
    def parse_budget(self, path: Path) -> list[BudgetLineIn]:
        """Read approved vs executed budget per partida from the donor report spreadsheet."""

    @abstractmethod
    def partida_catalog(self) -> list[Partida]:
        """Donor partidas with definitions, used to suggest/validate the partida of each expense."""

    @abstractmethod
    def limits(self) -> list[Limit]:
        """Donor caps (percentages / amounts) checked in code against the budget execution."""
