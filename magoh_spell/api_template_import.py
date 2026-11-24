"""Mapping SearchController MAGOH -> template MAGOH Spell.

Questa prima integrazione scrive soltanto le tre entita' core richieste:
Intervento, Ritrovamento (III livello) ed Evidenza archeologica (IV livello).
Le altre entita' di search-adv restano disponibili in ``api_structure.py`` e
possono essere collegate a tabelle dedicate in release future senza cambiare
il contratto del downloader.

Gli identificativi ufficiali non vengono rinumerati:
* Intervento: ``id`` SearchController -> ``idscheda`` / ``id`` MAGOH;
* Ritrovamento: suffisso numerico di ``ritrovamento_id``;
* IV livello: suffisso numerico di ``iv_livello_id``.
I suffissi sono gli ID originari usati dalla struttura MAGOH; la parte
prefissa dei SearchController ID serve a rendere esplicita la gerarchia.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .api_structure import decode_json_value, geometry_from_record
from .text_cleaning import scientific_description,plain_text


CORE_IMPORT_SPECS = {
    "intervento": {
        "label": "Schede di Intervento",
        "target": "magoh_scheda_intervento",
        "id_field": "idscheda",
    },
    "ritrovamento": {
        "label": "Schede di Ritrovamento",
        "target": "magoh_ritrovamenti",
        "id_field": "id",
    },
    "iv_livello": {
        "label": "Schede di Evidenza archeologica",
        "target": "magoh_evidenza_archeologica",
        "target_no_geom": "magoh_evidenza_archeologica_no_geom",
        "id_field": "id",
    },
}

def _norm(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().replace("’", "'").replace("`", "'")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).casefold()


def _decoded(value: Any) -> Any:
    return decode_json_value(value)


# Refusi presenti nei thesauri di progetti creati prima della V1.19.1: la
# grafia corretta usata da MAGOH deve trovare lo stesso codice.
_LABEL_TYPOS = (("poduzione", "produzione"),)


def _label_variants(label: str):
    if not label:
        return []
    variants = [label]
    for wrong, right in _LABEL_TYPOS:
        if wrong in label:
            variants.append(label.replace(wrong, right))
    return variants


def first_value(value: Any) -> Any:
    value = _decoded(value)
    if isinstance(value, list):
        return first_value(value[0]) if value else None
    if isinstance(value, dict):
        return None
    return value


def as_list(value: Any):
    value = _decoded(value)
    if value is None:
        return []
    if isinstance(value, list):
        return [first_value(v) for v in value if first_value(v) not in (None, "")]
    if isinstance(value, (tuple, set)):
        return [first_value(v) for v in value if first_value(v) not in (None, "")]
    return [value]


def tail_int(value: Any) -> Optional[int]:
    """Ricava l'ID numerico originario da ID gerarchici tipo 31196_8386_14514.

    Attenzione: ``int('31196_8386')`` in Python e' valido (gli underscore sono
    separatori numerici) e produrrebbe 311968386. Per questo le stringhe
    gerarchiche vengono sempre analizzate prima come testo.
    """
    value = first_value(value)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    s = str(value).strip()
    if re.fullmatch(r"-?\d+(?:\.0+)?", s):
        try:
            return int(float(s))
        except Exception:
            return None
    m = re.search(r"(-?\d+)\s*$", s)
    return int(m.group(1)) if m else None


def as_float(value: Any) -> Optional[float]:
    value = first_value(value)
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return None


def as_int(value: Any) -> Optional[int]:
    value = first_value(value)
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip().replace(",", ".")))
    except Exception:
        return None


def text(value: Any) -> Optional[str]:
    value = first_value(value)
    if value in (None, ""):
        return None
    return str(value)


def geojson_text(record: Dict[str, Any], entity_key: str) -> Optional[str]:
    field = {
        "intervento": "geojson",
        "ritrovamento": "ritrovamento_geojson",
        "iv_livello": "iv_livello_geojson",
    }.get(entity_key)
    value = record.get(field) if field else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    geom = geometry_from_record(record, entity_key)
    if geom:
        return json.dumps(geom, ensure_ascii=False, separators=(",", ":"))
    return None


@dataclass(frozen=True)
class ChronoValue:
    id: Optional[str] = None
    period: Optional[str] = None
    name: Optional[str] = None
    full: Optional[str] = None
    range: Optional[str] = None


class TemplateLookups:
    """Lookup dei thesauri gia' presenti nel GeoPackage MAGOH Spell."""

    def __init__(self):
        from .geographic_lookups import municipality_codes
        self.comuni = {_norm(name):code for name,code in municipality_codes().items()}
        self.chrono: Dict[str, ChronoValue] = {}
        self.period: Dict[str, str] = {}
        self.reliability: Dict[str, str] = {}
        self.i_level: Dict[str, str] = {}
        self.ii_level: Dict[Tuple[str, str], str] = {}
        self.ii_level_loose: Dict[str, str] = {}
        self.iii_level: Dict[Tuple[str, str], str] = {}
        self.iii_codes: Dict[str, set] = {}
        self.iii_level_loose: Dict[str, str] = {}
        self.iv_level: Dict[str, str] = {}
        self.ogd: Dict[str, str] = {}
        self.ogm: Dict[str, str] = {}

    @classmethod
    def from_gpkg(cls, path: str) -> "TemplateLookups":
        obj = cls()
        conn = sqlite3.connect(path)
        try:
            for row in conn.execute("SELECT id,id_periodo,name,name_full,range FROM thesaurus_crono"):
                cid, period, name, full, rng = row
                cv = ChronoValue(
                    str(cid) if cid is not None else None,
                    str(period) if period is not None else None,
                    str(name) if name is not None else None,
                    str(full) if full is not None else None,
                    str(rng) if rng is not None else None,
                )
                for candidate in (cid, name, full):
                    key = _norm(candidate)
                    if key:
                        obj.chrono.setdefault(key, cv)
            for code, period, full in conn.execute("SELECT cod_periodo,periodo,name_full FROM thesaurus_periodo"):
                for candidate in (period, full, code):
                    key = _norm(candidate)
                    if key:
                        obj.period.setdefault(key, str(code))
            for name, rid in conn.execute('SELECT nome,id FROM "thesaurus_affidabilità"'):
                if _norm(name):
                    obj.reliability[_norm(name)] = str(rid)
            for name, code in conn.execute("SELECT i_lev,i_lev_code FROM I_livelli_new"):
                if _norm(name):
                    obj.i_level[_norm(name)] = str(code)
            for code, name, parent in conn.execute("SELECT ii_lev_code,ii_lev,i_lev_code FROM II_livelli_new"):
                if _norm(name):
                    obj.ii_level[(_norm(parent), _norm(name))] = str(code)
                    obj.ii_level_loose.setdefault(_norm(name), str(code))
            # II codes are unique, while dataset_completo spells I codes "A9"
            # where I_livelli_new has "9": III labels are keyed by II code alone.
            for ii_code, iii_name, iii_code in conn.execute(
                "SELECT ii_lev_code,iii_lev,iii_lev_code FROM dataset_completo"
            ):
                obj.iii_codes.setdefault(_norm(ii_code), set()).add(str(iii_code))
                for label in _label_variants(_norm(iii_name)):
                    obj.iii_level[(_norm(ii_code), label)] = str(iii_code)
                    obj.iii_level_loose.setdefault(label, str(iii_code))
            for rid, name in conn.execute("SELECT id,nome FROM thesaurus_iv_livello"):
                if _norm(name):
                    obj.iv_level[_norm(name)] = str(rid)
            # OGD/OGM non hanno tabelle dedicate nel GeoPackage: il template
            # li codifica come ValueMap nello stile QGIS della Scheda Intervento.
            # Leggiamo quindi il vocabolario dal template stesso, senza hardcode.
            try:
                row = conn.execute(
                    "SELECT styleQML FROM layer_styles WHERE f_table_name='magoh_scheda_intervento' "
                    "AND styleQML IS NOT NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    root = ET.fromstring(row[0])
                    for fname, target in (("ogd", obj.ogd), ("ogm", obj.ogm)):
                        for field in root.findall('.//field'):
                            if field.get('name') != fname:
                                continue
                            widget = field.find('editWidget')
                            if widget is None or widget.get('type') != 'ValueMap':
                                continue
                            for option in widget.findall('.//Option'):
                                label = option.get('name')
                                code = option.get('value')
                                if label and code is not None and label != 'map' and _norm(label):
                                    target.setdefault(_norm(label), str(code))
            except Exception:
                pass
        finally:
            conn.close()
        return obj

    def chrono_value(self, value: Any) -> ChronoValue:
        key = _norm(first_value(value))
        if not key:
            return ChronoValue()
        found = self.chrono.get(key)
        if found:
            return found
        # SearchController a volte usa la denominazione del periodo generale.
        p = self.period.get(key)
        if p:
            return ChronoValue(period=p, name=text(value), full=text(value))
        return ChronoValue(name=text(value), full=text(value))

    def level_codes(self, i_name: Any, ii_name: Any, iii_name: Any):
        i_code = self.i_level.get(_norm(i_name))
        ii_code = None
        if i_code:
            ii_code = self.ii_level.get((_norm(i_code), _norm(ii_name)))
        if not ii_code:
            ii_code = self.ii_level_loose.get(_norm(ii_name))
        if ii_code:
            # A label of another II level ("reperti mobili" exists twice) would
            # be a code the III widget cannot show: better none than a wrong one.
            iii_code = self.iii_level.get((_norm(ii_code), _norm(iii_name)))
        else:
            iii_code = self.iii_level_loose.get(_norm(iii_name))
        return i_code, ii_code, iii_code

    def consistent_iii_code(self, ii_code: Any, iii_code: Any, iii_name: Any) -> Optional[str]:
        """The III code of ``iii_name`` within ``ii_code``, when the stored one is missing or of another II."""
        ii, current = _norm(ii_code), str(iii_code or '').strip()
        if not ii or current in self.iii_codes.get(ii, ()):
            return None
        return self.iii_level.get((ii, _norm(iii_name)))


def _period_flags(record: Dict[str, Any]) -> Dict[str, str]:
    source = {_norm(x) for x in as_list(record.get("intervento_datazione"))}
    aliases = {
        "preistoria": ("preistoria", "eta' preistorica", "eta preistorica"),
        "eta_protostorica": ("eta protostorica", "eta' protostorica"),
        "eta_etrusca": ("eta etrusca", "eta' etrusca"),
        "eta_romana": ("eta romana", "eta' romana"),
        "eta_tardo_antica": ("eta tardo antica", "eta' tardo antica", "eta tardoantica", "eta' tardoantica"),
        "altomedioevo": ("alto medioevo", "eta altomedievale", "eta' altomedievale"),
        "bassomedievo": ("basso medioevo", "eta bassomedievale", "eta' bassomedievale"),
        "eta_moderna": ("eta moderna", "eta' moderna"),
        "eta_contemporanea": ("eta contemporanea", "eta' contemporanea"),
        "non_identificati": ("non identificati", "non determinato"),
    }
    out = {}
    for field, values in aliases.items():
        out[field] = "si" if any(_norm(v) in source for v in values) else "no"
    return out


def map_intervento(record: Dict[str, Any], lookups: TemplateLookups):
    from .geographic_lookups import municipality_code,geographic_code
    from .precision import canonical
    ident = tail_int(record.get("id"))
    commune=municipality_code(record.get('comune'))
    attrs = {
        "idscheda": ident,
        "id": ident,
        "ubicazione": text(record.get("ubicazione")),
        "id_comune": commune,
        "indirizzo": geographic_code('street',record.get('indirizzo'),commune),
        "sigla": text(record.get("intervento_sigla")),
        "data_intervento": text(record.get("intervento_data_s")),
        "anno": as_int(record.get("intervento_anno_i")),
        "durata_intervento": text(record.get("intervento_durata_s")),
        "esecutore": text(record.get("intervento_esecutore")),
        "tipologia_intervento": text(record.get("intervento_tipo")),
        "numero_saggi": text(record.get("intervento_numero_saggi_s")),
        "direzione_scientifica": text(record.get("intervento_direzione")),
        "estensione": as_float(record.get("intervento_estensione_s")),
        "profondita_massima": as_float(record.get("intervento_profondita_s")),
        "profondita_falda": as_float(record.get("intervento_profondita_falda")),
        "data_di_redazione": text(record.get("intervento_data_redazione_d")),
        "data_ultimo_aggiornamento": text(record.get("intervento_data_ultimo_agg_d")),
        "motivazione_aggiornamento": text(record.get("intervento_mot_agg_doc_s")),
        "compilatore_scheda": text(record.get("intervento_redattore")),
        "collocazione_materiali": text(record.get("intervento_materiali_coll_doc_s")),
        "materiali_dispersi": text(record.get("intervento_materiali_disp_doc_s")),
        "note": plain_text(text(record.get("note"))) or None,
        "fonte_informazione": text(record.get("intervento_istituzione")),
        "numero_protocollo": text(record.get("intervento_protocollo_doc_s")),
        "data_protocollo": text(record.get("intervento_data_doc_d")),
        "tipo_documento": text(record.get("intervento_tipo_doc_s")),
        "funzionario_competente": text(record.get("intervento_funzionario")),
        "titolo": text(record.get("pubb_titolo_s")),
        "contenuto_in": text(record.get("pubb_in_s")),
        "luogo_data_pubblicazione": text(record.get("pubb_luogo_data_s")),
        "autore": text(record.get("pubb_autore")),
        "pagina": text(record.get("pubb_pagine_s")),
        "url_mod": text(record.get("intervento_url_mod")),
        "centroid": text(record.get("centroid")),
        "area": as_float(record.get("intervento_area")),
        "perimeter": as_float(record.get("intervento_perimeter")),
        "toponimo": geographic_code('toponym',record.get('toponimo'),commune),
        "numero_civico": text(record.get("numero_civico")),
        "geologico": text(record.get("intervento_geologico")),
        "ogd": lookups.ogd.get(_norm(record.get("intervento_ogd"))),
        "ogm": lookups.ogm.get(_norm(record.get("intervento_ogm"))),
        "published": as_int(record.get("published")),
        "descrizione_sintetica": scientific_description(text(record.get("decrizione")) or text(record.get("descrizione"))) or None,
        "sistema_di_rilevazione": text(record.get("intervento_sistema_di_rilevazione")),
        "tipo_cartografico_di_riferimento": text(record.get("intervento_tipo_cartografico")),
        "scala_cartografica": text(record.get("intervento_scala_cartografica")),
        "precisione_di_georeferenziazione": canonical(text(record.get("intervento_geo_precisione"))),
    }
    attrs.update(_period_flags(record))
    # Preserve unmatched source text for traceability without writing labels
    # into PostGIS ID fields or choosing an arbitrary road segment.
    unresolved=[]
    for source,target in (('comune','id_comune'),('indirizzo','indirizzo'),('toponimo','toponimo')):
        raw=text(record.get(source))
        if raw and attrs[target] is None:
            unresolved.append(source+': '+raw)
    if unresolved:
        attrs['note']='Valori geografici originali API da associare a un codice univoco: '+'; '.join(unresolved)
    return attrs, geometry_from_record(record, "intervento")


def map_ritrovamento(record: Dict[str, Any], lookups: TemplateLookups):
    ident = tail_int(record.get("ritrovamento_id") or record.get("id"))
    parent = tail_int(record.get("ritrovamento_intervento_id") or record.get("parent_intervento_id"))
    i_name = text(record.get("ritrovamento_i_livello"))
    ii_name = text(record.get("ritrovamento_ii_livello"))
    iii_name = text(record.get("ritrovamento_iii_livello"))
    i_code, ii_code, iii_code = lookups.level_codes(i_name, ii_name, iii_name)
    c_ini = lookups.chrono_value(record.get("ritrovamento_cronologia_inizio"))
    c_fin = lookups.chrono_value(record.get("ritrovamento_cronologia_fine"))
    quota_aff = text(record.get("ritrovamento_affidabilita_quota"))
    attrs = {
        "id": ident,
        "idscheda": parent,
        "idperiodo": c_ini.period,
        "i_livello": i_name,
        "ii_livello": ii_name,
        "iii_livello": iii_name,
        "cronologia_iniziale": c_ini.period,
        "cronologia_finale": c_fin.period,
        "datazione": text(record.get("ritrovamento_anno_inizio")),
        "datazione_finale": text(record.get("ritrovamento_anno_fine")),
        "profondit": as_float(record.get("ritrovamento_profondita")),
        "affidabilita": quota_aff,
        "affidabilita_2": text(record.get("ritrovamento_affidabilita_dati")),
        "id_i_livello": i_code,
        "id_ii_livello": ii_code,
        "id_iii_livello": iii_code,
        "geojson": geojson_text(record, "ritrovamento"),
        "id_affidabilita": lookups.reliability.get(_norm(quota_aff)),
        "crono_iniziale": c_ini.id,
        "crono_finale": c_fin.id,
        "range_inizio": c_ini.range,
        "range_finale": c_fin.range,
    }
    return attrs, geometry_from_record(record, "ritrovamento")


def map_iv_livello(record: Dict[str, Any], lookups: TemplateLookups):
    ident = tail_int(record.get("iv_livello_id") or record.get("id"))
    parent_find = tail_int(record.get("iv_livello_iii_livello_id") or record.get("parent_ritrovamento_id"))
    parent_int = tail_int(record.get("parent_intervento_id") or record.get("idintervento"))
    iv_name = text(record.get("iv_livello_iv_livello"))
    c_ini = lookups.chrono_value(record.get("iv_livello_cronologia_inizio"))
    c_fin = lookups.chrono_value(record.get("iv_livello_cronologia_fine"))
    quota_aff = text(record.get("iv_livello_affidabilita_quota"))
    attrs = {
        "id": ident,
        "idiii_livello": parent_find,
        "idintervento": parent_int,
        "idperiodo": c_ini.period,
        "iv_livello": iv_name,
        "cronologia": c_ini.period,
        "datazione_iniziale": as_int(record.get("iv_livello_anno_inizio")),
        "datazione_finale": as_int(record.get("iv_livello_anno_fine")),
        "affidabilita": lookups.reliability.get(_norm(quota_aff)),
        "quota": as_float(record.get("iv_livello_quota")),
        "iv_liv_orig": iv_name,
        "dat_ini_orig": text(record.get("iv_livello_cronologia_inizio")),
        "dat_fin_orig": text(record.get("iv_livello_cronologia_fine")),
        "note": text(record.get("iv_livello_note")),
        "geojson": geojson_text(record, "iv_livello"),
        "id_iv_livello": lookups.iv_level.get(_norm(iv_name)),
        "crono_iniziale": c_ini.id,
        "crono_finale": c_fin.id,
        "cronologia_finale": c_fin.period,
        "range_iniziale": c_ini.range,
        "range_finale": c_fin.range,
    }
    return attrs, geometry_from_record(record, "iv_livello")


MAPPERS = {
    "intervento": map_intervento,
    "ritrovamento": map_ritrovamento,
    "iv_livello": map_iv_livello,
}


def map_entity(entity_key: str, record: Dict[str, Any], lookups: TemplateLookups):
    mapper = MAPPERS.get(entity_key)
    if mapper is None:
        raise KeyError(entity_key)
    from .precision import normalize_import
    attrs, geometry = mapper(record, lookups)
    tables = {"intervento":"magoh_scheda_intervento", "ritrovamento":"magoh_ritrovamenti",
              "iv_livello":"magoh_evidenza_archeologica"}
    normalize_import(tables[entity_key],attrs)
    return attrs, geometry
