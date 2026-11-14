from app.adapters.aecid import AecidAdapter
from app.adapters.base import DonorAdapter
from app.adapters.xunta import XuntaAdapter
from app.enums import Donor

_ADAPTERS: dict[Donor, type[DonorAdapter]] = {
    Donor.XUNTA: XuntaAdapter,
    Donor.AECID: AecidAdapter,
}


def get_adapter(donor: Donor) -> DonorAdapter:
    return _ADAPTERS[donor]()


__all__ = ["AecidAdapter", "DonorAdapter", "XuntaAdapter", "get_adapter"]
