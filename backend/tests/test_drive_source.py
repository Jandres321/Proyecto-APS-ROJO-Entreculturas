"""Google Drive as a source of files: listing, mirroring, the registry of connected folders and
the /api/ui routes, against a fake Drive API (no network, no credentials)."""

import json
import re
import time
from types import SimpleNamespace

import pytest

from app import drive, projects
from app.api import ui
from app.config import Settings, get_settings
from app.pipeline.run import PilotProject

FOLDER, SHEET, SHORTCUT = drive.MIME_FOLDER, drive.MIME_GOOGLE_SHEET, drive.MIME_SHORTCUT
PDF = "application/pdf"


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeDrive:
    """`service.files()` of the Drive API: folders are {folder id: [items]}."""

    def __init__(self, tree, contents=None, names=None):
        self.tree, self.contents, self.names = tree, contents or {}, names or {}
        self.downloads: list[str] = []
        self.exports: list[str] = []

    def files(self):
        return self

    def list(self, q, **kwargs):
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        return Request({"files": self.tree.get(parent, [])})

    def get(self, fileId, **kwargs):  # noqa: N803 - the Drive API's own parameter name
        if fileId not in self.tree:
            return Request(RuntimeError("404"))
        return Request(
            {"id": fileId, "name": self.names.get(fileId, "Carpeta"), "mimeType": FOLDER}
        )

    def get_media(self, fileId, **kwargs):  # noqa: N803
        self.downloads.append(fileId)
        return Request(self.contents.get(fileId, b"contenido de " + fileId.encode()))

    def export_media(self, fileId, mimeType):  # noqa: N803
        self.exports.append(fileId)
        return Request(b"hoja exportada")


def item(file_id, name, mime=PDF, md5="m1", **extra):
    return {"id": file_id, "name": name, "mimeType": mime, "md5Checksum": md5, **extra}


def tree():
    return {
        "raiz": [
            item("f1", "1. Factura.pdf"),
            item("f2", "Informe económico.xlsx", "application/xlsx"),
            item("sub", "Nóminas", FOLDER, None),
            item("hoja", "Listado", SHEET, None, modifiedTime="2024-05-01T10:00:00Z"),
            item("doc", "Notas", "application/vnd.google-apps.document", None),
            item(
                "atajo",
                "Atajo a carpeta",
                SHORTCUT,
                None,
                shortcutDetails={"targetId": "otra", "targetMimeType": FOLDER},
            ),
        ],  # fmt: skip
        "sub": [
            item("f3", "2. Nómina: julio/agosto.pdf"),
            item("f4", "2. Nómina_ julio_agosto.pdf"),
        ],
        "otra": [item("f5", "3. Recibo.pdf")],
    }


# --- ids, listing and mirroring ---


def test_folder_id_from_urls_and_ids():
    assert drive.folder_id("https://drive.google.com/drive/folders/1AbC_def-123?usp=sharing") == (
        "1AbC_def-123"
    )
    assert drive.folder_id("https://drive.google.com/open?id=1AbC_def-123") == "1AbC_def-123"
    assert drive.folder_id(" 1AbC_def-123XYZ ") == "1AbC_def-123XYZ"
    with pytest.raises(drive.DriveError):
        drive.folder_id("https://example.com/no-es-drive")


def test_listing_is_recursive_safe_and_unique():
    paths = {f.id: f.path for f in drive.list_folder(FakeDrive(tree()), "raiz")}
    assert paths["f1"] == "1. Factura.pdf"
    assert paths["hoja"] == "Listado.xlsx"  # a Google Sheet is exported as Excel
    assert paths["f5"] == "Atajo a carpeta/3. Recibo.pdf"  # shortcuts to folders are followed
    # characters Windows rejects are replaced, and the two names that then collide stay apart
    assert paths["f3"] == "Nóminas/2. Nómina_ julio_agosto.pdf"
    assert paths["f4"] == "Nóminas/2. Nómina_ julio_agosto (2).pdf"


