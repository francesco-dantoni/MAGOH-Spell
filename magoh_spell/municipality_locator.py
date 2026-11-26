"""Comune from geometry, light enough to run when a feature is created.

Source: the user-supplied ``comuni.geojson`` (field ``id`` = MAGOH code, the
same file used for the Comune vocabulary; SHA256 recorded in
municipalities_lookup.json.gz). Boundaries are simplified to 50 m and stored,
without other attributes, in ``data/magoh_comuni_confini.gpkg`` with a spatial
index. Nothing is loaded until the first lookup; each lookup reads only the
few municipalities whose bounding box touches the geometry.

Rule: the municipality containing the whole geometry; otherwise the one with
the largest share (area for polygons, length for lines); for points, the one
containing the point. Borders are simplified, so a result within ~50 m of a
municipal boundary is a proposal that the user can change in the form.
"""
from pathlib import Path

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest,
                       QgsGeometry, QgsProject, QgsVectorLayer, QgsWkbTypes, QgsExpression,
                       QgsRectangle)

DATA = Path(__file__).resolve().parent / 'data' / 'magoh_comuni_confini.gpkg'
FUNCTION_NAME = 'magoh_comune_da_geometria'
DEFAULT_EXPRESSION = FUNCTION_NAME + '()'
_layer = None
_cache = {}


def _boundaries():
    global _layer
    if _layer is None or not _layer.isValid():
        if not DATA.is_file():
            return None
        _layer = QgsVectorLayer(str(DATA) + '|layername=magoh_comuni_confini', 'magoh_comuni_confini', 'ogr')
    return _layer if _layer.isValid() else None


def municipality_for_geometry(geometry, crs=None):
    """Return the MAGOH Comune code (str) for a geometry, or None."""
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    layer = _boundaries()
    if layer is None:
        return None
    geom = QgsGeometry(geometry)
    if crs is not None and crs.isValid() and crs != layer.crs():
        try:
            geom.transform(QgsCoordinateTransform(crs, layer.crs(), QgsProject.instance()))
        except Exception:
            return None
    key = bytes(geom.asWkb())
    if key in _cache:
        return _cache[key]
    gtype = geom.type()
    probe = geom.pointOnSurface() if gtype == QgsWkbTypes.GeometryType.PolygonGeometry else geom
    box = geom.boundingBox()
    if box.isEmpty():
        box = QgsRectangle(box.xMinimum() - 1, box.yMinimum() - 1, box.xMaximum() + 1, box.yMaximum() + 1)
    request = QgsFeatureRequest().setFilterRect(box)
    request.setSubsetOfAttributes(['codice'], layer.fields())
    best, best_share = None, 0.0
    candidates = list(layer.getFeatures(request))
    for feature in candidates:
        boundary = feature.geometry()
        if boundary.contains(geom):
            best = str(feature['codice'])
            break
        if gtype == QgsWkbTypes.GeometryType.PointGeometry:
            continue
        if not boundary.intersects(geom):
            continue
        part = boundary.intersection(geom)
        share = part.area() if gtype == QgsWkbTypes.GeometryType.PolygonGeometry else part.length()
        if share > best_share:
            best, best_share = str(feature['codice']), share
    if best is None and not probe.isEmpty():
        for feature in candidates:
            if feature.geometry().contains(probe):
                best = str(feature['codice'])
                break
    if len(_cache) > 256:
        _cache.clear()
    _cache[key] = best
    return best


def _expression_function(values, feature, parent, context=None):
    try:
        geometry = feature.geometry() if feature is not None else None
        crs = None
        if context is not None:
            value = context.variable('layer_crs')
            if value:
                crs = QgsCoordinateReferenceSystem(str(value))
        return municipality_for_geometry(geometry, crs)
    except Exception:
        return None


_registered = []


def register_expression_function():
    """Expression function used as default value of id_comune for new features."""
    if QgsExpression.isFunctionName(FUNCTION_NAME):
        return
    from qgis.core import qgsfunction

    @qgsfunction(group='MAGOH Spell', usesgeometry=True, referenced_columns=[], register=True,
                 name=FUNCTION_NAME)
    def magoh_comune_da_geometria(feature, parent, context):
        """<h3>magoh_comune_da_geometria()</h3>Restituisce il codice MAGOH del Comune che contiene
        la geometria dell'elemento (o la sua parte maggiore). Usato da MAGOH Spell come valore
        predefinito di id_comune per i nuovi elementi."""
        return _expression_function(None, feature, parent, context)

    _registered.append(magoh_comune_da_geometria)


def unregister_expression_function():
    try:
        if QgsExpression.isFunctionName(FUNCTION_NAME):
            QgsExpression.unregisterFunction(FUNCTION_NAME)
    except Exception:
        pass
    _registered.clear()
    global _layer
    _layer = None
    _cache.clear()
