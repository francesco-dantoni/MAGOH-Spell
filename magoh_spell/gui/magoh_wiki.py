"""Links to the MAGOH Wiki at the top of the native QGIS MAGOH forms.

The existing attribute editor hierarchy (fields, relations, tabs, editors) is
preserved.  The old, obsolete 'Info' attribution widget is removed from the
four affected forms, including when an older MAGOH project is reopened.

No remote requests are made on form opening: the Wiki is opened only after
explicitly clicking the native QGIS action button.
"""

from qgis.PyQt.QtCore import QUuid
from qgis.core import QgsAction, QgsAttributeEditorAction


# Constant UUIDs make reloading a project and restoring an existing workspace
# idempotent. They are distinct from the official GNA MOPR guide action.
WIKI_FORMS = {
    'magoh_scheda_intervento': (
        '{8fb41c90-f31a-4b15-b726-c7a4e4a15401}',
        'https://magoh-wiki.cfs.unipi.it/intervento.html',
        'Apri MAGOH Wiki — Scheda di Intervento',
        'MAGOH Wiki — Intervention record',
    ),
    'magoh_ritrovamenti': (
        '{8fb41c90-f31a-4b15-b726-c7a4e4a15402}',
        'https://magoh-wiki.cfs.unipi.it/ritrovamento.html',
        'Apri MAGOH Wiki — Scheda di Ritrovamento',
        'MAGOH Wiki — Finding record',
    ),
    'magoh_evidenza_archeologica': (
        '{8fb41c90-f31a-4b15-b726-c7a4e4a15403}',
        'https://magoh-wiki.cfs.unipi.it/evidenza-archeologica.html',
        'Apri MAGOH Wiki — Evidenza archeologica',
        'MAGOH Wiki — Archaeological evidence',
    ),
    'magoh_evidenza_archeologica_no_geom': (
        '{8fb41c90-f31a-4b15-b726-c7a4e4a15404}',
        'https://magoh-wiki.cfs.unipi.it/evidenza-archeologica.html',
        'Apri MAGOH Wiki — Evidenza archeologica',
        'MAGOH Wiki — Archaeological evidence',
    ),
}


def _url_action_type():
    try:
        from qgis.core import Qgis
        return Qgis.AttributeActionType.OpenUrl
    except AttributeError:
        return QgsAction.OpenUrl


def _ensure_action(layer, values):
    action_uuid, address, it_label, en_label = values
    uid = QUuid(action_uuid)
    from ..i18n import _tf
    label = _tf(it_label, en_label)
    manager = layer.actions()
    for action in manager.actions():
        if action.id() == uid:
            return action
    action = QgsAction(uid, _url_action_type(), label, address,
                       '', False, label, {'Form', 'Feature', 'Layer'})
    manager.addAction(action)
    return action


def _is_this_action(element, layer, action_uuid):
    if not isinstance(element, QgsAttributeEditorAction):
        return False
    try:
        action = element.action(layer)
        return action.id() == action_uuid
    except Exception:
        return False


def apply_to_layer(layer, values):
    """Install the action as the FIRST form element; delete obsolete Info.

    Attribute-editor containers do not provide insertChildElement in QGIS 3.34.
    Clone existing children before clearing the root; doing so retains all
    original attributes, relations, layout settings, and tab ordering.
    """
    action = _ensure_action(layer, values)
    cfg = layer.editFormConfig()
    root = cfg.invisibleRootContainer()
    old = list(root.children())
    if old and _is_this_action(old[0], layer, action.id()) and not any(
            element.name() == 'Info' for element in old):
        return False

    clones = [element.clone(root) for element in old
              if element.name() != 'Info'
              and not _is_this_action(element, layer, action.id())]
    root.clear()
    root.addChildElement(QgsAttributeEditorAction(action, root))
    for element in clones:
        root.addChildElement(element)
    layer.setEditFormConfig(cfg)
    return True


def install(loaded):
    """Apply only to MAGOH's four main layers; do not touch other layers."""
    count = 0
    for name, values in WIKI_FORMS.items():
        layer = loaded.get(name)
        if layer is None or not layer.isValid():
            continue
        if apply_to_layer(layer, values):
            count += 1
    return count