def test_sync_downloads_only_what_is_new_or_changed_and_removes_what_is_gone(tmp_path):
    files, target, manifest = tree(), tmp_path / "copia", tmp_path / "copia.manifest.json"
    service = FakeDrive(files)
    seen = []
    first = drive.sync_folder(service, "raiz", target, manifest, lambda d, t: seen.append((d, t)))
    assert (first.total, first.downloaded, first.unchanged, first.removed) == (7, 6, 0, 0)
    assert first.skipped == ["Notas"]  # a Google Doc is not an evidence file
    assert (target / "1. Factura.pdf").read_bytes() == b"contenido de f1"
    assert (target / "Listado.xlsx").read_bytes() == b"hoja exportada"
    assert (target / "Nóminas" / "2. Nómina_ julio_agosto (2).pdf").is_file()
    assert seen[-1] == (7, 7) and not list(target.rglob("*.json"))  # manifest kept outside

    service = FakeDrive(files)
    again = drive.sync_folder(service, "raiz", target, manifest)
    assert (again.downloaded, again.unchanged) == (0, 6) and service.downloads == []

    files["raiz"][0] = item("f1", "1. Factura.pdf", md5="m2")  # changed in Drive
    del files["otra"][0]  # deleted in Drive
    (target / "1. Factura.pdf").write_bytes(b"viejo")
    service = FakeDrive(files, {"f1": b"nuevo"})
    third = drive.sync_folder(service, "raiz", target, manifest)
    assert (third.downloaded, third.unchanged, third.removed) == (1, 4, 1)
    assert service.downloads == ["f1"] and (target / "1. Factura.pdf").read_bytes() == b"nuevo"
    assert not (target / "Atajo a carpeta" / "3. Recibo.pdf").exists()


def test_status_and_credentials_errors_are_explained(tmp_path):
    missing = Settings(google_credentials=tmp_path / "no.json")
    assert drive.status(missing)["available"] is False
    with pytest.raises(drive.DriveError, match="credenciales"):
        drive.build_service(missing)

    pytest.importorskip("google_auth_oauthlib")
    oauth = tmp_path / "credentials.json"
    oauth.write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}))
    settings = Settings(google_credentials=oauth, google_token=tmp_path / "token.json")
    assert drive.status(settings)["available"] is True
    # a server cannot open a browser: it asks for the one-off authorisation instead of hanging
    with pytest.raises(drive.DriveError, match="python -m app.drive"):
        drive.build_service(settings)


def test_a_folder_that_cannot_be_opened_is_a_clear_error():
    with pytest.raises(drive.DriveError, match="no tiene acceso"):
        drive.folder_name(FakeDrive(tree()), "desconocida")
    assert (
        drive.folder_name(FakeDrive(tree(), names={"raiz": "Proyecto X"}), "raiz") == "Proyecto X"
    )


# --- registry of connected folders and the report ---


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(projects, "PILOTS", {})
    monkeypatch.setattr(ui, "_jobs", {})
    return tmp_path


def test_connected_folders_are_remembered_and_listed_after_the_pilots(data_dir, monkeypatch):
    builtin = PilotProject("p", "Piloto", "xunta", "P-1", "carpeta", "informe.xls")
    monkeypatch.setattr(projects, "PILOTS", {"p": builtin})
    assert projects.drive_projects() == {}

    project = projects.connect_drive_folder("1AbCdEfGhIjKlMn", "Mi proyecto", "aecid", "inf.xlsx")
    assert (project.key, project.code, project.folder) == (
        "drive-1abcdefghi",
        "DRIVE-1AbCdEfGhI",
        "_drive/DRIVE-1AbCdEfGhI",
    )
    assert list(projects.all_projects()) == ["p", "drive-1abcdefghi"]
    assert projects.all_projects()[project.key].donor == "aecid"
    # connecting the same folder again updates it instead of duplicating it
    projects.connect_drive_folder("1AbCdEfGhIjKlMn", "Otro nombre", "xunta")
    assert [p.title for p in projects.drive_projects().values()] == ["Otro nombre"]

    with pytest.raises(projects.ProjectError, match="Donante"):
        projects.connect_drive_folder("1AbCdEfGhIjKlMn", "x", "echo")
    with pytest.raises(projects.ProjectError, match="nombre"):
        projects.connect_drive_folder("1AbCdEfGhIjKlMn", " ", "xunta")
    projects.disconnect_drive_folder(project.key)
    assert projects.drive_projects() == {}
    with pytest.raises(projects.ProjectError):
        projects.disconnect_drive_folder(project.key)


def test_the_report_is_found_or_the_candidates_are_named(tmp_path):
    def folder(*names):
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        for name in names:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(b"x")
        return root

    assert projects.find_report(folder("a.pdf", "Listado.xlsx")) == "Listado.xlsx"  # the only one
    both = folder("Informe económico final.xls", "sub/Presupuesto.xlsx", "~$Informe.xlsx")
    assert projects.find_report(both) == "Informe económico final.xls"  # the one named "informe"
    assert projects.find_report(both, "presupuesto") == "sub/Presupuesto.xlsx"  # the user's hint
    with pytest.raises(projects.ProjectError, match="No encuentro el informe «otro»"):
        projects.find_report(both, "otro")
    with pytest.raises(projects.ProjectError, match="varias hojas.*A.xlsx, B.xlsx"):
        projects.find_report(folder("A.xlsx", "B.xlsx"))
    with pytest.raises(projects.ProjectError, match="ninguna hoja"):
        projects.find_report(folder("a.pdf"))


