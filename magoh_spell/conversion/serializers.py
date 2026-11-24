# -*- coding: utf-8 -*-
"""Scrittura del pacchetto di staging GNA (Template 1.6.0).

Lo staging contiene solo i **dati**, in `DSC/` e `MOSI/`, conformi allo schema
GNA e a nient'altro: nei GeoJSON non compare nessun campo che il Template non
preveda. Servono come anteprime in QGIS.

Il **log di conversione** (`conversion_log.csv`), che lega ogni record GNA al
record MAGOH da cui viene, non si scrive qui ma dentro il pacchetto finale
(vedi `gna_package.py`), così la provenienza viaggia insieme ai dati. Il report
di verifica resta nella finestra del plugin.

Struttura prodotta::

    GNA_output/
    ├── DSC/
    │   └── DSC_multipolygon.geojson
    └── MOSI/
        ├── MOSI_multipolygon.geojson
        ├── MOSI_multiline.geojson
        └── MOSI_multipoint.geojson
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- campi di controllo

#: campi interni al convertitore: tracciano la provenienza ma **non** finiscono
#: mai nei GeoJSON, perché il GNA non li prevede. Vivono nel log.
CTRL = ["_source_codici_evidenze", "_source_codice_locale", "_source_system", "_source_layer", "_source_id", "_conversion_direction",
        "_conversion_quality", "_conversion_notes", "_original_geometry_type", "_geometry_source",
        "_geometry_output_type", "_source_intervento_id", "_source_ritrovamento_id",
        "_source_evidenza_ids", "_source_idscheda", "_precisione", "_rule",
        "_unmapped_fields", "_unmapped_values", "_layer_gna"]

#: colonne del log di conversione, nell'ordine chiesto dall'handover 1.6.
#: `rule` è la regola che ha deciso l'esito, quando una regola c'è stata.
LOG_COLS = ["source_database", "source_layer", "source_id", "source_idscheda",
            "target_layer", "target_id", "geometry_type", "status", "rule",
            "warnings", "errors"]

#: nel pacchetto il log porta anche la chiave del record nel GeoPackage GNA
#: (DSCH per le DSC, ACCC per i MOSI): è quella che si ritrova nel Template.
LOG_COLS_PACCHETTO = LOG_COLS[:6] + ["codice_locale", "codici_evidenze", "gna_key_field", "gna_key"] + LOG_COLS[6:]

#: dalla tabella interna al file su disco
LAYOUT = {
    "GNA_DSC": ("DSC", "DSC_multipolygon.geojson", "DSC"),
    "GNA_MOSI_multipolygon": ("MOSI", "MOSI_multipolygon.geojson", "MOSI"),
    "GNA_MOSI_multilinea": ("MOSI", "MOSI_multiline.geojson", "MOSI"),
    "GNA_MOSI_multipoint": ("MOSI", "MOSI_multipoint.geojson", "MOSI"),
}

NOTA_TEMPLATE = ("I dati prodotti sono predisposti secondo il Template GNA 1.6.0. "
                 "I campi dipendenti dal Modulo di Progetto (MOPR) non vengono compilati "
                 "in questa fase e devono essere valorizzati nel Template GNA ufficiale "
                 "prima del conferimento.")


def _blank(v):
    if v is None:
        return True
    s = str(v).strip()
    return s == "" or s.upper() == "NULL"


# ---------------------------------------------------------------- GeoJSON conformi

def schema_fields(mapping, scheda):
    return [x["campo"] if isinstance(x, dict) else str(x)
            for x in (mapping.get("gna_schema") or {}).get(scheda, [])]


def _valore_pulito(v):
    """Ultimo controllo prima del file: niente oggetti Qt, niente loro tracce.

    La normalizzazione avviene a monte, quando i valori entrano nel
    convertitore. Questo è il controllo finale, e serve proprio perché è
    l'ultimo: un valore che arrivasse fin qui con dentro un
    `PyQt5.QtCore.QDateTime(...)` finirebbe nel GNA come dato reale. Meglio un
    campo vuoto, che si vede, di un campo sbagliato che sembra buono.
    """
    from .validators import REPR_QT, normalizza_valore

    v = normalizza_valore(v)
    if v is None:
        return ""
    if isinstance(v, (int, float, bool)):
        return v
    if not isinstance(v, str):
        v = str(v)
    return "" if REPR_QT.search(v) else v


def conforming_properties(row, mapping, scheda, gid):
    """Solo i campi dello schema GNA, nell'ordine dello schema. Niente altro."""
    out = {}
    for c in schema_fields(mapping, scheda):
        if c.lower() == "gid":
            out[c] = gid
            continue
        out[c] = _valore_pulito(row.get(c, row.get(c.lower(), "")))
    return out


def write_geojson(path, rows, mapping, scheda):
    feats = []
    for i, r in enumerate(rows, 1):
        feats.append({"type": "Feature",
                      "properties": conforming_properties(r, mapping, scheda, i),
                      "geometry": r.get("_geometry")})
    fc = {"type": "FeatureCollection",
          "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
          "features": feats}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")
    return Path(path)


