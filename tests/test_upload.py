import io
import zipfile

import httpx
import pytest

from conftest import pdf, rmdoc
from src.config import Config
from src.usb_client import USBClient


def setup_upload(h, monkeypatch):
    calls = []
    monkeypatch.setattr(h.client, "upload", lambda source, name: calls.append((name, source.read_bytes())))
    h.root.mkdir(exist_ok=True)
    return calls


@pytest.mark.parametrize("fmt, content", [("pdf", pdf), ("rmdoc", rmdoc)])
def test_new_local_file_uploaded_once_and_exports_excluded(h, monkeypatch, fmt, content):
    calls = setup_upload(h, monkeypatch)
    source = h.root / f"Local.{fmt}"
    original = content()
    source.write_bytes(original)
    assert h.sync().uploaded == 1
    assert calls == [(source.name, original)]
    assert h.sync().uploaded == 0
    assert len(calls) == 1
    source.write_bytes(content("edited"))
    assert h.sync().uploaded == 0


@pytest.mark.parametrize("native_name", ["Javier Manero.rmdoc", "JAVIER MANERO.RMDOC"])
def test_native_notebook_preferred_over_matching_pdf(h, monkeypatch, native_name):
    calls = setup_upload(h, monkeypatch)
    folder = h.root / "Local"
    folder.mkdir()
    native = rmdoc()
    (folder / native_name).write_bytes(native)
    (folder / "Javier Manero.pdf").write_bytes(pdf())
    result = h.sync(dry_run=True)
    assert [p for p in result.plan if p["action"] == "upload"] == [
        {"action": "upload", "path": f"Local/{native_name}"}
    ]
    assert not calls and not h.rows("uploads")
    assert h.sync().uploaded == 1
    assert calls == [(native_name, native)]
    assert h.sync().uploaded == 0
    assert len(calls) == 1
    assert (folder / "Javier Manero.pdf").read_bytes() == pdf()


def test_native_can_be_added_after_pdf_was_already_uploaded(h, monkeypatch):
    calls = setup_upload(h, monkeypatch)
    (h.root / "Javier Manero.pdf").write_bytes(pdf())
    assert h.sync().uploaded == 1
    native = rmdoc()
    (h.root / "Javier Manero.rmdoc").write_bytes(native)
    assert h.sync().uploaded == 1
    assert calls == [("Javier Manero.pdf", pdf()), ("Javier Manero.rmdoc", native)]
    assert h.sync().uploaded == 0


def test_native_in_another_folder_or_symlink_does_not_suppress_pdf(h, monkeypatch, tmp_path):
    calls = setup_upload(h, monkeypatch)
    (h.root / "Local.pdf").write_bytes(pdf())
    native = tmp_path / "original.rmdoc"
    native.write_bytes(rmdoc())
    (h.root / "Local.rmdoc").symlink_to(native)
    folder = h.root / "Nested"
    folder.mkdir()
    (folder / "Local.rmdoc").write_bytes(native.read_bytes())
    assert h.sync().uploaded == 2
    assert [name for name, _ in calls] == ["Local.pdf", "Local.rmdoc"]


def test_invalid_native_does_not_fall_back_to_pdf_and_can_be_fixed(h, monkeypatch):
    calls = setup_upload(h, monkeypatch)
    source = h.root / "Local.rmdoc"
    source.write_bytes(b"partial copy")
    (h.root / "Local.pdf").write_bytes(pdf())
    assert not h.sync().complete
    assert not calls and not h.rows("uploads")
    native = rmdoc()
    source.write_bytes(native)
    assert h.sync().uploaded == 1
    assert calls == [("Local.rmdoc", native)]


def test_uncertain_native_does_not_retry_or_upload_pdf(h, monkeypatch):
    calls = setup_upload(h, monkeypatch)
    (h.root / "Local.pdf").write_bytes(pdf())
    (h.root / "Local.rmdoc").write_bytes(rmdoc())

    def disconnected(source, name):
        calls.append(name)
        raise httpx.ReadError("response lost")

    monkeypatch.setattr(h.client, "upload", disconnected)
    assert not h.sync().complete
    assert h.rows("uploads")[0]["status"] == "pending"
    assert not h.sync().complete
    assert calls == ["Local.rmdoc"]


