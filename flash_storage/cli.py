"""FlashStorageCLI - SSH login to a Huawei flash storage device CLI and execute commands interactively.

Implemented with paramiko (pure-Python SSH library), compatible with both Windows
and Linux - no system `expect` command is required.

Each call to execute_commands establishes an independent SSH connection:
login -> run commands sequentially -> quit, then closes the connection.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import defaultdict
from typing import Dict, List, Tuple

import paramiko


# Device CLI prompts:
#   normal    admin:/>            engineer    engineer:/>
#   developer developer:/>        debug       admin:/diagnose>
#   minisystem Storage: minisystem>
_PROMPT_RE = re.compile(r"(?:[\w-]+:/>|/diagnose>|minisystem>)")
# Risk confirmation prompt (Huawei CLI's (y/n) question)
_CONFIRM_RE = re.compile(r"\(y/n\)")

_RECV_CHUNK = 65535


# ---------------------------------------------------------------------------
# FlashStorageCLI
# ---------------------------------------------------------------------------

class FlashStorageCLI:
    """Login to a Huawei flash storage device CLI over SSH (paramiko) and run batch commands."""

    def __init__(
        self,
        address: str,
        username: str,
        password: str,
        timeout: int = 60,
    ) -> None:
        self._address = address
        self._username = username
        self._password = password
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def execute_commands(
        self, commands: List[str], logfile: str | None = None
    ) -> str:
        """Execute multiple commands (one per line, sequentially).

        Args:
            commands: The list of commands to execute.
            logfile: Write the interaction log to this file (for debugging).

        Returns:
            The device output text.
        """
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                self._address,
                username=self._username,
                password=self._password,
                timeout=self._timeout,
                auth_timeout=self._timeout,
                look_for_keys=False,
                allow_agent=False,
            )
        except paramiko.AuthenticationException as ex:
            raise RuntimeError(f"SSH login failed (authentication error): {ex}") from ex
        except Exception as ex:
            raise RuntimeError(f"SSH connection failed: {ex}") from ex

        shell = client.invoke_shell(width=200, height=50)
        shell.settimeout(self._timeout)
        log = open(logfile, "w", encoding="utf-8") if logfile else None
        try:
            # Login banner + first prompt (kept as part of the output)
            output = self._read_until_prompt(shell, log)
            for cmd in commands:
                if log:
                    log.write(f">>> {cmd}\n")
                shell.send(cmd + "\r")
                output += self._read_until_prompt(shell, log)
            # Quit the device CLI
            self._quit(shell, log)
            return output
        finally:
            if log:
                log.close()
            client.close()

    # ------------------------------------------------------------------
    # Interaction primitives
    # ------------------------------------------------------------------

    def _read_until_prompt(self, shell, log=None) -> str:
        """Read device output until a CLI prompt appears (auto-accepting (y/n) risk prompts)."""
        buf = ""
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            try:
                data = shell.recv(_RECV_CHUNK).decode("utf-8", "replace")
            except (TimeoutError, OSError, paramiko.SSHException):
                break
            if not data:
                break
            buf += data
            if log:
                log.write(data)
            # Risk confirmation prompt: auto-send "y" and continue
            while _CONFIRM_RE.search(buf):
                buf = _CONFIRM_RE.sub("", buf, count=1)
                shell.send("y\r")
            if _PROMPT_RE.search(buf):
                break
        return buf

    def _quit(self, shell, log=None) -> None:
        """Quit the device CLI: send exit, handle (y/n) confirmations, until the connection closes."""
        try:
            for _ in range(5):
                shell.send("exit\r")
                try:
                    data = shell.recv(_RECV_CHUNK).decode("utf-8", "replace")
                except (TimeoutError, OSError, paramiko.SSHException):
                    break
                if not data:
                    break
                if log:
                    log.write(data)
                if _CONFIRM_RE.search(data):
                    shell.send("y\r")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Docs-based help (no device connection required)
# ---------------------------------------------------------------------------

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
# High-risk command list ({command: function}), packaged under config/
RISK_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "config", "high-risk-commands.json",
)


def slugify(name: str) -> str:
    """Lowercase, strip non-alphanumeric chars, join whitespace runs with '-'."""
    s = name.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s-]+", "-", s)
    return s.strip("-")


def _read_doc(rel_path: str, what: str) -> str:
    path = os.path.join(DOCS_DIR, rel_path)
    if not os.path.isfile(path):
        raise RuntimeError(
            f"{what} not found: {path} (run gen_help_docs.py to generate the docs)"
        )
    with open(path, encoding="utf-8") as f:
        return f.read()


def list_topics() -> str:
    """Return the content of docs/_topics.md."""
    return _read_doc("_topics.md", "Topics file")


def list_commands(group: str) -> str:
    """Return the command list of a command group (docs/<group>/_index.md)."""
    group_slug = slugify(group)
    try:
        return _read_doc(
            os.path.join(group_slug, "_index.md"),
            f"Index for group '{group}'",
        )
    except RuntimeError:
        available = sorted(
            d for d in os.listdir(DOCS_DIR)
            if os.path.isdir(os.path.join(DOCS_DIR, d))
        ) if os.path.isdir(DOCS_DIR) else []
        raise RuntimeError(
            f"Command group not found: '{group}' (slug '{group_slug}'). "
            f"Available groups: {', '.join(available) or 'none'}"
        ) from None


def show_command_help(command: str) -> str:
    """Search all topic dirs for the command help file and return its content."""
    command_slug = slugify(command)
    if os.path.isdir(DOCS_DIR):
        for d in sorted(os.listdir(DOCS_DIR)):
            dir_path = os.path.join(DOCS_DIR, d)
            if not os.path.isdir(dir_path):
                continue
            file_path = os.path.join(dir_path, f"{command_slug}.md")
            if os.path.isfile(file_path):
                with open(file_path, encoding="utf-8") as f:
                    return f.read()
    raise RuntimeError(
        f"Command not found: '{command}' (slug '{command_slug}'). "
        "Run gen_help_docs.py to generate the docs, or check the command name."
    )


# ---------------------------------------------------------------------------
# High-risk command check
# ---------------------------------------------------------------------------

def load_risk_commands() -> Dict[str, List[Tuple[str, str]]]:
    """Load the high-risk command list and index it by first word.

    The on-disk config keeps the flat {command: function} mapping (as generated
    by gen_help_docs.py); the first-word index narrows prefix matching to a
    small bucket per command instead of scanning all commands.
    """
    try:
        with open(RISK_CONFIG_PATH, encoding="utf-8") as f:
            risk: Dict[str, str] = json.load(f)
    except (OSError, ValueError):
        return {}
    index: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for cmd, function in risk.items():
        first = cmd.split(" ", 1)[0] if cmd else ""
        index[first].append((cmd, function))
    return index


def find_risk_commands(
    commands: List[str],
    risk_index: Dict[str, List[Tuple[str, str]]],
) -> List[Tuple[int, str, str, str]]:
    """Return (line_no, input, risk_command, function) for inputs matching a high-risk command.

    Matching is a word-boundary prefix test: an input matches when it equals a
    risk command or starts with it followed by a space, e.g. 'delete lun 1'
    matches 'delete lun', but 'delete lunx' does not. line_no is the 1-based
    index of the input command within `commands`.
    """
    hits: List[Tuple[int, str, str, str]] = []
    for line_no, raw in enumerate(commands, start=1):
        cmd = raw.strip()
        first = cmd.split(" ", 1)[0] if cmd else ""
        for risk_cmd, function in risk_index.get(first, []):
            if cmd == risk_cmd or cmd.startswith(risk_cmd + " "):
                hits.append((line_no, cmd, risk_cmd, function))
                break
    return hits


# ---------------------------------------------------------------------------
# Command validation (--dry-run)
# ---------------------------------------------------------------------------

def _format_variants(help_text: str) -> List[str]:
    """Return the '##### Format' lines of a command help text (one per variant)."""
    variants: List[str] = []
    in_format = False
    for line in help_text.split("\n"):
        s = line.strip()
        if s == "##### Format":
            in_format = True
            continue
        if in_format:
            if s.startswith("##### "):
                break
            if s:
                variants.append(s)
    return variants


def _parse_variant(variant: str) -> Tuple[set, List[set]]:
    """Parse a Format variant into (required params, or-groups).

    Tokens: 'name=?' parameters; '[...]' optional groups; '{...}' or-groups
    (at least one member is required unless the group sits inside brackets).
    The markdown-escaped brackets ('\\[', '\\]', '\\{', '\\}') are unescaped
    before scanning.
    """
    s = variant.replace(r"\[", "[").replace(r"\]", "]")
    s = s.replace(r"\{", "{").replace(r"\}", "}")
    required: set = set()
    groups: List[set] = []
    depth = 0
    group_members: set | None = None
    group_depth = 0
    for m in re.finditer(r"\[|\]|\{|\}|(\w+)=\?", s):
        tok = m.group(0)
        if tok == "[":
            depth += 1
        elif tok == "]":
            depth = max(0, depth - 1)
        elif tok == "{":
            group_members = set()
            group_depth = depth
        elif tok == "}":
            if group_members is not None:
                if group_depth == 0:
                    groups.append(group_members)
                group_members = None
        else:
            pname = m.group(1)
            if group_members is not None:
                group_members.add(pname)
            elif depth == 0:
                required.add(pname)
    return required, groups


def validate_command(command: str) -> List[str]:
    """Validate a command against its bundled help: existence + required parameters.

    The command name is located by progressively stripping trailing tokens
    (parameters), e.g. 'delete lun 1' resolves to the help of 'delete lun'.
    Returns a list of problems (empty list means the command is valid).
    """
    tokens = command.split()
    help_text = None
    for n in range(len(tokens), 0, -1):
        try:
            help_text = show_command_help(" ".join(tokens[:n]))
            break
        except RuntimeError:
            continue
    if help_text is None:
        return [f"Command not found: '{command}'. Use --show-command-help to check the command name."]
    variants = _format_variants(help_text)
    if not variants:
        return []  # no Format section, nothing more to validate
    input_params = set(re.findall(r"(\w+)=", command))
    for variant in variants:
        required, groups = _parse_variant(variant)
        if required <= input_params and all(g & input_params for g in groups):
            return []
    # No variant matches: report what the first variant still requires
    required, groups = _parse_variant(variants[0])
    missing = sorted(required - input_params)
    for g in groups:
        if not (g & input_params):
            missing.append("one of: " + ", ".join(sorted(g)))
    return [f"missing required parameter(s): {', '.join(missing)}"]


def validate_batch(
    commands: List[str],
) -> Tuple[List[Tuple[int, str, str, str, List[str]]], bool]:
    """Validate a batch of commands; returns (rows, has_failures).

    Each row is (line_no, command, valid, risk, problems) where valid is
    'OK'/'FAIL' and risk is 'LOW'/'HIGH' (HIGH = the command matches the
    high-risk command list).
    """
    risk_hits = {
        cmd for _, cmd, _, _ in find_risk_commands(commands, load_risk_commands())
    }
    rows: List[Tuple[int, str, str, str, List[str]]] = []
    has_fail = False
    for i, raw in enumerate(commands, start=1):
        cmd = raw.strip()
        problems = validate_command(cmd)
        if problems:
            has_fail = True
        risk = "HIGH" if cmd in risk_hits else "LOW"
        rows.append((i, cmd, "OK" if not problems else "FAIL", risk, problems))
    return rows, has_fail


def print_validation(rows: List[Tuple[int, str, str, str, List[str]]]) -> None:
    """Print the validation table, aligned with ASCII spaces."""
    idx_w = max(len(f"[{n}]") for n, *_ in rows)
    valid_w = 6
    risk_w = 6
    cmd_w = max(len(cmd) for _, cmd, *_ in rows)
    print(f"{'[#]':<{idx_w}}  {'valid':<{valid_w}}  {'risk':<{risk_w}}  command")
    print(f"{'-' * idx_w}  {'-' * valid_w}  {'-' * risk_w}  {'-' * cmd_w}")
    for n, cmd, valid, risk, problems in rows:
        print(
            f"[{n}]".ljust(idx_w) + "  "
            + valid.ljust(valid_w) + "  "
            + risk.ljust(risk_w) + "  " + cmd
        )
        for p in problems:
            print(" " * (idx_w + 2 + valid_w + 2 + risk_w + 2) + p)


def _parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SSH login to a Huawei flash storage device CLI and execute commands",
    )
    parser.add_argument(
        "--address",
        default=os.environ.get("STORAGE_ADDRESS", ""),
        help="device IP address (env STORAGE_ADDRESS)",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("STORAGE_USERNAME", ""),
        help="login username (env STORAGE_USERNAME)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("STORAGE_PASSWORD", ""),
        help="login password (env STORAGE_PASSWORD; prefer passing it via the environment)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("STORAGE_TIMEOUT", "60")),
        help="command timeout in seconds, default 60 (env STORAGE_TIMEOUT)",
    )
    parser.add_argument(
        "--logfile",
        default=None,
        help="write the interaction log to this file (for debugging)",
    )
    parser.add_argument(
        "--accept-risk",
        action="store_true",
        default=os.environ.get("STORAGE_ACCEPT_RISK", "").lower() in ("1", "true", "yes"),
        help="accept high-risk commands: print warnings and continue instead of rejecting (env STORAGE_ACCEPT_RISK=true)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate commands (existence, required parameters, risk) without executing them",
    )
    parser.add_argument(
        "--list-topics",
        action="store_true",
        help="list all command topics (command help, no device connection needed)",
    )
    parser.add_argument(
        "--list-commands",
        metavar="GROUP",
        help="list the commands of a command group, e.g. base (command help, no device connection needed)",
    )
    parser.add_argument(
        "--show-command-help",
        metavar="COMMAND",
        help="show the command help (Format, Parameters, Example) of a command, e.g. \"create lun\" (no device connection needed)",
    )
    parser.add_argument(
        "commands",
        nargs="?",
        default="",
        help="commands to execute (multiple commands separated by \\n)",
    )
    ns = parser.parse_args(argv)

    help_mode = ns.list_topics or ns.list_commands or ns.show_command_help
    if not (help_mode or ns.dry_run):
        missing = [k for k in ("address", "username", "password") if not getattr(ns, k)]
        if missing:
            parser.error(f"missing required arguments: {', '.join(missing)} (can be set via environment variables)")

    return ns


def main(argv: List[str] | None = None) -> None:
    args = _parse_args(argv)

    # Help mode: read docs locally, no device connection required
    try:
        if args.show_command_help:
            print(show_command_help(args.show_command_help))
            return
        if args.list_commands:
            print(list_commands(args.list_commands))
            return
        if args.list_topics:
            print(list_topics())
            return
    except RuntimeError as ex:
        print(f"Error: {ex}")
        return

    raw_commands = args.commands.split(r"\n") if args.commands else []
    commands = [c for c in raw_commands if c.strip()]

    if not commands:
        print("Please provide commands to execute.")
        return

    # Validate the whole batch: existence, required parameters, risk level
    rows, has_fail = validate_batch(commands)
    high_risk = any(risk == "HIGH" for _, _, _, risk, _ in rows)

    if args.dry_run:
        print("Dry-run validation (no command will be executed):")
        print_validation(rows)
        if high_risk:
            print(
                "\nContains HIGH-risk command(s). "
                "Specify --accept-risk to accept the risk and proceed."
            )
        return

    # Normal execution: the same dry-run must pass first
    print("Dry-run validation:")
    print_validation(rows)
    if has_fail:
        print("\nValidation failed. Fix the commands and try again.")
        return
    if high_risk and not args.accept_risk:
        print(
            "\nContains HIGH-risk command(s). "
            "Specify --accept-risk to accept the risk and proceed."
        )
        return

    cli = FlashStorageCLI(
        address=args.address,
        username=args.username,
        password=args.password,
        timeout=args.timeout,
    )

    try:
        results = cli.execute_commands(commands, args.logfile)
    except RuntimeError as ex:
        print(f"Error: {ex}")
        return
    print(results)


if __name__ == "__main__":
    main()
