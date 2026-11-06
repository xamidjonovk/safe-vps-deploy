"""
sdp — Safe Deploy Proxy (core library)

A small privilege broker for deploying docker-compose projects on a *shared* VPS.

  * An AI agent (or CI) logs in as an unprivileged user `sdp-<project>`.
  * That user can run exactly one privileged program via sudo: /usr/local/sbin/sdp
  * sdp derives the project from SUDO_USER, so an agent can only ever touch its own
    project: its git checkout, its compose stack, its nginx sites, its certificates.
  * Secrets live in /srv/apps/<project>/.env (root:root 0600). The agent can create,
    generate and list keys, but sdp never prints secret values; logs are masked.
  * Every compose file is checked against a security policy before anything runs
    (no privileged containers, no host mounts, no public ports, no host network, ...),
    so a deploy cannot escape to the host or reach other projects.

Installed to /usr/local/lib/sdp/sdp_core.py (root:root 0644).
Entry points: /usr/local/sbin/sdp (agent/CI/root) and /usr/local/sbin/sdp-admin (root only).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import getpass
import grp
import ipaddress
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

VERSION = "1.0.0"

ETC = "/etc/sdp"
PROJECTS_ETC = ETC + "/projects"
APPS_ROOT = "/srv/apps"
ACME_ROOT = "/var/www/sdp-acme"
NGINX_CONF_D = "/etc/nginx/conf.d"
LE_LIVE = "/etc/letsencrypt/live"
LOCK_DIR = "/run/lock"
SDP_BIN = "/usr/local/sbin/sdp"
CI_ENTRY = "/usr/local/sbin/sdp-ci-entry"
PORT_RANGE = (20000, 29999)

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]{0,99}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(
    r"^(git@[A-Za-z0-9.-]+:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(\.git)?"
    r"|ssh://git@[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(\.git)?"
    r"|https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(\.git)?)$"
)
SSH_PUBKEY_RE = re.compile(
    r"^(ssh-ed25519|sk-ssh-ed25519@openssh\.com|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ssh-rsa)\s+([A-Za-z0-9+/=]{40,})(\s.*)?$"
)
PUBLIC_ENV_PREFIXES = ("NEXT_PUBLIC_", "VITE_", "PUBLIC_", "REACT_APP_", "NUXT_PUBLIC_", "EXPO_PUBLIC_")
SECRET_HINT_RE = re.compile(
    r"(SECRET|PASSW|PWD|TOKEN|PRIVATE|CREDENTIAL|API_?KEY|ACCESS_?KEY|AUTH|DSN|SALT|SIGNING|CONN|_URL$|_URI$)",
    re.I,
)
COMPOSE_CANDIDATES = (
    "docker-compose.prod.yml", "docker-compose.prod.yaml", "compose.prod.yml", "compose.prod.yaml",
    "docker-compose.production.yml", "docker-compose.production.yaml",
    "compose.production.yml", "compose.production.yaml",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
)
ENV_EXAMPLE_CANDIDATES = (".env.example", ".env.sample", ".env.template", ".env.dist", "example.env")

DEFAULT_CONF = {
    "repo_url": "",
    "branch": "main",
    "compose_file": "",
    "le_email": "",
    "health_urls": [],
    "allow_exec": True,
    "wait_timeout": 300,
}


# --------------------------------------------------------------------------- output


class SdpError(Exception):
    pass


def die(msg: str):
    raise SdpError(msg)


def _err(prefix: str, msg: str):
    sys.stderr.write(MASK(f"{prefix}{msg}") + "\n")
    sys.stderr.flush()


def info(msg: str):
    _err("[sdp] ", msg)


def ok(msg: str):
    _err("[sdp] OK  ", msg)


def warn(msg: str):
    _err("[sdp] WARN ", msg)


def out(msg: str = ""):
    sys.stdout.write(MASK(msg) + "\n")
    sys.stdout.flush()


class Masker:
    """Replaces secret values from the project's .env in any text sdp prints."""

    def __init__(self):
        self.values: list[str] = []

    def load_env(self, path: str):
        vals = set(self.values)
        for k, v in read_env(path).items():
            if not v:
                continue
            if k.upper().startswith(PUBLIC_ENV_PREFIXES) or is_template_value(v) or is_credential_free_url(v):
                continue
            if is_secret_key(k) or len(v) >= 16:
                if len(v) >= 6:
                    vals.add(v)
            for m in re.finditer(r"://[^:/@\s]+:([^@\s]{4,})@", v):
                if not m.group(1).startswith("${"):
                    vals.add(m.group(1))
        self.values = sorted(vals, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        if not text:
            return text
        for v in self.values:
            text = text.replace(v, "***")
        return text


MASK = Masker()


def is_credential_free_url(v: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9+.-]*://", v)) and not re.search(
        r"://[^/@\s]*:[^/@\s]*@|(?i:password|passwd|secret|token|apikey|api_key|key)=", v)


def is_template_value(v: str) -> bool:
    """A value composed only from ${REFERENCES} plus non-secret text (no literal URL password)."""
    return "${" in v and not re.search(r"://[^:/@\s]+:(?!\$\{)[^@\s]+@", v)


def is_secret_key(key: str) -> bool:
    if key.upper().startswith(PUBLIC_ENV_PREFIXES):
        return False
    return bool(SECRET_HINT_RE.search(key))


# --------------------------------------------------------------------------- process helpers


def base_env() -> dict:
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DOCKER_CLI_HINTS": "false",
        "BUILDKIT_PROGRESS": "plain",
    }


