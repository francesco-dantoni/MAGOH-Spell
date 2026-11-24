"""Import official presentation without replacing the user's QGIS project.

Clone full, trusted layer definitions (forms, aliases, defaults, renderers,
widgets and help actions), remapping every layer/relation reference per batch.
Existing host styles, layer IDs, relations and edit buffers are not modified.
"""
from pathlib import Path
import copy
import uuid
import re
import xml.etree.ElementTree as ET
from qgis.core import (QgsProject, QgsVectorLayer, QgsReadWriteContext,
                       QgsPathResolver, QgsRelation)
from qgis.PyQt.QtXml import QDomDocument
from ..gna_template_resources import (official_project_xml, materialize_vocabularies,
                                      normalize_dictionary_references, portable_form_code, native_photo_forms)

GENERATED = 'magoh/gna_export_generated'
BATCH = 'magoh/gna_presentation_batch'


#: V1.23.2: layer IDs of the official Template that the official GNA plugin
#: looks up with getLayerById() when it downloads data from the GNA.
GNA_PLUGIN_LAYER_IDS = ('MOPR_9f2ef54b_70dd_412b_a0d8_c6133776fa24',
                        'MOSI_multipolygon_7931f3fe_36bd_43fe_b5bb_0bb1ed6ba32c',
                        'MOSI_multipoint_1d334a5f_d67b_4b85_b45a_89ad206558ee',
                        'MOSI_multilinea_3fbe2d7f_a506_4e1f_8477_a27e6abca119',
                        'R_MOSI_multilinea_f3603817_f00d_42af_b8d8_e331fb4b0a92',
                        'R_MOSI_multipolygon_6df39107_c7f1_4093_8628_472cd7c84455')


