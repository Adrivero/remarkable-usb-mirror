import io
import zipfile

import httpx
import pytest

from conftest import pdf
from src.config import Config
from src.usb_client import USBClient


def setup_upload(h, monkeypatch):
    calls = []
    monkeypatch.setattr(h.client, "upload", lambda source, name: calls.append((name, source.read_bytes())))
    h.root.mkdir(exist_ok=True)
    return calls


def test_new_local_pdf_uploaded_once_and_exports_excluded(h, monkeypatch):
    calls = setup_upload(h, monkeypatch)
    (h.root / "Local.pdf").write_bytes(pdf())
    assert h.sync().uploaded == 1
    assert calls == [("Local.pdf", pdf())]
    assert h.sync().uploaded == 0
    assert len(calls) == 1
    (h.root / "Local.pdf").write_bytes(pdf("edited"))
    assert h.sync().uploaded == 0


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


def test_upload_http_multipart_and_no_retry(h, tmp_path):
    requests = []
    source = tmp_path / "snapshot"
    source.write_bytes(pdf())

    def handle(request):
        requests.append(request)
        assert request.method == "POST" and request.url.path == "/upload"
        assert b'name="file"; filename="Local.pdf"' in request.content
        assert pdf() in request.content
        return httpx.Response(200, json={"status": "ok"})

    client = USBClient(h.config, transport=httpx.MockTransport(handle))
    try:
        client.upload(source, "Local.pdf")
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
