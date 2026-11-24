"""Durable human-readable identifiers, scoped to one MAGOH archive."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import time
import unicodedata

TABLES = {
    'magoh_scheda_intervento': 'idscheda',
    'magoh_ritrovamenti': 'id',
    'magoh_evidenza_archeologica': 'id',
    'magoh_evidenza_archeologica_no_geom': 'id',
}
STOP = set("a ad al allo alla ai agli alle da dal dallo dalla dai dagli dalle de di del dello della dei degli delle d e ed il lo la i gli le l in nel nello nella nei negli nelle su sul sullo sulla sui sugli sulle con per tra fra un uno una".split())


SUFFIX = dict(zip(TABLES, ("", "R", "E", "E")))
NEW_RECORD = 'nuova scheda (codice al salvataggio)'


def shown_id(key, new_record=NEW_RECORD):
    """QGIS expression of the visible identifier: local code, else MAGOH ID.

    A feature not yet saved has neither: its negative technical key is never shown.
    """
    new_record = new_record.replace("'", "''")
    return (f'CASE WHEN "codice_locale" IS NOT NULL THEN "codice_locale" '
            f"WHEN $id < 0 THEN '{new_record}' "
            f'ELSE to_string("{key}") END')


def suggest_prefix(name):
    plain = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    words = [w.upper() for w in re.findall('[a-zA-Z]+', plain) if w.lower() not in STOP]
    if len(words) == 1:
        return words[0][:3]
    return ''.join(w[0] for w in words)[:5]


def validate(prefix):
    if not re.fullmatch('[A-Z]{2,5}', prefix):
        raise ValueError('Il prefisso deve contenere da 2 a 5 lettere A–Z.')


def install(path, prefix, backup_dir=None):
    with closing(sqlite3.connect(path, timeout=8)) as db, db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='magoh_code_settings'").fetchone():
            return False
        validate(prefix)
        if backup_dir:
            folder = Path(backup_dir)
            folder.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(folder / f'prima_codici_{time.time_ns()}.gpkg')) as dst:
                db.backup(dst)
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='magoh_code_settings'").fetchone():
            return False
        db.execute('CREATE TABLE magoh_code_settings (singleton INTEGER PRIMARY KEY CHECK(singleton=1), prefix TEXT NOT NULL)')
        db.execute('INSERT INTO magoh_code_settings VALUES (1,?)', (prefix,))
        db.execute('CREATE TABLE magoh_code_ledger (kind TEXT NOT NULL, seq INTEGER NOT NULL, code TEXT UNIQUE NOT NULL, origin TEXT NOT NULL UNIQUE, owner_table TEXT NOT NULL, owner_fid INTEGER NOT NULL, PRIMARY KEY(kind,seq))')
        for table, key in TABLES.items():
            suffix = SUFFIX[table]
            db.execute(f'ALTER TABLE {table} ADD COLUMN codice_locale TEXT')
            db.execute(f'CREATE UNIQUE INDEX {table}_code ON {table}(codice_locale) WHERE codice_locale IS NOT NULL')
            # Reserve in the same SQLite transaction as the feature insertion.
            # Registry rows survive feature deletion and are copied with backups.
            db.execute(f"""CREATE TRIGGER {table}_allocate_code AFTER INSERT ON {table}
                WHEN NEW.{key} < 0 AND NEW.codice_locale IS NULL BEGIN
                INSERT INTO magoh_code_ledger(kind,seq,code,origin,owner_table,owner_fid)
                SELECT '{suffix}',n,prefix || '{suffix}-' || n,lower(hex(randomblob(16))),'{table}',NEW.fid
                FROM (SELECT coalesce(max(seq),0)+1 AS n FROM magoh_code_ledger, magoh_code_settings
                      WHERE kind='{suffix}' OR code=prefix || '{suffix}-' || seq), magoh_code_settings;
                UPDATE {table} SET codice_locale=(SELECT code FROM magoh_code_ledger WHERE kind='{suffix}' ORDER BY seq DESC LIMIT 1)
                WHERE fid=NEW.fid;
                END""")
            db.execute(f"""CREATE TRIGGER {table}_guard_insert BEFORE INSERT ON {table} BEGIN
                SELECT CASE WHEN NEW.codice_locale IS NOT NULL THEN RAISE(ABORT,'Codice locale gia assegnato: verificare la copia di origine') END;
                SELECT CASE WHEN NEW.{key}<0 AND EXISTS(SELECT 1 FROM {table} WHERE {key}=NEW.{key})
                THEN RAISE(ABORT,'ID tecnico locale duplicato: ricreare la scheda dopo aver aggiornato il layer') END;
                END""")
            db.execute(f"""CREATE TRIGGER {table}_guard_code BEFORE UPDATE OF codice_locale ON {table}
                WHEN OLD.codice_locale IS NOT NEW.codice_locale AND
                (OLD.codice_locale IS NOT NULL OR NOT EXISTS
                 (SELECT 1 FROM magoh_code_ledger WHERE code=NEW.codice_locale AND owner_table='{table}' AND owner_fid=NEW.fid)
                 OR EXISTS(SELECT 1 FROM {table} WHERE codice_locale=NEW.codice_locale))
                BEGIN SELECT RAISE(ABORT,'Codice locale gestito dal progetto'); END""")
    return True


def project_prefix(path, new=None):
    with closing(sqlite3.connect(path, timeout=8)) as db, db:
        if new is not None:
            validate(new)
            db.execute('UPDATE magoh_code_settings SET prefix=? WHERE singleton=1', (new,))
        return db.execute('SELECT prefix FROM magoh_code_settings WHERE singleton=1').fetchone()[0]


def collisions(first, second):
    def registry(path):
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
            return dict(db.execute('SELECT code,origin FROM magoh_code_ledger'))
    a, b = registry(first), registry(second)
    return sorted(code for code in a.keys() & b.keys() if a[code] != b[code])


def reserve_technical_ids(path, table, size=128):
    """Reserve negative relational keys separately from visible codes, before editing."""
    key = TABLES[table]
    floor = -1000000000 if table.endswith('_no_geom') else 0
    with closing(sqlite3.connect(path, timeout=3)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS magoh_technical_reservations (layer TEXT PRIMARY KEY, low INTEGER NOT NULL)')
        minimum = db.execute(f'SELECT min({key}) FROM {table}').fetchone()[0]
        stored = db.execute('SELECT low FROM magoh_technical_reservations WHERE layer=?',(table,)).fetchone()
        high = min(floor, minimum or floor, stored[0] if stored else floor)
        if table == 'magoh_evidenza_archeologica' and high-size <= -1000000000:
            raise ValueError('Intervallo degli ID tecnici delle evidenze esaurito.')
        if table == 'magoh_scheda_intervento' and high-size < -2147483648:
            raise ValueError('Intervallo degli ID tecnici degli interventi esaurito.')
        db.execute('INSERT OR REPLACE INTO magoh_technical_reservations VALUES(?,?)',(table,high-size))
        return iter(range(high-1, high-size-1, -1))
