"""Parser for the deliberately small Gitllery command language."""

from __future__ import annotations

from dataclasses import dataclass
import shlex
from typing import Any

MAX_FILE_BYTES = 1024 * 1024
MAX_WORKS = 25


class GLLParseError(ValueError):
    pass


@dataclass(frozen=True)
class GLLDocument:
    message: str
    reason: str | None
    expected_parent_commit_id: str | None
    operations: tuple[dict[str, Any], ...]


def _tokens(line: str, number: int) -> list[str]:
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        return list(lexer)
    except ValueError as exc:
        raise GLLParseError(f"line {number}: {exc}") from exc


def parse_gll(content: str | bytes) -> GLLDocument:
    if isinstance(content, bytes):
        if len(content) > MAX_FILE_BYTES:
            raise GLLParseError("command file exceeds 1 MiB")
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GLLParseError("command file must be UTF-8") from exc
    elif len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise GLLParseError("command file exceeds 1 MiB")

    message: str | None = None
    reason: str | None = None
    expected: str | None = None
    version_seen = False
    operations: list[dict[str, Any]] = []
    subjects: dict[str, set[str]] = {}

    for line_number, line in enumerate(content.splitlines(), 1):
        tokens = _tokens(line, line_number)
        if not tokens:
            continue
        command = tokens[0]
        if command == "version" and tokens == ["version", "1"]:
            if version_seen:
                raise GLLParseError(f"line {line_number}: duplicate version")
            version_seen = True
            continue
        if command in {"message", "reason", "expect-head"}:
            if len(tokens) != 2:
                raise GLLParseError(f"line {line_number}: {command} expects one value")
            if command == "message":
                if message is not None:
                    raise GLLParseError(f"line {line_number}: duplicate message")
                message = tokens[1]
            elif command == "reason":
                if reason is not None:
                    raise GLLParseError(f"line {line_number}: duplicate reason")
                reason = tokens[1]
            else:
                if expected is not None:
                    raise GLLParseError(f"line {line_number}: duplicate expect-head")
                expected = tokens[1]
            continue
        if len(tokens) < 3 or tokens[0] != "work":
            raise GLLParseError(f"line {line_number}: unsupported statement")
        work_id, action = tokens[1], tokens[2]
        operation: dict[str, Any] = {"work_id": work_id, "action": action}
        if action == "trash":
            if len(tokens) != 3:
                raise GLLParseError(f"line {line_number}: trash accepts no extra arguments")
        elif action == "restore":
            if len(tokens) not in {3, 5} or (len(tokens) == 5 and tokens[3] != "reason"):
                raise GLLParseError(f"line {line_number}: expected restore [reason VALUE]")
            if len(tokens) == 5:
                operation["reason"] = tokens[4]
        elif action == "favorite":
            if len(tokens) != 4 or tokens[3] not in {"on", "off"}:
                raise GLLParseError(f"line {line_number}: expected favorite on|off")
            operation["value"] = tokens[3] == "on"
        elif action == "tag":
            if len(tokens) != 5 or tokens[3] not in {"add", "remove"}:
                raise GLLParseError(f"line {line_number}: expected tag add|remove NAME")
            operation["action"] = f"tag-{tokens[3]}"
            operation["tag"] = tokens[4]
        else:
            raise GLLParseError(f"line {line_number}: unsupported work action {action}")
        seen = subjects.setdefault(work_id, set())
        conflict_key = (
            f"tag-add:{operation['tag'].casefold()}"
            if operation["action"] == "tag-add"
            else f"tag-remove:{operation['tag'].casefold()}"
            if operation["action"] == "tag-remove"
            else operation["action"]
        )
        opposite = (
            f"tag-remove:{operation['tag'].casefold()}"
            if operation["action"] == "tag-add"
            else f"tag-add:{operation['tag'].casefold()}"
            if operation["action"] == "tag-remove"
            else "restore"
            if operation["action"] == "trash"
            else "trash"
            if operation["action"] == "restore"
            else "favorite"
        )
        if conflict_key in seen or opposite in seen:
            raise GLLParseError(f"line {line_number}: contradictory operation for work {work_id}")
        seen.add(conflict_key)
        operations.append(operation)

    if not version_seen:
        raise GLLParseError("missing 'version 1' header")
    if message is None or not message.strip():
        raise GLLParseError("message is required")
    if not operations:
        raise GLLParseError("at least one work operation is required")
    if len(subjects) > MAX_WORKS:
        raise GLLParseError("a command may affect at most 25 unique works")
    return GLLDocument(message, reason, expected, tuple(operations))
