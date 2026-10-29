"""Server-side account administration for the hosted profile.

The first administrator can only be created here, on the server, because the
hosted API has no bootstrap or self-registration route. Passwords are read from
an interactive prompt or, for automation, a single line on stdin; they are
never accepted as command-line arguments, which leak into shell history and
process listings.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from interfaces.services.account_administration import AccountAdministrationError

SERVER_CLI_ACTOR = "server-cli"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("accounts", help="Manage hosted service accounts on the server")
    commands = parser.add_subparsers(dest="accounts_command", required=True)

    create_admin = commands.add_parser("create-admin", help="Create an administrator account")
    create_admin.add_argument("username")
    _password_source(create_admin)
    create_admin.set_defaults(handler=create_admin_account)

    create_user = commands.add_parser("create-user", help="Create a regular account")
    create_user.add_argument("username")
    _password_source(create_user, allow_generated=True)
    create_user.set_defaults(handler=create_user_account)

    list_parser = commands.add_parser("list", help="List accounts")
    list_parser.set_defaults(handler=list_accounts)

    disable = commands.add_parser("disable", help="Disable an account and end its sessions")
    disable.add_argument("username")
    disable.set_defaults(handler=disable_account)

    enable = commands.add_parser("enable", help="Enable a disabled account")
    enable.add_argument("username")
    enable.set_defaults(handler=enable_account)

    reset = commands.add_parser("reset-password", help="Set a new password and end the account's sessions")
    reset.add_argument("username")
    _password_source(reset, allow_generated=True)
    reset.set_defaults(handler=reset_account_password)


def _password_source(parser: argparse.ArgumentParser, *, allow_generated: bool = False) -> None:
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from the first line of stdin instead of prompting",
    )
    if allow_generated:
        parser.add_argument(
            "--generate-password",
            action="store_true",
            help="Generate a one-time password and print it once",
        )


def create_admin_account(args: argparse.Namespace) -> int:
    return _run(lambda runtime: _create(runtime, args, role="admin"))


def create_user_account(args: argparse.Namespace) -> int:
    return _run(lambda runtime: _create(runtime, args, role="user"))


def list_accounts(_args: argparse.Namespace) -> int:
    return _run(lambda runtime: {"accounts": [item.to_dict() for item in runtime.accounts.list_accounts()]})


def disable_account(args: argparse.Namespace) -> int:
    return _run(lambda runtime: _set_enabled(runtime, args.username, enabled=False))


def enable_account(args: argparse.Namespace) -> int:
    return _run(lambda runtime: _set_enabled(runtime, args.username, enabled=True))


def reset_account_password(args: argparse.Namespace) -> int:
    def call(runtime) -> dict[str, Any]:
        account = _find(runtime, args.username)
        password = None if args.generate_password else _read_password(args)
        updated, generated = runtime.accounts.reset_password(user_id=account.userId, password=password)
        return _with_generated(updated, generated)

    return _run(call)


def _create(runtime, args: argparse.Namespace, *, role: str) -> dict[str, Any]:
    generate = bool(getattr(args, "generate_password", False))
    password = None if generate else _read_password(args)
    account, generated = runtime.accounts.create_account(
        username=args.username, role=role, password=password
    )
    return _with_generated(account, generated)


def _set_enabled(runtime, username: str, *, enabled: bool) -> dict[str, Any]:
    account = _find(runtime, username)
    # The server operator is not a signed-in account, so the self-disable
    # rule does not apply; the last-administrator rule still does.
    updated = runtime.accounts.set_enabled(
        actor_user_id=SERVER_CLI_ACTOR, user_id=account.userId, enabled=enabled
    )
    return {"account": updated.to_dict()}


def _find(runtime, username: str):
    wanted = username.strip().lower()
    for account in runtime.accounts.list_accounts():
        if account.username.lower() == wanted:
            return account
    raise LookupError(f"account {username!r} was not found")


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        line = sys.stdin.readline()
        password = line.rstrip("\r\n")
        if not password:
            raise ValueError("no password was provided on stdin")
        return password
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise ValueError("passwords do not match")
    return first


def _with_generated(account, generated: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"account": account.to_dict()}
    if generated is not None:
        payload["initialPassword"] = generated
    return payload


def _run(call) -> int:
    # Imported lazily so `news --help` does not need the hosted configuration.
    from interfaces.composition.hosted import build_hosted_account_runtime
    from interfaces.services.deployment_profile import DeploymentConfigurationError

    try:
        runtime = build_hosted_account_runtime()
        payload = call(runtime)
    except DeploymentConfigurationError as exc:
        return _fail("deployment_configuration_invalid", str(exc))
    except AccountAdministrationError as exc:
        return _fail(exc.code, str(exc))
    except LookupError as exc:
        return _fail("account_not_found", str(exc))
    except ValueError as exc:
        return _fail(getattr(exc, "code", "account_invalid_request"), str(exc))
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _fail(code: str, message: str) -> int:
    print(json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False), file=sys.stderr)
    return 1


__all__ = ["register"]
