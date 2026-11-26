"""Read-only identifier of a MAGOH record: local code, else MAGOH ID.

The widget sits on the technical key field, so the form keeps the Template
layout and label style; the key itself is shown to nobody and never changed.
"""
from qgis.gui import QgsEditorWidgetFactory, QgsEditorWidgetWrapper
from qgis.PyQt.QtWidgets import QLineEdit
from .editor_registry import register_editor, config_widget, field_value
from .geographic_editor import clean
from ..local_codes import NEW_RECORD

ID = 'MAGOHRecordId'
_owners = []


def shown(feature, value):
    fields = feature.fields()
    code = clean(field_value(feature, fields.indexOf('codice_locale')))
    if code:
        return code
    if not feature.isValid() or feature.id() < 0:
        return NEW_RECORD
    return clean(value)


class Wrapper(QgsEditorWidgetWrapper):
    def createWidget(self, parent):
        return QLineEdit(parent)

    def initWidget(self, editor):
        self.edit = editor
        self.original = None
        editor.setReadOnly(True)

    def updateValues(self, value, additional_values=None):
        self.original = value
        self.edit.setText(shown(self.formFeature(), value))

    def setFeature(self, feature):
        # Like the other MAGOH editors: load the feature directly, without
        # relying on the C++ base to dispatch updateValues() to Python.
        self.setFormFeature(feature)
        self.updateValues(field_value(feature, self.fieldIdx()))

    def showIndeterminateState(self):
        self.original = None
        self.edit.clear()

    def value(self):
        return self.original

    def valid(self):
        return getattr(self, 'edit', None) is not None


class Factory(QgsEditorWidgetFactory):
    def __init__(self):
        super().__init__('MAGOH — Identificativo scheda')

    def create(self, layer, index, editor, parent):
        return Wrapper(layer, index, editor, parent)

    def fieldScore(self, layer, index):
        return 0

    def configWidget(self, layer, index, parent):
        return config_widget(layer, index, parent)


def register():
    register_editor(ID, Factory, {'Wrapper': Wrapper}, _owners)