# ---------------------------------------------------------------- CSV

def write_csv(path, rows, cols):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    return Path(path)


# ---------------------------------------------------------------- log di conversione

from .validators import INADMISSIBLE  # noqa: E402


def _indice_segnalazioni(report):
    """Segnalazioni raggruppate per (tabella, record): servono riga per riga."""
    idx = {}
    for e in (report.esiti if report is not None else []):
        idx.setdefault((str(e.tabella), str(e.record)), []).append(e)
    return idx


def _tabella_report(nome_tabella):
    """Il Ctx annota su GNA_DSC / GNA_MOSI, non sui tre layer MOSI separati."""
    return "GNA_DSC" if nome_tabella == "GNA_DSC" else "GNA_MOSI"


def log_rows(res, report=None, chiavi=None):
    """Una riga per record convertito, con il legame verso il record MAGOH.

    È la garanzia di reversibilità: `source_id` e `source_idscheda` permettono
    sempre di risalire dal record GNA di staging alla scheda MAGOH di partenza,
    anche ora che DSCH, VRRP e ACCC restano vuoti.
    """
    from .validators import ERROR, WARNING

    report = report if report is not None else getattr(res, "report", None)
    idx = _indice_segnalazioni(report)
    righe = []
    for tabella, rows in res.all_tables.items():
        cartella, nome_file, scheda = LAYOUT.get(tabella, ("", tabella + ".geojson", scheda_di(tabella)))
        target_layer = Path(nome_file).stem
        chiave_tab = _tabella_report(tabella)
        for i, r in enumerate(rows, 1):
            sid = str(r.get("_source_id", "") or "")
            segnalazioni = idx.get((chiave_tab, sid), [])
            errori = [e for e in segnalazioni if e.classe == ERROR]
            avvisi = [e for e in segnalazioni if e.classe == WARNING]
            tid = r.get("id")
            if _blank(tid):
                tid = "gid=%d" % i
            righe.append({
                "source_database": "MAGOH",
                "codice_locale": r.get("_source_codice_locale") or "",
                "codici_evidenze": r.get("_source_codici_evidenze") or "",
                "source_layer": r.get("_source_layer", ""),
                "source_id": sid,
                "source_idscheda": (r.get("_source_idscheda")
                                    or r.get("_source_intervento_id") or ""),
                "target_layer": target_layer,
                "target_id": tid,
                "geometry_type": r.get("_geometry_output_type", "") or "",
                "status": "ERROR" if errori else ("WARNING" if avvisi else "OK"),
                "rule": r.get("_rule", "") or "",
                "warnings": " | ".join(e.messaggio for e in avvisi),
                "errors": " | ".join(e.messaggio for e in errori),
            })
            if chiavi is not None:
                campo, valore = chiavi.get(id(r), ("", ""))
                if tabella == "GNA_MOSI_multilinea":
                    righe[-1]["target_layer"] = "MOSI_multilinea"
                righe[-1]["gna_key_field"] = campo
                righe[-1]["gna_key"] = valore

    # I record esclusi da una regola non entrano in nessun layer, ma devono
    # restare nel log: un record che sparisce senza lasciare traccia è il modo
    # più efficace per non accorgersi mai di averlo perso.
    for r in getattr(res, "dsc_inammissibili", []) or []:
        sid = str(r.get("_source_id", "") or "")
        segnalazioni = idx.get(("GNA_DSC", sid), [])
        motivi = [e.messaggio for e in segnalazioni if e.classe == INADMISSIBLE]
        righe.append({
            "source_database": "MAGOH",
                "codice_locale": r.get("_source_codice_locale") or "",
                "codici_evidenze": r.get("_source_codici_evidenze") or "",
            "source_layer": r.get("_source_layer", ""),
            "source_id": sid,
            "source_idscheda": r.get("_source_idscheda") or "",
            "target_layer": "",
            "target_id": "",
            "geometry_type": r.get("_original_geometry_type", "") or "",
            "status": INADMISSIBLE,
            "rule": r.get("_rule", "") or "",
            "warnings": "",
            "errors": " | ".join(motivi) or (r.get("_conversion_notes") or ""),
        })
    return righe


def scheda_di(tabella):
    """Schema GNA (DSC o MOSI) a cui una tabella interna deve conformarsi."""
    return LAYOUT.get(tabella, (None, None, "MOSI"))[2]


# ---------------------------------------------------------------- sommario

