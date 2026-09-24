"""История — из файла сессии самого Claude Code: своей копии переписки плагин не держит,
и сервер Бакса её тоже не хранит — релей остаётся простым посредником (заказчик 23.09).

В файле есть всё: задачи с телефона, ответы через `reply` и то, что человек писал прямо
в терминале, — приложение видит полную историю сессии.

Файл сессии: `~/.claude/projects/<путь-проекта-через-дефисы>/<id сессии>.jsonl`, одна запись —
одна строка. `id` сообщения — номер строки в файле: уникален, растёт, и листание вверх — это
чтение строк с меньшими номерами. Id своей сессии плагин получает от Claude Code
в `CLAUDE_CODE_SESSION_ID`.

Файл читается **с конца**: у долгой сессии он бывает на сотни мегабайт, а при открытии агента
нужны последние десять сообщений.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Ответ модели в телефон — вызов этого инструмента; в ленте это обычный ответ
REPLY_TOOL = "mcp__plugin_bax_bax__reply"
#: Задача с телефона — служебная запись с обёрткой канала
CHANNEL = re.compile(r'^<channel\b[^>]*\bsource="bax"[^>]*>\s*(.*?)\s*</channel>\s*$', re.S)
#: Эхо слэш-команд и вывода локальных команд — не реплики человека
COMMAND_ECHO = ("<command-", "<local-command-")
#: Уведомление о фоновой задаче: Claude Code пишет его в сессию от имени человека
#: (заказчик 23.09: в телефоне оно шло сырым служебным текстом)
TASK_NOTE = re.compile(r"<task-notification>.*?</task-notification>", re.S)
TASK_STATUS = {"completed": "завершена", "failed": "упала", "killed": "остановлена", "stopped": "остановлена"}


def _task_line(text: str) -> str | None:
    """Уведомление о фоновой задаче → одна строка активности: что за задача и чем кончилась."""
    note = TASK_NOTE.search(text)
    if note is None:
        return None
    def tag(name: str) -> str:
        found = re.search(rf"<{name}>(.*?)</{name}>", note.group(0), re.S)
        return found.group(1).strip() if found else ""
    summary = tag("summary") or "фоновая задача"
    status = TASK_STATUS.get(tag("status"), tag("status"))
    return f"⏱ {summary}" + (f" — {status}" if status and status not in summary else "")


@dataclass(frozen=True)
class Message:
    id: int
    kind: str  # user | assistant | thinking | tool
    text: str


def projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def session_file(project: Path, session_id: str) -> Path | None:
    """Файл своей сессии. Каталог проекта Claude Code пишет через дефисы; если имя вышло
    иным (точки, подчёркивания), ищем файл по id сессии — он уникален."""
    root = projects_root()
    if session_id:
        direct = root / str(project).replace("/", "-") / f"{session_id}.jsonl"
        if direct.exists():
            return direct
        found = next(iter(root.glob(f"*/{session_id}.jsonl")), None) if root.is_dir() else None
        if found is not None:
            return found
    # старый Claude Code id сессии не передаёт — берём самый свежий файл проекта
    folder = root / str(project).replace("/", "-")
    files = sorted(folder.glob("*.jsonl"), key=lambda item: item.stat().st_mtime) if folder.is_dir() else []
    return files[-1] if files else None


def _queued(index: int, entry: dict, live: bool) -> list[Message]:
    """Сообщение, пришедшее, пока модель работала: Claude Code пишет его не репликой,
    а вложением `queued_command` (заказчик 23.09: ответ, отправленный посреди работы,
    пропадал из ленты и не находился даже при новом открытии агента)."""
    attachment = entry.get("attachment") or {}
    if attachment.get("type") != "queued_command":
        return []
    text = str(attachment.get("prompt") or "").strip()
    channel = CHANNEL.match(text)
    if channel:
        task = channel.group(1).strip()
        return [Message(index, "user", task)] if task and not live else []
    task = _task_line(text)
    if task is not None:
        return [Message(index, "tool", task)]
    if not text or text.startswith(COMMAND_ECHO):
        return []
    return [Message(index, "user", text)]


def _tool_line(block: dict) -> str:
    """Вызов инструмента — одной строкой активности: простыни вывода в ленту не тащим."""
    name = str(block.get("name") or "инструмент")
    data = block.get("input")
    if isinstance(data, dict):
        hint = data.get("description") or data.get("command") or data.get("file_path") or data.get("pattern")
        if hint:
            return f"{name}: {str(hint)[:160]}"
    return name


def _text_of(content) -> str:
    if isinstance(content, str):
        return content.strip()
    return "\n".join(
        str(block.get("text") or "").strip()
        for block in content or []
        if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip()
    )


def messages_from(index: int, entry: dict, live: bool = False) -> list[Message]:
    """Запись файла → сообщения ленты.

    `live` — запись только что дописана, и плагин шлёт её на лету: задачу с телефона и ответ
    через `reply` он уже отправил сам в момент события — второй раз их не шлём."""
    kind = entry.get("type")
    if kind == "attachment" and not entry.get("isSidechain"):
        return _queued(index, entry, live)
    if kind not in ("user", "assistant") or entry.get("isSidechain"):
        return []
    content = (entry.get("message") or {}).get("content")

    if kind == "user":
        text = _text_of(content)
        if not text:
            return []  # tool_result — вывод инструмента, он уже показан строкой активности
        channel = CHANNEL.match(text)
        if channel:
            # задача с телефона: Claude Code пишет её служебной записью в обёртке канала
            task = channel.group(1).strip()
            return [Message(index, "user", task)] if task and not live else []
        task = _task_line(text)
        if task is not None:
            return [Message(index, "tool", task)]
        if entry.get("isMeta") or text.startswith(COMMAND_ECHO):
            return []  # вставки CLI (навыки, подсказки) и эхо команд — не реплики человека
        return [Message(index, "user", text)]

    found = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and str(block.get("text") or "").strip():
            found.append(Message(index, "assistant", block["text"].strip()))
        elif block_type == "thinking" and str(block.get("thinking") or "").strip():
            found.append(Message(index, "thinking", block["thinking"].strip()))
        elif block_type == "tool_use":
            data = block.get("input") if isinstance(block.get("input"), dict) else {}
            if block.get("name") == REPLY_TOOL:
                if str(data.get("text") or "").strip() and not live:
                    found.append(Message(index, "assistant", str(data["text"]).strip()))
            else:
                found.append(Message(index, "tool", _tool_line(block)))
    return found


def _lines_backwards(file: Path, chunk: int = 1 << 20):
    """(номер строки, строка) от конца файла к началу — не разбирая весь файл."""
    with file.open("rb") as fh:
        size = fh.seek(0, 2)
        if size == 0:
            return
        fh.seek(size - 1)
        end = size - 1 if fh.read(1) == b"\n" else size
        fh.seek(0)
        lines, read = 0, 0
        while read < end:
            block = fh.read(min(chunk, end - read))
            read += len(block)
            lines += block.count(b"\n")
        index, position, head = lines, end, b""
        while position > 0:
            step = min(chunk, position)
            position -= step
            fh.seek(position)
            parts = (fh.read(step) + head).split(b"\n")
            head = parts[0]
            for part in reversed(parts[1:]):
                yield index, part
                index -= 1
        yield index, head


def _messages_backwards(file: Path, before: int | None = None):
    for index, raw in _lines_backwards(file):
        if before is not None and index >= before:
            continue
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue  # последняя строка может быть недописана — файл пишется на лету
        yield from reversed(messages_from(index, entry))


def _last(file: Path | None, limit: int, before: int | None = None) -> list[Message]:
    if file is None or not file.exists():
        return []
    found: list[Message] = []
    for message in _messages_backwards(file, before):
        found.append(message)
        if len(found) >= limit:
            break
    return list(reversed(found))


def tail(file: Path | None, limit: int = 10) -> list[Message]:
    """Последние сообщения — то, что приложение получает при открытии агента."""
    return _last(file, limit)


def before(file: Path | None, before_id: int, limit: int = 10) -> list[Message]:
    """Листание вверх: сообщения со строк раньше `before_id`."""
    return _last(file, limit, before_id)


def next_id(file: Path | None) -> int:
    """Номер следующей строки: с него нумеруются сообщения, которые плагин шлёт на лету,
    пока Claude Code ещё не дописал их в файл, — так они встают после истории."""
    if file is None or not file.exists():
        return 0
    with file.open("rb") as fh:
        lines, last = 0, b""
        while block := fh.read(1 << 20):
            lines += block.count(b"\n")
            last = block[-1:]
    return lines if last in (b"\n", b"") else lines + 1


def turn_state(entry: dict) -> str | None:
    """Идёт ли ход — по записи файла (заказчик 23.09: агент работал, а в телефоне «ждёт
    задачу»). Конец хода Claude Code отмечает записью `system/turn_duration`; любая запись
    модели, результат инструмента или реплика человека — ход идёт."""
    kind = entry.get("type")
    if kind == "system" and entry.get("subtype") == "turn_duration":
        return "ready"
    if entry.get("isSidechain"):
        return None
    if kind == "assistant":
        return "busy"
    if kind == "attachment" and (entry.get("attachment") or {}).get("type") == "queued_command":
        return "busy"
    text = str(_text_of((entry.get("message") or {}).get("content")))
    if kind == "user" and not text.startswith(COMMAND_ECHO):
        return "busy"
    return None


#: Фоновые задачи сессии: запуск виден в результате инструмента, конец — уведомлением
#: `<task-notification>` или результатом остановки. Пока задача идёт, агент «работает»,
#: даже если ход закончился (заказчик 23.09: ход кончился ожиданием загрузки — в телефоне
#: «ждёт задачу», хотя задача не доделана)
TASK_STARTED = re.compile(
    r"running in background with ID: (\w+)"
    r"|Monitor started \(task (\w+)"
    # долгая команда, которую Claude Code увёл в фон сам, по таймауту
    r"|moved to the background \(ID: (\w+)\)"
)
TASK_STOPPED = re.compile(r"Successfully stopped task: (\w+)")
TASK_DONE = re.compile(r"<task-id>(\w+)</task-id>.*?<status>(\w+)</status>", re.S)


def _result_texts(content) -> list[str]:
    """Тексты результатов инструментов из записи человека (`tool_result`)."""
    found = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        inner = block.get("content")
        if isinstance(inner, str):
            found.append(inner)
        elif isinstance(inner, list):
            found += [str(b.get("text") or "") for b in inner if isinstance(b, dict)]
    return found


def background_changes(entry: dict) -> tuple[set[str], set[str]]:
    """(запущенные, закончившиеся) фоновые задачи по записи файла."""
    if entry.get("type") != "user":
        return set(), set()
    content = (entry.get("message") or {}).get("content")
    started, finished = set(), set()
    for text in _result_texts(content):
        for match in TASK_STARTED.finditer(text):
            started.add(next(group for group in match.groups() if group))
        finished |= set(TASK_STOPPED.findall(text))
    text = content if isinstance(content, str) else _text_of(content)
    for task_id, status in TASK_DONE.findall(text or ""):
        if status in TASK_STATUS:
            finished.add(task_id)
    return started, finished


#: Инструменты, которые запускают фоновую задачу: из их вызова берём, что это за задача
def _describe_tool(block: dict) -> str:
    data = block.get("input") if isinstance(block.get("input"), dict) else {}
    text = data.get("description") or data.get("command") or data.get("prompt") or block.get("name") or ""
    return " ".join(str(text).split())[:160]


def _parse_time(entry: dict) -> float:
    """Время записи — секунды эпохи; нет — 0."""
    stamp = str(entry.get("timestamp") or "")
    if not stamp:
        return 0.0
    try:
        from datetime import datetime  # noqa: PLC0415

        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


@dataclass
class Background:
    """Фоновые задачи сессии с подробностями — для кнопки фоновых процессов в приложении
    (заказчик 24.09): что за задача, когда запущена, чем кончилась."""

    running: dict = field(default_factory=dict)   # id → {id, description, started_at}
    finished: dict = field(default_factory=dict)  # id → {…, status, finished_at}
    _calls: dict = field(default_factory=dict)    # id вызова инструмента → описание

    def observe(self, entry: dict) -> bool:
        """Учесть запись файла. True — список задач поменялся."""
        changed = False
        moment = _parse_time(entry)
        content = (entry.get("message") or {}).get("content")
        if entry.get("type") == "assistant":
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._calls[str(block.get("id") or "")] = _describe_tool(block)
            return False
        if entry.get("type") not in ("user", "attachment"):
            return False
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            for text in _result_texts([block]):
                for match in TASK_STARTED.finditer(text):
                    task_id = next(group for group in match.groups() if group)
                    self.running[task_id] = {
                        "id": task_id,
                        "description": self._calls.get(str(block.get("tool_use_id") or ""), "") or task_id,
                        "started_at": moment,
                    }
                    changed = True
        started, finished = background_changes(entry)
        text = content if isinstance(content, str) else _text_of(content)
        if entry.get("type") == "attachment":
            text = str((entry.get("attachment") or {}).get("prompt") or "")
            finished |= {task_id for task_id, status in TASK_DONE.findall(text) if status in TASK_STATUS}
        statuses = dict(TASK_DONE.findall(text or ""))
        for task_id in finished:
            task = self.running.pop(task_id, None)
            if task is not None:
                task.update(status=statuses.get(task_id, "stopped"), finished_at=moment)
                self.finished[task_id] = task
                changed = True
        # закончившиеся — не больше десяти последних
        for old in list(self.finished)[:-10]:
            self.finished.pop(old)
        return changed

    def forget_older_than(self, cutoff: float) -> None:
        """Задачи, запущенные давно и без конца в файле, — скорее всего, умерли с прошлой
        сессией: при подключении их не считаем идущими."""
        for task_id, task in list(self.running.items()):
            if task["started_at"] and task["started_at"] < cutoff:
                self.running.pop(task_id)

    def frame(self) -> list:
        tasks = [dict(task, status="running") for task in self.running.values()]
        return tasks + list(self.finished.values())


def scan_background(file: Path | None, since: float, window: int = 4 << 20) -> Background:
    """Фоновые задачи из последних `window` байт файла — чтобы при подключении плагин знал
    и о задачах, запущенных до него (заказчик 24.09: агент с идущей задачей был «ждёт задачу»)."""
    tracked = Background()
    if file is None or not file.exists():
        return tracked
    with file.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - window))
        chunk = fh.read()
    lines = chunk.split(b"\n")
    if size > window:
        lines = lines[1:]  # первая строка обрезана посередине
    for raw in lines:
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(entry, dict):
            tracked.observe(entry)
    tracked.forget_older_than(since)
    return tracked


@dataclass
class Follower:
    """Слежение за файлом сессии: что дописано с прошлого раза (заказчик 23.09 — новые
    сообщения на телефоне появлялись, только если выйти из агента и зайти снова).

    Читает с запомненного места только целые строки: последняя может быть недописана."""

    file: Path | None
    offset: int = 0
    index: int = 0
    #: busy | ready — по последней записи, которая об этом говорит; None — пока не знаем
    turn: str | None = None
    #: фоновые задачи сессии: идущие и недавно закончившиеся
    tasks: Background = field(default_factory=Background)
    #: список задач поменялся с прошлого раза — приложению стоит прислать его заново
    tasks_changed: bool = False

    @property
    def background(self) -> dict:
        """Идущие фоновые задачи."""
        return self.tasks.running

    @property
    def state(self) -> str | None:
        """Что показать: ход кончился, но фоновая задача идёт — всё ещё «работает»."""
        if self.turn == "ready" and self.tasks.running:
            return "busy"
        return self.turn

    @classmethod
    def at_end(cls, file: Path | None) -> Follower:
        """Начать с конца файла: то, что уже в нём, приложение получает историей."""
        if file is None or not file.exists():
            return cls(file)
        return cls(file, file.stat().st_size, next_id(file))

    def poll(self) -> list[Message]:
        if self.file is None or not self.file.exists():
            return []
        size = self.file.stat().st_size
        if size < self.offset:  # файл пересоздали — начинаем заново с его конца
            self.offset, self.index = size, next_id(self.file)
            return []
        if size == self.offset:
            return []
        with self.file.open("rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read(size - self.offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        found: list[Message] = []
        for raw in chunk[: end + 1].split(b"\n")[:-1]:
            index = self.index
            self.index += 1
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            found.extend(messages_from(index, entry, live=True))
            self.turn = turn_state(entry) or self.turn
            if self.tasks.observe(entry):
                self.tasks_changed = True
        self.offset += end + 1
        return found