# --- /api/ui ---


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def wait_for(client, key, state):
    for _ in range(200):
        job = client.get(f"/api/ui/pilots/{key}/run").json()
        if job["state"] == state:
            return job
        time.sleep(0.02)
    raise AssertionError(f"the run never reached {state!r}: {job}")


def test_connecting_and_analysing_a_drive_folder(data_dir, client, monkeypatch):
    service = FakeDrive(tree(), names={"raiz1234567890": "Carpeta del socio"})
    service.tree["raiz1234567890"] = service.tree.pop("raiz")
    monkeypatch.setattr(ui.drive, "build_service", lambda *a, **k: service)
    monkeypatch.setattr(ui.db, "SessionLocal", FakeSession)
    seen = {}

    def fake_pipeline(session, pilot, *, reread_report, on_progress):
        seen["pilot"] = pilot
        on_progress("ingest", 0.0, "Leyendo el informe económico")
        matching = SimpleNamespace(
            expenses_linked=1, verified=0, from_name=1, from_content=0, not_in_sample=0
        )
        return SimpleNamespace(expenses=2, documents=6, matching=matching, report_reread=True)

    monkeypatch.setattr(ui, "run_pipeline", fake_pipeline)

    url = "https://drive.google.com/drive/folders/raiz1234567890"
    created = client.post(
        "/api/ui/drive/projects", json={"url": url, "title": "", "donor": "xunta"}
    )
    assert created.status_code == 201
    key = created.json()["key"]
    assert created.json()["folder_name"] == "Carpeta del socio"  # the default title

    # (the pilots listing itself needs the database: see test_web_app.py)
    assert key in projects.all_projects() and projects.all_projects()[key].title == (
        "Carpeta del socio"
    )

    assert client.post(f"/api/ui/pilots/{key}/run").status_code == 202
    done = wait_for(client, key, "done")
    assert (
        "Drive: 7 archivos (6 descargados, 0 sin cambios, 1 documentos de Google omitidos)"
        in (done["message"])
    )
    pilot = seen["pilot"]
    # the pipeline got the local mirror (inside DATA_DIR) and the report that was found
    assert (pilot.folder, pilot.report) == ("_drive/DRIVE-raiz123456", "Informe económico.xlsx")
    assert (data_dir / pilot.folder / "1. Factura.pdf").is_file()
    assert (data_dir / "_drive" / "DRIVE-raiz123456.manifest.json").is_file()

    assert client.delete(f"/api/ui/drive/projects/{key}").status_code == 204
    assert client.delete(f"/api/ui/drive/projects/{key}").status_code == 404
    assert (data_dir / pilot.folder / "1. Factura.pdf").is_file()  # downloaded files are kept


def test_drive_problems_are_reported_to_the_user(data_dir, client, monkeypatch):
    service = FakeDrive({"raiz1234567890": [item("f1", "a.pdf")]})
    monkeypatch.setattr(ui.drive, "build_service", lambda *a, **k: service)
    monkeypatch.setattr(ui.db, "SessionLocal", FakeSession)

    def body(**changes):
        base = {"url": "https://drive.google.com/drive/folders/raiz1234567890", "title": "P"}
        return {**base, "donor": "xunta", **changes}

    bad_url = client.post("/api/ui/drive/projects", json=body(url="no es un enlace"))
    assert bad_url.status_code == 400 and "No reconozco" in bad_url.json()["detail"]
    no_access = client.post("/api/ui/drive/projects", json=body(url="carpetasinacceso1"))
    assert no_access.status_code == 400 and "no tiene acceso" in no_access.json()["detail"]
    assert client.post("/api/ui/drive/projects", json=body(donor="echo")).status_code == 400

    key = client.post("/api/ui/drive/projects", json=body()).json()["key"]
    assert client.post(f"/api/ui/pilots/{key}/run").status_code == 202
    failed = wait_for(client, key, "error")  # the folder has no report spreadsheet
    assert "no tiene ninguna hoja de cálculo" in failed["message"]

    def no_credentials(*args, **kwargs):
        raise drive.DriveError("Falta autorizar el acceso a Google Drive.")

    monkeypatch.setattr(ui.drive, "build_service", no_credentials)
    refused = client.post("/api/ui/drive/projects", json=body())
    assert refused.status_code == 400 and "Falta autorizar" in refused.json()["detail"]