def sommario(res, opzioni=None, percorso=""):
    """Riepilogo per il dialogo del plugin."""
    report = getattr(res, "report", None)
    conteggi = report.conteggi() if report is not None else {}
    differiti = report.campi_differiti() if report is not None else []
    log = log_rows(res, report)
    esclusi = list(getattr(res, "dsc_inammissibili", []) or [])
    # Il numero che conta davvero non è quanti interventi sono stati esclusi,
    # ma quanti spariscono del tutto dal conferimento: quelli che perdono la
    # DSC e non hanno nemmeno un MOSI a rappresentarli.
    interventi_con_mosi = {str(r.get("_source_intervento_id") or "")
                           for tab in res.all_tables.values() for r in tab
                           if r.get("_source_intervento_id")}
    esclusi_con_mosi = [r for r in esclusi
                        if str(r.get("_source_idscheda") or r.get("_source_id") or "")
                        in interventi_con_mosi]
    per_regola = {}
    for r in esclusi:
        per_regola[r.get("_rule") or "?"] = per_regola.get(r.get("_rule") or "?", 0) + 1
    return {
        "template_gna": "1.6.0",
        "direzione": "MAGOH_TO_GNA",
        "generato": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "percorso_output": str(percorso or ""),
        "interventi_totali": len(res.dsc) + len(esclusi),
        "dsc_convertite": len(res.dsc),
        "dsc_escluse": len(esclusi),
        "dsc_escluse_per_regola": per_regola,
        "esclusi_con_almeno_un_mosi": len(esclusi_con_mosi),
        "esclusi_senza_nessun_record": len(esclusi) - len(esclusi_con_mosi),
        "mosi_convertiti": res.n_mosi,
        "mosi_per_geometria": {
            "MultiPolygon": len(res.mosi_multipolygon),
            "MultiLineString": len(res.mosi_multilinea),
            "MultiPoint": len(res.mosi_multipoint),
        },
        "record_totali": len(log),
        "record_con_errori": sum(1 for r in log if r["status"] == "ERROR"),
        "record_con_avvisi": sum(1 for r in log if r["status"] == "WARNING"),
        "segnalazioni": conteggi,
        "campi_da_completare_nel_template_gna": differiti,
        "opzioni": dict(opzioni or {}),
        "nota": NOTA_TEMPLATE,
    }


def testo_sommario(s):
    """La stessa sintesi in forma leggibile, per il messaggio finale del dialogo."""
    from .validators import DEFERRED_GNA, ERROR, INADMISSIBLE, WARNING

    g = s.get("mosi_per_geometria") or {}
    seg = s.get("segnalazioni") or {}
    righe = [
        "Interventi letti:             %d" % s.get("interventi_totali", 0),
        "Schede DSC convertite:        %d" % s.get("dsc_convertite", 0),
        "Schede MOSI convertite:       %d  (poligoni %d, linee %d, punti %d)"
        % (s.get("mosi_convertiti", 0), g.get("MultiPolygon", 0),
           g.get("MultiLineString", 0), g.get("MultiPoint", 0)),
        "",
        "Interventi esclusi:           %d  (di cui %d conservano almeno un MOSI,"
        " %d spariscono del tutto)"
        % (s.get("dsc_escluse", 0), s.get("esclusi_con_almeno_un_mosi", 0),
           s.get("esclusi_senza_nessun_record", 0)),
        "",
        "Errori bloccanti:             %d" % seg.get(ERROR, 0),
        "Avvisi:                       %d" % seg.get(WARNING, 0),
        "Da completare nel Template:   %d" % seg.get(DEFERRED_GNA, 0),
        "Esclusi dal trasferimento:    %d" % seg.get(INADMISSIBLE, 0),
    ]
    campi = s.get("campi_da_completare_nel_template_gna") or []
    if campi:
        righe.append("   campi: " + ", ".join(campi))
    righe += ["", "Cartella di output:", str(s.get("percorso_output", "")), "", s.get("nota", "")]
    return "\n".join(righe)


# ---------------------------------------------------------------- pacchetto

def scrivi_pacchetto(converter, res, outdir, opzioni=None):
    """Scrive i dati di staging nella struttura del Template 1.6.

    Restituisce un dizionario con i percorsi scritti e il sommario, così il
    chiamante (dialogo QGIS o riga di comando) non deve ricostruirli.
    """
    base = Path(outdir)
    base.mkdir(parents=True, exist_ok=True)
    mapping = converter.mapping

    # Si scrivono TUTTI i layer previsti, anche quelli rimasti senza record.
    # Un file non riscritto è peggio di un file vuoto: resta lì dalla corsa
    # precedente con dei dati che sembrano quelli di adesso.
    tabelle = dict(res.all_tables)
    scritti = {}
    for tabella in LAYOUT:
        rows = tabelle.get(tabella, [])
        cartella, nome_file, scheda = LAYOUT[tabella]
        percorso = (base / cartella / nome_file) if cartella else (base / nome_file)
        write_geojson(percorso, rows, mapping, scheda)
        scritti[tabella] = percorso
    for tabella, rows in tabelle.items():
        if tabella in scritti:
            continue
        cartella, nome_file, scheda = ("", tabella + ".geojson", scheda_di(tabella))
        percorso = base / nome_file
        write_geojson(percorso, rows, mapping, scheda)
        scritti[tabella] = percorso

    s = sommario(res, opzioni, base)

    return {"cartella": base, "geojson": scritti,
            "sommario": s, "testo": testo_sommario(s)}