def run(cmd, *, check=True, cwd=None, env=None, input_text=None, timeout=None):
    try:
        r = subprocess.run(
            cmd, cwd=cwd, env=env or base_env(), input=input_text, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, errors="replace",
        )
    except FileNotFoundError:
        die(f"required command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        die(f"command timed out: {' '.join(cmd[:5])} ...")
    if check and r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        die(f"command failed ({r.returncode}): {' '.join(cmd[:6])}\n{detail[-4000:]}")
    return r


def run_stream(cmd, *, env=None, cwd=None) -> int:
    """Run a command, streaming masked output to stderr. Returns exit code."""
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env or base_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        die(f"required command not found: {cmd[0]}")
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stderr.write(MASK(line))
    sys.stderr.flush()
    return proc.wait()


def have(cmd: str) -> bool:
    return shutil.which(cmd, path=base_env()["PATH"]) is not None


@contextlib.contextmanager
def file_lock(name: str, timeout: int = 1200):
    os.makedirs(LOCK_DIR, exist_ok=True)
    fd = os.open(f"{LOCK_DIR}/sdp-{name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    start = time.time()
    announced = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not announced:
                    info(f"waiting for another sdp operation ({name}) to finish ...")
                    announced = True
                if time.time() - start > timeout:
                    die(f"another sdp operation ({name}) is still running; try again later")
                time.sleep(2)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --------------------------------------------------------------------------- files


def write_file(path: str, content: str, mode: int = 0o600, uid: int = 0, gid: int = 0):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".sdp-tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.chown(tmp, uid, gid)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(default))
    except json.JSONDecodeError as e:
        die(f"corrupt state file {path}: {e}")


def save_json(path: str, data, mode: int = 0o600):
    write_file(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode)


def inside(path: str, root: str) -> bool:
    rp = os.path.realpath(path)
    rr = os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)


# --------------------------------------------------------------------------- .env handling


def _parse_env_value(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        if v[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()


def _env_line_key(line: str):
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s.startswith("export "):
        s = s[7:].lstrip()
    if "=" not in s:
        return None
    k = s.split("=", 1)[0].strip()
    return k if ENV_KEY_RE.match(k) else None


def read_env(path: str) -> dict:
    result: dict = {}
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return result
    for line in lines:
        k = _env_line_key(line)
        if k is None:
            continue
        s = line.strip()
        if s.startswith("export "):
            s = s[7:].lstrip()
        result[k] = _parse_env_value(s.split("=", 1)[1])
    return result


def env_keys_in_order(path: str) -> list:
    keys = []
    try:
        with open(path) as f:
            for line in f.read().splitlines():
                k = _env_line_key(line)
                if k and k not in keys:
                    keys.append(k)
    except FileNotFoundError:
        pass
    return keys


def format_env_value(v: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@%+,=${}\-]*", v):
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_env_key(path: str, key: str, value: str):
    if not ENV_KEY_RE.match(key):
        die(f"invalid variable name: {key}")
    if "\n" in value or "\r" in value:
        die("values cannot contain newlines")
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    new_line = f"{key}={format_env_value(value)}"
    out_lines, replaced = [], False
    for line in lines:
        if _env_line_key(line) == key:
            if not replaced:
                out_lines.append(new_line)
                replaced = True
            continue
        out_lines.append(line)
    if not replaced:
        out_lines.append(new_line)
    # Compose interpolates .env top-down: values that reference ${OTHER} must come after OTHER.
    plain = [ln for ln in out_lines if not (_env_line_key(ln) and "${" in ln.split("=", 1)[1])]
    composed = [ln for ln in out_lines if ln not in plain]
    write_file(path, "\n".join(plain + composed) + "\n", 0o600)


def unset_env_key(path: str, key: str) -> bool:
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return False
    kept = [ln for ln in lines if _env_line_key(ln) != key]
    if len(kept) == len(lines):
        return False
    write_file(path, "\n".join(kept) + ("\n" if kept else ""), 0o600)
    return True


# --------------------------------------------------------------------------- project model


class Project:
    def __init__(self, name: str):
        if not NAME_RE.match(name or ""):
            die(f"invalid project name '{name}': use 3-32 chars of a-z, 0-9 and '-', starting with a letter")
        self.name = name
        self.dir = f"{APPS_ROOT}/{name}"
        self.repo = f"{self.dir}/repo"
        self.env = f"{self.dir}/.env"
        self.data = f"{self.dir}/data"
        self.etc = f"{PROJECTS_ETC}/{name}"
        self.conf_path = f"{self.etc}/conf.json"
        self.routes_path = f"{self.etc}/routes.json"
        self.state_path = f"{self.etc}/state.json"
        self.deploy_key = f"{self.etc}/deploy_key"
        self.known_hosts = f"{self.etc}/known_hosts"
        self.override = f"{self.etc}/compose.override.json"
        self.user = f"sdp-{name}"
        self.cproj = f"sdp-{name}"
        self.home = f"/home/{self.user}"
        self.auth_keys = f"{self.home}/.ssh/authorized_keys"

    def exists(self) -> bool:
        return os.path.isdir(self.etc)

    def require(self):
        if not self.exists():
            die(f"project '{self.name}' does not exist (create it with: sdp-admin add-project {self.name} ...)")

    def conf(self) -> dict:
        c = dict(DEFAULT_CONF)
        c.update(load_json(self.conf_path, {}))
        return c

    def save_conf(self, c: dict):
        save_json(self.conf_path, c)

    def routes(self) -> dict:
        return load_json(self.routes_path, {})

    def save_routes(self, r: dict):
        save_json(self.routes_path, r)

    def state(self) -> dict:
        return load_json(self.state_path, {})

    def save_state(self, s: dict):
        save_json(self.state_path, s)

    def gid(self) -> int:
        try:
            return grp.getgrnam(self.user).gr_gid
        except KeyError:
            return 0


def all_projects() -> list:
    try:
        return sorted(n for n in os.listdir(PROJECTS_ETC) if NAME_RE.match(n))
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------- git


def git_env(p: Project) -> dict:
    e = base_env()
    e["GIT_TERMINAL_PROMPT"] = "0"
    e["GIT_SSH_COMMAND"] = (
        f"ssh -i {p.deploy_key} -o IdentitiesOnly=yes -o BatchMode=yes "
        f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={p.known_hosts}"
    )
    return e


def git(p: Project, *args, check=True, timeout=600):
    return run(["git", "-C", p.repo, *args], env=git_env(p), check=check, timeout=timeout)


def repo_head(p: Project) -> str:
    if not os.path.isdir(p.repo + "/.git"):
        return ""
    r = git(p, "rev-parse", "HEAD", check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def ensure_env_file(p: Project):
    if not os.path.exists(p.env):
        write_file(p.env, "# Managed by sdp. Edit with: sdp env-* (agent) or sdp-admin secret/env-edit (root)\n", 0o600)
    os.chown(p.env, 0, 0)
    os.chmod(p.env, 0o600)


def ensure_env_link(p: Project):
    link = p.repo + "/.env"
    target = "../.env"
    if os.path.islink(link):
        if os.readlink(link) == target:
            return
        os.unlink(link)
    elif os.path.exists(link):
        if os.path.isdir(link):
            die("repo contains a directory named .env — remove it")
        os.unlink(link)
    os.symlink(target, link)


def fix_repo_perms(p: Project):
    gid = p.gid()
    os.chown(p.dir, 0, gid)
    os.chmod(p.dir, 0o750)
    run(["chown", "-R", "-h", f"0:{gid}", p.repo], check=False)
    run(["chmod", "-R", "g+rX,g-w,o-rwx", p.repo], check=False)


def repo_sync(p: Project, c: dict, sha: str | None = None) -> str:
    url, branch = c.get("repo_url"), c.get("branch") or "main"
    if not url:
        die("no repository configured. Run: sdp repo-init <git-url> --branch <branch>")
    if not os.path.exists(p.deploy_key) and not url.startswith("https://"):
        die("no deploy key. Run: sdp repo-init <git-url>")
    ensure_env_file(p)
    if not os.path.isdir(p.repo + "/.git"):
        if os.path.exists(p.repo):
            if os.listdir(p.repo):
                die(f"{p.repo} exists but is not a git checkout")
            os.rmdir(p.repo)
        info(f"cloning {url} ({branch}) ...")
        r = run(["git", "clone", "--branch", branch, "--single-branch", url, p.repo],
                env=git_env(p), check=False, timeout=900)
        if r.returncode != 0:
            die("git clone failed. Is the deploy key added to the repository (read-only)?\n" + r.stderr.strip())
    else:
        git(p, "remote", "set-url", "origin", url)
        r = git(p, "fetch", "--prune", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                check=False, timeout=900)
        if r.returncode != 0:
            die("git fetch failed. Is the deploy key still added to the repository?\n" + r.stderr.strip())
    if sha:
        if not SHA_RE.match(sha):
            die("--sha must be a full 40-character commit hash")
        if git(p, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
            die(f"commit {sha} not found on origin/{branch}")
        if git(p, "merge-base", "--is-ancestor", sha, f"origin/{branch}", check=False).returncode != 0:
            die(f"commit {sha} is not part of origin/{branch}; refusing to deploy it")
        target = sha
    else:
        target = git(p, "rev-parse", f"origin/{branch}").stdout.strip()
    git(p, "checkout", "--detach", "--force", target)
    ensure_env_link(p)
    fix_repo_perms(p)
    return target


def repo_checkout(p: Project, sha: str):
    git(p, "checkout", "--detach", "--force", sha)
    ensure_env_link(p)
    fix_repo_perms(p)


# --------------------------------------------------------------------------- compose


def compose_file(p: Project, c: dict) -> str:
    name = c.get("compose_file") or ""
    if name:
        if "/" in name or name.startswith("."):
            die("compose_file must be a file name in the repository root")
        path = f"{p.repo}/{name}"
        if not os.path.isfile(path):
            die(f"compose file '{name}' not found in repository root")
        return path
    for cand in COMPOSE_CANDIDATES:
        path = f"{p.repo}/{cand}"
        if os.path.isfile(path):
            return path
    die("no compose file found in the repository root (expected e.g. docker-compose.prod.yml or docker-compose.yml). "
        "Set it with: sdp config set compose_file <name>")


def compose_cmd(p: Project, c: dict, override: bool = True) -> list:
    cmd = ["docker", "compose", "--ansi", "never", "-p", p.cproj,
           "--project-directory", p.repo, "--env-file", p.env, "-f", compose_file(p, c)]
    if override and os.path.exists(p.override):
        cmd += ["-f", p.override]
    return cmd


def compose_config(p: Project, c: dict, override: bool = False):
    ensure_env_file(p)
    r = run(compose_cmd(p, c, override) + ["config", "--format", "json"], check=False, timeout=180)
    unset = sorted(set(re.findall(r'The \\?"?([A-Za-z_][A-Za-z0-9_]*)\\?"? variable is not set', r.stderr or "")))
    if r.returncode != 0:
        die("docker compose could not parse the project:\n" + (r.stderr or "").strip()[-4000:])
    try:
        cfg = json.loads(r.stdout)
    except json.JSONDecodeError:
        die("unexpected output from 'docker compose config'")
    return cfg, unset


def compose_ps(p: Project, c: dict) -> list:
    r = run(compose_cmd(p, c) + ["ps", "-a", "--format", "json"], check=False, timeout=60)
    text = (r.stdout or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            with contextlib.suppress(json.JSONDecodeError):
                rows.append(json.loads(line))
    return rows


def published_ports(cfg: dict) -> set:
    ports = set()
    for svc in (cfg.get("services") or {}).values():
        for prt in svc.get("ports") or []:
            if not isinstance(prt, dict):
                continue
            pub = str(prt.get("published") or "")
            m = re.fullmatch(r"(\d+)(?:-(\d+))?", pub)
            if not m:
                continue
            a = int(m.group(1))
            b = int(m.group(2) or a)
            ports.update(range(a, b + 1))
    return ports


def listening_ports() -> dict:
    """port -> set(process names) for TCP listeners."""
    r = run(["ss", "-H", "-ltnp"], check=False)
    res: dict = {}
    for line in (r.stdout or "").splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        local = cols[3]
        port_s = local.rsplit(":", 1)[-1]
        if not port_s.isdigit():
            continue
        procs = set(re.findall(r'\(\("([^"]+)"', line))
        res.setdefault(int(port_s), set()).update(procs)
    return res


def project_running_ports(p: Project) -> set:
    r = run(["docker", "ps", "--filter", f"label=com.docker.compose.project={p.cproj}", "--format", "{{.Ports}}"],
            check=False)
    ports = set()
    for m in re.finditer(r":(\d+)->", r.stdout or ""):
        ports.add(int(m.group(1)))
    return ports


def other_projects_ports(p: Project) -> dict:
    res = {}
    for name in all_projects():
        if name == p.name:
            continue
        st = load_json(f"{PROJECTS_ETC}/{name}/state.json", {})
        for port in st.get("ports") or []:
            res[int(port)] = name
    return res


# --------------------------------------------------------------------------- security policy

SAFE_SERVICE_KEYS = {
    "annotations", "attach", "blkio_config", "build", "cap_add", "cap_drop", "cgroup", "command", "configs",
    "container_name", "cpu_count", "cpu_percent", "cpu_period", "cpu_quota", "cpu_shares", "cpus", "cpuset",
    "depends_on", "deploy", "develop", "dns", "dns_opt", "dns_search", "domainname", "entrypoint", "env_file",
    "environment", "expose", "extra_hosts", "gpus", "group_add", "healthcheck", "hostname", "image", "init",
    "ipc", "label_file", "labels", "links", "logging", "mac_address", "mem_limit", "mem_reservation",
    "mem_swappiness", "memswap_limit", "network_mode", "networks", "oom_kill_disable", "oom_score_adj", "pid",
    "pids_limit", "platform", "ports", "post_start", "pre_stop", "privileged", "profiles", "pull_policy",
    "read_only", "restart", "runtime", "scale", "secrets", "security_opt", "shm_size", "stdin_open",
    "stop_grace_period", "stop_signal", "sysctls", "tmpfs", "tty", "ulimits", "use_api_socket", "user",
    "userns_mode", "uts", "volumes", "volumes_from", "working_dir",
}
FORBIDDEN_SERVICE_KEYS = {
    "devices": "host devices",
    "device_cgroup_rules": "host device access",
    "cgroup_parent": "custom cgroup parent",
    "provider": "provider services run binaries on the host",
    "credential_spec": "host credential specs",
    "isolation": "custom isolation",
    "storage_opt": "storage driver options",
    "external_links": "links to containers of other projects",
    "cpu_rt_runtime": "realtime scheduling",
    "cpu_rt_period": "realtime scheduling",
    "models": "model runner integration",
}
SAFE_BUILD_KEYS = {
    "context", "dockerfile", "dockerfile_inline", "args", "labels", "target", "tags", "platforms", "shm_size",
    "extra_hosts", "network", "cache_from", "cache_to", "no_cache", "pull", "secrets", "additional_contexts",
    "ulimits", "privileged", "entitlements", "ssh", "isolation", "provenance", "sbom",
}
HARMLESS_CAPS = {"NET_BIND_SERVICE", "CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER", "KILL", "FSETID",
                 "SETPCAP", "SETFCAP", "MKNOD", "AUDIT_WRITE", "NET_RAW", "SYS_CHROOT"}
TOP_LEVEL_KEYS = {"name", "services", "networks", "volumes", "secrets", "configs"}
URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://|^git@")


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


class Report:
    def __init__(self):
        self.errors: list = []
        self.warnings: list = []

    def err(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)


def check_tracked_secrets(p: Project, rep: Report):
    r = git(p, "ls-files", "-z", check=False)
    for n in (r.stdout or "").split("\0"):
        if not n:
            continue
        base = os.path.basename(n)
        if re.fullmatch(r"\.env(\..+)?|.+\.env", base) and not re.search(
                r"(example|sample|template|dist|defaults?)$", base, re.I):
            rep.err(f"secret file is committed to git: {n} — remove it (git rm --cached {n}), add it to "
                    f".gitignore and rotate every secret it contained")
        elif base in ("id_rsa", "id_ed25519", "id_ecdsa") or re.search(r"\.(pem|key|p12|pfx)$", base):
            rep.warn(f"possible private key committed to git: {n}")


def raw_compose_check(p: Project, path: str, rep: Report, seen: set):
    """Checks paths that `docker compose config` resolves away (env_file, include, extends)."""
    try:
        import yaml  # python3-yaml
    except ImportError:
        rep.err("python3-yaml is not installed on the server (apt-get install python3-yaml)")
        return
    rp = os.path.realpath(path)
    if rp in seen:
        return
    seen.add(rp)
    if not inside(rp, p.dir):
        rep.err(f"compose file outside the project: {path}")
        return
    try:
        with open(rp) as f:
            doc = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        rep.err(f"cannot parse {os.path.relpath(rp, p.repo)}: {e}")
        return
    if not isinstance(doc, dict):
        rep.err(f"{os.path.relpath(rp, p.repo)} is not a compose mapping")
        return
    base = os.path.dirname(rp)
    rel = os.path.relpath(rp, p.repo)

    def check_path(val, what):
        if isinstance(val, dict):
            val = val.get("path")
        if not isinstance(val, str) or not val:
            rep.err(f"{rel}: {what}: expected a plain path")
            return None
        if "$" in val:
            rep.err(f"{rel}: {what}: variables are not allowed in file paths ('{val}')")
            return None
        full = val if os.path.isabs(val) else os.path.join(base, val)
        if not inside(full, p.dir):
            rep.err(f"{rel}: {what}: '{val}' points outside the project directory")
            return None
        return full

    for item in _as_list(doc.get("include")):
        if isinstance(item, str):
            f = check_path(item, "include")
            if f and os.path.isfile(f):
                raw_compose_check(p, f, rep, seen)
        elif isinstance(item, dict):
            for sub in _as_list(item.get("path")):
                f = check_path(sub, "include.path")
                if f and os.path.isfile(f):
                    raw_compose_check(p, f, rep, seen)
            for ef in _as_list(item.get("env_file")):
                check_path(ef, "include.env_file")
            if item.get("project_directory") is not None:
                check_path(item.get("project_directory"), "include.project_directory")

    services = doc.get("services") or {}
    if not isinstance(services, dict):
        return
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for ef in _as_list(svc.get("env_file")):
            check_path(ef, f"services.{name}.env_file")
        for lf in _as_list(svc.get("label_file")):
            check_path(lf, f"services.{name}.label_file")
        ext = svc.get("extends")
        if isinstance(ext, dict) and ext.get("file"):
            f = check_path(ext["file"], f"services.{name}.extends.file")
            if f and os.path.isfile(f):
                raw_compose_check(p, f, rep, seen)
        env = svc.get("environment")
        pairs = []
        if isinstance(env, dict):
            pairs = list(env.items())
        elif isinstance(env, list):
            for e in env:
                if isinstance(e, str) and "=" in e:
                    pairs.append(tuple(e.split("=", 1)))
        for k, v in pairs:
            if v is None or str(v) == "" or "$" in str(v):
                continue
            if is_secret_key(str(k)) and len(str(v)) >= 4:
                rep.warn(f"{rel}: services.{name}.environment.{k} is hard-coded in the compose file — "
                         f"move the value to .env and write {k}: ${{{k}}}")
    for section in ("secrets", "configs"):
        sec = doc.get(section) or {}
        if isinstance(sec, dict):
            for n, d in sec.items():
                if isinstance(d, dict) and d.get("file"):
                    check_path(d["file"], f"{section}.{n}.file")


def policy_check(p: Project, cfg: dict, rep: Report):
    pre = p.cproj
    services = cfg.get("services") or {}
    svc_names = set(services)

    for k in cfg:
        if k.startswith("x-"):
            continue
        if k not in TOP_LEVEL_KEYS:
            rep.err(f"top-level '{k}' is not allowed by the server policy")

    def path_ok(path, what):
        if not inside(path, p.dir):
            rep.err(f"{what}: '{path}' is outside the project directory — only paths inside the repository "
                    f"(or named volumes) may be mounted")
            return False
        return True

    for name, svc in services.items():
        w = f"services.{name}"
        for key in svc:
            if key.startswith("x-"):
                continue
            if key in FORBIDDEN_SERVICE_KEYS:
                if svc.get(key):
                    rep.err(f"{w}.{key} is forbidden ({FORBIDDEN_SERVICE_KEYS[key]})")
                continue
            if key not in SAFE_SERVICE_KEYS:
                rep.err(f"{w}.{key} is not allowed by the server policy")

        if svc.get("privileged"):
            rep.err(f"{w}.privileged: true is forbidden (it gives the container full root on the host)")
        if svc.get("use_api_socket"):
            rep.err(f"{w}.use_api_socket is forbidden (Docker API access = root on the host)")
        if svc.get("runtime") not in (None, "", "runc"):
            rep.err(f"{w}.runtime '{svc.get('runtime')}' is not allowed")
        if svc.get("cgroup") == "host":
            rep.err(f"{w}.cgroup: host is forbidden")
        if svc.get("oom_kill_disable"):
            rep.warn(f"{w}.oom_kill_disable can freeze the whole server when memory runs out")

        nm = svc.get("network_mode")
        if nm and nm != "none" and not (nm.startswith("service:") and nm[8:] in svc_names):
            rep.err(f"{w}.network_mode '{nm}' is forbidden (use the project's default network)")
        for ns in ("pid", "ipc"):
            v = svc.get(ns)
            if v and v not in ("private", "shareable") and not (v.startswith("service:") and v[8:] in svc_names):
                rep.err(f"{w}.{ns} '{v}' is forbidden (sharing host/other containers' namespaces)")
        for ns in ("uts", "userns_mode"):
            if svc.get(ns):
                rep.err(f"{w}.{ns} '{svc.get(ns)}' is forbidden")

        for cap in svc.get("cap_add") or []:
            c = str(cap).upper().replace("CAP_", "")
            if c not in HARMLESS_CAPS:
                rep.err(f"{w}.cap_add {cap} is forbidden")
        for opt in svc.get("security_opt") or []:
            o = str(opt).lower()
            if "unconfined" in o or "label=disable" in o or "label:disable" in o:
                rep.err(f"{w}.security_opt '{opt}' is forbidden")
        for sk in (svc.get("sysctls") or {}):
            if not str(sk).startswith("net."):
                rep.err(f"{w}.sysctls {sk} is forbidden (only net.* is allowed)")

        for hook_key in ("post_start", "pre_stop"):
            for h in svc.get(hook_key) or []:
                if isinstance(h, dict) and h.get("privileged"):
                    rep.err(f"{w}.{hook_key} privileged hooks are forbidden")

        for vf in svc.get("volumes_from") or []:
            v = str(vf)
            if v.startswith("container:"):
                rep.err(f"{w}.volumes_from '{v}' is forbidden (containers of other projects)")

        for vol in svc.get("volumes") or []:
            if not isinstance(vol, dict):
                rep.err(f"{w}.volumes: unexpected entry {vol!r}")
                continue
            t = vol.get("type")
            src = vol.get("source") or ""
            if t == "bind":
                if "docker.sock" in src:
                    rep.err(f"{w}.volumes: mounting the Docker socket is forbidden (it is root on the host)")
                else:
                    path_ok(src, f"{w}.volumes")
            elif t in ("volume", "tmpfs", "image", None):
                pass
            else:
                rep.err(f"{w}.volumes type '{t}' is forbidden")

        for ef in _as_list(svc.get("env_file")):
            path = ef.get("path") if isinstance(ef, dict) else ef
            if isinstance(path, str) and path:
                path_ok(path if os.path.isabs(path) else os.path.join(p.repo, path), f"{w}.env_file")
        for lf in _as_list(svc.get("label_file")):
            if isinstance(lf, str):
                path_ok(lf if os.path.isabs(lf) else os.path.join(p.repo, lf), f"{w}.label_file")

        for prt in svc.get("ports") or []:
            if not isinstance(prt, dict):
                rep.err(f"{w}.ports: unexpected entry {prt!r}")
                continue
            hip = prt.get("host_ip") or ""
            pub = str(prt.get("published") or "")
            tgt = prt.get("target")
            if hip not in ("127.0.0.1", "::1"):
                rep.err(f"{w}.ports: '{hip or '0.0.0.0'}:{pub or '?'}:{tgt}' is public. Bind to localhost only: "
                        f"\"127.0.0.1:<host_port>:{tgt}\" (nginx is the only public entry point)")
                continue
            m = re.fullmatch(r"(\d+)(?:-(\d+))?", pub)
            if not m:
                rep.err(f"{w}.ports: a fixed host port is required, e.g. \"127.0.0.1:{PORT_RANGE[0]}:{tgt}\"")
                continue
            if int(m.group(1)) < 1024:
                rep.err(f"{w}.ports: host port {pub} is privileged; use {PORT_RANGE[0]}-{PORT_RANGE[1]}")

        b = svc.get("build")
        if isinstance(b, dict):
            for k in b:
                if k not in SAFE_BUILD_KEYS:
                    rep.err(f"{w}.build.{k} is not allowed by the server policy")
            ctx = b.get("context") or ""
            if ctx and not URL_SCHEME_RE.match(ctx):
                path_ok(ctx, f"{w}.build.context")
            df = b.get("dockerfile")
            if df and not URL_SCHEME_RE.match(ctx or ""):
                full = df if os.path.isabs(df) else os.path.join(ctx or p.repo, df)
                path_ok(full, f"{w}.build.dockerfile")
            for cname, cval in (b.get("additional_contexts") or {}).items():
                cv = str(cval)
                if cv.startswith(("docker-image://", "service:", "oci-layout://")) or URL_SCHEME_RE.match(cv):
                    if cv.startswith("oci-layout://"):
                        rep.err(f"{w}.build.additional_contexts.{cname}: oci-layout is forbidden")
                    continue
                path_ok(cv, f"{w}.build.additional_contexts.{cname}")
            if b.get("network") == "host":
                rep.err(f"{w}.build.network: host is forbidden")
            if b.get("privileged"):
                rep.err(f"{w}.build.privileged is forbidden")
            if b.get("entitlements"):
                rep.err(f"{w}.build.entitlements are forbidden")
            if b.get("ssh"):
                rep.err(f"{w}.build.ssh is forbidden (it would expose server keys to the build)")
            if b.get("cache_to"):
                rep.err(f"{w}.build.cache_to is forbidden on this server")
            for cf in b.get("cache_from") or []:
                if "type=local" in str(cf):
                    rep.err(f"{w}.build.cache_from type=local is forbidden")
            img = svc.get("image")
            if img and not (img.startswith(pre + "-") or img.startswith(pre + "_") or img.startswith(pre + "/")):
                rep.err(f"{w}.image '{img}': built images must be named '{pre}-{name}' (or remove 'image:') "
                        f"so they can never overwrite another project's image")

        if svc.get("container_name"):
            rep.warn(f"{w}.container_name is set — remove it; fixed names collide with other projects")
        if svc.get("restart") in (None, "") and not (svc.get("deploy") or {}).get("restart_policy"):
            rep.warn(f"{w} has no restart policy — use 'restart: unless-stopped' "
                     f"(or 'restart: \"no\"' for one-shot jobs like migrations)")
        limits = (((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {})
        mem = svc.get("mem_limit") or limits.get("memory")
        if not mem:
            rep.warn(f"{w} has no memory limit — on a shared server add e.g. 'mem_limit: 512m'")
        img = svc.get("image") or ""
        if img and not b and (":" not in img.rsplit("/", 1)[-1] or img.endswith(":latest")):
            rep.warn(f"{w}.image '{img}' is not pinned to a version tag")

    for vname, vol in (cfg.get("volumes") or {}).items():
        vol = vol or {}
        if vol.get("external"):
            rep.err(f"volumes.{vname}: external volumes are forbidden (they may belong to other projects)")
        real = vol.get("name") or ""
        if real and not real.startswith(pre + "_") and not real.startswith(pre + "-"):
            rep.err(f"volumes.{vname}.name '{real}' must start with '{pre}_' (remove 'name:' to use the default)")
        if vol.get("driver") not in (None, "", "local"):
            rep.err(f"volumes.{vname}: driver '{vol.get('driver')}' is forbidden")
        dev = (vol.get("driver_opts") or {}).get("device")
        if dev and str(dev).startswith("/"):
            path_ok(str(dev), f"volumes.{vname}.driver_opts.device")

    for nname, net in (cfg.get("networks") or {}).items():
        net = net or {}
        if net.get("external"):
            rep.err(f"networks.{nname}: external networks are forbidden (they may belong to other projects)")
        real = net.get("name") or ""
        if real and not real.startswith(pre + "_") and not real.startswith(pre + "-"):
            rep.err(f"networks.{nname}.name '{real}' must start with '{pre}_' (remove 'name:' to use the default)")
        if net.get("driver") not in (None, "", "bridge"):
            rep.err(f"networks.{nname}: driver '{net.get('driver')}' is forbidden")

    for section in ("secrets", "configs"):
        for sname, s in (cfg.get(section) or {}).items():
            s = s or {}
            if s.get("external"):
                rep.err(f"{section}.{sname}: external {section} are forbidden")
            if s.get("file"):
                path_ok(s["file"], f"{section}.{sname}.file")


def validate(p: Project, c: dict, *, strict_env: bool) -> tuple:
    rep = Report()
    if not os.path.isdir(p.repo + "/.git"):
        die("repository is not cloned yet. Run: sdp sync")
    ensure_env_file(p)
    ensure_env_link(p)
    cf = compose_file(p, c)
    check_tracked_secrets(p, rep)
    raw_compose_check(p, cf, rep, set())
    if rep.errors:
        # Stop before `docker compose config` would read files outside the project.
        return {"services": {}}, rep
    cfg, unset = compose_config(p, c, override=False)
    policy_check(p, cfg, rep)

    if unset:
        msg = ("variables used by the compose file but missing from the server .env: " + ", ".join(unset) +
               " — set them (sdp env-gen / env-set, or ask the human: sdp-admin secret) "
               "or give them a default (${VAR:-default})")
        (rep.err if strict_env else rep.warn)(msg)

    ours_running = project_running_ports(p)
    listening = listening_ports()
    others = other_projects_ports(p)
    for port in sorted(published_ports(cfg)):
        if port in listening and port not in ours_running:
            procs = ",".join(sorted(listening[port])) or "another process"
            rep.err(f"host port {port} is already used by {procs}. Pick another one (sdp port-suggest)")
        elif port in others:
            rep.err(f"host port {port} is reserved by another project on this server. Pick another one "
                    f"(sdp port-suggest)")
    if not published_ports(cfg):
        rep.warn("no service publishes a port on 127.0.0.1 — nginx will have nothing to proxy to")
    return cfg, rep


def print_report(rep: Report):
    for w in rep.warnings:
        warn(w)
    for e in rep.errors:
        _err("[sdp] ERROR ", e)


def write_override(p: Project, cfg: dict):
    services = {}
    for name in (cfg.get("services") or {}):
        services[name] = {"logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}}
    save_json(p.override, {"services": services}, 0o600)


# --------------------------------------------------------------------------- health


def wait_healthy(p: Project, c: dict, cfg: dict, timeout: int) -> tuple:
    services = cfg.get("services") or {}
    oneshot = {s for s, v in services.items() if (v.get("restart") in (None, "", "no"))}
    deadline = time.time() + timeout
    pending: list = []
    while time.time() < deadline:
        rows = compose_ps(p, c)
        bad, pending = [], []
        for r in rows:
            svc = r.get("Service") or r.get("Name")
            state = (r.get("State") or "").lower()
            health = (r.get("Health") or "").lower()
            code = r.get("ExitCode", 0)
            if state == "running":
                if health in ("", "healthy"):
                    continue
                pending.append(f"{svc}: {health or state}")
            elif state == "exited":
                if svc in oneshot:
                    if code == 0:
                        continue
                    bad.append(f"{svc}: exited with code {code}")
                else:
                    pending.append(f"{svc}: exited ({code})")
            else:
                pending.append(f"{svc}: {state}")
        if bad:
            return False, bad
        if rows and not pending:
            return True, []
        time.sleep(3)
    return False, pending or ["no containers were started"]


def _http_status(url: str) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "sdp-healthcheck"})
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return 0


def http_checks(p: Project, c: dict, cfg: dict, timeout: int = 90) -> tuple:
    urls = list(c.get("health_urls") or [])
    strict = bool(urls)
    if not urls:
        ports = sorted({r["port"] for d in p.routes().values() for r in d.get("routes", [])})
        urls = [f"http://127.0.0.1:{port}/" for port in ports]
    failures = []
    for url in urls:
        deadline = time.time() + timeout
        status = 0
        while time.time() < deadline:
            status = _http_status(url)
            if status and (status < 400 if strict else status < 500):
                break
            time.sleep(3)
        if not status or (status >= 400 if strict else status >= 500):
            failures.append(f"{url} -> {status or 'no response'}")
        else:
            ok(f"{url} -> {status}")
    return (not failures), failures


# --------------------------------------------------------------------------- nginx / tls


def nginx_version() -> tuple:
    r = run(["nginx", "-v"], check=False)
    m = re.search(r"nginx/(\d+)\.(\d+)\.(\d+)", (r.stderr or "") + (r.stdout or ""))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def has_ipv6() -> bool:
    try:
        with open("/proc/net/if_inet6") as f:
            return bool(f.read().strip())
    except OSError:
        return False


COMMON_CONF = f"{NGINX_CONF_D}/sdp-00-common.conf"
COMMON_CONF_TEXT = ("# Managed by sdp — shared helpers for sdp sites\n"
                    "map $http_upgrade $sdp_connection_upgrade {\n    default upgrade;\n    ''      close;\n}\n")


def ensure_common_conf():
    try:
        with open(COMMON_CONF) as f:
            if f.read() == COMMON_CONF_TEXT:
                return
    except FileNotFoundError:
        pass
    write_file(COMMON_CONF, COMMON_CONF_TEXT, 0o644)


def site_path(p: Project, domain: str) -> str:
    return f"{NGINX_CONF_D}/sdp-{p.name}--{domain}.conf"


def cert_paths(domain: str) -> tuple:
    return f"{LE_LIVE}/{domain}/fullchain.pem", f"{LE_LIVE}/{domain}/privkey.pem"


def has_cert(domain: str) -> bool:
    full, key = cert_paths(domain)
    return os.path.exists(full) and os.path.exists(key)


def _proxy_block(match: str, target: str) -> str:
    return (
        f"    location {match} {{\n"
        f"        proxy_pass {target};\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "        proxy_set_header X-Forwarded-Host $host;\n"
        "        proxy_set_header Upgrade $http_upgrade;\n"
        "        proxy_set_header Connection $sdp_connection_upgrade;\n"
        "        proxy_read_timeout 300s;\n"
        "        proxy_send_timeout 300s;\n"
        "    }\n"
    )


def render_locations(d: dict) -> str:
    parts = []
    for r in sorted(d.get("routes", []), key=lambda x: len(x["path"]), reverse=True):
        path, port, strip = r["path"], int(r["port"]), bool(r.get("strip"))
        upstream = f"http://127.0.0.1:{port}"
        if path == "/":
            parts.append(_proxy_block("/", upstream))
        elif strip:
            parts.append(f"    location = {path} {{\n        return 308 {path}/$is_args$args;\n    }}\n")
            parts.append(_proxy_block(f"{path}/", upstream + "/"))
        else:
            parts.append(_proxy_block(f"= {path}", upstream))
            parts.append(_proxy_block(f"{path}/", upstream))
    return "".join(parts)


def render_site(p: Project, domain: str, d: dict, with_tls: bool) -> str:
    v6 = has_ipv6()
    max_body = d.get("max_body") or "25m"
    acme = (
        "    location ^~ /.well-known/acme-challenge/ {\n"
        f"        root {ACME_ROOT};\n"
        "        default_type text/plain;\n"
        "    }\n"
    )
    head = (f"# Managed by sdp (project: {p.name}). Do not edit by hand —\n"
            f"# use: sdp domain-add / domain-remove / tls\n")
    http = "server {\n    listen 80;\n"
    if v6:
        http += "    listen [::]:80;\n"
    http += f"    server_name {domain};\n" + acme
    if with_tls:
        http += "    location / {\n        return 301 https://$host$request_uri;\n    }\n}\n"
        full, key = cert_paths(domain)
        if nginx_version() >= (1, 25, 1):
            listen = "    listen 443 ssl;\n" + ("    listen [::]:443 ssl;\n" if v6 else "") + "    http2 on;\n"
        else:
            listen = "    listen 443 ssl http2;\n" + ("    listen [::]:443 ssl http2;\n" if v6 else "")
        https = (
            "\nserver {\n" + listen +
            f"    server_name {domain};\n"
            f"    ssl_certificate {full};\n"
            f"    ssl_certificate_key {key};\n"
            "    ssl_protocols TLSv1.2 TLSv1.3;\n"
            "    ssl_prefer_server_ciphers off;\n"
            "    add_header Strict-Transport-Security \"max-age=31536000\" always;\n"
            f"    client_max_body_size {max_body};\n" +
            acme + render_locations(d) + "}\n"
        )
        return head + http + https
    http += f"    client_max_body_size {max_body};\n" + render_locations(d) + "}\n"
    return head + http


def nginx_test() -> tuple:
    r = run(["nginx", "-t"], check=False)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()


def nginx_reload():
    r = run(["systemctl", "reload", "nginx"], check=False)
    if r.returncode != 0:
        r2 = run(["nginx", "-s", "reload"], check=False)
        if r2.returncode != 0:
            die("nginx reload failed:\n" + (r.stderr or r2.stderr or "").strip())


def apply_site(p: Project, domain: str, d: dict | None):
    """Write (or remove when d is None) a site file atomically; revert if nginx -t fails."""
    path = site_path(p, domain)
    old = None
    if os.path.exists(path):
        with open(path) as f:
            old = f.read()
    ensure_common_conf()
    good_before, msg = nginx_test()
    if not good_before:
        die("nginx configuration on this server is already broken (not by sdp); refusing to touch it:\n" + msg)
    if d is None:
        if old is None:
            return
        os.unlink(path)
    else:
        write_file(path, render_site(p, domain, d, has_cert(domain) and d.get("tls", False)), 0o644)
    good, msg = nginx_test()
    if not good:
        if old is None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        else:
            write_file(path, old, 0o644)
        die("nginx rejected the new site config; the change was reverted:\n" + msg)
    nginx_reload()


def nginx_server_names(exclude_prefix: str) -> dict:
    r = run(["nginx", "-T"], check=False)
    cur = ""
    names: dict = {}
    for line in (r.stdout or "").splitlines():
        m = re.match(r"# configuration file (.+):$", line)
        if m:
            cur = m.group(1)
            continue
        if os.path.basename(cur).startswith(exclude_prefix):
            continue
        code = line.split("#", 1)[0]
        for m in re.finditer(r"(?:^|[\s{;])server_name\s+([^;]+);", code):
            for n in m.group(1).split():
                names.setdefault(n.lower().rstrip("."), set()).add(cur)
    return names


def public_ips() -> set:
    r = run(["ip", "-o", "addr", "show", "scope", "global"], check=False)
    ips = set()
    for m in re.finditer(r"\binet6?\s+([0-9a-fA-F:.]+)/", r.stdout or ""):
        with contextlib.suppress(ValueError):
            ip = ipaddress.ip_address(m.group(1))
            if ip.is_global:
                ips.add(str(ip))
    return ips


def resolve(domain: str) -> set:
    try:
        return {ai[4][0] for ai in socket.getaddrinfo(domain, 80, proto=socket.IPPROTO_TCP)}
    except socket.gaierror:
        return set()


def cert_expiry(domain: str) -> str:
    full, _ = cert_paths(domain)
    if not os.path.exists(full):
        return ""
    r = run(["openssl", "x509", "-enddate", "-noout", "-in", full], check=False)
    return (r.stdout or "").strip().replace("notAfter=", "")


# --------------------------------------------------------------------------- ssh keys


def key_line(kind: str, p: Project, pubkey: str) -> str:
    m = SSH_PUBKEY_RE.match(pubkey.strip())
    if not m:
        die("not a valid SSH public key (expected e.g. 'ssh-ed25519 AAAA... comment')")
    ktype, kb64 = m.group(1), m.group(2)
    if kind == "agent":
        opts = "no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-user-rc"
    elif kind == "ci":
        opts = f'restrict,command="{CI_ENTRY}"'
    else:
        die("unknown key kind")
    return f"{opts} {ktype} {kb64} sdp-{kind}:{p.name}"


def read_auth_keys(p: Project) -> list:
    try:
        with open(p.auth_keys) as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]
    except FileNotFoundError:
        return []


def write_auth_keys(p: Project, lines: list):
    os.makedirs(os.path.dirname(p.auth_keys), exist_ok=True)
    os.chown(os.path.dirname(p.auth_keys), 0, 0)
    os.chmod(os.path.dirname(p.auth_keys), 0o755)
    write_file(p.auth_keys, "\n".join(lines) + ("\n" if lines else ""), 0o644)


def set_key(p: Project, kind: str, pubkey: str | None):
    """Replace all keys of `kind` with pubkey (or remove them when None)."""
    marker = f"sdp-{kind}:{p.name}"
    lines = [ln for ln in read_auth_keys(p) if not ln.endswith(" " + marker)]
    if pubkey:
        lines.append(key_line(kind, p, pubkey))
    write_auth_keys(p, lines)


# --------------------------------------------------------------------------- agent commands


def cmd_status(p: Project, a):
    c, st, routes = p.conf(), p.state(), p.routes()
    out(f"project:       {p.name}  (ssh user {p.user})")
    out(f"repository:    {c.get('repo_url') or '-'}  branch {c.get('branch')}")
    head = repo_head(p)
    out(f"checked out:   {head[:12] or '-'}")
    lg = st.get("last_good") or ""
    out(f"last good:     {lg[:12] or '-'}  {st.get('last_deploy') or ''}")
    if os.path.isdir(p.repo + "/.git"):
        with contextlib.suppress(SdpError):
            out(f"compose file:  {os.path.basename(compose_file(p, c))}")
    out(f"exec allowed:  {'yes' if c.get('allow_exec') else 'no'}")
    out(f"health urls:   {', '.join(c.get('health_urls') or []) or '(default: / on each routed port)'}")
    out("domains:")
    if not routes:
        out("  (none)")
    for dom, d in sorted(routes.items()):
        exp = cert_expiry(dom)
        tls = f"https until {exp}" if (exp and d.get("tls")) else "http only"
        rs = ", ".join(f"{r['path']} -> 127.0.0.1:{r['port']}{' (strip)' if r.get('strip') else ''}"
                       for r in d.get("routes", []))
        out(f"  {dom}  [{tls}]  {rs}")
    if os.path.isdir(p.repo + "/.git"):
        out("containers:")
        with contextlib.suppress(SdpError):
            for r in compose_ps(p, c):
                h = f" ({r.get('Health')})" if r.get("Health") else ""
                out(f"  {r.get('Service', '?'):<16} {r.get('State', '?')}{h}  {r.get('Status', '')}")


def cmd_preflight(p: Project, a):
    osr = {}
    with contextlib.suppress(OSError):
        with open("/etc/os-release") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip().split("=", 1)
                    osr[k] = v.strip('"')
    out(f"os:            {osr.get('PRETTY_NAME', '?')}  kernel {os.uname().release}")
    mem = {}
    with contextlib.suppress(OSError):
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
    out(f"cpu / memory:  {os.cpu_count()} cpu, {mem.get('MemTotal', 0) // 1024} MB total, "
        f"{mem.get('MemAvailable', 0) // 1024} MB available")
    du = shutil.disk_usage("/")
    out(f"disk /:        {du.free // 2**30} GB free of {du.total // 2**30} GB")
    dv = run(["docker", "version", "--format", "{{.Server.Version}}"], check=False)
    cv = run(["docker", "compose", "version", "--short"], check=False)
    out(f"docker:        {dv.stdout.strip() or 'NOT RUNNING/INSTALLED'}  compose {cv.stdout.strip() or 'MISSING'}")
    nv = nginx_version()
    out(f"nginx:         {'.'.join(map(str, nv)) if nv != (0, 0, 0) else 'MISSING'}")
    cb = run(["certbot", "--version"], check=False)
    out(f"certbot:       {((cb.stdout or '') + (cb.stderr or '')).strip() or 'MISSING'}")
    lp = listening_ports()
    for port in (80, 443):
        owners = ",".join(sorted(lp.get(port, []))) or "free"
        out(f"port {port}:      {owners}")
    used = sorted(x for x in lp if PORT_RANGE[0] <= x <= PORT_RANGE[1])
    out(f"used ports in {PORT_RANGE[0]}-{PORT_RANGE[1]}: {', '.join(map(str, used)) or 'none'}")
    out(f"public IPs:    {', '.join(sorted(public_ips())) or 'none detected (NAT?)'}")
    out(f"sdp projects:  {len(all_projects())} on this server")
    if nv != (0, 0, 0):
        good, _ = nginx_test()
        out(f"nginx config:  {'valid' if good else 'BROKEN (fix before adding sites)'}")
    if have("ufw"):
        r = run(["ufw", "status"], check=False)
        out(f"ufw:           {(r.stdout or '').splitlines()[0] if r.stdout else 'unknown'}")


def cmd_port_suggest(p: Project, a):
    used = set(listening_ports()) | set(other_projects_ports(p)) | project_running_ports(p)
    res = []
    for port in range(PORT_RANGE[0], PORT_RANGE[1] + 1):
        if port not in used:
            res.append(port)
        if len(res) >= a.n:
            break
    out(" ".join(map(str, res)))


def cmd_repo_init(p: Project, a):
    url = a.url.strip()
    if not REPO_RE.match(url):
        die("unsupported repository URL. Use git@github.com:owner/repo.git (private) or https://... (public)")
    if a.branch and not BRANCH_RE.match(a.branch):
        die("invalid branch name")
    c = p.conf()
    c["repo_url"] = url
    if a.branch:
        c["branch"] = a.branch
    p.save_conf(c)
    if not os.path.exists(p.deploy_key):
        host = socket.gethostname()
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"sdp-deploy-{p.name}@{host}", "-f", p.deploy_key])
        os.chmod(p.deploy_key, 0o600)
    with open(p.deploy_key + ".pub") as f:
        pub = f.read().strip()
    info(f"repository set to {url} (branch {c['branch']})")
    info("add this PUBLIC key to the repository as a READ-ONLY deploy key "
         "(GitHub: Settings > Deploy keys > Add deploy key, leave 'Allow write access' unchecked):")
    out(pub)


def cmd_sync(p: Project, a):
    c = p.conf()
    with file_lock(p.name):
        head = repo_sync(p, c, None)
    ok(f"repository checked out at {head[:12]} (nothing was deployed)")


def cmd_validate(p: Project, a):
    c = p.conf()
    MASK.load_env(p.env)
    with file_lock(p.name):
        if a.sync:
            repo_sync(p, c, None)
        cfg, rep = validate(p, c, strict_env=True)
    print_report(rep)
    ports = sorted(published_ports(cfg))
    out(f"services: {', '.join(sorted(cfg.get('services') or {}))}")
    out(f"localhost ports: {', '.join(map(str, ports)) or 'none'}")
    if rep.errors:
        die(f"{len(rep.errors)} policy error(s) — fix the compose file in the repo, push, and re-run")
    ok(f"policy check passed ({len(rep.warnings)} warning(s))")


def _compose_up(p: Project, c: dict, cfg: dict, pull: bool) -> bool:
    write_override(p, cfg)
    base = compose_cmd(p, c)
    build = base + ["build"] + (["--pull"] if pull else [])
    info("building images ...")
    if run_stream(build) != 0:
        warn("image build failed")
        return False
    info("starting containers ...")
    if run_stream(base + ["up", "-d", "--remove-orphans"]) != 0:
        warn("docker compose up failed")
        return False
    return True


def _show_failure_logs(p: Project, c: dict, services: list):
    names = sorted({s.split(":")[0].strip() for s in services if s})
    for name in names[:4]:
        r = run(compose_cmd(p, c) + ["logs", "--no-color", "--tail", "60", name], check=False)
        sys.stderr.write(MASK(f"----- last logs: {name} -----\n{(r.stdout or '') + (r.stderr or '')}\n"))


def cmd_deploy(p: Project, a):
    c = p.conf()
    MASK.load_env(p.env)
    with file_lock(p.name):
        st = p.state()
        prev = st.get("last_good") or ""
        before = repo_head(p)
        head = repo_sync(p, c, a.sha)
        info(f"deploying {head[:12]} (previous good: {prev[:12] or 'none'})")
        cfg, rep = validate(p, c, strict_env=True)
        print_report(rep)
        if rep.errors:
            if before:
                repo_checkout(p, before)
            die(f"{len(rep.errors)} policy error(s); nothing was changed on the running app")
        started = _compose_up(p, c, cfg, a.pull)
        healthy, problems = (False, ["build/start failed"])
        if started:
            healthy, problems = wait_healthy(p, c, cfg, int(c.get("wait_timeout") or 300))
            if healthy:
                healthy, problems = http_checks(p, c, cfg)
        if healthy:
            st.update({"last_good": head, "last_deploy": datetime.datetime.now().isoformat(timespec="seconds"),
                       "ports": sorted(published_ports(cfg))})
            p.save_state(st)
            run(["docker", "image", "prune", "-f", "--filter", f"label=com.docker.compose.project={p.cproj}"],
                check=False)
            ok(f"deployed {head[:12]}")
            for dom in sorted(p.routes()):
                out(f"{'https' if has_cert(dom) else 'http'}://{dom}")
            return
        warn("deploy is NOT healthy: " + "; ".join(problems))
        if started:
            _show_failure_logs(p, c, problems)
        if prev and prev != head:
            warn(f"rolling back to {prev[:12]} ...")
            repo_checkout(p, prev)
            rcfg, rrep = validate(p, c, strict_env=False)
            if not rrep.errors and _compose_up(p, c, rcfg, False):
                good, _ = wait_healthy(p, c, rcfg, int(c.get("wait_timeout") or 300))
                if good:
                    die(f"deploy of {head[:12]} failed; rolled back to {prev[:12]} which is running again. "
                        "Note: database migrations are not rolled back automatically.")
            die(f"deploy of {head[:12]} failed AND rollback to {prev[:12]} did not become healthy — check `sdp logs`")
        die(f"deploy of {head[:12]} failed (no previous good version to roll back to) — check `sdp logs`")


def cmd_ps(p: Project, a):
    r = run(compose_cmd(p, p.conf()) + ["ps", "-a"], check=False)
    out((r.stdout or "") + (r.stderr or ""))


def cmd_logs(p: Project, a):
    MASK.load_env(p.env)
    if a.tail < 1 or a.tail > 5000:
        die("--tail must be between 1 and 5000")
    cmd = compose_cmd(p, p.conf()) + ["logs", "--no-color", "--timestamps", "--tail", str(a.tail)]
    if a.since:
        if not re.fullmatch(r"\d+[smhd]", a.since):
            die("--since must look like 10m, 2h, 1d")
        cmd += ["--since", a.since]
    if a.service:
        cmd.append(a.service)
    r = run(cmd, check=False, timeout=120)
    out((r.stdout or "") + (r.stderr or ""))


def cmd_restart(p: Project, a):
    MASK.load_env(p.env)
    with file_lock(p.name):
        rc = run_stream(compose_cmd(p, p.conf()) + ["restart", *a.services])
    if rc != 0:
        die("restart failed")
    ok("restarted")


def cmd_down(p: Project, a):
    MASK.load_env(p.env)
    with file_lock(p.name):
        rc = run_stream(compose_cmd(p, p.conf()) + ["down", "--remove-orphans"])
    if rc != 0:
        die("docker compose down failed")
    ok("stopped (volumes and data are kept)")


def _exec_like(p: Project, a, mode: str):
    c = p.conf()
    if not c.get("allow_exec"):
        die("exec/run is disabled for this project by the server admin (sdp-admin exec <project> on)")
    cmd = list(a.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        die(f"usage: sdp {mode} <service> -- <command> [args...]")
    MASK.load_env(p.env)
    base = compose_cmd(p, c)
    full = base + (["exec", "-T", a.service] if mode == "exec" else ["run", "--rm", "-T", a.service]) + cmd
    r = run(full, check=False, timeout=1800)
    out(((r.stdout or "") + (r.stderr or "")).rstrip())
    if r.returncode != 0:
        die(f"command exited with code {r.returncode}")


def cmd_exec(p, a):
    _exec_like(p, a, "exec")


def cmd_run(p, a):
    _exec_like(p, a, "run")


def find_env_example(p: Project) -> str:
    for n in ENV_EXAMPLE_CANDIDATES:
        path = f"{p.repo}/{n}"
        if os.path.isfile(path):
            return path
    return ""


def cmd_env_status(p: Project, a):
    ensure_env_file(p)
    env = read_env(p.env)
    ex_path = find_env_example(p) if os.path.isdir(p.repo) else ""
    example = read_env(ex_path) if ex_path else {}
    out(f"server .env: {len(env)} variable(s)  (values are never shown)")
    for k in env_keys_in_order(p.env):
        v = env.get(k, "")
        if not v:
            state = "EMPTY"
        elif (k.upper().startswith(PUBLIC_ENV_PREFIXES) or is_template_value(v) or is_credential_free_url(v)
              or (not is_secret_key(k) and len(v) < 40)):
            state = f"set  = {v}"
        else:
            state = f"set  (secret, {len(v)} chars)"
        out(f"  {k:<32} {state}")
    if example:
        missing = [k for k in example if k not in env]
        out(f"keys in {os.path.basename(ex_path)} missing on the server: {', '.join(missing) or 'none'}")
    if os.path.isdir(p.repo + "/.git"):
        with contextlib.suppress(SdpError):
            _, unset = compose_config(p, p.conf())
            out(f"variables used by compose but not set: {', '.join(unset) or 'none'}")


def cmd_env_template(p: Project, a):
    ex_path = find_env_example(p)
    if not ex_path:
        die("no .env.example in the repository root. Add one (keys with placeholder values) and push.")
    ensure_env_file(p)
    env = read_env(p.env)
    example = read_env(ex_path)
    added, secret_keys = [], []
    for k, v in example.items():
        if k in env:
            continue
        if is_secret_key(k):
            set_env_key(p.env, k, "")
            secret_keys.append(k)
        else:
            set_env_key(p.env, k, v)
        added.append(k)
    ok(f"added {len(added)} key(s) from {os.path.basename(ex_path)}")
    if secret_keys:
        info("these secret keys were added EMPTY — fill them with `sdp env-gen KEY` (random) or ask the human "
             "to run `sdp-admin secret <project> KEY` (external API keys): " + ", ".join(secret_keys))


def cmd_env_gen(p: Project, a):
    if not ENV_KEY_RE.match(a.key):
        die("invalid variable name")
    if a.bytes < 16 or a.bytes > 128:
        die("--bytes must be 16..128")
    env = read_env(p.env)
    if env.get(a.key) and not a.force:
        die(f"{a.key} already has a value (use --force to rotate it; services must be redeployed afterwards)")
    ensure_env_file(p)
    set_env_key(p.env, a.key, secrets.token_hex(a.bytes))
    ok(f"{a.key} set to a new random value ({a.bytes * 2} hex chars, not shown)")


def cmd_env_set(p: Project, a):
    if not ENV_KEY_RE.match(a.key):
        die("invalid variable name")
    if is_secret_key(a.key) and not is_template_value(a.value) and not is_credential_free_url(a.value):
        die(f"{a.key} looks like a secret, so it cannot be set by the agent. Use `sdp env-gen {a.key}` for a "
            f"random secret, compose it from other variables (e.g. postgresql://app:${{POSTGRES_PASSWORD}}@db/app), "
            f"or ask the human to run: sdp-admin secret {p.name} {a.key}")
    ensure_env_file(p)
    set_env_key(p.env, a.key, a.value)
    ok(f"{a.key} set")


def cmd_env_unset(p: Project, a):
    if unset_env_key(p.env, a.key):
        ok(f"{a.key} removed")
    else:
        info(f"{a.key} was not set")


CONFIG_KEYS_AGENT = {"compose_file", "branch", "le_email", "health_urls", "wait_timeout"}


def cmd_config(p: Project, a, is_agent: bool):
    c = p.conf()
    if a.action == "show" or not a.action:
        out(json.dumps(c, indent=2, sort_keys=True))
        return
    key, value = a.key, a.value
    allowed = CONFIG_KEYS_AGENT | ({"allow_exec"} if not is_agent else set())
    if key not in allowed:
        die(f"config key must be one of: {', '.join(sorted(allowed))}")
    if key == "compose_file":
        if "/" in value or value.startswith("."):
            die("compose_file must be a file name in the repository root")
    elif key == "branch":
        if not BRANCH_RE.match(value):
            die("invalid branch")
    elif key == "le_email":
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            die("invalid email")
    elif key == "health_urls":
        urls = [u.strip() for u in value.split(",") if u.strip()]
        for u in urls:
            if not re.fullmatch(r"http://127\.0\.0\.1:\d{4,5}(/[A-Za-z0-9._~/?=&%-]*)?", u):
                die(f"health url must look like http://127.0.0.1:<port>/health (got {u})")
        value = urls
    elif key == "wait_timeout":
        if not value.isdigit() or not 30 <= int(value) <= 1800:
            die("wait_timeout must be 30..1800 seconds")
        value = int(value)
    elif key == "allow_exec":
        value = value.lower() in ("1", "true", "yes", "on")
    c[key] = value
    p.save_conf(c)
    ok(f"{key} updated")


def cmd_domains(p: Project, a):
    routes = p.routes()
    if not routes:
        out("(no domains)")
    for dom, d in sorted(routes.items()):
        out(f"{dom}  tls={'yes' if d.get('tls') and has_cert(dom) else 'no'}  max_body={d.get('max_body', '25m')}")
        for r in d.get("routes", []):
            out(f"   {r['path']:<12} -> 127.0.0.1:{r['port']}{'  (prefix stripped)' if r.get('strip') else ''}")


def cmd_domain_add(p: Project, a):
    domain = a.domain.lower().rstrip(".")
    if not DOMAIN_RE.match(domain):
        die(f"invalid domain: {a.domain}")
    if not 1024 <= a.port <= 65535:
        die("port must be 1024..65535")
    path = a.path or "/"
    if not re.fullmatch(r"/[A-Za-z0-9._~/-]*", path) or "//" in path or "/../" in path + "/":
        die("invalid --path")
    if path != "/":
        path = path.rstrip("/")
    if a.max_body and not re.fullmatch(r"\d{1,5}[kKmMgG]?", a.max_body):
        die("--max-body must look like 25m, 1g")
    c = p.conf()
    cfg, _ = compose_config(p, c)
    ports = published_ports(cfg)
    if a.port not in ports:
        die(f"port {a.port} is not published by this project's compose file. Published localhost ports: "
            f"{', '.join(map(str, sorted(ports))) or 'none'}. Add e.g. \"127.0.0.1:{a.port}:8000\" to the service, "
            f"push, run `sdp deploy`, then retry.")
    for other in all_projects():
        if other == p.name:
            continue
        if domain in load_json(f"{PROJECTS_ETC}/{other}/routes.json", {}):
            die(f"{domain} is already used by another project on this server")
    used = nginx_server_names(f"sdp-{p.name}--")
    if domain in used:
        die(f"{domain} is already served by another nginx site on this server ({', '.join(sorted(used[domain]))}). "
            "Ask the server owner.")
    for n in used:
        if n.startswith("*.") and domain.endswith(n[1:]):
            warn(f"another site has a wildcard server_name {n}; this exact domain will take precedence")
    if not a.skip_dns:
        got, mine = resolve(domain), public_ips()
        if not got:
            die(f"{domain} does not resolve yet. Create a DNS A record pointing to {', '.join(sorted(mine)) or 'this server'}"
                ", wait a few minutes, then retry (or use --skip-dns).")
        if mine and not (got & mine):
            die(f"{domain} resolves to {', '.join(sorted(got))} but this server is {', '.join(sorted(mine))}. "
                "Fix the A record (if it is behind Cloudflare proxy, use --skip-dns).")
    routes = p.routes()
    d = routes.get(domain) or {"routes": [], "max_body": "25m", "tls": False}
    d["routes"] = [r for r in d["routes"] if r["path"] != path] + [{"path": path, "port": a.port, "strip": a.strip}]
    if a.max_body:
        d["max_body"] = a.max_body
    with file_lock("nginx"):
        apply_site(p, domain, d)
    routes[domain] = d
    p.save_routes(routes)
    ok(f"{'https' if d.get('tls') and has_cert(domain) else 'http'}://{domain}{'' if path == '/' else path} -> 127.0.0.1:{a.port}")
    if not d.get("tls"):
        info(f"next: sdp tls {domain}")


def cmd_domain_remove(p: Project, a):
    domain = a.domain.lower().rstrip(".")
    routes = p.routes()
    if domain not in routes:
        die(f"{domain} is not configured for this project")
    if a.path:
        path = a.path.rstrip("/") or "/"
        d = routes[domain]
        d["routes"] = [r for r in d["routes"] if r["path"] != path]
        with file_lock("nginx"):
            apply_site(p, domain, d if d["routes"] else None)
        if d["routes"]:
            routes[domain] = d
        else:
            routes.pop(domain)
    else:
        with file_lock("nginx"):
            apply_site(p, domain, None)
        routes.pop(domain)
    p.save_routes(routes)
    ok("removed")


def cmd_tls(p: Project, a):
    domain = a.domain.lower().rstrip(".")
    routes = p.routes()
    if domain not in routes:
        die(f"add the domain first: sdp domain-add {domain} <port>")
    c = p.conf()
    email = c.get("le_email")
    if not email:
        die("set a Let's Encrypt contact email first (ask the human): sdp config set le_email you@example.com")
    if not have("certbot"):
        die("certbot is not installed (run install.sh again as root)")
    os.makedirs(f"{ACME_ROOT}/.well-known/acme-challenge", exist_ok=True)
    d = routes[domain]
    with file_lock("nginx"):
        apply_site(p, domain, d)
    token = secrets.token_hex(8)
    probe = f"{ACME_ROOT}/.well-known/acme-challenge/sdp-probe-{token}"
    write_file(probe, token, 0o644)
    status = _http_status(f"http://{domain}/.well-known/acme-challenge/sdp-probe-{token}")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(probe)
    if status != 200:
        warn(f"self-check of http://{domain}/.well-known/acme-challenge/ returned {status or 'no response'} "
             "(can be a false alarm on some networks; continuing with a Let's Encrypt dry run)")
    base = ["certbot", "certonly", "--webroot", "-w", ACME_ROOT, "--cert-name", domain, "-d", domain,
            "--non-interactive", "--agree-tos", "-m", email]
    info("Let's Encrypt dry run ...")
    r = run(base + ["--dry-run"], check=False, timeout=300)
    if r.returncode != 0:
        die("Let's Encrypt dry run failed (no certificate was requested, no rate limit used):\n" +
            (r.stderr or r.stdout or "").strip()[-3000:] +
            "\nCheck: DNS A record, port 80 open in any firewall (Contabo has none by default; check ufw), "
            "and that nothing else answers on port 80.")
    info("requesting the real certificate ...")
    r = run(base + ["--keep-until-expiring", "--deploy-hook", "systemctl reload nginx"], check=False, timeout=300)
    if r.returncode != 0 or not has_cert(domain):
        die("certbot failed:\n" + (r.stderr or r.stdout or "").strip()[-3000:])
    d["tls"] = True
    with file_lock("nginx"):
        apply_site(p, domain, d)
    routes[domain] = d
    p.save_routes(routes)
    ok(f"https://{domain} is live (certificate valid until {cert_expiry(domain)}; auto-renewal by certbot timer)")


def cmd_ci_key(p: Project, a):
    if sys.stdout.isatty() and not a.show:
        die("refusing to print a private key to a terminal. Pipe it straight into GitHub instead:\n"
            "  ssh <server> sudo sdp ci-key | gh secret set SDP_SSH_KEY -R <owner>/<repo>\n"
            "(or add --show if a human copies it by hand)")
    with tempfile.TemporaryDirectory() as td:
        k = f"{td}/k"
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"sdp-ci:{p.name}", "-f", k])
        with open(k + ".pub") as f:
            pub = f.read().strip()
        with open(k) as f:
            priv = f.read()
    set_key(p, "ci", pub)
    sys.stdout.write(priv)
    sys.stdout.flush()
    info("new CI deploy key installed (any previous CI key was revoked). It can ONLY run `sdp deploy` for "
         f"project {p.name}. Store it as the GitHub secret SDP_SSH_KEY.")


def cmd_host_keys(p: Project, a):
    ips = sorted(public_ips())
    if not ips:
        die("no public IP detected on this server")
    lines = []
    for f in sorted(os.listdir("/etc/ssh")):
        if f.startswith("ssh_host_") and f.endswith("_key.pub"):
            with open(f"/etc/ssh/{f}") as fh:
                parts = fh.read().split()
            if len(parts) >= 2:
                for ip in ips:
                    host = ip if ":" not in ip else f"[{ip}]"
                    lines.append(f"{host} {parts[0]} {parts[1]}")
    out("\n".join(lines))


# --------------------------------------------------------------------------- entry: sdp


def resolve_project(arg_project: str | None) -> tuple:
    if os.geteuid() != 0:
        die("run me with sudo:  sudo sdp <command>")
    sudo_user = os.environ.get("SUDO_USER") or ""
    m = re.fullmatch(r"sdp-([a-z][a-z0-9-]{1,30}[a-z0-9])", sudo_user)
    if m:
        name = m.group(1)
        if arg_project and arg_project != name:
            die(f"you can only manage your own project ({name})")
        p = Project(name)
        p.require()
        return p, True
    if not arg_project:
        names = all_projects()
        die("as root, choose a project with -p <name>. Projects: " + (", ".join(names) or "none"))
    p = Project(arg_project)
    p.require()
    return p, False


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sdp", description="Safe Deploy Proxy — deploy one docker-compose project "
                                 "on a shared server without root access.")
    ap.add_argument("-p", "--project", help="project name (root only; agents are bound to their own project)")
    ap.add_argument("--version", action="version", version=f"sdp {VERSION}")
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("status", help="summary of repo, containers, domains, TLS")
    sp.add_parser("preflight", help="read-only server facts (OS, docker, nginx, ports, disk)")
    s = sp.add_parser("port-suggest", help="suggest free localhost ports for this project")
    s.add_argument("-n", type=int, default=3)

    s = sp.add_parser("repo-init", help="set the git repository and print the deploy key to add on GitHub")
    s.add_argument("url")
    s.add_argument("--branch")
    sp.add_parser("sync", help="clone/fetch and check out the branch (does not deploy)")
    s = sp.add_parser("validate", help="run the security policy check on the compose file")
    s.add_argument("--sync", action="store_true", help="fetch the latest commit first")

    s = sp.add_parser("deploy", help="sync, validate, build, start, health-check; auto-rollback on failure")
    s.add_argument("--sha", help="deploy this exact commit (must be on the branch)")
    s.add_argument("--pull", action="store_true", help="also pull newer base images")

    sp.add_parser("ps", help="list containers")
    s = sp.add_parser("logs", help="show logs (secrets are masked)")
    s.add_argument("service", nargs="?")
    s.add_argument("--tail", type=int, default=200)
    s.add_argument("--since")
    s = sp.add_parser("restart", help="restart services")
    s.add_argument("services", nargs="*")
    sp.add_parser("down", help="stop and remove containers (volumes/data are kept)")
    for name in ("exec", "run"):
        s = sp.add_parser(name, help=f"docker compose {name} in a service (output masked)")
        s.add_argument("service")
        s.add_argument("cmd", nargs=argparse.REMAINDER)

    sp.add_parser("env-status", help="list .env keys (no secret values) and what is missing")
    sp.add_parser("env-template", help="add missing keys from .env.example (secrets left empty)")
    s = sp.add_parser("env-gen", help="set KEY to a strong random value without showing it")
    s.add_argument("key")
    s.add_argument("--bytes", type=int, default=32)
    s.add_argument("--force", action="store_true")
    s = sp.add_parser("env-set", help="set a NON-secret variable")
    s.add_argument("key")
    s.add_argument("value")
    s = sp.add_parser("env-unset", help="remove a variable")
    s.add_argument("key")

    s = sp.add_parser("config", help="show or set project settings")
    s.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")

    sp.add_parser("domains", help="list domains and routes")
    s = sp.add_parser("domain-add", help="route a domain (or a path on it) to a localhost port of this project")
    s.add_argument("domain")
    s.add_argument("port", type=int)
    s.add_argument("--path", default="/")
    s.add_argument("--strip", action="store_true", help="strip the path prefix before proxying")
    s.add_argument("--max-body", help="max upload size, e.g. 100m")
    s.add_argument("--skip-dns", action="store_true")
    s = sp.add_parser("domain-remove", help="remove a domain (or one path route)")
    s.add_argument("domain")
    s.add_argument("--path")
    s = sp.add_parser("tls", help="get a Let's Encrypt certificate and enable https")
    s.add_argument("domain")

    s = sp.add_parser("ci-key", help="create a deploy-only SSH key for GitHub Actions (prints PRIVATE key)")
    s.add_argument("--show", action="store_true")
    sp.add_parser("host-keys", help="print known_hosts lines for this server")
    return ap


def main_sdp(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        p, is_agent = resolve_project(a.project)
        MASK.load_env(p.env)
        if a.cmd == "config":
            if a.action == "set" and (not a.key or a.value is None):
                die("usage: sdp config set <key> <value>")
            cmd_config(p, a, is_agent)
            return 0
        handlers = {
            "status": cmd_status, "preflight": cmd_preflight, "port-suggest": cmd_port_suggest,
            "repo-init": cmd_repo_init, "sync": cmd_sync, "validate": cmd_validate, "deploy": cmd_deploy,
            "ps": cmd_ps, "logs": cmd_logs, "restart": cmd_restart, "down": cmd_down,
            "exec": cmd_exec, "run": cmd_run,
            "env-status": cmd_env_status, "env-template": cmd_env_template, "env-gen": cmd_env_gen,
            "env-set": cmd_env_set, "env-unset": cmd_env_unset,
            "domains": cmd_domains, "domain-add": cmd_domain_add, "domain-remove": cmd_domain_remove,
            "tls": cmd_tls, "ci-key": cmd_ci_key, "host-keys": cmd_host_keys,
        }
        handlers[a.cmd](p, a)
        return 0
    except SdpError as e:
        _err("[sdp] ERROR ", str(e))
        return 1
    except BrokenPipeError:
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        return 130


# --------------------------------------------------------------------------- entry: sdp-admin (root only)


def require_real_root():
    if os.geteuid() != 0:
        die("sdp-admin must be run as root")
    if re.fullmatch(r"sdp-.+", os.environ.get("SUDO_USER") or ""):
        die("sdp-admin cannot be used by project users")


def admin_add_project(a):
    p = Project(a.name)
    if p.exists():
        die(f"project {p.name} already exists (use grant-agent to add a key)")
    key_line("agent", p, a.agent_key)  # validates
    try:
        pwd.getpwnam(p.user)
        die(f"system user {p.user} already exists")
    except KeyError:
        pass
    run(["useradd", "--home-dir", p.home, "--no-create-home", "--shell", "/bin/bash", "--user-group",
         "--comment", f"sdp agent for {p.name}", p.user])
    run(["usermod", "-p", "*", p.user])
    os.makedirs(p.home, exist_ok=True)
    os.chown(p.home, 0, 0)
    os.chmod(p.home, 0o755)
    for denyfile in ("/etc/cron.deny", "/etc/at.deny"):
        with contextlib.suppress(OSError):
            with open(denyfile, "a") as f:
                f.write(p.user + "\n")
    gid = p.gid()
    os.makedirs(PROJECTS_ETC, mode=0o700, exist_ok=True)
    os.makedirs(p.etc, mode=0o700, exist_ok=True)
    os.makedirs(APPS_ROOT, exist_ok=True)
    os.chmod(APPS_ROOT, 0o711)
    os.makedirs(p.dir, exist_ok=True)
    os.chown(p.dir, 0, gid)
    os.chmod(p.dir, 0o750)
    os.makedirs(p.data, exist_ok=True)
    ensure_env_file(p)
    p.save_conf(dict(DEFAULT_CONF))
    sudoers = (f"# Managed by sdp: {p.user} may only run the sdp broker for project {p.name}\n"
               f"Defaults:{p.user} env_reset, !requiretty\n"
               f"{p.user} ALL=(root) NOPASSWD: {SDP_BIN}, {SDP_BIN} *\n")
    tmp = f"/etc/sudoers.d/.sdp-{p.name}.tmp"
    write_file(tmp, sudoers, 0o440)
    chk = run(["visudo", "-cf", tmp], check=False)
    if chk.returncode != 0:
        os.unlink(tmp)
        die("sudoers validation failed: " + chk.stdout + chk.stderr)
    os.replace(tmp, f"/etc/sudoers.d/sdp-{p.name}")
    set_key(p, "agent", a.agent_key)
    ok(f"project '{p.name}' created")
    ips = sorted(public_ips())
    out(f"\nThe AI agent can now connect with:\n  ssh -i <its private key> {p.user}@{ips[0] if ips else '<server-ip>'}"
        f"\nand run only:  sudo sdp <command>   (try: sudo sdp preflight)\n"
        f"\nWhen the setup is finished, revoke the agent's access:\n  sdp-admin revoke-agent {p.name}\n")


def admin_grant_agent(a):
    p = Project(a.name)
    p.require()
    set_key(p, "agent", a.agent_key)
    ok(f"agent key installed for {p.name}")


def admin_revoke_agent(a):
    p = Project(a.name)
    p.require()
    set_key(p, "agent", None)
    for cmd in (["crontab", "-r", "-u", p.user], ["loginctl", "disable-linger", p.user],
                ["pkill", "-KILL", "-u", p.user]):
        if have(cmd[0]):
            run(cmd, check=False)
    ok(f"agent access for {p.name} revoked (the CI deploy key, if any, still works)")


def admin_secret(a):
    p = Project(a.name)
    p.require()
    if not ENV_KEY_RE.match(a.key):
        die("invalid variable name")
    v1 = getpass.getpass(f"value for {a.key} (input hidden): ")
    if not v1:
        die("empty value, nothing changed")
    v2 = getpass.getpass("repeat: ")
    if v1 != v2:
        die("values do not match, nothing changed")
    ensure_env_file(p)
    set_env_key(p.env, a.key, v1)
    ok(f"{a.key} saved ({len(v1)} chars). Redeploy for it to take effect:  sdp -p {p.name} deploy")


def admin_env_edit(a):
    p = Project(a.name)
    p.require()
    ensure_env_file(p)
    editor = os.environ.get("EDITOR") or ("nano" if have("nano") else "vi")
    subprocess.call([editor, p.env])
    os.chown(p.env, 0, 0)
    os.chmod(p.env, 0o600)
    ok("saved. Redeploy for changes to take effect")


def admin_exec(a):
    p = Project(a.name)
    p.require()
    c = p.conf()
    c["allow_exec"] = a.state == "on"
    p.save_conf(c)
    ok(f"exec/run {'enabled' if c['allow_exec'] else 'disabled'} for {p.name}")


def admin_list(a):
    names = all_projects()
    if not names:
        out("(no projects)")
    for n in names:
        p = Project(n)
        keys = read_auth_keys(p)
        agent = any(k.endswith(f" sdp-agent:{n}") for k in keys)
        ci = any(k.endswith(f" sdp-ci:{n}") for k in keys)
        st = p.state()
        out(f"{n:<20} agent={'ACTIVE' if agent else 'revoked'}  ci={'yes' if ci else 'no'}  "
            f"domains={','.join(sorted(p.routes())) or '-'}  last_good={str(st.get('last_good', ''))[:12] or '-'}")


def admin_remove_project(a):
    p = Project(a.name)
    p.require()
    typed = input(f"Type the project name '{p.name}' to confirm removal"
                  f"{' INCLUDING ALL DATA (volumes, .env, certificates)' if a.purge else ''}: ")
    if typed.strip() != p.name:
        die("not confirmed")
    c = p.conf()
    if os.path.isdir(p.repo + "/.git"):
        with contextlib.suppress(SdpError):
            run(compose_cmd(p, c) + ["down", "--remove-orphans"] + (["--volumes"] if a.purge else []), check=False,
                timeout=600)
    routes = p.routes()
    with file_lock("nginx"):
        for dom in routes:
            with contextlib.suppress(SdpError, OSError):
                apply_site(p, dom, None)
    if a.purge:
        for dom in routes:
            run(["certbot", "delete", "--non-interactive", "--cert-name", dom], check=False)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(f"/etc/sudoers.d/sdp-{p.name}")
    if have("pkill"):
        run(["pkill", "-KILL", "-u", p.user], check=False)
    run(["userdel", p.user], check=False)
    shutil.rmtree(p.home, ignore_errors=True)
    shutil.rmtree(p.etc, ignore_errors=True)
    if a.purge:
        shutil.rmtree(p.dir, ignore_errors=True)
    ok(f"project {p.name} removed" + ("" if a.purge else f" (data kept in {p.dir})"))


def main_admin(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sdp-admin", description="Root-only administration for sdp projects.")
    ap.add_argument("--version", action="version", version=f"sdp {VERSION}")
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("add-project", help="create an isolated project + agent login")
    s.add_argument("name")
    s.add_argument("--agent-key", required=True, help="the AI agent's SSH PUBLIC key (one line)")
    s = sp.add_parser("grant-agent", help="(re)install an agent SSH key")
    s.add_argument("name")
    s.add_argument("--agent-key", required=True)
    s = sp.add_parser("revoke-agent", help="remove the agent's SSH access (CI key keeps working)")
    s.add_argument("name")
    s = sp.add_parser("secret", help="type a secret value into the project's .env (hidden input)")
    s.add_argument("name")
    s.add_argument("key")
    s = sp.add_parser("env-edit", help="open the project's .env in an editor")
    s.add_argument("name")
    s = sp.add_parser("exec", help="allow/deny `sdp exec/run` for the agent")
    s.add_argument("name")
    s.add_argument("state", choices=["on", "off"])
    sp.add_parser("list", help="list projects and access state")
    s = sp.add_parser("remove-project", help="remove a project (keeps data unless --purge)")
    s.add_argument("name")
    s.add_argument("--purge", action="store_true")
    a = ap.parse_args(argv)
    try:
        require_real_root()
        {
            "add-project": admin_add_project, "grant-agent": admin_grant_agent, "revoke-agent": admin_revoke_agent,
            "secret": admin_secret, "env-edit": admin_env_edit, "exec": admin_exec, "list": admin_list,
            "remove-project": admin_remove_project,
        }[a.cmd](a)
        return 0
    except SdpError as e:
        _err("[sdp-admin] ERROR ", str(e))
        return 1
    except KeyboardInterrupt:
        return 130
