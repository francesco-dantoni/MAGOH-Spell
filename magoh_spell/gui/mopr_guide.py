"""Official GNA guide to MOPR compilation, reachable wherever a MOPR is filled in.

The link sits above the tabs of the MAGOH-GNA converter, in the "1 · MOPR"
page of the converter and in the native QGIS form of the "MOPR — compilazione"
layer (identify, attribute table, edit form).

The URL is opened only when the user clicks: nothing is fetched otherwise.
"""
from qgis.PyQt.QtCore import QUrl, QUuid, QSize
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QPushButton
from qgis.core import QgsAction

MOPR_GUIDE_URL = 'https://gna.cultura.gov.it/wiki/index.php/Compilare_il_MOPR'
# Stable identifier: re-configuring a layer never duplicates the action.
ACTION_ID = QUuid('{6d4f2b7e-1c3a-4b8e-9f51-4d0a8c2e7a14}')


def _t(it_text, en_text):
    from ..i18n import _tf
    return _tf(it_text, en_text)


def guide_label():
    return _t('Guida alla compilazione MOPR (wiki GNA)', 'MOPR completion guide (GNA wiki)')


def open_guide(*_args):
    return QDesktopServices.openUrl(QUrl(MOPR_GUIDE_URL))


def guide_button(parent=None, object_name='MagohMoprGuide'):
    """A recognisable button (info icon, explicit text, URL in the tooltip)."""
    button = QPushButton(guide_label(), parent)
    button.setObjectName(object_name)
    button.setAutoDefault(False)
    try:
        from ..ui_theme import ui_icon
        button.setIcon(ui_icon('info'))
        button.setIconSize(QSize(16, 16))
    except Exception:
        pass
    button.setToolTip(_t('Apre nel browser la guida ufficiale del Geoportale Nazionale per l’Archeologia:\n',
                         'Opens the official National Geoportal for Archaeology guide in the browser:\n') + MOPR_GUIDE_URL)
    button.setAccessibleName(guide_label())
    button.clicked.connect(open_guide)
    return button


def _open_url_type():
    try:
        from qgis.core import Qgis
        return Qgis.AttributeActionType.OpenUrl
    except AttributeError:
        return QgsAction.OpenUrl


def ensure_layer_action(layer):
    """Add (once) an 'open URL' action to a MOPR layer; returns the action.

    Scopes: Form (button inside the QGIS attribute form), Feature (attribute
    table / identify) and Layer (attribute table toolbar).
    """
    manager = layer.actions()
    for action in manager.actions():
        if action.id() == ACTION_ID:
            return action
    action = QgsAction(ACTION_ID, _open_url_type(), guide_label(), MOPR_GUIDE_URL, '', False,
                       guide_label(), {'Form', 'Feature', 'Layer'})
    manager.addAction(action)
    return action
