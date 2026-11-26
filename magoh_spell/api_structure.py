"""Normalizzazione read-only della struttura SearchController MAGOH.

Il modulo non dipende da QGIS ed e' volutamente privo di operazioni di rete o
scrittura sul database MAGOH. Trasforma i documenti gerarchici restituiti da
``/v0/search/search-adv`` in collezioni di entita' relazionate, conservando le
chiavi originali e aggiungendo solo chiavi ``parent_*`` esplicite per facilitare
la consultazione in QGIS.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional


ENTITY_SPECS = OrderedDict([
    ("intervento", {
        "label": "Interventi",
        "entity_type": "Intervento",
        "geometry_field": "geojson",
        "id_fields": ("id",),
        "children": ("ritrovamenti", "bibliografia", "documentazione"),
    }),
    ("ritrovamento", {
        "label": "Ritrovamenti (III livello)",
        "entity_type": "Ritrovamento",
        "geometry_field": "ritrovamento_geojson",
        "id_fields": ("ritrovamento_id", "id"),
        "children": ("iv_livelli",),
    }),
    ("iv_livello", {
        "label": "IV livelli archeologici",
        "entity_type": "IV Livello",
        "geometry_field": "iv_livello_geojson",
        "id_fields": ("iv_livello_id", "id"),
        "children": ("sepolture",),
    }),
    ("sepoltura", {
        "label": "Sepolture",
        "entity_type": "Sepoltura",
        "geometry_field": "sepoltura_geojson",
        "id_fields": ("sepoltura_id", "id"),
        "children": ("salme",),
    }),
    ("dato_antropologico", {
        "label": "Dati antropologici",
        "entity_type": "Scheda dati antropologici",
        "geometry_field": "antropologico_geojson",
        "id_fields": ("antropologico_id", "id"),
        "children": (),
    }),
    ("documentazione", {
        "label": "Documentazione",
        "entity_type": "Document",
        "geometry_field": None,
        "id_fields": ("doc_id", "id"),
        "children": (),
    }),
    ("bibliografia", {
        "label": "Bibliografia",
        "entity_type": "Riferimento bibliografico",
        "geometry_field": None,
        "id_fields": ("biblio_id", "id"),
        "children": (),
    }),
])

# Campi che contengono duplicazioni cartografiche pre-renderizzate. Restano nel
# JSON integrale di search-adv, ma non vengono duplicati negli attributi QGIS:
# la geometria viene gia' materializzata come geometria del layer.
PREBUILT_MAP_FIELDS = {
    "layer_ritrovamenti", "layer_iv_livelli", "layer_sepolture",
    "layer_dati_antropologici",
}


def as_items(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    return []


def decode_json_value(value: Any) -> Any:
    """Decodifica un valore JSON o una stringa JSON, tollerando trailing comma."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        repaired = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            return json.loads(repaired)
        except Exception:
            return value


def geometry_from_record(record: Dict[str, Any], entity_key: str) -> Optional[Dict[str, Any]]:
    spec = ENTITY_SPECS[entity_key]
    field = spec.get("geometry_field")
    if not field:
        return None
    obj = decode_json_value(record.get(field))
    if not isinstance(obj, dict):
        return None
    kind = str(obj.get("type", ""))
    if kind and ("coordinates" in obj or (kind == "GeometryCollection" and obj.get("geometries"))):
        # anche le GeometryCollection; il membro «crs» resta con la geometria.
        return obj
    if kind.lower() == "feature":
        geom = obj.get("geometry")
        if not isinstance(geom, dict):
            return None
        if "crs" in obj and "crs" not in geom:
            geom = dict(geom, crs=obj["crs"])
        return geom
    return None


def _copy_with_parents(record: Dict[str, Any], **parents: Any) -> Dict[str, Any]:
    out = dict(record)
    for key, value in parents.items():
        if value not in (None, ""):
            out.setdefault(key, value)
    return out


def flatten_search_docs(docs: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Espande la gerarchia MAGOH in sette collezioni relazionate."""
    out = {key: [] for key in ENTITY_SPECS}
    for parent in docs:
        if not isinstance(parent, dict):
            continue
        intervention_id = parent.get("id")
        p = _copy_with_parents(parent, idintervento=intervention_id)
        out["intervento"].append(p)

        for rit in as_items(parent.get("ritrovamenti")):
            rit_id = rit.get("ritrovamento_id") or rit.get("id")
            r = _copy_with_parents(
                rit,
                parent_intervento_id=intervention_id,
                idintervento=intervention_id,
            )
            out["ritrovamento"].append(r)

            for iv in as_items(rit.get("iv_livelli")):
                iv_id = iv.get("iv_livello_id") or iv.get("id")
                i = _copy_with_parents(
                    iv,
                    parent_intervento_id=intervention_id,
                    parent_ritrovamento_id=rit_id,
                    idintervento=intervention_id,
                )
                out["iv_livello"].append(i)

                for sep in as_items(iv.get("sepolture")):
                    sep_id = sep.get("sepoltura_id") or sep.get("id")
                    s = _copy_with_parents(
                        sep,
                        parent_intervento_id=intervention_id,
                        parent_ritrovamento_id=rit_id,
                        parent_iv_livello_id=iv_id,
                        idintervento=intervention_id,
                    )
                    out["sepoltura"].append(s)

                    for antrop in as_items(sep.get("salme")):
                        a = _copy_with_parents(
                            antrop,
                            parent_intervento_id=intervention_id,
                            parent_ritrovamento_id=rit_id,
                            parent_iv_livello_id=iv_id,
                            parent_sepoltura_id=sep_id,
                            idintervento=intervention_id,
                        )
                        out["dato_antropologico"].append(a)

        for document in as_items(parent.get("documentazione")):
            out["documentazione"].append(_copy_with_parents(
                document, parent_intervento_id=intervention_id, idintervento=intervention_id
            ))
        for biblio in as_items(parent.get("bibliografia")):
            out["bibliografia"].append(_copy_with_parents(
                biblio, parent_intervento_id=intervention_id, idintervento=intervention_id
            ))
    return out


def scalarize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def qgis_properties(record: Dict[str, Any], entity_key: str) -> Dict[str, Any]:
    """Attributi QGIS: tutti i campi dell'entita', senza contenitori figli/mappe duplicate."""
    spec = ENTITY_SPECS[entity_key]
    excluded = set(spec.get("children") or ()) | PREBUILT_MAP_FIELDS
    return {
        str(k): scalarize(v)
        for k, v in record.items()
        if k not in excluded
    }


def feature_collection(records: Iterable[Dict[str, Any]], entity_key: str) -> Dict[str, Any]:
    features = []
    for record in records:
        if not isinstance(record, dict):
            continue
        features.append({
            "type": "Feature",
            "properties": qgis_properties(record, entity_key),
            "geometry": geometry_from_record(record, entity_key),
        })
    return {
        "type": "FeatureCollection",
        "name": "MAGOH_API_" + entity_key,
        "features": features,
    }


def entity_counts(flattened: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, int]]:
    result = {}
    for key, records in flattened.items():
        result[key] = {
            "records": len(records),
            "with_geometry": sum(1 for r in records if geometry_from_record(r, key) is not None),
        }
    return result
