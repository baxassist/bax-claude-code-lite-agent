"""Вложения с телефона сохраняются в проекте; Claude читает их обычным инструментом Read."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
from pathlib import Path

MAX_FILES = 8
MAX_FILE_BYTES = 6 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024


def prepare(project: Path, raw: object) -> str:
    if raw is None or raw == []:
        return ""
    if not isinstance(raw, list) or len(raw) > MAX_FILES:
        raise ValueError("Можно прикрепить до 8 файлов")
    files, total = [], 0
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Не удалось прочитать вложение")
        name, mime, encoded = item.get("name"), item.get("mime"), item.get("data")
        if (
            not isinstance(name, str)
            or not name.strip()
            or name in {".", ".."}
            or any(ch in name for ch in ("/", "\\"))
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
            or len(name.encode()) > 240
        ):
            raise ValueError("Недопустимое имя файла")
        if not isinstance(mime, str) or not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", mime):
            raise ValueError("Неизвестный формат файла")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
            raise ValueError("Один файл должен быть не больше 6 МиБ")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("Не удалось прочитать данные файла") from error
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Файлы в одной задаче должны быть не больше 8 МиБ вместе")
        files.append((name, data))
    project = project.resolve()
    root = project / ".bax-attachments"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.resolve().parent != project:
        raise ValueError("Папка вложений должна находиться внутри проекта и не быть ссылкой")
    ignore = root / ".gitignore"
    try:
        descriptor = os.open(
            ignore, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except FileExistsError:
        if ignore.is_symlink():
            raise ValueError("Файл исключений вложений не должен быть ссылкой") from None
        descriptor = os.open(ignore, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r+") as stream:
            existing = stream.read()
            if existing.splitlines()[-1:] != ["*"]:
                stream.write(("" if not existing or existing.endswith("\n") else "\n") + "*\n")
    else:
        with os.fdopen(descriptor, "w") as stream:
            stream.write("*\n")
    folder = Path(tempfile.mkdtemp(prefix="upload-", dir=root))
    paths = []
    for name, data in files:
        target = Path(tempfile.mkdtemp(prefix="file-", dir=folder)) / name
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        paths.append(f"{json.dumps(name, ensure_ascii=False)}: {json.dumps(str(target), ensure_ascii=False)}")
    return "\n\nПрикреплённые файлы (прочитайте их при выполнении задачи):\n" + "\n".join(paths)
