"""Compact lookup from user-supplied street attributes, without geometry."""
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

TABLE='shape_str_comunali'
@lru_cache(maxsize=1)
def street_rows():
    # V1.16: the identical vocabulary is already in the bundled template.
    # Read-only access avoids shipping a second copy (123243 original IDs).
    source=(Path(__file__).parent/'data/magoh_spell.gpkg').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(source,uri=True)) as db:
        return tuple(db.execute('SELECT id,den_uff,comune,pro_com_t FROM shape_str_comunali ORDER BY fid'))

@lru_cache(maxsize=1)
def street_names():return {row[0]:row[1] for row in street_rows()}

def ensure_street_schema(path,backup_dir=None):
    with closing(sqlite3.connect(str(path),timeout=3)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():return False
        if backup_dir:
            import time
            folder=Path(backup_dir);folder.mkdir(parents=True,exist_ok=True)
            dest=folder/(Path(path).stem+'_pre_V1_12_'+str(time.time_ns())+'.gpkg')
            with closing(sqlite3.connect(str(dest))) as copy:db.backup(copy)
        with db:
            db.execute('CREATE TABLE shape_str_comunali(fid INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,den_uff TEXT NOT NULL,comune TEXT,pro_com_t TEXT)')
            db.executemany('INSERT INTO shape_str_comunali(id,den_uff,comune,pro_com_t) VALUES(?,?,?,?)',street_rows())
            db.execute('CREATE INDEX magoh_streets_comune ON shape_str_comunali(comune)')
            db.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,description,srs_id) VALUES('shape_str_comunali','attributes','shape_str_comunali','ID e denominazioni originali dal layer stradale fornito; nessuna geometria',NULL)")
    return True
