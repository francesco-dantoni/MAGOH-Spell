"""Keep only complete DSC/MOSI geometries covered by the working MOPR.

The perimeter never clips/changes a source geometry; partial intersections and
null geometries are explicitly excluded and reported.

V1.15.1
- Tolerance of about 1 cm on the MOPR boundary. A MOPR copied from an
  intervention differs from it by fractions of a millimetre after coordinate
  transformations; without tolerance an evidence lying on the edge was
  reported as "partially outside" for 10 cm².
- The polygons of the evidences of one finding are fused (union, no clipping)
  into a single MOSI area. Overlapping or touching parts made an invalid
  MultiPolygon, and the whole MOSI was discarded as "geometria_non_valida".
"""
import json
import math
from copy import copy

from qgis.core import QgsGeometry
from .gna_template_writer import _wkb

TOLERANCE_M = 0.01


def _tolerance_degrees(perimeter):
    """~1 cm expressed in degrees (WGS84), generous in both directions."""
    try:
        lat = perimeter.centroid().asPoint().y()
    except Exception:
        lat = 43.0
    return TOLERANCE_M / (111320.0 * max(math.cos(math.radians(lat)), 0.2))


def _fused(record, geom):
    """Union of the parts of a MultiPolygon; None if nothing changes."""
    parts = [QgsGeometry.fromPolygonXY(p) for p in geom.asMultiPolygon()] if geom.isMultipart() else []
    if len(parts) < 2 and geom.isGeosValid():
        return None
    fused = QgsGeometry.unaryUnion(parts) if len(parts) >= 2 else QgsGeometry(geom)
    if fused.isNull() or fused.isEmpty():
        return None
    if not fused.isGeosValid():
        fused = fused.makeValid()
    fused.convertToMultiType()
    if fused.isEmpty() or not fused.isGeosValid():
        return None
    if geom.isGeosValid() and len(fused.asMultiPolygon()) == len(parts) and fused.equals(geom):
        return None  # already disjoint and valid (e.g. second pass): nothing to fuse
    return fused


def filter_to_mopr(result, geometry):
    perimeter = QgsGeometry(); perimeter.fromWkb(_wkb(geometry)[0])
    if perimeter.isEmpty() or not perimeter.isGeosValid():
        raise ValueError('Il perimetro MOPR non è valido.')
    tolerance = _tolerance_degrees(perimeter)
    covering = perimeter.buffer(tolerance, 8)
    filtered = copy(result)
    report = {'policy': 'whole_geometry_covered_by_MOPR_no_clipping', 'tolerance_m': TOLERANCE_M,
              'included': {}, 'excluded': [], 'fused': []}
    for name in ('dsc', 'mosi_multipolygon', 'mosi_multilinea', 'mosi_multipoint'):
        kept = []
        for record in getattr(result, name):
            raw = record.get('_geometry'); geom = QgsGeometry()
            if raw: geom.fromWkb(_wkb(raw)[0])
            reason = 'geometria_assente'
            if raw and name == 'dsc' and raw.get('type') not in ('Polygon', 'MultiPolygon'):
                report['excluded'].append({'level': name, 'source_id': str(record.get('_source_id') or ''),
                                           'reason': 'tipo_geometrico_non_rappresentabile_nel_livello_DSC'})
                continue
            if not geom.isEmpty() and name == 'mosi_multipolygon':
                fused = _fused(record, geom)
                if fused is not None:
                    parts = len(geom.asMultiPolygon()) if geom.isMultipart() else 1
                    after = len(fused.asMultiPolygon())
                    note = (('geometria MOSI: %d poligoni delle evidenze fusi (%d aree risultanti), senza ritagli'
                             % (parts, after)) if after < parts else
                            'geometria MOSI resa valida (parti sovrapposte o a contatto), senza ritagli')
                    record = dict(record)
                    record['_geometry'] = json.loads(fused.asJson())
                    record['_conversion_notes'] = ' | '.join(x for x in (record.get('_conversion_notes'), note) if x)
                    report['fused'].append({'source_id': str(record.get('_source_id') or ''),
                                            'parts_before': parts, 'parts_after': len(fused.asMultiPolygon())})
                    geom = fused
            if not geom.isEmpty():
                if geom.isGeosValid():
                    remainder = geom.difference(covering)
                    if not remainder.lastError() and remainder.isEmpty():
                        kept.append(record); continue
                    reason = 'fuori_o_parzialmente_fuori_MOPR'
                else:
                    reason = 'geometria_non_valida'
            report['excluded'].append({'level': name, 'source_id': str(record.get('_source_id') or ''), 'reason': reason})
        setattr(filtered, name, kept)
        report['included'][name] = len(kept)
    excluded_non_luogo = list(getattr(result, 'mosi_esclusi_non_luogo', []) or [])
    if excluded_non_luogo:
        report['excluded_non_luogo'] = excluded_non_luogo
    return filtered, report
