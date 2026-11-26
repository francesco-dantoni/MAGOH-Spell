"""Closed ID/label lookups. Internal MAGOH codes must never be inferred from ISTAT."""
import gzip
import json
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from .street_lookups import street_rows,street_names


@lru_cache(maxsize=1)
def municipality_rows():
    source=(Path(__file__).parent/'data/magoh_spell.gpkg').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(source,uri=True)) as db:
        return tuple(db.execute('SELECT codice,nome,istat,provincia,regione FROM magoh_comuni ORDER BY fid'))


@lru_cache(maxsize=1)
def municipality_names():
    return {row[0]:row[1] for row in municipality_rows()}


@lru_cache(maxsize=1)
def municipality_codes():
    by_name={}
    for code,name,*_ in municipality_rows():
        by_name.setdefault(name.strip().casefold(),[]).append(code)
    return {name:codes[0] for name,codes in by_name.items() if len(codes)==1}


def municipality_code(value):
    text=str(value or '').strip()
    return text if text in municipality_names() else municipality_codes().get(text.casefold())


@lru_cache(maxsize=1)
def toponym_rows():
    with gzip.open(Path(__file__).parent/'data/toponyms_lookup.json.gz','rt',encoding='utf-8') as stream:
        return tuple(tuple(row) for row in json.load(stream)['rows'])


@lru_cache(maxsize=1)
def toponym_names():
    return {row[0]:row[1] for row in toponym_rows()}


def municipality_label(value, lookup=None):
    """Resolve actual codes where provided; keep legacy labels, never invent codes."""
    text = str(value or '').strip()
    return municipality_names().get(text, '' if text.isdigit() else text)


@lru_cache(maxsize=2)
def geographic_ids(kind):
    """Resolve only unique labels within a municipality; never pick a segment arbitrarily."""
    result={}
    for code,label,commune,_ in (street_rows() if kind=='street' else toponym_rows()):
        key=(commune.strip().casefold(),label.strip().casefold())
        result.setdefault(key,set()).add(code)
    return {key:next(iter(values)) for key,values in result.items() if len(values)==1}


def geographic_code(kind, value, commune):
    text=str(value or '').strip()
    if not text:return None
    names = street_names() if kind=='street' else toponym_names()
    if text in names:return text
    name=municipality_label(commune)
    return geographic_ids(kind).get((name.casefold(),text.casefold()))


@lru_cache(maxsize=2)
def by_municipality(kind):
    result = {}
    for ident, label, commune, _ in (street_rows() if kind == 'street' else toponym_rows()):
        result.setdefault(commune.strip().casefold(), []).append((ident, label))
    return {key:tuple(sorted(rows,key=lambda row:(row[1].casefold(),row[0]))) for key,rows in result.items()}


def ensure_toponym_schema(path, backup_dir=None):
    with closing(sqlite3.connect(str(path),timeout=3)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shape_toponimi'").fetchone():
            return False
        if backup_dir:
            import time
            folder=Path(backup_dir); folder.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(str(folder/(Path(path).stem+'_backup_'+str(time.time_ns())+'.gpkg')))) as backup:
                db.backup(backup)
        with db:
            db.execute('CREATE TABLE shape_toponimi(fid INTEGER PRIMARY KEY AUTOINCREMENT,pk_uid TEXT NOT NULL UNIQUE,toponimo TEXT NOT NULL,comune TEXT,codcom TEXT)')
            db.executemany('INSERT INTO shape_toponimi(pk_uid,toponimo,comune,codcom) VALUES(?,?,?,?)',toponym_rows())
            db.execute('CREATE INDEX magoh_toponyms_comune ON shape_toponimi(comune)')
            db.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,description,srs_id) VALUES('shape_toponimi','attributes','shape_toponimi','Vocabolario originale fornito: pk_uid, toponimo e Comune; senza geometrie',NULL)")
    return True


# --- readable labels (no ALL CAPS, no codes shown) ------------------
import re as _re

_SMALL = {
    'di', 'da', 'del', 'dello', 'della', 'dei', 'degli', 'delle', 'dal', 'dallo', 'dalla', 'dai', 'dagli', 'dalle',
    'a', 'al', 'allo', 'alla', 'ai', 'agli', 'alle', 'in', 'nel', 'nello', 'nella', 'nei', 'negli', 'nelle',
    'su', 'sul', 'sullo', 'sulla', 'sui', 'sugli', 'sulle', 'per', 'con', 'tra', 'fra', 'e', 'ed', 'o', 'senza',
}
_ELIDED_SMALL = {'d', 'dell', 'dall', 'all', 'nell', 'sull', 'coll', 'dagl', 'degl', 'sugl', 'negl', 'agl'}
_ROMAN = _re.compile(r'^(?=[IVXL])L?X{0,3}(IX|IV|V?I{0,3})$')


def _cap(word):
    return word[:1].upper() + word[1:]


def _word(token, first):
    if not token:
        return token
    upper = token.upper()
    if _ROMAN.match(upper):
        return upper
    if any(ch.isdigit() for ch in token) or token.rstrip('.').count('.') >= 1:
        return upper  # S.S., S.C., SP12: abbreviations and codes stay as written
    pieces = token.lower().split("'")
    out = []
    for index, piece in enumerate(pieces):
        is_last = index == len(pieces) - 1
        lower_piece = piece in (_ELIDED_SMALL if not is_last else _SMALL)
        if lower_piece and not (first and index == 0):
            out.append(piece)
        else:
            out.append('-'.join(_cap(part) for part in piece.split('-')))
    return "'".join(out)


def readable_label(text):
    """'CORSO FRATELLI ROSSELLI' -> 'Corso Fratelli Rosselli'; mixed case unchanged."""
    text = str(text or '').strip()
    if not text or text != text.upper() or not any(ch.isalpha() for ch in text):
        return text
    words = text.split()
    return ' '.join(_word(w, i == 0) for i, w in enumerate(words)).replace(' senza Nome', ' senza nome')


@lru_cache(maxsize=2)
def readable_names(kind):
    rows = street_rows() if kind == 'street' else toponym_rows()
    return {row[0]: readable_label(row[1]) for row in rows}