def test_disabled_and_dry_run_do_not_post(h, monkeypatch):
    calls = setup_upload(h, monkeypatch)
    (h.root / "Local.pdf").write_bytes(pdf())
    h.config.two_way_sync = False
    assert h.sync().uploaded == 0
    h.config.two_way_sync = True
    result = h.sync(dry_run=True)
    assert {"action": "upload", "path": "Local.pdf"} in result.plan
    assert not calls and not h.rows("uploads")
    assert h.sync().uploaded == 1


def test_nested_epub_hidden_symlink_and_unsupported_files(h, monkeypatch, tmp_path):
    calls = setup_upload(h, monkeypatch)
    folder = h.root / "Books"
    folder.mkdir()
    book = io.BytesIO()
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", "<container/>")
    (folder / "Book.EPUB").write_bytes(book.getvalue())
    (h.root / ".hidden.pdf").write_bytes(pdf())
    (h.root / "notes.txt").write_text("notes")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(pdf())
    (h.root / "link.pdf").symlink_to(outside)
    (h.root / "linked-folder").symlink_to(tmp_path, target_is_directory=True)
    assert h.sync().uploaded == 1
    assert calls == [("Book.EPUB", book.getvalue())]


def test_uncertain_upload_is_not_retried_after_restart(h, monkeypatch):
    calls = setup_upload(h, monkeypatch)
    (h.root / "Local.pdf").write_bytes(pdf())

    def disconnected(source, name):
        calls.append(name)
        raise httpx.ReadError("response lost")

    monkeypatch.setattr(h.client, "upload", disconnected)
    assert not h.sync().complete
    assert h.rows("uploads")[0]["status"] == "pending"
    assert not h.sync().complete
    assert calls == ["Local.pdf"]
    assert (h.root / "Local.pdf").read_bytes() == pdf()


def test_invalid_pdf_can_be_fixed_and_retried(h, monkeypatch):
    calls = setup_upload(h, monkeypatch)
    source = h.root / "Local.pdf"
    source.write_bytes(b"partial copy")
    assert not h.sync().complete
    assert not calls and not h.rows("uploads")
    source.write_bytes(pdf())
    assert h.sync().uploaded == 1


@pytest.mark.parametrize("name, content, mime", [
    ("Local.pdf", pdf, b"application/pdf"),
    ("Local.RMDOC", rmdoc, b"application/octet-stream"),
])
def test_upload_http_multipart_and_no_retry(h, tmp_path, name, content, mime):
    requests = []
    source = tmp_path / "snapshot"
    original = content()
    source.write_bytes(original)

    def handle(request):
        requests.append(request)
        assert request.method == "POST" and request.url.path == "/upload"
        assert f'name="file"; filename="{name}"'.encode() in request.content
        assert b"Content-Type: " + mime in request.content
        assert original in request.content
        return httpx.Response(200, json={"status": "ok"})

    client = USBClient(h.config, transport=httpx.MockTransport(handle))
    try:
        client.upload(source, name)
    finally:
        client.close()
    assert len(requests) == 1


def test_existing_config_defaults_to_two_way_sync_and_saves_toggle(tmp_path):
    (tmp_path / "config.json").write_text('{}')
    config = Config.load(tmp_path)
    assert config.two_way_sync
    config.two_way_sync = False
    config.save(tmp_path)
    assert not Config.load(tmp_path).two_way_sync


def test_failed_connection_can_retry_next_sync(h, monkeypatch):
    from src.models import UploadNotSent

    calls = setup_upload(h, monkeypatch)
    (h.root / "Local.pdf").write_bytes(pdf())

    def disconnected(source, name):
        raise UploadNotSent("connection refused")

    monkeypatch.setattr(h.client, "upload", disconnected)
    assert not h.sync().complete
    assert not h.rows("uploads")
    monkeypatch.setattr(h.client, "upload", lambda source, name: calls.append(name))
    assert h.sync().uploaded == 1


@pytest.mark.parametrize("status", [400, 500, 302])
def test_http_failure_never_retries_post(h, tmp_path, status):
    from src.models import MirrorError

    calls = []
    source = tmp_path / "source.pdf"
    source.write_bytes(pdf())

    def handle(request):
        calls.append(request)
        return httpx.Response(status)

    client = USBClient(h.config, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(MirrorError):
            client.upload(source, "source.pdf")
    finally:
        client.close()
    assert len(calls) == 1
