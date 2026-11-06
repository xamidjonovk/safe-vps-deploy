# /// script
# requires-python = ">=3.11"
# dependencies = ["paramiko>=3.5,<5", "python-dotenv>=1,<2"]
# ///
"""Read SSH credentials privately, inspect a host, optionally bootstrap isolated access."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit

import paramiko
from dotenv import dotenv_values


class BootstrapError(Exception):
    pass


def target(values):
    raw = (values.get("SERVER") or "").strip()
    if not raw:
        raise BootstrapError("SERVER is missing")
    url = urlsplit(raw if "://" in raw else "ssh://" + raw)
    if url.scheme != "ssh" or not url.hostname or url.password or url.path not in ("", "/") or url.query or url.fragment:
        raise BootstrapError("SERVER must be host, user@host or ssh://user@host:port; no password in URL")
    user = values.get("SSH_USER") or url.username or "root"
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise BootstrapError("Invalid SSH user")
    try:
        port = int(values.get("SSH_PORT") or url.port or 22)
    except ValueError:
        raise BootstrapError("Invalid SSH port") from None
    if not 1 <= port <= 65535:
        raise BootstrapError("Invalid SSH port")
    return url.hostname, port, user


def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class HostPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, path, expected, trust_new):
        self.path, self.expected, self.trust_new = path, expected, trust_new

    def missing_host_key(self, client, hostname, key):
        actual = fingerprint(key)
        if self.expected and actual != self.expected:
            raise BootstrapError("Host fingerprint does not match SSH_HOST_FINGERPRINT")
        if not self.expected and not self.trust_new:
            raise BootstrapError(f"Unknown host key: {actual}. Verify it independently or explicitly use --trust-new-host (TOFU).")
        client.get_host_keys().add(hostname, key.get_name(), key)
        client.save_host_keys(str(self.path))
        os.chmod(self.path, 0o600)
        print(f"Pinned host key: {actual}")


def connect(host, port, user, known, expected, trust_new, *, password=None, key=None):
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if known.exists():
        client.load_host_keys(str(known))
    client.set_missing_host_key_policy(HostPolicy(known, expected, trust_new))
    try:
        client.connect(hostname=host, port=port, username=user, password=password,
                       key_filename=str(key) if key else None, look_for_keys=False,
                       allow_agent=False, timeout=15, auth_timeout=15, banner_timeout=15)
        remote_key = client.get_transport().get_remote_server_key()
        if expected and fingerprint(remote_key) != expected:
            raise BootstrapError("Known host does not match requested fingerprint")
        # Also pin keys accepted from the user/system known_hosts into the dedicated
        # file printed for subsequent OpenSSH invocations.
        token = host if port == 22 else f"[{host}]:{port}"
        client.get_host_keys().add(token, remote_key.get_name(), remote_key)
        client.save_host_keys(str(known))
        os.chmod(known, 0o600)
        client.get_transport().set_keepalive(20)
        return client
    except Exception:
        client.close()
        raise


def run(client, command, *, stdin_text=None, timeout=90):
    """Use SSHClient file handles and drain both streams without an early stdin EOF."""
    import queue
    import threading
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    channel = stdout.channel
    if stdin_text:
        stdin.write(stdin_text)
        stdin.flush()
    events = queue.Queue()
    def drain(stream):
        try:
            while True:
                part = stream.read(32768)
                if not part:
                    break
                events.put(part)
        except Exception as exc:
            events.put(exc)
        finally:
            events.put(None)
    for stream in (stdout, stderr):
        threading.Thread(target=drain, args=(stream,), daemon=True).start()
    chunks, size, finished, deadline = [], 0, 0, time.monotonic() + timeout
    try:
        while finished < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BootstrapError("Remote command timed out; inspect partial state before retrying")
            try:
                part = events.get(timeout=remaining)
            except queue.Empty:
                raise BootstrapError("Remote output timed out") from None
            if part is None:
                finished += 1
                continue
            if isinstance(part, Exception):
                raise BootstrapError("SSH output stream failed")
            size += len(part)
            if size > 512_000:
                raise BootstrapError("Remote output limit exceeded")
            chunks.append(part)
        output = b"".join(chunks).decode("utf-8", "replace")
        if channel.recv_exit_status() != 0:
            raise BootstrapError("Remote operation failed:\n" + output)
        return output
    finally:
        stdin.close()
        stdout.close()
        stderr.close()
        channel.close()


def ensure_key(path, project):
    public = Path(str(path) + ".pub")
    if path.exists():
        if not public.exists():
            raise BootstrapError("Existing private key has no .pub file; recover it manually without overwriting")
    else:
        if public.exists():
            raise BootstrapError("Public key exists without private key; refusing to overwrite")
        exe = shutil.which("ssh-keygen")
        if not exe:
            raise BootstrapError("ssh-keygen is not installed")
        result = subprocess.run([exe, "-t", "ed25519", "-N", "", "-C", f"server-deploy-{project}", "-f", str(path)], capture_output=True)
        if result.returncode:
            raise BootstrapError("ssh-keygen failed; existing key files were not replaced")
    os.chmod(path, 0o600)
    key = public.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?", key):
        raise BootstrapError("Expected a single Ed25519 public key")
    return key


INVENTORY = 'set -eu; printf \'OS: \'; . /etc/os-release; printf \'%s\\n\' "$PRETTY_NAME"; id; free -m; df -h /; ss -ltn; if command -v docker >/dev/null; then docker compose version; printf \'BEGIN_CONTAINER_STATE\\n\'; docker ps --format \'{{.Names}} {{.State}}\' | sort; printf \'END_CONTAINER_STATE\\n\'; else printf \'Docker missing\\n\'; fi'


def native_preflight(host, port, user, known, key, expected=None):
    """Use the platform SSH client for routine project access after bootstrap."""
    if not known.exists():
        return False
    token = host if port == 22 else f"[{host}]:{port}"
    saved = paramiko.HostKeys(str(known)).lookup(token)
    if not saved:
        return False
    if expected and not any(fingerprint(k) == expected for k in saved.values()):
        raise BootstrapError("Pinned project host does not match requested fingerprint")
    exe = shutil.which("ssh")
    if not exe:
        raise BootstrapError("OpenSSH client is required for project access")
    result = subprocess.run([exe, "-i", str(key), "-p", str(port),
        "-o", f'UserKnownHostsFile="{known.as_posix()}"',
        "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=20", f"{user}@{host}",
        "sudo", "-n", "sdp", "preflight"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=90)
    if result.returncode == 255 and "Permission denied" in result.stderr:
        return False
    if result.returncode:
        raise BootstrapError("Project SSH/preflight failed; inspect the pinned host, key and broker. No password fallback was attempted.")
    print(result.stdout)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--apply", action="store_true", help="Install broker and isolated project access after inspection")
    parser.add_argument("--trust-new-host", action="store_true", help="Explicit first-use trust; never accepts changed keys")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", args.project):
        raise BootstrapError("Project must match [a-z][a-z0-9-]{1,30}")
    if not args.env_file.is_file():
        raise BootstrapError("Environment file not found")
    values = dotenv_values(args.env_file, interpolate=False)
    host, port, user = target(values)
    password = values.get("PAROL")
    expected = values.get("SSH_HOST_FINGERPRINT")
    # Never store bootstrap credentials in metadata or print parsed values.
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    known = ssh_dir / f"server_deploy_{args.project}_known_hosts"
    key = ssh_dir / f"server_deploy_{args.project}"
    project_user = "sdp-" + args.project
    if key.exists() and native_preflight(host, port, project_user, known, key, expected):
        print(json.dumps({"host": host, "port": port, "user": project_user, "key": str(key), "known_hosts": str(known), "status": "existing access verified"}))
        return
    if not password:
        raise BootstrapError("PAROL missing and project key authentication unavailable")
    with connect(host, port, user, known, expected, args.trust_new_host, password=password) as client:
        prefix = "" if user == "root" else "sudo -S -p '' "
        sudo_input = None if user == "root" else password + "\n"
        before = run(client, prefix + "sh -c " + shlex.quote(INVENTORY), stdin_text=sudo_input)
        print(before)
        if not args.apply:
            print("Inspection complete; no remote files or access were changed. Use --apply for the authorized bootstrap.")
            return
        public_key = ensure_key(key, args.project)
        stage = run(client, "mktemp -d /tmp/server-deploy.XXXXXXXX").strip()
        if not re.fullmatch(r"/tmp/server-deploy\.[A-Za-z0-9]+", stage):
            raise BootstrapError("Unexpected remote staging path")
        server_dir = Path(__file__).resolve().parents[1] / "server"
        names = ["sdp", "sdp-admin", "sdp-ci-entry", "sdp_core.py", "bootstrap-tools.sh"]
        with client.open_sftp() as sftp:
            try:
                for name in names:
                    # Normalize Windows line endings without loading any private keys.
                    content = (server_dir / name).read_text(encoding="utf-8-sig").replace("\r\n", "\n")
                    with sftp.file(stage + "/" + name, "w") as remote:
                        remote.write(content.encode())
                command = prefix + "bash " + " ".join(shlex.quote(x) for x in [stage + "/bootstrap-tools.sh", args.project, stage, public_key])
                print(run(client, command, stdin_text=sudo_input, timeout=120))
                after = run(client, prefix + "sh -c " + shlex.quote(INVENTORY), stdin_text=sudo_input)
                states = lambda text: re.search(r"BEGIN_CONTAINER_STATE\n(.*?)END_CONTAINER_STATE", text, re.S)
                old_states, new_states = states(before), states(after)
                if bool(old_states) != bool(new_states) or (old_states and old_states.group(1) != new_states.group(1)):
                    raise BootstrapError("Existing container states changed during bootstrap; inspect before deploying")
            finally:
                for name in names:
                    try:
                        sftp.remove(stage + "/" + name)
                    except OSError:
                        pass
                try:
                    sftp.rmdir(stage)
                except OSError:
                    pass
    if not native_preflight(host, port, project_user, known, key, expected):
        raise BootstrapError("Bootstrap completed but project key authentication failed; inspect before retrying")
    print(json.dumps({"host": host, "port": port, "user": project_user, "key": str(key), "known_hosts": str(known), "status": "project access verified"}))


if __name__ == "__main__":
    try:
        main()
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except paramiko.BadHostKeyException:
        print("ERROR: Server host key changed. Stop and verify with the server owner.", file=sys.stderr)
        sys.exit(1)
    except paramiko.AuthenticationException:
        print("ERROR: SSH authentication failed; no credentials printed or retried.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # Library/OS exception strings may contain server-controlled text; avoid credential leakage.
        print(f"ERROR: {type(exc).__name__}; inspect connection/prerequisites without printing secrets.", file=sys.stderr)
        sys.exit(1)
