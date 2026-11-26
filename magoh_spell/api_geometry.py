"""Lettura delle geometrie GeoJSON restituite dalle API MAGOH.

Un solo punto di ingresso per l'import nel template e per il filtro spaziale.
Ogni geometria rifiutata ha un motivo leggibile, che finisce nel log
dell'import: niente più «errori_geometria» senza spiegazione.

Regole:
- il membro ``crs`` del GeoJSON, quando presente, è rispettato;
- senza ``crs``, coordinate entro ±180/±90 sono lette in EPSG:4326;
- senza ``crs`` e con coordinate proiettate, si usa il CRS del layer di
  destinazione soltanto se le coordinate cadono nel suo dominio; l'ipotesi
  viene dichiarata nel log, mai applicata in silenzio;
- da una GeometryCollection si tengono le sole parti del tipo ammesso dal layer;
- tipi incompatibili, coordinate non finite e trasformazioni fallite sono
  rifiutati con il motivo; nessuna geometria viene riparata o inventata.
"""
import json
import math
import re

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCoordinateTransformContext,
    QgsCsException, QgsGeometry, QgsJsonUtils, QgsWkbTypes,
)

WGS84 = 'EPSG:4326'
# CRS del modello dati MAGOH (tabelle del template): ipotesi dichiarata per le
# coordinate proiettate prive del membro ``crs``.
MAGOH_CRS = 'EPSG:3003'
_TYPE_NAMES = {0: 'punti', 1: 'linee', 2: 'poligoni'}
_EPSG = re.compile(r'EPSG(?::|/0/|::)(\d+)', re.IGNORECASE)


def _decode(value):
    if isinstance(value, str):
        text = value.strip()
        if not text or text[0] not in '[{':
            return None
        try:
            return json.loads(text)
        except ValueError:
            try:
                return json.loads(re.sub(r',\s*([}\]])', r'\1', text))
            except ValueError:
                return None
    return value


def _crs_from_member(obj):
    member = obj.get('crs') if isinstance(obj, dict) else None
    if not isinstance(member, dict):
        return None
    name = str((member.get('properties') or {}).get('name') or '')
    if 'CRS84' in name.upper():
        return WGS84
    match = _EPSG.search(name)
    return 'EPSG:' + match.group(1) if match else None


def unwrap(value):
    """Restituisce (dizionario geometria, CRS dichiarato) oppure (None, motivo)."""
    obj = _decode(value)
    if obj in (None, '', {}, []):
        return None, 'geometria assente'
    if not isinstance(obj, dict):
        return None, 'GeoJSON non leggibile'
    declared = _crs_from_member(obj)
    kind = str(obj.get('type', ''))
    if kind == 'FeatureCollection':
        features = obj.get('features') or []
        if len(features) != 1:
            return None, 'FeatureCollection con %d elementi' % len(features)
        obj = features[0]
        declared = _crs_from_member(obj) or declared
        kind = str(obj.get('type', '')) if isinstance(obj, dict) else ''
    if kind == 'Feature':
        obj = obj.get('geometry')
        if not isinstance(obj, dict):
            return None, 'geometria assente'
        kind = str(obj.get('type', ''))
    declared = _crs_from_member(obj) or declared
    if kind == 'GeometryCollection':
        if not obj.get('geometries'):
            return None, 'GeometryCollection vuota'
    elif 'coordinates' not in obj:
        return None, 'GeoJSON senza coordinate (tipo «%s»)' % (kind or '?')
    return (obj, declared), None


def _parse(obj):
    text = json.dumps({k: v for k, v in obj.items() if k != 'crs'})
    parser = getattr(QgsJsonUtils, 'geometryFromGeoJson', None)
    if parser is not None:
        return parser(text)
    from osgeo import ogr  # QGIS < 3.36
    ogr_geom = ogr.CreateGeometryFromJson(text)
    return QgsGeometry.fromWkt(ogr_geom.ExportToIsoWkt()) if ogr_geom is not None else QgsGeometry()


def _finite(rect):
    return all(math.isfinite(v) for v in (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()))


def _geographic(rect):
    return -180.0 <= rect.xMinimum() <= rect.xMaximum() <= 180.0 and -90.0 <= rect.yMinimum() <= rect.yMaximum() <= 90.0


def _within_crs_domain(rect, crs, context):
    """True se il rettangolo, letto nel CRS indicato, cade nel dominio d'uso di quel CRS."""
    bounds = crs.bounds()  # in EPSG:4326
    if bounds.isEmpty():
        return False
    try:
        back = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem(WGS84), context).transformBoundingBox(rect)
    except QgsCsException:
        return False
    return _finite(back) and bounds.contains(back)


