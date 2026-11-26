"""Organize only MAGOH-owned nodes, preserving layers and user edit buffers."""
import os
from qgis.core import QgsProject, QgsLayerTreeGroup, QgsVectorLayer, QgsProviderRegistry

MARK = 'magoh/tree_role'

#: same group names as the official Template. The official GNA plugin
#: looks for «SITI ARCHEOLOGICI E GEO-PALEONTOLOGICI (MOSI)» when it downloads.
GROUP_NAMES = {'mosi': 'SITI ARCHEOLOGICI E GEO-PALEONTOLOGICI (MOSI)',
               'dsc': 'DATI DI SCAVO (DSC)',
               'mopr': 'MOPR'}
LEGACY_NAMES = {'mosi': 'MOSI', 'dsc': 'DSC'}

#: Temporary layers of the official GNA plugin: links to the national database
#: that the plugin removes itself at the end of a download.
GNA_PLUGIN_TEMPORARY = ('GNA_mosi_multipoint_pt', 'GNA_mosi_multipolygon_pl', 'GNA_mosi_multilinea_ln',
                        'GNA_r_mosi_multilinea_ln', 'GNA_r_mosi_multipolygon_pl', 'GNA_dsc_multipolygon_pl',
                        'GNA_dsc_multilinea_ln', 'GNA_dsc_multipoint_pt')


def move(node, parent, index=None):
    old = node.parent()
    if old is parent and (index is None or parent.children().index(node) == index):
        return node
    copy = node.clone()
    parent.insertChildNode(len(parent.children()) if index is None else index, copy)
    if old is not None:
        old.removeChildNode(node)
    return copy


def subgroup(parent, name):
    found = next((n for n in parent.children() if isinstance(n, QgsLayerTreeGroup) and n.name() == name), None)
    return found if found is not None else parent.addGroup(name)


def role_group(group, role):
    """Subgroup of the GNA group with the official name; renames an older one."""
    name = GROUP_NAMES[role]
    found = next((n for n in group.children() if isinstance(n, QgsLayerTreeGroup) and n.name() == name), None)
    legacy = next((n for n in group.children() if isinstance(n, QgsLayerTreeGroup)
                   and n.name() == LEGACY_NAMES.get(role)), None)
    if found is None and legacy is not None:
        legacy.setName(name)
        return legacy
    if found is not None and legacy is not None:
        for child in list(legacy.children()):
            move(child, found)
        if not legacy.children():
            group.removeChildNode(legacy)
    return found if found is not None else group.addGroup(name)


def remove_gna_plugin_leftovers():
    """national-database layers left behind by an interrupted GNA download.

    The official plugin adds them only while it downloads and removes them at
    the end. Only those exact names, read from the national schema, are
    removed from the project; no file and no user layer is touched.
    """
    from ..gna_identity import national_service
    project = QgsProject.instance()
    stale = [l.id() for l in project.mapLayers().values()
             if l.name() in GNA_PLUGIN_TEMPORARY and national_service(l)]
    if stale:
        project.removeMapLayers(stale)
    return len(stale)


def gna_group():
    root = QgsProject.instance().layerTreeRoot()
    group = subgroup(root, 'GNA')
    group.setCustomProperty(MARK, 'gna')
    return move(group, root, 0)


def normalize_gna():
    remove_gna_plugin_leftovers()
    root = QgsProject.instance().layerTreeRoot()
    group = next((n for n in root.children() if isinstance(n, QgsLayerTreeGroup) and n.name() == 'GNA'), None)
    if group is None:
        return
    from .mopr_single import reconcile
    reconcile()
    group = move(group, root, 0)
    # Workspace MOPR layers of older projects may sit directly in GNA.
    for node in list(group.findLayers()):
        layer = node.layer()
        if layer is None:
            continue
        role = str(layer.customProperty('magoh/gna_ruolo', '')).lower()
        working_file = os.path.basename(str(layer.source()).split('|', 1)[0]).casefold() == 'mopr.gpkg'
        from .mopr_workspace import make_removable, unlock_workspace_layer
        if layer.customProperty('magoh/gna_mopr_workspace', False) or working_file:
            unlock_workspace_layer(layer)
        elif layer.customProperty('magoh/gna_export_generated', False):
            # GNA layers added by the plugin, also in older projects.
            # User-added layers are left as they are.
            if role in ('mosi', 'dsc', 'mopr'):
                unlock_workspace_layer(layer)
            else:
                make_removable(layer)
        target = 'mopr' if layer.customProperty('magoh/gna_mopr_workspace', False) else (
            'mosi' if role.startswith('mosi') else 'dsc' if role == 'dsc' else None)
        if target:
            move(node, role_group(group, target))
    for index, role in enumerate(('mosi', 'dsc', 'mopr')):
        child = role_group(group, role)
        child.setCustomProperty(MARK, 'gna_' + role)
        move(child, group, index)


