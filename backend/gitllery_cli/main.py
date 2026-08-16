from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid
from typing import Any

from gitllery_format import GitlleryFormatError, SegmentRepository

from .client import APIError, GitlleryClient, login, token_from_inputs
from .config import load_config, profile as select_profile, save_config
from .gll import GLLParseError, parse_gll

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_CONFLICT = 4
EXIT_UNAVAILABLE = 5
EXIT_VERIFY = 6
EXIT_NOT_FOUND = 7


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gitllery", description="Gitllery v1 command client")
    parser.add_argument("--profile")
    parser.add_argument("--repo")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true")
    mode.add_argument("--remote", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--token-stdin", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_set = config_sub.add_parser("set")
    config_set.add_argument("key", choices=["url", "repository", "current-profile"])
    config_set.add_argument("value")
    config_get = config_sub.add_parser("get")
    config_get.add_argument("key", choices=["url", "repository", "current-profile"])

    auth = sub.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_login = auth_sub.add_parser("login")
    auth_login.add_argument("--username", required=True)
    auth_sub.add_parser("logout")
    auth_sub.add_parser("status")

    init = sub.add_parser("init")
    init.add_argument("--repository-id", required=True)
    init.add_argument("--source")
    init.add_argument("--creator-dir")
    sub.add_parser("status")
    log_parser = sub.add_parser("log")
    log_parser.add_argument("--limit", type=int, default=50)
    show = sub.add_parser("show")
    show.add_argument("commit_id")
    diff = sub.add_parser("diff")
    diff.add_argument("from_commit")
    diff.add_argument("to_commit")
    verify = sub.add_parser("verify")
    verify.add_argument("--deep", action="store_true")
    verify.add_argument("--remote", action="store_true", dest="verify_remote")
    export = sub.add_parser("export")
    export.add_argument("--output", required=True)

    commit = sub.add_parser("commit")
    commit.add_argument("--file")
    commit.add_argument("--message", "-m")
    commit.add_argument("--reason")
    commit.add_argument("--expect-head")
    commit.add_argument("--idempotency-key")
    commit.add_argument("--dry-run", action="store_true")
    commit.add_argument("--wait", action="store_true")
    commit.add_argument("--force-head", action="store_true")
    commit_sub = commit.add_subparsers(dest="subject")
    work = commit_sub.add_parser("work")
    work.add_argument("action", choices=["trash", "restore", "favorite", "tag-add", "tag-remove"])
    work.add_argument("work_ids", nargs="+")
    work.add_argument("--set", choices=["on", "off"])
    work.add_argument("--tag")

    revert = sub.add_parser("revert")
    revert.add_argument("commit_id")
    revert.add_argument("--wait", action="store_true")
    sync = sub.add_parser("sync")
    sync.add_argument("--wait", action="store_true")
    build = sub.add_parser("build")
    build.add_argument("--wait", action="store_true")
    sub.add_parser("push")
    sub.add_parser("pull")
    task = sub.add_parser("task")
    task.add_argument("task_id")
    restore = sub.add_parser("restore")
    restore.add_argument("action", choices=["plan", "stage", "status", "promote"])
    restore.add_argument("value", nargs="?")
    return parser


def _emit(value: Any, *, json_output: bool) -> None:
    if json_output or isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=None if json_output else 2, sort_keys=True, default=str))
    else:
        print(value)


def _local_repo(args: argparse.Namespace, selected: dict[str, Any]) -> SegmentRepository:
    root = args.repo or selected.get("repository") or "."
    path = Path(root)
    if path.name == ".gitllery" and (path / "manifest.json").is_file():
        return SegmentRepository(path)
    return SegmentRepository.discover(path)