def load_official_outputs(gpkg):
    from .layer_organization import gna_group, subgroup, normalize_gna, role_group
    gpkg = Path(gpkg).resolve()
    project = QgsProject.instance()
    for layer in project.mapLayers().values():
        if (layer.customProperty(GENERATED, False) and isinstance(layer, QgsVectorLayer) and
                layer.isEditable() and str(layer.customProperty('magoh/gna_export_path', '')).casefold() == str(gpkg).casefold()):
            names = [l.name() for l in project.mapLayers().values()
                     if l.customProperty(GENERATED, False) and l.customProperty('magoh/gna_ruolo', '') in ('mosi','dsc','mopr')]
            return names, ['Questo pacchetto è già aperto in modifica: nessun layer è stato ricaricato / This package has unsaved edits: no layer reloaded']
    doc = ET.fromstring(official_project_xml())
    dictionary_sources = materialize_vocabularies(gpkg.parent / 'vocabolari_MAGOH')
    normalize_dictionary_references(doc, dictionary_sources)
    batch = uuid.uuid4().hex
    definitions, layers, source_paths = {}, {}, {}
    def load_order(elem):
        source = elem.findtext('datasource', '')
        priority = ('DSC_multipolygon', 'MOPR', 'MOSI_multipolygon', 'MOSI_multipoint', 'MOSI_multilinea')
        table = source.split('|layername=')[-1]
        return priority.index(table) if table in priority else len(priority)
    for elem in sorted(doc.findall('./projectlayers/maplayer'), key=load_order):
        source = elem.findtext('datasource', '')
        ident = elem.findtext('id')
        if source in dictionary_sources:
            path = str(dictionary_sources[source])
        elif source.startswith('./Template_GNA.gpkg|layername='):
            table = source.split('|layername=', 1)[1]
            path = str(gpkg) + '|layername=' + table
        else:
            continue  # no WMS services, remote geometry or unrelated host layers
        # V1.23.2: all three MOSI layers, empty ones too: the official GNA
        # plugin writes the records it downloads into each of them.
        definitions[ident], source_paths[ident] = copy.deepcopy(elem), path

    previous = [l for l in project.mapLayers().values() if l.customProperty(GENERATED, False)]
    from .mopr_single import ORIGIN
    origins = []
    for layer in previous:
        if layer.customProperty('magoh/gna_ruolo', '') == 'mopr' and layer.customProperty(ORIGIN, ''):
            origins.append(({(str(f['CPR']), str(f['MOPR_ID'])) for f in layer.getFeatures()}, str(layer.customProperty(ORIGIN))))
    warnings = []
    previous_ids = {l.id() for l in previous}
    custom_relations = any(not r.id().startswith('magoh_gna_') and
                           (r.referencedLayerId() in previous_ids or r.referencingLayerId() in previous_ids)
                           for r in project.relationManager().relations().values())
    preserve = custom_relations or any(isinstance(l, QgsVectorLayer) and l.isEditable() for l in previous)
    if preserve:
        warnings.append('Esportazione precedente conservata con tutti i riferimenti: modifiche non salvate o relazioni personalizzate / Previous export retained with all references: unsaved edits or custom relations')
    # V1.23.2: keep the layer IDs of the official Template, as in the package
    # project, so that the official GNA plugin finds MOPR and MOSI here too.
    # Fresh IDs only when one of them is already taken by a layer that stays.
    staying = set(project.mapLayers()) - (set() if preserve else previous_ids)
    official_ids = not (set(definitions) & staying)
    if official_ids:
        ids = {ident: ident for ident in definitions}
    else:
        options = QgsVectorLayer.LayerOptions()
        options.loadDefaultStyle = False  # the trusted official style is applied below
        for ident, elem in definitions.items():
            layers[ident] = QgsVectorLayer(source_paths[ident], elem.findtext('layername', ''), 'ogr', options)
        ids = {ident: layer.id() for ident, layer in layers.items()}
        warnings.append('Identificativi del Template GNA già in uso nel progetto: il plugin GNA ufficiale non potrà importare dati qui finché '
                        "l'esportazione precedente resta aperta / Template GNA layer IDs already in use: the official GNA plugin cannot "
                        'download into this project while the previous export stays open')

    relation_nodes = [r for r in doc.findall('./relations/relation')
                      if r.get('referencedLayer') in ids and r.get('referencingLayer') in ids]
    relation_ids = {r.get('id'): 'magoh_gna_' + batch + '_' + r.get('id') for r in relation_nodes}
    substitutions = dict(ids, **relation_ids)
    for ident, elem in definitions.items():
        native_photo_forms(elem)
        elem.find('datasource').text = source_paths[ident]
        # Keep the trusted image preview working with Qt6 scoped enums too.
        code = elem.find('editforminitcode')
        if code is not None and code.text:
            code.text = portable_form_code(code.text)
        replacements = {remote: str(path) for remote, path in dictionary_sources.items()}
        replacements['./Template_GNA.gpkg'] = str(gpkg)
        for node in elem.iter():
            if node.text:
                for old, new in replacements.items():
                    node.text = node.text.replace(old, new)
            for key, value in list(node.attrib.items()):
                for old, new in replacements.items():
                    value = value.replace(old, new)
                node.set(key, value)
        xml = ET.tostring(elem, encoding='unicode')
        # Substitute once: a newly generated layer ID may contain a short
        # relation ID such as MOPR_04. Sequential replace() could corrupt it.
        pattern = '|'.join(re.escape(old) for old in sorted(substitutions, key=len, reverse=True))
        xml = re.sub(pattern, lambda match: substitutions[match.group()], xml)
        # Also replace fallback datasource metadata; no reference can resolve
        # to the bundled template, an old export or a remote vocabulary.
        dom = QDomDocument()
        parsed = dom.setContent(xml)
        if isinstance(parsed, tuple):
            parsed = parsed[0]
        if not parsed:
            raise ValueError('Definizione GNA XML non valida / Invalid GNA XML')
        context = QgsReadWriteContext()
        context.setPathResolver(QgsPathResolver(str(gpkg.parent / 'host.qgs')))
        if official_ids:
            # The complete layer definition of the Template, its ID included,
            # read on the package GeoPackage (datasource already rewritten).
            layer = QgsVectorLayer()
            result = layer.readLayerXml(dom.documentElement(), context)
            if not result or layer.id() != ident:
                raise ValueError('Impossibile leggere il layer GNA / GNA layer could not be read: ' + elem.findtext('layername', ident))
            layers[ident] = layer
        else:
            # Import the complete QML presentation, not the datasource/provider
            # state of the foreign project into an already-created OGR layer.
            result = layers[ident].readSymbology(dom.documentElement(), '', context)
            if isinstance(result, tuple):
                result = result[0]
            if not result:
                raise ValueError('Impossibile applicare la presentazione GNA / GNA presentation could not be applied')
        if not layers[ident].isValid():
            raise ValueError('Layer GNA non valido / Invalid GNA layer: ' + layers[ident].name())
        # V1.23: in the MAGOH working project the GNA data layers (MOSI, DSC,
        # MOPR) stay editable and every layer can be removed from the legend.
        # Only the vocabulary tables keep the read-only state of the Template.
        # The standalone GNA package project is not touched here.
        from .mopr_workspace import make_removable, unlock_workspace_layer
        table = source_paths[ident].split('|layername=')[-1] if '|layername=' in source_paths[ident] else ''
        if table == 'MOPR' or table.startswith(('MOSI_', 'DSC_')):
            unlock_workspace_layer(layers[ident])
        else:
            layers[ident].setReadOnly(elem.get('readOnly') == '1')
            make_removable(layers[ident])

    added, added_relations = [], []
    loaded = []
    try:
        if official_ids and not preserve and set(definitions) & previous_ids:
            # Same IDs as the export already open: it leaves the project first.
            project.removeMapLayers([l.id() for l in previous])
            previous = []
        group = gna_group()
        tables_group = subgroup(group, 'thesauri GNA')
        tables_group.setExpanded(False)
        for ident, layer in layers.items():
            elem = definitions[ident]
            source = elem.findtext('datasource', '')
            table = source.split('|layername=')[-1] if '|layername=' in source else ''
            role = 'mosi' if table.startswith('MOSI_') else 'dsc' if table.startswith('DSC_') else 'mopr' if table == 'MOPR' else ''
            layer.setCustomProperty(GENERATED, True)
            layer.setCustomProperty(BATCH, batch)
            layer.setCustomProperty('magoh/gna_export_path', str(gpkg))
            layer.setCustomProperty('magoh/gna_ruolo', role)
            layer.setCustomProperty('magoh/gna_official_presentation', '1.6.0')
            if table == 'MOPR':
                layer.setName('MOPR')
            if project.addMapLayer(layer, False) is None:
                raise ValueError('Layer GNA non aggiunto al progetto / GNA layer not added: ' + layer.name())
            added.append(layer.id())
            parent = role_group(group, role) if role else tables_group
            node = parent.addLayer(layer)
            node.setItemVisibilityChecked(role in ('mosi', 'dsc') and layer.featureCount() > 0)
            if role:
                loaded.append(layer.name())
        for elem in relation_nodes:
            relation = QgsRelation()
            relation.setId(relation_ids[elem.get('id')])
            relation.setName(elem.get('name', 'GNA'))
            relation.setReferencedLayer(ids[elem.get('referencedLayer')])
            relation.setReferencingLayer(ids[elem.get('referencingLayer')])
            for ref in elem.findall('fieldRef'):
                relation.addFieldPair(ref.get('referencingField'), ref.get('referencedField'))
            if not relation.isValid():
                raise ValueError('Relazione GNA non valida / Invalid GNA relation: ' + relation.name())
            project.relationManager().addRelation(relation)
            added_relations.append(relation.id())
        for layer in layers.values():
            layer.resolveReferences(project)
        if not preserve:
            for layer in layers.values():
                if layer.customProperty('magoh/gna_ruolo', '') == 'mopr':
                    keys = {(str(f['CPR']), str(f['MOPR_ID'])) for f in layer.getFeatures()}
                    matching = {origin for old_keys, origin in origins if keys and old_keys == keys}
                    if len(matching) == 1:
                        layer.setCustomProperty(ORIGIN, matching.pop())
                        layer.setCustomProperty('magoh/gna_mopr_workspace', True)
            project.removeMapLayers([l.id() for l in previous])
        normalize_gna()
        return loaded, warnings
    except Exception:
        for ident in added_relations:
            project.relationManager().removeRelation(ident)
        project.removeMapLayers(added)
        raise


