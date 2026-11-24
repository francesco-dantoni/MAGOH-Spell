"""Read MAGOH sources and load export previews without discarding user edits."""
import json
from pathlib import Path
from qgis.core import (QgsProject, QgsVectorLayer, QgsGeometry, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsLayerTreeGroup)
from ..conversion.validators import normalizza_valore
from ..i18n import _tf as t
from .gna_symbology import applica_simbologia

GENERATED = 'magoh/gna_export_generated'
GROUP = 'magoh/gna_export_group'
ALIASES = {'i': ('magoh_scheda_intervento', 'scheda_intervento'),
           'r': ('magoh_ritrovamenti', 'ritrovamenti'),
           'e': ('magoh_evidenza_archeologica', 'evidenza_archeologica')}


def find(kind):
    for layer in QgsProject.instance().mapLayers().values():
        if isinstance(layer, QgsVectorLayer) and layer.name().casefold() in ALIASES[kind]:
            return layer
    return None


def rows(layer):
    if layer is None: return []
    target = QgsCoordinateReferenceSystem('EPSG:4326')
    output = []
    for feature in layer.getFeatures():
        record = {f.name(): normalizza_valore(feature[f.name()]) for f in feature.fields()}
        record['__geom'] = None
        if feature.hasGeometry() and not feature.geometry().isEmpty():
            if not layer.crs().isValid():
                raise ValueError(t('Sistema di riferimento mancante: ', 'Missing coordinate reference system: ') + layer.name())
            geometry = QgsGeometry(feature.geometry())
            if layer.crs() != target:
                geometry.transform(QgsCoordinateTransform(layer.crs(), target, QgsProject.instance()))
            record['__geom'] = json.loads(geometry.asJson())
        output.append(record)
    return output


def _prune_empty_generated_groups(group):
    for child in list(group.children()):
        if isinstance(child, QgsLayerTreeGroup):
            _prune_empty_generated_groups(child)
            if child.customProperty(GROUP, False) and not child.children():
                group.removeChildNode(child)


def load_gna_outputs(directory, gpkg=None):
    if gpkg:
        from .gna_presentation import load_official_outputs
        return load_official_outputs(gpkg)
    from .layer_organization import gna_group, normalize_gna, role_group
    project = QgsProject.instance()
    warnings = []
    removable = []
    for layer in project.mapLayers().values():
        if layer.customProperty(GENERATED, False):
            if isinstance(layer, QgsVectorLayer) and layer.isEditable():
                warnings.append('Anteprima precedente conservata perché in modifica: ' + layer.name())
            else:
                removable.append(layer.id())
    project.removeMapLayers(removable)
    root = project.layerTreeRoot()
    _prune_empty_generated_groups(root)
    group = gna_group()
    subgroups = {}
    for name in ('MOSI', 'DSC'):
        subgroups[name] = role_group(group, name.lower())
        subgroups[name].setCustomProperty(GROUP, True)
    specs = [
        ('DSC', 'DSC_multipolygon.geojson', 'DSC — esito indagine (DSCEX)', 'dsc'),
        ('MOSI', 'MOSI_multipolygon.geojson', 'MOSI — areali (MultiPolygon)', 'mosi_poligono'),
        ('MOSI', 'MOSI_multipoint.geojson', 'MOSI — puntuali (MultiPoint)', 'mosi_punto'),
        ('MOSI', 'MOSI_multiline.geojson', 'MOSI — lineari (MultiLineString)', 'mosi_linea'),
    ]
    loaded = []
    for parent, filename, name, role in specs:
        path = Path(gpkg) if gpkg else Path(directory) / parent / filename
        if not path.is_file():
            warnings.append('Anteprima mancante: ' + str(path))
            continue
        table = Path(filename).stem.replace('MOSI_multiline', 'MOSI_multilinea')
        source = str(path)+'|layername='+table if gpkg else str(path)
        layer = QgsVectorLayer(source, name, 'ogr')
        if not layer.isValid():
            warnings.append('Impossibile caricare l’anteprima: ' + filename)
            continue
        if parent == 'MOSI' and layer.featureCount() == 0:
            # V1.15: only MOSI geometry types actually produced by the conversion.
            continue
        layer.setCustomProperty(GENERATED, True)
        layer.setCustomProperty('magoh/gna_export_path', str(path))
        layer.setCustomProperty('magoh/gna_ruolo', role)
        applica_simbologia(layer, role)
        project.addMapLayer(layer, False)
        subgroups[parent].addLayer(layer)
        loaded.append(name)
    normalize_gna()
    return loaded, warnings