def _diff(repo: SegmentRepository, older: str, newer: str) -> dict[str, Any]:
    before = repo.find_commit(older)
    after = repo.find_commit(newer)
    if before is None or after is None:
        raise GitlleryFormatError("one or both commits were not found")
    def changes(commit: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        return {(str(item.get("subject_type")), str(item.get("subject_id"))): item for item in commit.get("changes") or []}
    left, right = changes(before), changes(after)
    keys = sorted(set(left) | set(right))
    return {
        "from": older,
        "to": newer,
        "changes": [
            {"subject_type": key[0], "subject_id": key[1], "from": left.get(key), "to": right.get(key)}
            for key in keys if left.get(key) != right.get(key)
        ],
    }


def _diff_payloads(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    def changes(commit: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (str(item.get("subject_type")), str(item.get("subject_id"))): item
            for item in commit.get("changes") or []
        }

    left, right = changes(before), changes(after)
    keys = sorted(set(left) | set(right))
    return {
        "from": str(before.get("id") or before.get("commit_id")),
        "to": str(after.get("id") or after.get("commit_id")),
        "changes": [
            {
                "subject_type": key[0],
                "subject_id": key[1],
                "from": left.get(key),
                "to": right.get(key),
            }
            for key in keys
            if left.get(key) != right.get(key)
        ],
    }


def _command_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.file:
        document = parse_gll(Path(args.file).read_bytes())
        operations = list(document.operations)
        message = document.message
        reason = document.reason
        expected = document.expected_parent_commit_id
    else:
        if args.subject != "work" or not args.message:
            raise GLLParseError("commit requires --file or 'work ACTION IDS... -m MESSAGE'")
        if len(set(args.work_ids)) > 25:
            raise GLLParseError("a command may affect at most 25 unique works")
        operations = []
        for work_id in dict.fromkeys(args.work_ids):
            operation: dict[str, Any] = {"work_id": work_id, "action": args.action}
            if args.action == "favorite":
                if args.set is None:
                    raise GLLParseError("favorite requires --set on|off")
                operation["value"] = args.set == "on"
            if args.action in {"tag-add", "tag-remove"}:
                if not args.tag:
                    raise GLLParseError(f"{args.action} requires --tag")
                operation["tag"] = args.tag
            operations.append(operation)
        message, reason, expected = args.message, args.reason, args.expect_head
    return {
        "version": 1,
        "message": message,
        "reason": reason,
        "expected_parent_commit_id": None if args.force_head else expected,
        "idempotency_key": args.idempotency_key or str(uuid.uuid4()),
        "operations": operations,
    }


def _remote(args: argparse.Namespace, selected: dict[str, Any]) -> GitlleryClient:
    url = selected.get("url")
    if not url:
        raise GLLParseError("remote URL is not configured")
    return GitlleryClient(url, token_from_inputs(selected, token_stdin=args.token_stdin), args.timeout)


def run(args: argparse.Namespace) -> Any:
    config = load_config()
    profile_name, selected = select_profile(config, args.profile)
    if args.command == "config":
        if args.config_command == "get":
            key = "current_profile" if args.key == "current-profile" else args.key
            return config.get(key) if key == "current_profile" else selected.get(key)
        if args.key == "current-profile":
            config["current_profile"] = args.value
        else:
            config.setdefault("profiles", {}).setdefault(profile_name, {})[args.key] = args.value
        save_config(config)
        return {"updated": args.key, "profile": profile_name}
    if args.command == "auth":
        if args.auth_command == "login":
            if not selected.get("url"):
                raise GLLParseError("configure the remote URL before login")
            selected["token"] = login(selected["url"], args.username, args.timeout)
            config.setdefault("profiles", {})[profile_name] = selected
            save_config(config)
            return {"authenticated": True, "profile": profile_name}
        if args.auth_command == "logout":
            selected.pop("token", None)
            config.setdefault("profiles", {})[profile_name] = selected
            save_config(config)
            return {"authenticated": False, "profile": profile_name}
        return {"authenticated": bool(token_from_inputs(selected, token_stdin=False)), "profile": profile_name}

    if args.command == "init":
        if args.remote:
            raise GLLParseError("init is a local Gitllery v1 repository command")
        requested = Path(args.repo or selected.get("repository") or ".").resolve()
        root = requested if requested.name == ".gitllery" else requested / ".gitllery"
        repository = SegmentRepository(root)
        repository.initialise(
            repository_id=args.repository_id,
            source=args.source,
            creator_dir=args.creator_dir,
        )
        return repository.read_manifest()

    local_commands = {"status", "log", "show", "diff", "verify", "export"}
    use_local = args.command in local_commands and (
        args.local
        or (
            not args.remote
            and not getattr(args, "verify_remote", False)
        )
    )
    if use_local:
        repo = _local_repo(args, selected)
        if args.command == "status":
            return repo.read_manifest()
        if args.command == "log":
            return list(repo.iter_commits(newest_first=True))[: max(1, args.limit)]
        if args.command == "show":
            result = repo.find_commit(args.commit_id)
            if result is None:
                raise GitlleryFormatError(f"commit not found: {args.commit_id}")
            return result
        if args.command == "diff":
            return _diff(repo, args.from_commit, args.to_commit)
        if args.command == "verify":
            return repo.verify(deep=args.deep).as_dict()
        if args.command == "export":
            output = Path(args.output)
            count = 0
            with output.open("w", encoding="utf-8") as handle:
                handle.write('{"manifest":')
                json.dump(repo.read_manifest(), handle, ensure_ascii=False, sort_keys=True)
                handle.write(',"commits":[')
                first = True
                for commit in repo.iter_commits():
                    if not first:
                        handle.write(",")
                    json.dump(commit, handle, ensure_ascii=False, sort_keys=True)
                    first = False
                    count += 1
                handle.write("]}\n")
            return {"output": args.output, "commits": count}

    client = _remote(args, selected)
    repository = args.repo or selected.get("repository")
    if args.command == "status":
        path = f"/api/v1/curation/repositories/{repository}/gitllery/status" if repository else "/api/v1/curation/gitllery/status"
        return client.request("GET", path)
    if args.command == "log":
        if not repository:
            raise GLLParseError("--repo is required for remote log")
        return client.request("GET", f"/api/v1/curation/repositories/{repository}/gitllery/log", params={"limit": args.limit})
    if args.command == "show":
        return client.request("GET", f"/api/v1/curation/commits/{args.commit_id}")
    if args.command == "diff":
        before = client.request(
            "GET", f"/api/v1/curation/commits/{args.from_commit}"
        )
        after = client.request(
            "GET", f"/api/v1/curation/commits/{args.to_commit}"
        )
        return _diff_payloads(before, after)
    if args.command == "commit":
        payload = _command_payload(args)
        suffix = "preview" if args.dry_run else "execute"
        result = client.request("POST", f"/api/v1/curation/gitllery/commands/{suffix}", json=payload)
        if args.wait and result.get("task_id"):
            return client.request("GET", f"/api/v1/tasks/{result['task_id']}")
        return result
    if args.command == "revert":
        return client.request("POST", f"/api/v1/curation/commits/{args.commit_id}/revert")
    if args.command == "sync":
        return client.request("POST", "/api/v1/curation/gitllery/reconcile", params={"repository_id": repository} if repository else None)
    if args.command == "build":
        return client.request("POST", "/api/v1/curation/gitllery/build", json={"repository_id": repository})
    if args.command in {"push", "pull"}:
        if not repository:
            raise GLLParseError("--repo is required for remote push/pull")
        return client.request(
            "POST",
            f"/api/v1/curation/gitllery/{args.command}",
            json={"repository_id": repository, "product_version": "v1"},
        )
    if args.command == "verify":
        return client.request("POST", "/api/v1/curation/gitllery/verify", json={"repository_id": repository, "deep": args.deep})
    if args.command == "task":
        return client.request("GET", f"/api/v1/tasks/{args.task_id}")
    if args.command == "restore":
        value = args.value or ""
        return client.request("POST", f"/api/v1/curation/gitllery/restore/{args.action}", json={"value": value, "repository_id": repository})
    raise GLLParseError(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        result = run(args)
        _emit(result, json_output=args.json_output)
        if args.command == "verify" and isinstance(result, dict) and result.get("ok") is False:
            return EXIT_VERIFY
        return EXIT_OK
    except (GLLParseError, ValueError) as exc:
        print(f"gitllery: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except GitlleryFormatError as exc:
        print(f"gitllery: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except APIError as exc:
        print(f"gitllery: {exc.detail}", file=sys.stderr)
        if exc.status_code in {401, 403}:
            return EXIT_AUTH
        if exc.status_code in {409, 412}:
            return EXIT_CONFLICT
        if exc.status_code >= 500:
            return EXIT_UNAVAILABLE
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