def upgrade_generated_ids():
    """V1.23.2: projects saved before: reload the GNA package with the official IDs.

    Only when it is safe: one package, file present, nothing in editing, no
    user relation on those layers, IDs not taken by other layers. The reload
    replaces plugin-generated layers only; data files are not touched.
    """
    project = QgsProject.instance()
    generated = [l for l in project.mapLayers().values()
                 if l.customProperty(GENERATED, False) and l.customProperty('magoh/gna_official_presentation', '')]
    if not generated or any(l.id() in GNA_PLUGIN_LAYER_IDS for l in generated):
        return None
    paths = {str(l.customProperty('magoh/gna_export_path', '')) for l in generated}
    if len(paths) != 1 or not Path(next(iter(paths))).is_file():
        return None
    if any(isinstance(l, QgsVectorLayer) and l.isEditable() for l in generated):
        return None
    ids = {l.id() for l in generated}
    if any(not r.id().startswith('magoh_gna_') and (r.referencedLayerId() in ids or r.referencingLayerId() in ids)
           for r in project.relationManager().relations().values()):
        return None
    if set(GNA_PLUGIN_LAYER_IDS) & (set(project.mapLayers()) - ids):
        return None
    return load_official_outputs(next(iter(paths)))


def apply_workspace_mopr_style(layer):
    """Official perimeter appearance only; keep MAGOH's editable custom form."""
    if layer.customProperty('magoh/gna_workspace_style', '') == '1.6.0':
        return
    from qgis.core import QgsFeatureRenderer
    doc = ET.fromstring(official_project_xml())
    elem = next(l for l in doc.findall('./projectlayers/maplayer')
                if l.findtext('datasource') == './Template_GNA.gpkg|layername=MOPR')
    dom = QDomDocument()
    dom.setContent(ET.tostring(elem, encoding='unicode'))
    renderer = QgsFeatureRenderer.load(dom.documentElement().firstChildElement('renderer-v2'), QgsReadWriteContext())
    if renderer is None:
        raise ValueError('Stile MOPR ufficiale non disponibile / Official MOPR style unavailable')
    layer.setRenderer(renderer)
    layer.setOpacity(float(elem.findtext('layerOpacity', '1')))
    layer.setCustomProperty('magoh/gna_workspace_style', '1.6.0')


