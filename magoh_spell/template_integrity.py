"""Persistent archive integrity: transactional cascades and a closed Comune list.

Deletes run in the parent's SQLite transaction, including filtered/unloaded rows.
No records are deleted while installing this schema or before a QGIS commit.
"""
import sqlite3
import time
import uuid
from functools import lru_cache
from pathlib import Path
from contextlib import closing

TABLE = 'magoh_comuni'
TRIGGERS = {
    'magoh_spell_guard_intervento_v1': '''CREATE TRIGGER magoh_spell_guard_intervento_v1
BEFORE DELETE ON magoh_scheda_intervento WHEN EXISTS
(SELECT 1 FROM magoh_spell_edit_guard WHERE parent_table='magoh_scheda_intervento'
 AND expires_at > CAST(strftime('%s','now') AS INTEGER)) BEGIN
 SELECT RAISE(ABORT, 'Salvare o annullare prima le modifiche delle schede figlie MAGOH'); END''',
    'magoh_spell_guard_ritrovamento_v1': '''CREATE TRIGGER magoh_spell_guard_ritrovamento_v1
BEFORE DELETE ON magoh_ritrovamenti WHEN EXISTS
(SELECT 1 FROM magoh_spell_edit_guard WHERE parent_table='magoh_ritrovamenti'
 AND expires_at > CAST(strftime('%s','now') AS INTEGER)) BEGIN
 SELECT RAISE(ABORT, 'Salvare o annullare prima le modifiche delle evidenze MAGOH'); END''',
    'magoh_spell_delete_intervento_v1': '''CREATE TRIGGER magoh_spell_delete_intervento_v1
AFTER DELETE ON magoh_scheda_intervento WHEN OLD.idscheda IS NOT NULL BEGIN
 DELETE FROM magoh_evidenza_archeologica WHERE idintervento=OLD.idscheda OR idiii_livello IN
 (SELECT id FROM magoh_ritrovamenti WHERE idscheda=OLD.idscheda);
 DELETE FROM magoh_evidenza_archeologica_no_geom WHERE idintervento=OLD.idscheda OR idiii_livello IN
 (SELECT id FROM magoh_ritrovamenti WHERE idscheda=OLD.idscheda);
 DELETE FROM magoh_ritrovamenti WHERE idscheda=OLD.idscheda;
END''',
    'magoh_spell_delete_ritrovamento_v1': '''CREATE TRIGGER magoh_spell_delete_ritrovamento_v1
AFTER DELETE ON magoh_ritrovamenti WHEN OLD.id IS NOT NULL BEGIN
 DELETE FROM magoh_evidenza_archeologica WHERE idiii_livello=OLD.id;
 DELETE FROM magoh_evidenza_archeologica_no_geom WHERE idiii_livello=OLD.id;
END''',
    'magoh_spell_comune_insert_v2': '''CREATE TRIGGER magoh_spell_comune_insert_v2
BEFORE INSERT ON magoh_scheda_intervento WHEN NEW.id_comune IS NOT NULL AND trim(NEW.id_comune)<>''
 AND NOT EXISTS(SELECT 1 FROM magoh_comuni WHERE codice=CAST(NEW.id_comune AS TEXT) AND legacy=0) BEGIN
 SELECT RAISE(ABORT, 'Comune non presente nel vocabolario chiuso MAGOH'); END''',
    'magoh_spell_comune_update_v2': '''CREATE TRIGGER magoh_spell_comune_update_v2
BEFORE UPDATE OF id_comune ON magoh_scheda_intervento
WHEN NEW.id_comune IS NOT OLD.id_comune AND NEW.id_comune IS NOT NULL AND trim(NEW.id_comune)<>''
 AND NOT EXISTS(SELECT 1 FROM magoh_comuni WHERE codice=CAST(NEW.id_comune AS TEXT) AND legacy=0) BEGIN
 SELECT RAISE(ABORT, 'Comune non presente nel vocabolario chiuso MAGOH'); END''',
}


