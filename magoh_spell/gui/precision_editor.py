from qgis.core import QgsFieldFormatter
from qgis.gui import QgsEditorWidgetWrapper, QgsEditorWidgetFactory
from qgis.PyQt.QtWidgets import QComboBox
from qgis.PyQt.QtCore import QSignalBlocker
from .editor_registry import register_editor, register_formatter, config_widget, field_value
from .geographic_editor import clean
from ..precision import canonical, VALUES

ID = 'MAGOHPrecision'
_owners = []

class Wrapper(QgsEditorWidgetWrapper):
    def createWidget(self,parent):
        return QComboBox(parent)

    def initWidget(self,editor):
        self.combo = editor
        self.original = None
        editor.addItem('—',None)
        for value in VALUES:
            editor.addItem(value,value)
        editor.activated.connect(self.choose)

    def choose(self,index):
        self.original = self.combo.itemData(index)
        self.emitValueChanged()

    def updateValues(self,value,additional_values=None):
        self.original = value
        label = canonical(clean(value))
        with QSignalBlocker(self.combo):
            while self.combo.count() > len(VALUES)+1:
                self.combo.removeItem(self.combo.count()-1)
            index = self.combo.findText(label) if label else 0
            if index < 0:
                self.combo.addItem(label,value)
                index = self.combo.count()-1
            self.combo.setCurrentIndex(index)

    def setFeature(self,feature):
        # Like the other MAGOH editors: load the stored value directly, without
        # relying on the C++ base to dispatch updateValues() to Python.
        try:
            self.setFormFeature(feature)
        except Exception:
            pass
        self.updateValues(field_value(feature,self.fieldIdx()))

    def showIndeterminateState(self):
        self.updateValues(None)

    def value(self):
        return self.original

    def valid(self):
        return getattr(self,'combo',None) is not None

class Factory(QgsEditorWidgetFactory):
    def __init__(self):
        super().__init__('MAGOH — Precisione')

    def create(self,layer,index,editor,parent):
        return Wrapper(layer,index,editor,parent)

    def fieldScore(self,layer,index):
        return 0

    def configWidget(self,layer,index,parent):
        return config_widget(layer,index,parent)

class Formatter(QgsFieldFormatter):
    def id(self):
        return ID

    def representValue(self,layer,index,config,cache,value):
        return canonical(clean(value)) or '—'

def register():
    register_editor(ID,Factory,{'Wrapper':Wrapper},_owners)
    register_formatter(ID,Formatter,_owners)