def repair_known_generated_forms(*args):
    """Upgrade only generated forms whose Python matches the audited template.

    This changes project presentation in memory, never user records or global
    macro permissions. Unknown/custom Python is deliberately left untouched.
    """
    from qgis.core import QgsAttributeEditorQmlElement, QgsAttributeEditorContainer
    candidates = [layer for layer in QgsProject.instance().mapLayers().values()
                  if isinstance(layer, QgsVectorLayer) and layer.customProperty(GENERATED, False)
                  and layer.editFormConfig().initFunction() == 'formOpen']
    if not candidates:
        return
    trusted = ET.fromstring(official_project_xml())
    allowed = {node.findtext('editforminitcode') for node in trusted.findall('./projectlayers/maplayer')
               if node.findtext('editforminit') == 'formOpen'}
    allowed.update(portable_form_code(code) for code in tuple(allowed) if code)
    native_photo_forms(trusted)
    qml_code = next(node.text for node in trusted.findall('.//attributeEditorQmlElement') if node.get('name') == 'MAGOHPhotoPreview')
    for layer in candidates:
        config = layer.editFormConfig()
        if config.initFunction() != 'formOpen' or config.initCode() not in allowed:
            continue
        def upgrade(container):
            if not isinstance(container, QgsAttributeEditorContainer):
                return
            children = container.children()
            if any(c.name() == 'foto' for c in children) and not any(c.name() == 'MAGOHPhotoPreview' for c in children):
                element = QgsAttributeEditorQmlElement('MAGOHPhotoPreview', container)
                element.setShowLabel(False)
                element.setQmlCode(qml_code)
                container.addChildElement(element)
            for child in children:
                upgrade(child)
        for tab in config.tabs():
            upgrade(tab)
        config.setInitFunction('')
        config.setInitCode('')
        layer.setEditFormConfig(config)
