# -*- coding: utf-8 -*-
"""Compila il Template GNA 1.6.0 ufficiale con i dati MAGOH convertiti.

Perché questo modulo esiste, e perché non usa QGIS
---------------------------------------------------
Il Plugin GNA ufficiale non carica file sciolti: cerca dei layer con
identificativi fissi (``MOSI_multipolygon_7931f3fe_…``) dentro il progetto
``Template_GNA_1.6.qgz`` e copia i loro campi, uno per uno, nelle tabelle del
server. L'unico modo di far arrivare i dati MAGOH sul Geoportale è quindi
**riempire il Template_GNA.gpkg ufficiale**: nessun GeoJSON, per quanto
conforme, potrà mai essere conferito.

La versione precedente scriveva quel GeoPackage attraverso i layer PyQGIS. Qui
si scrive direttamente in SQLite. Non è un dettaglio implementativo: significa
che la generazione del pacchetto si può eseguire e collaudare **fuori da QGIS**,
cioè che si può verificare che il file prodotto sia giusto invece di sperarlo.
I trigger del GeoPackage (indice spaziale e conteggio) restano quelli del
Template e continuano a funzionare, perché le funzioni ``ST_MinX``/``ST_IsEmpty``
che invocano vengono registrate qui sotto.

Che cosa scrive il Template, davvero
------------------------------------
Nei campi a vocabolario controllato il Template **non** scrive l'etichetta ma la
chiave numerica: lo dicono i suoi stessi valori predefiniti (``MOSI.GEL = 1``,
``MOSI.GET = 3``, ``MOSI.SSK = '2'``). Fanno eccezione i campi che non sono
ValueRelation — ``DSC.GEL``, ``DSC.GET`` — e ``DTR``, la cui chiave è l'etichetta
stessa. Convertire le etichette prodotte dal convertitore nelle chiavi giuste è
il lavoro principale di questo modulo.
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------- utilità

class ErroreTemplate(RuntimeError):
    pass


def _nv(v):
    """Confronto tollerante fra etichette: accenti, punteggiatura e spazi."""
    if v is None:
        return ""
    s = unicodedata.normalize("NFD", str(v))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _vuoto(v):
    if v is None:
        return True
    s = str(v).strip()
    return s == "" or s.upper() == "NULL"


# ---------------------------------------------------------------- geometrie


def _wkb_point(c):
    return struct.pack("<dd", float(c[0]), float(c[1]))


def _wkb_ring(anello):
    out = struct.pack("<I", len(anello))
    for c in anello:
        out += _wkb_point(c)
    return out


def _wkb_polygon_body(anelli):
    out = struct.pack("<I", len(anelli))
    for a in anelli:
        out += _wkb_ring(a)
    return out


def _wkb(geom):
    """WKB little-endian di una geometria GeoJSON, sempre nella forma Multi.

    Il Template ha layer MULTIPOLYGON/MULTILINESTRING/MULTIPOINT: una geometria
    semplice va promossa, altrimenti QGIS la rifiuta al caricamento.
    """
    t = (geom or {}).get("type")
    c = (geom or {}).get("coordinates")
    if not t or c is None:
        return None, None
    if t == "Polygon":
        t, c = "MultiPolygon", [c]
    elif t == "LineString":
        t, c = "MultiLineString", [c]
    elif t == "Point":
        t, c = "MultiPoint", [c]

    if t == "MultiPolygon":
        corpo = struct.pack("<I", len(c))
        for poly in c:
            corpo += b"\x01" + struct.pack("<I", 3) + _wkb_polygon_body(poly)
        return b"\x01" + struct.pack("<I", 6) + corpo, t
    if t == "MultiLineString":
        corpo = struct.pack("<I", len(c))
        for linea in c:
            corpo += b"\x01" + struct.pack("<I", 2) + _wkb_ring(linea)
        return b"\x01" + struct.pack("<I", 5) + corpo, t
    if t == "MultiPoint":
        corpo = struct.pack("<I", len(c))
        for p in c:
            corpo += b"\x01" + struct.pack("<I", 1) + _wkb_point(p)
        return b"\x01" + struct.pack("<I", 4) + corpo, t
    return None, None


def _coordinate(x):
    """Tutte le coppie di coordinate contenute in una struttura annidata."""
    if isinstance(x, (list, tuple)):
        if len(x) >= 2 and all(isinstance(v, (int, float)) for v in x[:2]):
            yield x
        else:
            for y in x:
                for z in _coordinate(y):
                    yield z


def _bbox(geom):
    pts = list(_coordinate((geom or {}).get("coordinates", [])))
    if not pts:
        return None
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    return min(xs), max(xs), min(ys), max(ys)


def gpkg_blob(geom, srs_id=4326):
    """Geometria nel formato binario del GeoPackage, con envelope.

    L'envelope non è decorativo: i trigger dell'indice spaziale del Template
    leggono da lì i valori di ST_MinX e compagni.
    """
    wkb, _tipo = _wkb(geom)
    if wkb is None:
        return None
    bb = _bbox(geom)
    if bb is None:
        return None
    minx, maxx, miny, maxy = bb
    # magic 'GP', versione 0, flag: little endian + envelope XY (indicatore 1)
    intestazione = b"GP" + bytes([0, 0b00000011]) + struct.pack("<i", srs_id)
    intestazione += struct.pack("<dddd", minx, maxx, miny, maxy)
    return intestazione + wkb


def _envelope_dal_blob(blob):
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 40:
        return None
    if bytes(blob[:2]) != b"GP":
        return None
    flag = blob[3]
    if ((flag >> 1) & 0x07) == 0:
        return None
    return struct.unpack("<dddd", bytes(blob[8:40]))


def registra_funzioni_gpkg(con):
    """Registra ST_MinX & co., che i trigger dell'indice spaziale invocano.

    Senza queste funzioni ogni INSERT fallirebbe con "no such function".
    Leggono l'envelope dall'intestazione GeoPackage: nessun parsing di WKB.
    """
    def prendi(i):
        def f(blob):
            env = _envelope_dal_blob(blob)
            return None if env is None else env[i]
        return f

    con.create_function("ST_MinX", 1, prendi(0))
    con.create_function("ST_MaxX", 1, prendi(1))
    con.create_function("ST_MinY", 1, prendi(2))
    con.create_function("ST_MaxY", 1, prendi(3))
    con.create_function("ST_IsEmpty", 1,
                        lambda blob: 1 if _envelope_dal_blob(blob) is None else 0)


# ---------------------------------------------------------------- vocabolari

class Vocabolari:
    """Traduce le etichette del convertitore nelle chiavi del Template 1.6.

    Legge la copia locale incorporata nel plugin. Se è disponibile una sorgente
    ufficiale aggiornata (e c'è rete) la si può preferire con `aggiorna`, ma la
    generazione del pacchetto **non dipende** dalla rete: era il difetto della
    versione precedente, dove un vocabolario irraggiungibile impediva del tutto
    di creare il progetto.
    """

    def __init__(self, percorso_snapshot, percorso_cache=None):
        self.dati = json.loads(Path(percorso_snapshot).read_text(encoding="utf-8-sig"))
        self.cache_path = Path(percorso_cache) if percorso_cache else None
        if self.cache_path and self.cache_path.exists():
            try:
                extra = json.loads(self.cache_path.read_text(encoding="utf-8-sig"))
                for k, v in extra.items():
                    if not k.startswith("_"):
                        self.dati.setdefault(k, {}).update(v) if isinstance(v, dict) else None
            except Exception:
                pass
        official = Path(percorso_snapshot).with_name('mopr_vocab_official.json')
        if official.exists():
            document = json.loads(official.read_text(encoding='utf-8'))
            for field, data in document['fields'].items():
                cfg = data['config']
                rows = data['rows']
                if field == 'LCC':
                    values = {str(r[cfg['key']]): {'nome': r[cfg['value']], 'cod_reg': r['cod_reg'], 'cod_prov': r['cod_prov']} for r in rows}
                else:
                    values = {str(r[cfg['key']]): r[cfg['value']] for r in rows}
                self.dati[cfg['layer_name']] = {'voci': values}
        self.avvisi = []
        self._indice = {}

    def _voci(self, sigla):
        v = self.dati.get(sigla)
        if isinstance(v, dict) and "voci" in v:
            v = v["voci"]
        if not isinstance(v, dict):
            return {}
        return {k: x for k, x in v.items() if not str(k).startswith("_")}

    def etichette(self, sigla):
        voci = self._voci(sigla)
        out = {}
        for k, v in voci.items():
            out[k] = v.get("nome") if isinstance(v, dict) else v
        return out

    def chiave(self, sigla, etichetta):
        """La chiave del Template per un'etichetta. None se non c'è."""
        if _vuoto(etichetta):
            return None
        voci = self.etichette(sigla)
        testo = str(etichetta).strip()
        # già una chiave
        if testo in voci:
            return testo
        if sigla not in self._indice:
            self._indice[sigla] = {_nv(v): k for k, v in voci.items() if v}
        return self._indice[sigla].get(_nv(testo))

    def etichetta(self, sigla, chiave):
        return self.etichette(sigla).get(str(chiave))

    def anni_dtr(self, etichetta):
        voci = (self.dati.get("DTR") or {}).get("voci") or {}
        for k, v in voci.items():
            if _nv(k) == _nv(etichetta):
                return v
        return [None, None]


# ---------------------------------------------------------------- scrittura

class ScrittoreTemplate:
    """Inserisce MOPR, DSC e MOSI dentro una copia del Template ufficiale."""

    #: Campi a vocabolario che nel Template conservano la CHIAVE.
    #: (nome campo -> sigla del vocabolario da cui prendere la chiave)
    CHIAVI_DSC = {"DSCM": "DSCM", "DSCT": "AMA", "GPT": "GPT", "GPM": "GPM",
                  "GPBB": "GPBB", "DSCEX": "DSCEX"}
    # V1.20: FNTP è una ValueRelation su FNTP_key nel Template 1.6.0: si scrive la chiave,
    # altrimenti QGIS mostra l'etichetta tra parentesi, es. «(dato di archivio)».
    CHIAVI_MOSI = {"OGD": "OGD", "OGM": "OGM", "OGB": "OGB", "GEL": "GEL", "FNTP": "FNTP",
                   "GET": "GET", "GPT": "GPT", "GPM": "GPM", "GPBB": "GPBB",
                   "CDGG": "CDGG", "BPT": "BPT", "NVCT": "NVCT", "SSK": "SSK",
                   "AMA": "AMA"}
    CHIAVI_MOPR = {"AMA": "AMA", "CTG": "CTG", "GEL": "GEL", "GET": "GET",
                   "GPT": "GPT", "GPM": "GPM", "GPBB": "GPBB"}

    #: Campi interi del Template: un testo qui rende il record illeggibile.
    INTERI = {"DSC_multipolygon": {"PVCR", "PVCP", "PVCC", "DSCEX", "DSCY"},
              "MOSI": {"LCR", "LCP", "LCC", "DTSI", "DTSF", "SSK", "gid"},
              "MOPR": {"LCR", "LCP", "LCC", "ENTE", "gid"}}

    def __init__(self, gpkg, vocabolari):
        self.gpkg = Path(gpkg)
        self.voc = vocabolari
        self.avvisi = []
        self.errori = []
        self.con = sqlite3.connect(str(self.gpkg))
        registra_funzioni_gpkg(self.con)
        self.con.execute("PRAGMA foreign_keys=OFF")
        self._colonne = {}

    def chiudi(self):
        try:
            self.con.commit()
        finally:
            self.con.close()

    # -- struttura --------------------------------------------------------

    def colonne(self, tabella):
        if tabella not in self._colonne:
            info = list(self.con.execute('PRAGMA table_info("%s")' % tabella))
            if not info:
                raise ErroreTemplate("Il Template non contiene la tabella %s." % tabella)
            self._colonne[tabella] = [(r[1], (r[2] or "").upper()) for r in info]
        return self._colonne[tabella]

    def campo_geometria(self, tabella):
        r = self.con.execute(
            "select column_name from gpkg_geometry_columns where table_name=?",
            (tabella,)).fetchone()
        return r[0] if r else "geom"

    # -- conversione dei valori ------------------------------------------

    def _tipo_intero(self, tabella, campo):
        gruppo = "DSC_multipolygon" if tabella.startswith("DSC") else (
            "MOPR" if tabella == "MOPR" else "MOSI")
        return campo in self.INTERI.get(gruppo, set())

    def _valore(self, tabella, campo, valore, tipo_sql):
        if _vuoto(valore):
            return None
        if isinstance(valore, bool):
            return 1 if valore else 0
        if self._tipo_intero(tabella, campo) or tipo_sql.startswith(("INT", "MEDIUMINT")):
            try:
                return int(float(str(valore).strip()))
            except (TypeError, ValueError):
                self.avvisi.append(
                    "%s.%s: %r non è un numero, campo lasciato vuoto" % (tabella, campo, valore))
                return None
        return str(valore)

    def _traduci(self, tabella, attrs, tabella_chiavi):
        """Sostituisce le etichette con le chiavi del vocabolario del Template."""
        for campo, sigla in tabella_chiavi.items():
            v = attrs.get(campo)
            if _vuoto(v):
                continue
            k = self.voc.chiave(sigla, v)
            if k is None:
                self.avvisi.append(
                    '%s.%s: "%s" non è nel vocabolario GNA %s del Template 1.6: '
                    "campo lasciato vuoto, da completare nel Template"
                    % (tabella, campo, v, sigla))
                attrs[campo] = None
            else:
                attrs[campo] = k
        return attrs

    # -- inserimento ------------------------------------------------------

    def inserisci(self, tabella, righe):
        """Inserisce le righe indicate. `righe` è una lista di (attributi, geometria)."""
        if not righe:
            return 0
        cols = self.colonne(tabella)
        nomi = {c for c, _ in cols}
        tipi = dict(cols)
        gcol = self.campo_geometria(tabella)

        campi = [c for c, _ in cols if c not in ("fid", gcol)]
        sql = 'INSERT INTO "%s" ("%s","%s") VALUES (%s)' % (
            tabella, gcol, '","'.join(campi), ",".join(["?"] * (len(campi) + 1)))

        lotto = []
        for attrs, geom in righe:
            sconosciuti = [k for k in attrs if k not in nomi]
            for k in sconosciuti:
                self.avvisi.append("%s: il Template non ha il campo %s, valore ignorato"
                                   % (tabella, k))
                attrs.pop(k, None)
            blob = gpkg_blob(geom) if geom else None
            if blob is None:
                self.errori.append("%s: record senza geometria valida, non inserito" % tabella)
                continue
            valori = [blob] + [self._valore(tabella, c, attrs.get(c), tipi.get(c, "TEXT"))
                               for c in campi]
            lotto.append(valori)
        if not lotto:
            return 0
        self.con.executemany(sql, lotto)
        return len(lotto)

    def aggiorna_estensione(self, tabella):
        """Riallinea min/max di gpkg_contents: QGIS li usa per lo zoom iniziale."""
        gcol = self.campo_geometria(tabella)
        r = self.con.execute(
            'select min(ST_MinX("%s")), min(ST_MinY("%s")), max(ST_MaxX("%s")), max(ST_MaxY("%s")) '
            'from "%s" where "%s" is not null' % (gcol, gcol, gcol, gcol, tabella, gcol)
        ).fetchone()
        if r and r[0] is not None:
            self.con.execute(
                "update gpkg_contents set min_x=?, min_y=?, max_x=?, max_y=?, "
                "last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now') where table_name=?",
                (r[0], r[1], r[2], r[3], tabella))