@lru_cache(maxsize=1)
def municipality_names():
    from .geographic_lookups import municipality_rows
    return tuple(sorted({row[1] for row in municipality_rows()},key=str.casefold))


def ensure_archive_schema(path, backup_dir=None):
    """Backed-up upgrade; convert only unambiguous historical names to original IDs."""
    from .geographic_lookups import municipality_rows, municipality_code
    with closing(sqlite3.connect(str(path), timeout=2)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {'magoh_scheda_intervento', 'magoh_ritrovamenti',
                    'magoh_evidenza_archeologica', 'magoh_evidenza_archeologica_no_geom'}
        if not expected.issubset(tables):
            raise ValueError('Schema MAGOH incompleto: aggiornamento non applicato.')
        triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        columns={r[1] for r in db.execute('PRAGMA table_info(magoh_comuni)')}
        if 'codice' in columns and set(TRIGGERS).issubset(triggers):
            return False
        if backup_dir:
            folder = Path(backup_dir); folder.mkdir(parents=True, exist_ok=True)
            backup = folder / (Path(path).stem + '_pre_V1_13_' + str(time.time_ns()) + '.gpkg')
            with closing(sqlite3.connect(str(backup))) as dest:
                db.backup(dest)
        db.execute('BEGIN IMMEDIATE')
        for name in ('magoh_spell_comune_insert_v1','magoh_spell_comune_update_v1'):
            db.execute('DROP TRIGGER IF EXISTS '+name)
        if TABLE in tables and 'codice' not in columns:
            db.execute('ALTER TABLE magoh_comuni RENAME TO magoh_comuni_legacy_v1')
        db.execute('CREATE TABLE IF NOT EXISTS magoh_comuni (fid INTEGER PRIMARY KEY AUTOINCREMENT, codice TEXT NOT NULL UNIQUE, nome TEXT NOT NULL, istat TEXT, provincia TEXT, regione TEXT, legacy INTEGER NOT NULL DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS magoh_spell_edit_guard (token TEXT PRIMARY KEY, parent_table TEXT NOT NULL, expires_at INTEGER NOT NULL)')
        db.executemany('INSERT OR IGNORE INTO magoh_comuni(codice,nome,istat,provincia,regione) VALUES (?,?,?,?,?)',municipality_rows())
        db.execute('CREATE INDEX IF NOT EXISTS magoh_comuni_nome ON magoh_comuni(nome)')
        db.execute('CREATE TABLE IF NOT EXISTS magoh_spell_geo_migrations(fid INTEGER,old_value TEXT,new_value TEXT,changed_at INTEGER)')
        changes=[]
        for fid,value in db.execute("SELECT fid,id_comune FROM magoh_scheda_intervento WHERE id_comune IS NOT NULL AND trim(id_comune)<>''"):
            code=municipality_code(value)
            if code and str(value)!=code:changes.append((fid,str(value),code,int(time.time())))
        if changes:
            # Attribute-only update: suspend RTree UPDATE triggers in this same
            # transaction because plain sqlite lacks spatial SQL functions.
            # No geometry/fid is changed; definitions are restored before commit.
            spatial=db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='magoh_scheda_intervento' AND name LIKE 'rtree_%' AND upper(sql) LIKE '%UPDATE%'").fetchall()
            for name,_ in spatial:db.execute('DROP TRIGGER "'+name.replace('"','""')+'"')
            db.executemany('UPDATE magoh_scheda_intervento SET id_comune=? WHERE fid=?',((code,fid) for fid,old,code,stamp in changes))
            db.executemany('INSERT INTO magoh_spell_geo_migrations VALUES(?,?,?,?)',changes)
            for _,sql in spatial:db.execute(sql)
        # Unknown values remain visible and unchanged, but not valid new choices.
        db.execute("INSERT OR IGNORE INTO magoh_comuni(codice,nome,legacy) SELECT DISTINCT CAST(id_comune AS TEXT),CAST(id_comune AS TEXT),1 FROM magoh_scheda_intervento WHERE id_comune IS NOT NULL AND trim(id_comune)<>''")
        db.execute("INSERT OR IGNORE INTO gpkg_contents(table_name,data_type,identifier,description,srs_id) VALUES ('magoh_comuni','attributes','magoh_comuni','Codici originali MAGOH dal GeoJSON comuni fornito',NULL)")
        if 'gpkg_ogr_contents' in tables:
            db.execute("UPDATE gpkg_ogr_contents SET feature_count=(SELECT count(*) FROM magoh_comuni) WHERE table_name='magoh_comuni'")
        for name, sql in TRIGGERS.items():
            if name not in triggers:
                db.execute(sql)
        db.commit()
    return True


class CascadeCommitGuard:
    """Protect other edit buffers; refresh providers after the atomic SQL cascade."""
    def __init__(self, plugin):
        self.plugin = plugin
        self.connections = []
        self.blocked = set()
        self.token = str(uuid.uuid4())
        self.path = None

    def _guard(self, parent, blocked):
        if not self.path: return
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.execute('DELETE FROM magoh_spell_edit_guard WHERE expires_at<=?',(int(time.time()),))
            token = self.token + ':' + parent
            if blocked:
                db.execute('INSERT OR REPLACE INTO magoh_spell_edit_guard VALUES (?,?,?)',
                           (token, parent, int(time.time()) + 30))
            else:
                db.execute('DELETE FROM magoh_spell_edit_guard WHERE token=?', (token,))
            db.commit()

    def _peers(self, parent):
        layers=self.plugin._layers_for_gpkg(self.path) if self.path else {}
        return [layer for name,layer in layers.items() if name.startswith('magoh_evidenza_') or
                (parent=='magoh_scheda_intervento' and name=='magoh_ritrovamenti')]

    def dispose(self):
        from qgis.PyQt import sip
        for layer, signal_name, callback in self.connections:
            # Project.clear()/read() can destroy layers before emitting cleared.
            # Never disconnect a bound signal whose C++ owner no longer exists.
            if sip.isdeleted(layer):
                continue
            try: getattr(layer, signal_name).disconnect(callback)
            except (RuntimeError, TypeError): pass
        self.connections.clear()
        self.blocked.clear()
        if self.path:
            try:
                for parent in ('magoh_scheda_intervento', 'magoh_ritrovamenti'):
                    self._guard(parent, False)
            except sqlite3.Error:
                pass
        self.path = None

    def bind(self, path):
        self.dispose()
        self.path = str(path)
        layers = self.plugin._layers_for_gpkg(path)
        for parent in ('magoh_scheda_intervento', 'magoh_ritrovamenti'):
            layer = layers.get(parent)
            if layer is None: continue
            def before(*_args, source=layer, table=parent):
                buffer = source.editBuffer()
                deleting = buffer is not None and bool(buffer.deletedFeatureIds())
                conflicts = [l.name() for l in self._peers(table) if l.isEditable() and l.isModified()]
                if deleting and conflicts:
                    self._guard(table, True)
                    self.blocked.add(source.id())
                    self.plugin.iface.messageBar().pushWarning('MAGOH Spell',
                        'Prima di salvare la cancellazione / Before saving deletion: '
                        'salva o annulla le modifiche dei figli / save or discard child edits: ' + ', '.join(conflicts))
                else:
                    self._guard(table, False)
                    self.blocked.discard(source.id())
            def after(*_args, table=parent):
                self._guard(table, False)
                for child in self._peers(table):
                    if not (child.isEditable() and child.isModified()):
                        child.reload()
                        child.updateExtents(True)
                        child.dataChanged.emit()
                        child.triggerRepaint()
                self.plugin._refresh_open_ui_status()
            for signal_name, callback in (('beforeCommitChanges', before), ('afterCommitChanges', after)):
                getattr(layer, signal_name).connect(callback)
                self.connections.append((layer, signal_name, callback))
            callback = lambda *_args, table=parent:self._guard(table, False)
            layer.afterRollBack.connect(callback)
            self.connections.append((layer,'afterRollBack',callback))
