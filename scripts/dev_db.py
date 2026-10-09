"""
A local, throwaway Lab4 database for developing against: PostgreSQL 16 from conda-forge (through pixi) listening on
127.0.0.1 only, built from a Lab4 checkout's development fixtures and migrations, with one lab user provisioned
the way the Lab4 website provisions one.

    python scripts/dev_db.py up       # create on first run, then start
    python scripts/dev_db.py down     # stop
    python scripts/dev_db.py reset    # stop, delete, create and start again

.dev/secrets.json is that lab user's credentials file, the same shape the website hands out; point the app at it
with LAB4_SECRETS_PATH. .dev/admin.json is the local superuser, for tests to clean up after themselves.
"""

import argparse
import json
import secrets
import shutil
import subprocess
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / ".dev"
CLUSTER = DEV / "postgres"
LOG = DEV / "postgres.log"
ADMIN = DEV / "admin.json"
SECRETS = DEV / "secrets.json"
HOST = "127.0.0.1"
PORT = 55433
SPEC = "postgresql=16.*"
LAB_USER = "lab4_u_1"

# names from lab4.tables lookups, so the Metadata page has realistic choices
EXPERIMENTERS = ["Gergely Turi", "Eunji Kong", "Yu Xin", "Asako Noguchi"]
DEVICES = [("Bruker 1 Imaging", "imaging"), ("Bruker 2 Imaging", "imaging"), ("AOD 1 Imaging", "imaging"), ("Mini2p Imaging", "imaging")]


def pg(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["pixi", "exec", "-s", SPEC, "--", *args], check=check, capture_output=True, text=True)


def connect(credentials: dict, user_key: str = "DB_USER") -> psycopg.Connection:
    return psycopg.connect(
        host=credentials["DB_HOST"],
        port=credentials["DB_PORT"],
        user=credentials[user_key],
        password=credentials["DB_PASSWORD"],
        dbname=credentials.get("DB_DATABASE", "postgres"),
        autocommit=True,
    )


def write_json(path: Path, values: dict) -> None:
    path.write_text(json.dumps(values, indent=2) + "\n")


def running() -> bool:
    return pg("pg_ctl", "status", "-D", str(CLUSTER), check=False).returncode == 0


def start() -> None:
    if not running():
        # the server inherits pg_ctl's handles; captured pipes would never reach EOF on Windows
        command = ["pixi", "exec", "-s", SPEC, "--", "pg_ctl", "start", "-w", "-D", str(CLUSTER), "-l", str(LOG), "-o", f"-h {HOST} -p {PORT}"]
        subprocess.run(command, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop() -> None:
    if CLUSTER.is_dir() and running():
        pg("pg_ctl", "stop", "-w", "-D", str(CLUSTER), "-m", "fast")


def create(lab4: Path) -> None:
    DEV.mkdir(exist_ok=True)
    password = secrets.token_urlsafe(32)
    pwfile = DEV / "pwfile"
    pwfile.write_text(password + "\n")
    try:
        pg("initdb", "-D", str(CLUSTER), "-U", "postgres", "-E", "UTF8", "--auth=scram-sha-256", f"--pwfile={pwfile}")
    finally:
        pwfile.unlink()
    admin = {"DB_BACKEND": "postgresql", "DB_HOST": HOST, "DB_PORT": PORT, "DB_USER": "postgres", "DB_PASSWORD": password, "DB_NAME": "lab4", "DB_DATABASE": "postgres"}
    write_json(ADMIN, admin)
    start()
    with connect(admin) as conn:
        conn.execute((lab4 / "ops" / "dev" / "001-fixtures.sql").read_text())
        for name in EXPERIMENTERS:
            conn.execute('INSERT INTO lab4."#experimenter" (experimenter) VALUES (%s)', (name,))
        for name, kind in DEVICES:
            conn.execute('INSERT INTO lab4."#device" (device_name, device_type) VALUES (%s, %s)', (name, kind))
    # --secrets is always explicit: migrate.py defaults to ~/.lab4/secrets.json, which may be production
    subprocess.run(
        ["uv", "run", "--no-project", "--with", "psycopg2-binary==2.9.10", "python", str(lab4 / "scripts" / "migrate.py"), "--secrets", str(ADMIN)],
        check=True,
    )
    lab_password = secrets.token_urlsafe(32)
    with connect(admin) as conn:
        conn.execute("SELECT lab4_ops.provision_lab_user(%s, %s)", (LAB_USER, lab_password))
    write_json(
        SECRETS,
        {"DB_BACKEND": "postgresql", "DB_HOST": HOST, "DB_NAME": "lab4", "DB_PORT": PORT, "BIOHPC_MOUNT": "/biohpc", "SCHEMA_CACHE_TTL": 5, "DB_USER": LAB_USER, "DB_PASSWORD": lab_password},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["up", "down", "reset"])
    parser.add_argument("--lab4", type=Path, default=Path.home() / "repos" / "lab4", help="a Lab4 checkout to take fixtures and migrations from")
    args = parser.parse_args()
    if args.command in ("down", "reset"):
        stop()
    if args.command == "reset":
        shutil.rmtree(DEV, ignore_errors=True)
    if args.command in ("up", "reset"):
        if CLUSTER.is_dir():
            start()
        else:
            create(args.lab4.expanduser())
        print(f"Lab4 dev database on {HOST}:{PORT}; set LAB4_SECRETS_PATH={SECRETS}")


if __name__ == "__main__":
    main()
