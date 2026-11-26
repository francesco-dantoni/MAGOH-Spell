"""IV-level labels, with explicit missing values and lossless legacy display.

Opening a feature never fills or rewrites scientific data. A stored ID may
provide a display label, but only a deliberate vocabulary choice changes it.
"""
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QSignalBlocker, QTimer
from qgis.PyQt.QtWidgets import QComboBox
from qgis.core import QgsProject, QgsFieldFormatter, QgsEditorWidgetSetup, QgsFeatureRequest
from qgis.gui import QgsEditorWidgetFactory, QgsEditorWidgetWrapper
from .geographic_editor import clean
from .editor_registry import register_editor, register_formatter, config_widget, field_value
from ..i18n import _tf as t

ID = 'MAGOHEvidenceLevel'
_owners = []


def rows(config):
    lookup = QgsProject.instance().mapLayer(str(config.get('Layer', '')))
    if lookup is None:
        return []
    return [(clean(f['id']), clean(f['nome'])) for f in
            lookup.getFeatures(QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry))]


class Wrapper(QgsEditorWidgetWrapper):
    def __init__(self, *args):
        super().__init__(*args)
        self.combo = None
        self.original = None
        self.code = ''
        self.original_code = None
        self.items = []

    def createWidget(self, parent):
        return QComboBox(parent)

    def initWidget(self, editor):
        self.combo = editor
        self.items = rows(self.config())
        editor.setEditable(True)
        editor.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        editor.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        editor.activated.connect(self._chosen)
        editor.lineEdit().editingFinished.connect(self._populate)
        self._populate()
        QTimer.singleShot(0, self._populate)

    def _populate(self):
        if sip.isdeleted(self) or self.combo is None or sip.isdeleted(self.combo):
            return
        blocker = QSignalBlocker(self.combo)
        self.combo.clear()
        self.combo.addItem(t('— IV livello non indicato nei dati —', '— IV level not specified in source data —'), None)
        for code, label in sorted(self.items, key=lambda item: item[1].casefold()):
            self.combo.addItem(label, code)
        raw = clean(self.original)
        lookup = dict(self.items)
        # Text remains authoritative. A legacy numeric value or the separate
        # code is used only as a display lookup, not written back on open.
        label = lookup.get(raw, raw) if raw else lookup.get(self.code, '')
        index = self.combo.findText(label, Qt.MatchFlag.MatchExactly) if label else 0
        if index < 0:
            self.combo.addItem(label, self.code or raw)
            index = self.combo.count()-1
        if not label and self.code:
            self.combo.addItem(t('Codice IV livello non risolto: ', 'Unresolved IV-level code: ')+self.code, self.code)
            index = self.combo.count()-1
        self.combo.setCurrentIndex(index)
        self.combo.setToolTip(t('Il valore originale è conservato. Scegli una voce solo per modificarlo.',
                                'The original value is preserved. Select an entry only to change it.'))
        del blocker

    def _chosen(self, index):
        code = self.combo.itemData(index)
        # Only real vocabulary entries (or explicit clearing) are selectable
        # as new values; unknown historical text is retained unchanged.
        if code is not None and code not in dict(self.items):
            self._populate()
            return
        self.original = dict(self.items).get(code) if code is not None else None
        self.code = code or ''
        self.original_code = code
        self.emitValueChanged()

    def additionalFields(self):
        return ['id_iv_livello'] if self.layer().fields().lookupField('id_iv_livello') >= 0 else []

    def additionalFieldValues(self):
        return [self.original_code] if self.additionalFields() else []

    def value(self):
        return self.original

    def valid(self):
        return self.combo is not None and not sip.isdeleted(self.combo)

    def updateValues(self, value, additional_values=None):
        self.original = value
        if additional_values:
            self.original_code = additional_values[0]
            self.code = clean(self.original_code)
        self._populate()

    def setFeature(self, feature):
        self.setFormFeature(feature)
        index = feature.fields().lookupField('id_iv_livello')
        self.original_code = feature[index] if index >= 0 else None
        self.code = clean(self.original_code)
        self.updateValues(field_value(feature, self.fieldIdx()))

    def showIndeterminateState(self):
        self.code = ''
        self.original_code = None
        self.updateValues(None)


class Factory(QgsEditorWidgetFactory):
    def __init__(self):
        super().__init__('MAGOH — IV livello')

    def create(self, layer, index, editor, parent):
        return Wrapper(layer, index, editor, parent)

    def fieldScore(self, layer, index):
        return 0

    def configWidget(self, layer, index, parent):
        return config_widget(layer, index, parent)


class Formatter(QgsFieldFormatter):
    def id(self):
        return ID

    def createCache(self, layer, index, config):
        return dict(rows(config))

    def representValue(self, layer, index, config, cache, value):
        raw = clean(value)
        return (cache if isinstance(cache, dict) else dict(rows(config))).get(raw, raw) or '—'


def register():
    # dopo un aggiornamento senza riavvio si usano le classi correnti.
    register_editor(ID, Factory, {'Wrapper': Wrapper}, _owners)
    register_formatter(ID, Formatter, _owners)


def install(layers):
    register()
    lookup = layers.get('thesaurus_iv_livello')
    if lookup is None:
        return
    for name in ('magoh_evidenza_archeologica', 'magoh_evidenza_archeologica_no_geom'):
        layer = layers.get(name)
        if layer is not None:
            index = layer.fields().lookupField('iv_livello')
            if index >= 0:
                layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup(ID, {'Layer': lookup.id()}))
