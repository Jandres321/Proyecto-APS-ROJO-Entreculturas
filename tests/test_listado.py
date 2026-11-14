from pathlib import Path

import pytest

from conciliador.listado import _resolver_archivo


def test_ruta_normal_no_se_toca(tmp_path):
    assert _resolver_archivo(tmp_path, "datos/listado.xlsx") == tmp_path / "datos/listado.xlsx"


def test_comodin_encuentra_un_nombre_con_caracteres_raros(tmp_path):
    (tmp_path / "datos").mkdir()
    esperado = tmp_path / "datos" / "Copia de Anexo 3- Inf. Econ+¦mico Final.xlsx"
    esperado.write_bytes(b"x")
    (tmp_path / "datos" / "Otro archivo.xlsx").write_bytes(b"x")
    assert _resolver_archivo(tmp_path, "datos/Copia de Anexo 3*Final.xlsx") == esperado


@pytest.mark.parametrize("nombres", [[], ["a1.xlsx", "a2.xlsx"]])
def test_comodin_exige_un_unico_archivo(tmp_path, nombres):
    for n in nombres:
        (tmp_path / n).write_bytes(b"x")
    with pytest.raises(ValueError, match="único archivo"):
        _resolver_archivo(tmp_path, "a*.xlsx")
