"""Exact, read-only filtering of SearchController records (GeoJSON / EPSG:4326).

No undocumented API parameters. This module does not access live project layers
from worker threads and never changes the scientific geometry stored in a record.
"""
import json
from collections import Counter

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
    QgsPalLayerSettings, QgsVectorLayerSimpleLabeling,
)

from .api_geometry import in_4326
from .api_structure import ENTITY_SPECS, PREBUILT_MAP_FIELDS, decode_json_value, flatten_search_docs
from .local_codes import shown_id

CORE = ('intervento', 'ritrovamento', 'iv_livello')
WGS84 = 'EPSG:4326'


def transformed_area(geometry, source_crs, context):
    """Validate an AOI and transform a private copy, never the source feature."""
    if not source_crs.isValid():
        raise ValueError('CRS non valido / Invalid CRS')
    area = QgsGeometry(geometry)
    if area.isNull() or area.isEmpty() or area.type() != 2 or not area.isGeosValid():
        raise ValueError('Area poligonale vuota o non valida / Empty or invalid polygon area')
    target = QgsCoordinateReferenceSystem(WGS84)
    if source_crs != target:
        # Preserve curved projected edges more faithfully when transforming an AOI.
        box = area.boundingBox()
        step = max(box.width(), box.height()) / 256.0
        if step > 0:
            area = area.densifyByDistance(step)
        area.transform(QgsCoordinateTransform(source_crs, target, context))
    box = area.boundingBox()
    if (not area.isGeosValid() or box.xMinimum() < -180 or box.xMaximum() > 180
            or box.yMinimum() < -90 or box.yMaximum() > 90 or box.width() > 180):
        raise ValueError('Area fuori dominio o attraverso l’antimeridiano / Area outside CRS domain or crossing antimeridian')
    return area


def record_identity(record, key):
    for field in ENTITY_SPECS[key]['id_fields']:
        if record.get(field) not in (None, ''):
            return str(record[field])
    # A missing identifier must not collapse unrelated records into one.
    return json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)


def _own_geometry(record, key):
    """Geometria propria del record in EPSG:4326 (lettore condiviso api_geometry)."""
    raw = decode_json_value(record.get(ENTITY_SPECS[key]['geometry_field']))
    if raw in (None, '', {}):
        return None, 'missing'
    geom, _note, error = in_4326(raw)
    if geom is None:
        return None, 'missing' if error == 'geometria assente' else 'invalid'
    if not geom.isGeosValid():
        return None, 'invalid'
    return geom, 'valid'


def filter_page(task, docs, area_wkb, selected, include_parents=True):
    """QgsTask worker: exact own-geometry matches; nulls inherit nearest ancestor.

    Invalid geometries are reported and NOT replaced with centroids/ancestors.
    Parents included solely as dependencies never pull in unrelated siblings.
    """
    area = QgsGeometry()
    area.fromWkb(area_wkb)
    if area.isNull() or not area.isGeosValid():
        raise ValueError('Invalid spatial filter')
    engine = QgsGeometry.createGeometryEngine(area.constGet())
    engine.prepareGeometry()
    box = area.boundingBox()
    output = {key: [] for key in ENTITY_SPECS}
    stats = Counter()
    matched_ids, inherited_ids, dependency_ids = set(), set(), set()
    for doc in docs:
        if task is not None and task.isCanceled():
            return None
        flat = flatten_search_docs([doc])
        nodes = {}
        for key in CORE:
            for record in flat[key]:
                ident = record_identity(record, key)
                if key == 'intervento':
                    parent = None
                elif key == 'ritrovamento':
                    parent = ('intervento', str(record.get('parent_intervento_id', '')))
                else:
                    parent = ('ritrovamento', str(record.get('parent_ritrovamento_id', '')))
                geom, state = _own_geometry(record, key)
                inherited = False
                if state == 'missing':
                    match = bool(nodes.get(parent, {}).get('match', False))
                    inherited = match
                    stats['null_geometry'] += 1
                elif state == 'invalid':
                    match = False
                    stats['invalid_geometry'] += 1
                else:
                    match = box.intersects(geom.boundingBox()) and engine.intersects(geom.constGet())
                nodes[(key, ident)] = {'record': record, 'parent': parent, 'match': match,
                                       'inherited': inherited, 'geometry_state': state}
        chosen = {ident for ident, node in nodes.items() if ident[0] in selected and node['match']}
        stats['matches'] += len(chosen)
        stats['inherited_matches'] += sum(nodes[i]['inherited'] for i in chosen)
        matched_ids.update(chosen)
        inherited_ids.update(i for i in chosen if nodes[i]['inherited'])
        dependencies = set()
        if include_parents:
            for ident in tuple(chosen):
                parent = nodes[ident]['parent']
                while parent in nodes:
                    # Never reintroduce an invalid geometry through dependency
                    # expansion. The importer can use an existing trusted parent;
                    # otherwise it reports and skips children with a missing parent.
                    if nodes[parent]['geometry_state'] == 'invalid':
                        break
                    if parent not in chosen:
                        dependencies.add(parent)
                    parent = nodes[parent]['parent']
        stats['dependency_parents'] += len(dependencies)
        dependency_ids.update(dependencies)
        for key, ident in chosen | dependencies:
            record = nodes[(key, ident)]['record']
            excluded = set(ENTITY_SPECS[key].get('children', ())) | PREBUILT_MAP_FIELDS
            output[key].append({k: v for k, v in record.items() if k not in excluded})
    result_stats = dict(stats)
    result_stats['_match_ids'] = list(matched_ids)
    result_stats['_inherited_ids'] = list(inherited_ids)
    result_stats['_dependency_ids'] = list(dependency_ids)
    return output, result_stats


def apply_intervention_labels(layer):
    """Change the label source only, retaining existing simple/rule-based styling."""
    field = next((f.name() for f in layer.fields() if f.name().casefold() == 'idscheda'), None)
    if not field:
        return False
    labeling = layer.labeling().clone() if layer.labeling() else None

    has_code = layer.fields().indexOf('codice_locale') >= 0

    def configure(settings):
        settings.fieldName = shown_id(field, 'nuova') if has_code else field
        settings.isExpression = has_code
        return settings

    if labeling is None:
        labeling = QgsVectorLayerSimpleLabeling(configure(QgsPalLayerSettings()))
    elif labeling.type() == 'rule-based':
        def walk(rule):
            if rule.settings() is not None:
                rule.setSettings(configure(QgsPalLayerSettings(rule.settings())))
            for child in rule.children():
                walk(child)
        walk(labeling.rootRule())
    else:
        labeling = QgsVectorLayerSimpleLabeling(configure(QgsPalLayerSettings(labeling.settings())))
    layer.setLabeling(labeling)
    layer.setLabelsEnabled(True)
    layer.setDisplayExpression(shown_id(field) if has_code else '"' + field.replace('"', '""') + '"')
    layer.setCustomProperty('magoh/api_idscheda_labels', True)
    layer.triggerRepaint()
    return True