def _keep_parts(geom, wanted):
    parts = [QgsGeometry(p.clone()) for p in geom.constParts()] if geom.isMultipart() else [geom]
    kept = [p for p in parts if QgsWkbTypes.geometryType(p.wkbType()) == wanted]
    if not kept:
        return None
    return kept[0] if len(kept) == 1 else QgsGeometry.collectGeometry(kept)


def to_crs(value, target_crs, wanted_type=None, has_z=False, context=None, fallback_crs=None):
    """Legge una geometria API e la porta nel CRS di destinazione.

    Restituisce (QgsGeometry, nota, errore): con errore la geometria è None;
    la nota segnala un'ipotesi dichiarata (CRS dedotto, parti scartate).
    ``wanted_type`` è il tipo QGIS ammesso (0 punti, 1 linee, 2 poligoni);
    None accetta qualsiasi tipo. ``fallback_crs`` è il CRS ipotizzato per
    coordinate proiettate senza ``crs`` (predefinito: il CRS di destinazione,
    se proiettato).
    """
    context = context or QgsCoordinateTransformContext()
    unwrapped, reason = unwrap(value)
    if unwrapped is None:
        return None, None, reason
    obj, declared = unwrapped
    try:
        geom = _parse(obj)
    except Exception as exc:  # parser QGIS o OGR
        return None, None, 'GeoJSON non leggibile (%s)' % exc
    if geom is None or geom.isNull() or geom.isEmpty():
        return None, None, 'geometria vuota o non leggibile'
    notes = []
    kind = obj.get('type')
    if wanted_type is not None:
        actual = QgsWkbTypes.geometryType(geom.wkbType())
        if kind == 'GeometryCollection' or actual not in (0, 1, 2):
            kept = _keep_parts(geom, wanted_type)
            if kept is None:
                return None, None, 'GeometryCollection senza %s' % _TYPE_NAMES.get(wanted_type, 'parti compatibili')
            if kept.constGet().partCount() < geom.constGet().partCount():
                notes.append('parti di altro tipo scartate da una GeometryCollection')
            geom = kept
        elif actual != wanted_type:
            return None, None, 'tipo %s, il layer accetta solo %s' % (
                QgsWkbTypes.displayString(geom.wkbType()), _TYPE_NAMES.get(wanted_type, '?'))
    box = geom.boundingBox()
    if not _finite(box):
        return None, None, 'coordinate non finite'
    if declared:
        source = QgsCoordinateReferenceSystem(declared)
        if not source.isValid():
            return None, None, 'CRS dichiarato non riconosciuto (%s)' % declared
    elif _geographic(box):
        source = QgsCoordinateReferenceSystem(WGS84)
    else:
        guess = fallback_crs or (target_crs if target_crs.isValid() and not target_crs.isGeographic() else None)
        if guess is None or not guess.isValid() or guess.isGeographic() or not _within_crs_domain(box, guess, context):
            return None, None, 'coordinate non geografiche e CRS non dichiarato'
        source = guess
        notes.append('CRS non dichiarato: coordinate lette in %s, CRS del modello MAGOH' % guess.authid())
    if source != target_crs and target_crs.isValid():
        try:
            geom.transform(QgsCoordinateTransform(source, target_crs, context))
        except QgsCsException as exc:
            return None, None, 'trasformazione %s → %s non riuscita (%s)' % (source.authid(), target_crs.authid(), exc)
        if not _finite(geom.boundingBox()):
            return None, None, 'trasformazione %s → %s fuori dominio' % (source.authid(), target_crs.authid())
    if not has_z and QgsWkbTypes.hasZ(geom.wkbType()):
        geom.get().dropZValue()
    if QgsWkbTypes.hasM(geom.wkbType()):
        geom.get().dropMValue()
    return geom, '; '.join(notes) or None, None


def for_layer(value, layer, context=None):
    """Come :func:`to_crs`, con tipo, dimensione e CRS presi dal layer di destinazione."""
    wkb = layer.wkbType()
    geom, note, error = to_crs(value, layer.crs(), QgsWkbTypes.geometryType(wkb), QgsWkbTypes.hasZ(wkb), context)
    if geom is not None and QgsWkbTypes.isMultiType(wkb) and not geom.isMultipart():
        geom.convertToMultiType()
    if geom is not None and not QgsWkbTypes.isMultiType(wkb) and geom.isMultipart():
        if geom.constGet().partCount() != 1:
            return None, None, 'geometria multipla, il layer accetta solo geometrie singole'
        geom.convertToSingleType()
    return geom, note, error


def in_4326(value, context=None):
    """Geometria in EPSG:4326 per il filtro spaziale, qualunque sia il tipo."""
    return to_crs(value, QgsCoordinateReferenceSystem(WGS84), None, False, context,
                  fallback_crs=QgsCoordinateReferenceSystem(MAGOH_CRS))
