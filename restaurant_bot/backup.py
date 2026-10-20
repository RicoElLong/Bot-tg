"""Online SQLite backup; restoration to a NEW file only, with sends quarantined."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from dotenv import load_dotenv
from .config import Config


def backup_database(source: Path, destination: Path):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise ValueError("База не найдена")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation also prevents accidental overwrite or symlink replacement.
    with destination.open("xb"):
        pass
    if os.name != "nt":
        os.chmod(destination, 0o600)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Повреждённая резервная копия")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def restore_database(source: Path, destination: Path):
    backup_database(source, destination)
    with closing(sqlite3.connect(destination)) as conn:
        with conn:
            conn.execute("UPDATE recipients SET status='skipped',error='restore_quarantine' WHERE status IN ('pending','sending')")
            conn.execute("UPDATE messages SET status='uncertain',error='restore_quarantine' WHERE status IN ('sending','prepared')")
            conn.execute("UPDATE campaigns SET state='cancelled' WHERE state IN ('queued','running')")


def main():
    load_dotenv(override=False)
    parser = argparse.ArgumentParser(description="Локальная копия SQLite, без Telegram API")
    parser.add_argument("action", choices=["backup", "restore"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    cfg = Config.load()
    if args.action == "backup":
        destination = args.destination or cfg.backups / (datetime.now(timezone.utc).strftime("restaurant-%Y%m%d-%H%M%S-%f") + ".sqlite3")
        backup_database(args.source or cfg.database, destination)
        print("Резервная копия создана. Она содержит персональные данные — храните защищённо.")
    else:
        if not args.source or not args.destination:
            parser.error("Для restore нужны --source и --destination (НОВЫЙ файл)")
        restore_database(args.source, args.destination)
        print("Восстановлено в новый файл. Очередь отключена. До запуска повторно примените удаления/отписки после даты копии.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise SystemExit("Ошибка копирования/восстановления: " + type(exc).__name__) from None
