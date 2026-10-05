import base64
import json
from pathlib import Path

import pytest
from test_channel import channel as channel
from test_channel import channel_module


def file(name="Отчёт.pdf", data=b"%PDF-1.7\n", mime="application/pdf"):
    return {"name": name, "mime": mime, "data": base64.b64encode(data).decode()}


async def test_documents_and_photos_reach_the_session_as_existing_project_files(channel):
    channel.project.mkdir()
    await channel.on_frame(
        {"type": "run", "text": "Сравни", "attachments": [file(), file("Фото.jpg", b"photo", "image/jpeg")]}
    )
    content = channel.session.sent[0][1]["content"]
    paths = [Path(json.loads(line.split(": ", 1)[1])) for line in content.splitlines()[-2:]]
    assert [path.read_bytes() for path in paths] == [b"%PDF-1.7\n", b"photo"]
    assert all(path.is_relative_to(channel.project) for path in paths)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    assert channel.link.of("status")[-1]["state"] == "busy"


async def test_document_only_task_and_duplicate_names_are_preserved(channel):
    channel.project.mkdir()
    await channel.on_frame(
        {"type": "run", "text": "", "attachments": [file(data=b"first"), file(data=b"second")]}
    )
    assert channel.session.sent
    assert sorted(path.read_bytes() for path in channel.project.rglob("Отчёт.pdf")) == [b"first", b"second"]
    assert (channel.project / ".bax-attachments" / ".gitignore").read_text() == "*\n"


@pytest.mark.parametrize(
    "bad",
    [
        file("../secret"),
        file("a/b"),
        file("a\\b"),
        file("\n"),
        {"name": "x", "mime": "text/plain", "data": "%%%"},
    ],
)
async def test_invalid_batch_is_rejected_before_any_file_or_notification(channel, bad):
    channel.project.mkdir()
    await channel.on_frame({"type": "run", "text": "Задача", "attachments": [file(), bad]})
    assert not channel.session.sent and not list(channel.project.iterdir())
    assert channel.link.of("error")[-1]["code"] == "validation_error"


async def test_readonly_session_never_writes_incoming_files(channel):
    channel.project.mkdir()
    channel.observer = True
    await channel.on_frame({"type": "run", "text": "", "attachments": [file()]})
    assert not channel.session.sent and not list(channel.project.iterdir())
    assert channel.link.of("error")[-1]["code"] == "readonly"


async def test_background_request_reads_current_tasks(channel):
    await channel.on_frame({"type": "background.get"})
    assert channel.link.of("background")[-1]["tasks"] == []
    assert "background.get" in channel_module.CAPS["supports"]


def test_existing_ignore_cannot_expose_incoming_documents(tmp_path):
    root = tmp_path / ".bax-attachments"
    root.mkdir()
    (root / ".gitignore").write_text("*.png\n!*.pdf")
    channel_module.attachments.prepare(tmp_path, [file()])
    assert (root / ".gitignore").read_text() == "*.png\n!*.pdf\n*\n"