def normalize_magoh(gpkg_path):
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    target = os.path.normcase(os.path.abspath(gpkg_path))
    own = {}
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsVectorLayer) or layer.providerType() != 'ogr':
            continue
        uri = QgsProviderRegistry.instance().decodeUri('ogr', layer.source())
        if uri.get('path') and os.path.normcase(os.path.abspath(uri['path'])) == target:
            own[layer.id()] = (layer, uri.get('layerName', ''))
    if not own:
        return
    group = subgroup(root, 'MAGOH')
    group.setCustomProperty(MARK, 'magoh')
    thesauri = subgroup(group, 'thesauri')
    evidence = subgroup(group, 'evidenze_archeologiche')
    main = {'magoh_ritrovamenti', 'magoh_scheda_intervento'}
    for ident, (layer, table) in own.items():
        parent = evidence if table.startswith('magoh_evidenza_archeologica') else group if table in main else thesauri
        nodes = [n for n in root.findLayers() if n.layerId() == ident]
        if nodes:
            move(nodes[0], parent)
            for duplicate in nodes[1:]:
                duplicate.parent().removeChildNode(duplicate)
        else:
            parent.addLayer(layer)
    order = [thesauri]
    for table in ('magoh_ritrovamenti',):
        order.extend(n for n in group.children() if hasattr(n, 'layerId') and own.get(n.layerId(), (None, None))[1] == table)
    order.append(evidence)
    order.extend(n for n in group.children() if hasattr(n, 'layerId') and own.get(n.layerId(), (None, None))[1] == 'magoh_scheda_intervento')
    for index, node in enumerate(order):
        moved = move(node, group, index)
        if node is evidence:
            evidence = moved
    for index, table in enumerate(('magoh_evidenza_archeologica_no_geom', 'magoh_evidenza_archeologica')):
        node = next((n for n in evidence.children() if hasattr(n, 'layerId') and own.get(n.layerId(), (None, None))[1] == table), None)
        if node is not None:
            move(node, evidence, index)
    # Empty legacy wrappers only; unrelated layers inside them are retained.
    # also the empty "thesuari" group (misspelt) and the old
    # "MAGOH_Spell"/"MAGOH Spell" wrappers left by the embedded template projects,
    # plus empty duplicates of the canonical subgroups outside the MAGOH group.
    wrappers = ('MAGOH – Offline', 'MAGOH-Offline', 'MAGOH_offline', 'MAGOH Spell', 'MAGOH – Spell')
    canonical = ('thesauri', 'evidenze_archeologiche')
    def prune(parent, legacy=False):
        for child in list(parent.children()):
            if isinstance(child, QgsLayerTreeGroup):
                owned = legacy or child.name() in wrappers
                prune(child, owned)
                if child.children():
                    continue
                name = child.name()
                if (owned and name in wrappers + ('Layers', 'Tables')) or name == 'thesuari' \
                        or (parent is group and name in ('Layers', 'Tables')) \
                        or (name in canonical and parent is not group):
                    parent.removeChildNode(child)
    prune(root)
    normalize_gna()
    group = next(n for n in root.children() if isinstance(n, QgsLayerTreeGroup) and n.name() == 'MAGOH')
    # Keep arbitrary host groups in place; put MAGOH before the first basemap.
    nodes = root.children()
    basemap = next((i for i, n in enumerate(nodes) if isinstance(n, QgsLayerTreeGroup) and 'basemap' in n.name().casefold()), None)
    if basemap is not None and nodes.index(group) > basemap:
        move(group, root, basemap)
