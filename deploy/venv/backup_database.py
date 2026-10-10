"""Run pg_dump against DATABASE_URL without putting credentials in argv/logs."""
import os
import subprocess
import sys

from psycopg import pq
from psycopg.conninfo import conninfo_to_dict


def dump_environment(database_url):
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise ValueError("Backup requires a PostgreSQL DATABASE_URL.")
    try:
        parameters = conninfo_to_dict(database_url)
    except Exception:
        # Parsing errors can contain parts of the original secret URL.
        raise ValueError("Cannot parse the PostgreSQL DATABASE_URL for backup.") from None
    variable_names = {
        option.keyword.decode(): option.envvar.decode()
        for option in pq.Conninfo.get_defaults() if option.envvar
    }
    # Do not let inherited PGHOST/PGDATABASE silently select a different DB.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment["PGCONNECT_TIMEOUT"] = "10"
    for key, value in parameters.items():
        if key not in variable_names:
            raise ValueError(f"Unsupported database backup connection parameter: {key}")
        environment[variable_names[key]] = value
    return environment


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: backup_database.py OUTPUT_FILE")
    try:
        environment = dump_environment(os.environ.get("DATABASE_URL", ""))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    result = subprocess.run(
        ["pg_dump", "--no-password", "--format=custom", "--file=" + sys.argv[1]],
        env=environment,
        check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
