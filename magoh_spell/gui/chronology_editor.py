"""Closed chronology editors accepting legacy labels without changing them on open."""
from functools import lru_cache
from pathlib import Path
import sqlite3
from contextlib import closing
from qgis.PyQt.QtCore import Qt, QTimer, QSignalBlocker
from qgis.PyQt import sip
from qgis.PyQt.QtWidgets import QComboBox
from qgis.gui import QgsEditorWidgetFactory, QgsEditorWidgetWrapper
from qgis.core import QgsFieldFormatter, QgsEditorWidgetSetup
from .geographic_editor import _attribute_form, clean
from .editor_registry import register_editor, register_formatter
from ..conversion.entity_summary import normalize

ID='MAGOHChronologyCode'
_owners=[]


@lru_cache(maxsize=1)
def bundled():
    path=Path(__file__).resolve().parents[1]/'data/magoh_spell.gpkg'
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        return (list(db.execute('SELECT cod_periodo,name_full FROM thesaurus_periodo')),
                list(db.execute('SELECT id,id_periodo,name_full FROM thesaurus_crono')))


def resolve_period(raw, periods, chronology):
    raw=clean(raw)
    codes={str(code) for code,_ in periods}
    if raw in codes:return raw
    for code,period,label in chronology:
        if normalize(label)==normalize(raw):return str(period)
    for code,label in periods:
        if normalize(label)==normalize(raw.split('>')[0]):return str(code)
    return ''


def represented(value, mode):
    periods,chronology=bundled();raw=clean(value)
    if mode=='period':
        code=resolve_period(raw,periods,chronology)
        return dict((str(k),v) for k,v in periods).get(code,raw)
    return dict((str(k),v) for k,_,v in chronology).get(raw,raw)


class Wrapper(QgsEditorWidgetWrapper):
    def __init__(self,*args):
        super().__init__(*args);self.combo=None;self.original=None;self.form=None;self.period=''

    def createWidget(self,parent):return QComboBox(parent)

    def initWidget(self,editor):
        self.combo=editor;editor.setEditable(True);editor.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        editor.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        editor.activated.connect(self._chosen);editor.lineEdit().editingFinished.connect(self._restore)
        self._populate();QTimer.singleShot(0,self._bind)

    def _bind(self):
        if sip.isdeleted(self) or self.combo is None or sip.isdeleted(self.combo):return
        if self.form is not None:return
        form=_attribute_form(self.combo)
        if form is not None:
            self.form=form;form.widgetValueChanged.connect(self._changed)
            parent=self.config().get('PeriodField')
            if parent:
                index=self.layer().fields().indexFromName(parent)
                wrappers=[w for w in form.findChildren(QgsEditorWidgetWrapper) if w.fieldIdx()==index and w.layer().id()==self.layer().id()]
                if wrappers:self.period=clean(wrappers[0].value());self._populate()

    def _changed(self,name,value,*args):
        if name==self.config().get('PeriodField'):
            self.period=clean(value);self._populate()

    def _populate(self):
        if sip.isdeleted(self) or self.combo is None or sip.isdeleted(self.combo):return
        periods,chronology=bundled();mode=self.config().get('Mode','chronology')
        period=resolve_period(self.period,periods,chronology)
        rows=periods if mode=='period' else [(key,label) for key,parent,label in chronology if not period or str(parent)==period]
        blocker=QSignalBlocker(self.combo);self.combo.clear();self.combo.addItem('—',None)
        for key,label in rows:self.combo.addItem(label,str(key))
        raw=clean(self.original);code=resolve_period(raw,periods,chronology) if mode=='period' else raw
        index=self.combo.findData(code)
        if raw and index<0:
            # Keep a previously stored value visible even outside the new parent
            # filter. Only a deliberate user choice changes the stored value.
            self.combo.addItem(represented(raw,mode),raw);index=self.combo.count()-1
        self.combo.setCurrentIndex(max(index,0));del blocker

    def _chosen(self,index):
        self.original=self.combo.itemData(index);self.emitValueChanged()

    def _restore(self):self._populate()

    def value(self):return self.original

    def valid(self):return self.combo is not None and not sip.isdeleted(self.combo)

    def updateValues(self,value,additional_values=None):
        self.original=value;self._populate()

    def setFeature(self,feature):
        self.updateValues(feature.attribute(self.fieldIdx()))

    def showIndeterminateState(self):self.updateValues(None)


class Factory(QgsEditorWidgetFactory):
    def __init__(self):super().__init__('MAGOH — Periodo e cronologia')
    def create(self,layer,index,editor,parent):return Wrapper(layer,index,editor,parent)
    def fieldScore(self,layer,index):return 0


class Formatter(QgsFieldFormatter):
    def id(self):return ID
    def representValue(self,layer,index,config,cache,value):return represented(value,config.get('Mode','chronology'))


def register():
    # V1.19: dopo un aggiornamento senza riavvio si usano le classi correnti.
    register_editor(ID, Factory, {'Wrapper': Wrapper}, _owners)
    register_formatter(ID, Formatter, _owners)


def install(layers):
    register()
    for name,layer in layers.items():
        if name not in ('magoh_evidenza_archeologica','magoh_evidenza_archeologica_no_geom'):continue
        for field,mode,parent in (('cronologia','period',''),('cronologia_finale','period',''),
                                  ('crono_iniziale','chronology','cronologia'),('crono_finale','chronology','cronologia_finale')):
            index=layer.fields().indexFromName(field)
            if index>=0:layer.setEditorWidgetSetup(index,QgsEditorWidgetSetup(ID,{'Mode':mode,'PeriodField':parent}))
