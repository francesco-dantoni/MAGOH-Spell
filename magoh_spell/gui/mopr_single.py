"""One editable plugin-owned MOPR, without deleting files or losing edit buffers."""
from pathlib import Path
from qgis.core import QgsProject, QgsVectorLayer, QgsProviderRegistry
from .mopr_workspace import WORKSPACE_PROP, REQUIRED, value, unlock_workspace_layer

ORIGIN = 'magoh/gna_mopr_origin'
GENERATED = 'magoh/gna_export_generated'


def mopr_layers():
    return [layer for layer in QgsProject.instance().mapLayers().values()
            if isinstance(layer, QgsVectorLayer) and layer.providerType() == 'ogr'
            and QgsProviderRegistry.instance().decodeUri('ogr', layer.source()).get('layerName') == 'MOPR']


def reconcile():
    """Keep the exported MOPR with its native relations; retire only its twin.

    An original workspace file is never removed. Different perimeters, user
    relationships or active editing deliberately prevent automatic removal.
    """
    project = QgsProject.instance()
    layers = mopr_layers()
    for layer in layers:
        if layer.customProperty(WORKSPACE_PROP, False) or layer.customProperty(GENERATED, False):
            unlock_workspace_layer(layer)
    outputs = [l for l in layers if l.customProperty(GENERATED, False)]
    working = [l for l in layers if l.customProperty(WORKSPACE_PROP, False) and not l.customProperty(GENERATED, False)]
    for old in working:
        if old.isEditable():
            continue
        if any(r.referencedLayerId() == old.id() or r.referencingLayerId() == old.id()
               for r in project.relationManager().relations().values()):
            continue
        originals = list(old.getFeatures())
        if not originals:
            continue
        matching = []
        for output in outputs:
            if output.isEditable() or output.crs() != old.crs():
                continue
            indexed = {}
            for feature in output.getFeatures():
                key = (value(feature['CPR']), value(feature['MOPR_ID']))
                indexed.setdefault(key, []).append(feature)
            def equivalent(original):
                key = (value(original['CPR']), value(original['MOPR_ID']))
                candidates = indexed.get(key, [])
                def normalized(feature, field):
                    raw = value(feature[field])
                    return raw.strip('{}').strip() if field == 'FUR' else raw
                return len(candidates) == 1 and all(normalized(original, field) == normalized(candidates[0], field) for field in REQUIRED) and original.geometry().isGeosEqual(candidates[0].geometry())
            if all(equivalent(f) for f in originals):
                matching.append(output)
        if len(matching) != 1:
            continue
        output = matching[0]
        path = QgsProviderRegistry.instance().decodeUri('ogr', old.source()).get('path', '')
        output.setCustomProperty(ORIGIN, project.writePath(path))
        output.setCustomProperty(WORKSPACE_PROP, True)
        output.setName('MOPR')
        project.removeMapLayer(old.id())  # project object only, never the file


def from_origin(path):
    project = QgsProject.instance()
    target = Path(path).resolve()
    candidates = [l for l in mopr_layers() if l.customProperty(GENERATED, False)
                  and l.customProperty(ORIGIN, '')
                  and Path(project.readPath(str(l.customProperty(ORIGIN)))).resolve() == target]
    return candidates[0] if len(candidates) == 1 else None
