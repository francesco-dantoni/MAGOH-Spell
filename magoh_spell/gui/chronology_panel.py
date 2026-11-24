"""One inclusive life-span filter, optionally intersected with categories."""
import re
import time
from qgis.PyQt import sip
from qgis.core import QgsProject, QgsVectorLayer, QgsFeatureRequest
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
                                QCheckBox, QSpinBox, QListWidget, QListWidgetItem, QPushButton, QSlider)
from qgis.gui import QgsRangeSlider
from ..gna_identity import gna_role, source_table
from ..gna_display import labels
from ..related_filters import session_for, RelatedFilterError
from ..search_index import revision
from ..ui_theme import action_bar, set_role
from ..i18n import _tf as t
from ..ui_theme import apply_teal_style


def year(value):
    if value is None or (hasattr(value, 'isNull') and value.isNull()):
        return None
    text = str(value).strip()
    if not re.fullmatch(r'[-+]?\d+(?:\.0+)?', text):
        return None
    number = int(float(text))
    return number if abs(number) < 1000000 else None


def overlap(start, end, low, high, keep_null=False):
    a, b = year(start), year(end)
    if a is None and b is None:
        return keep_null
    a, b = (b if a is None else a), (a if b is None else b)
    return a <= b and a <= high and b >= low


class ChronologyPanel(QWidget):
    def __init__(self, iface, plugin):
        super().__init__(); self.iface=iface; self.plugin=plugin
        self._loading=True; self._rows={}; self._tokens={}; self._source_ids=[]; self._available={}
        layout=QVBoxLayout(self)
        self.cmb_layer=QComboBox(); layout.addWidget(QLabel(t('Schede da esplorare','Records to explore'))); layout.addWidget(self.cmb_layer)
        self.hint=QLabel(t('Mostra le entità presenti nel momento o nell’intervallo scelto. Le categorie restringono ulteriormente la stessa vista.',
                          'Show entities present at the chosen time or interval. Categories further restrict the same view.'))
        self.hint.setWordWrap(True); layout.addWidget(self.hint)
        self.chk_date_enable=QCheckBox(t('Filtra per cronologia','Filter by chronology')); self.chk_date_enable.setChecked(True); layout.addWidget(self.chk_date_enable)
        self.mode=QComboBox(); self.mode.addItem(t('Intervallo','Interval'),'interval'); self.mode.addItem(t('Anno','Year'),'year'); layout.addWidget(self.mode)
        row=QHBoxLayout(); self.spin_min=QSpinBox(); self.spin_max=QSpinBox()
        self.end_label=QLabel(t('a','to'))
        for spin in (self.spin_min,self.spin_max): spin.setRange(-999999,999999); spin.setKeyboardTracking(False)
        row.addWidget(self.spin_min); row.addWidget(self.end_label); row.addWidget(self.spin_max); layout.addLayout(row)
        self.slider=QgsRangeSlider(); layout.addWidget(self.slider)
        self.year_slider=QSlider(Qt.Orientation.Horizontal);self.year_slider.hide();layout.addWidget(self.year_slider)
        self.chk_keep_null=QCheckBox(t('Includi schede non datate','Include undated records')); layout.addWidget(self.chk_keep_null)
        self.cmb_them_field=QComboBox(); layout.addWidget(QLabel(t('Categoria','Category'))); layout.addWidget(self.cmb_them_field)
        self.list_values=QListWidget(); self.list_values.setMinimumHeight(130); layout.addWidget(self.list_values)
        self.status=QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.action_bar, actions=action_bar(self)
        reset=QPushButton(t('Ripristina tutti','Restore all')); set_role(reset,compact=True); actions.addWidget(reset)
        layout.addWidget(self.action_bar)
        self._apply_timer=QTimer(self); self._apply_timer.setSingleShot(True); self._apply_timer.setInterval(350)
        self._apply_timer.timeout.connect(self._apply_filters)
        self.cmb_layer.currentIndexChanged.connect(self._source_changed)
        self.cmb_them_field.currentIndexChanged.connect(self._categories)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.spin_min.valueChanged.connect(self._spins); self.spin_max.valueChanged.connect(self._spins)
        self.slider.rangeChanged.connect(self._slider)
        self.year_slider.valueChanged.connect(self.spin_min.setValue)
        self.list_values.itemChanged.connect(self._schedule)
        self.chk_keep_null.toggled.connect(self._schedule); self.chk_date_enable.toggled.connect(self._schedule)
        reset.clicked.connect(self._clear_filter)
        self._refresh_timer=QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.reload_layers)
        project=QgsProject.instance()
        project.layersAdded.connect(self._layers_changed)
        project.layersRemoved.connect(self._layers_changed)
        self.reload_layers(); self._loading=False; apply_teal_style(self)

    def _schedule(self, *args):
        if not self._loading:
            self._apply_timer.start()

    def _layers_changed(self, *args):
        self._apply_timer.stop()
        self._rows.clear()
        self._tokens.clear()
        self._refresh_timer.start(0)

    def _live_sources(self):
        project = QgsProject.instance()
        layers = [project.mapLayer(lid) for lid in self._source_ids]
        return [layer for layer in layers if isinstance(layer,QgsVectorLayer)
                and not sip.isdeleted(layer) and layer.isValid()]

    def reload_layers(self):
        project=QgsProject.instance()
        mosi=sorted([l for l in project.mapLayers().values() if gna_role(l)=='mosi'],key=lambda l:l.id())
        finding=sorted([l for l in project.mapLayers().values() if isinstance(l,QgsVectorLayer) and
                        source_table(l)=='magoh_ritrovamenti'],key=lambda l:l.id())
        if not finding:
            gpkg=self.plugin.get_active_gpkg()
            finding=[l for key,l in self.plugin._layers_for_gpkg(gpkg).items() if key=='magoh_ritrovamenti'] if gpkg else []
        before=self.cmb_layer.currentData()
        choices=([('GNA — MOSI','mosi')] if mosi else [])+[(t('Schede di Ritrovamento','Finding records')+' — '+l.name(),l.id()) for l in finding]
        existing=[(self.cmb_layer.itemText(i),self.cmb_layer.itemData(i)) for i in range(self.cmb_layer.count())]
        self._available={'mosi':[l.id() for l in mosi],**{l.id():[l.id()] for l in finding}}
        if choices!=existing:
            self.cmb_layer.blockSignals(True); self.cmb_layer.clear()
            for label,key in choices:self.cmb_layer.addItem(label,key)
            first_mosi=bool(mosi) and not any(key=='mosi' for _,key in existing)
            index=0 if first_mosi else self.cmb_layer.findData(before)
            if index>=0:self.cmb_layer.setCurrentIndex(index)
            self.cmb_layer.blockSignals(False)
            self._source_changed()
        elif self._source_ids != self._available.get(before,[]) or not self._tokens:
            self._source_changed()
        if not choices:
            self.status.setText(t('Carica Ritrovamenti MAGOH o MOSI GNA.','Load MAGOH Findings or GNA MOSI.'))

    def showEvent(self,event):
        self._tokens.clear()
        self.reload_layers()
        super().showEvent(event)

    def hideEvent(self,event):
        self._apply_timer.stop()
        super().hideEvent(event)


    def _source_changed(self, *args):
        old = self._loading
        try:
            self._populate_source()
        except (RelatedFilterError, RuntimeError) as exc:
            self._apply_timer.stop()
            self.list_values.clear()
            self.status.setText(str(exc))
        finally:
            self._loading = old
            self.cmb_them_field.blockSignals(False)

    def _populate_source(self):
        self._apply_timer.stop(); old=self._loading; self._loading=True
        self._source_ids=self._available.get(self.cmb_layer.currentData(),[])
        self.cmb_them_field.blockSignals(True)
        self.cmb_them_field.clear()
        options=(('OGD',t('Definizione GNA','GNA definition')),('OGT',t('Tipologia GNA','GNA type'))) if self.cmb_layer.currentData()=='mosi' else (
            ('i_livello','I livello'),('ii_livello','II livello'),('iii_livello','III livello'))
        for key,label in options:self.cmb_them_field.addItem(label,key)
        self.cmb_them_field.blockSignals(False)
        values=[]
        for layer in self._live_sources():
            for attrs in self._read(layer).values():
                values.extend(v for k in self._date_fields(layer) if (v:=year(attrs.get(k))) is not None)
        has_dates = bool(values)
        for control in (self.spin_min,self.spin_max,self.slider,self.year_slider,self.mode,self.chk_date_enable):
            control.setEnabled(has_dates)
        lo,hi=(min(values),max(values)) if has_dates else (0,0)
        self.list_values.setEnabled(bool(self._live_sources()))
        for spin,value in ((self.spin_min,lo),(self.spin_max,hi)):
            spin.blockSignals(True);spin.setValue(value);spin.blockSignals(False)
        self.slider.blockSignals(True);self.slider.setMinimum(lo);self.slider.setMaximum(hi);self.slider.setRange(lo,hi);self.slider.blockSignals(False)
        self.year_slider.blockSignals(True);self.year_slider.setRange(lo,hi);self.year_slider.setValue(lo);self.year_slider.blockSignals(False)
        self._categories(); self._loading=old
        self.status.setText(t('Sposta il cursore o scegli una categoria per filtrare.','Move the slider or choose a category to filter.'))

    @staticmethod
    def _date_fields(layer):
        fields={f.name().upper():f.name() for f in layer.fields()}
        return (fields.get('DTSI','datazione'),fields.get('DTSF','datazione_finale'))

    def _read(self, layer):
        session=session_for(self.plugin); token=revision(layer)
        base=session.bases.get(layer.id(),layer.subsetString()) if layer.subsetString()==session.applied.get(layer.id()) else layer.subsetString()
        stable=(token[:2]+(base,)+token[3:]) if token is not None else None
        if stable is not None and self._tokens.get(layer.id())==stable:return self._rows[layer.id()]
        source=layer
        if layer.subsetString()!=base:
            options=QgsVectorLayer.LayerOptions();options.loadDefaultStyle=False
            source=QgsVectorLayer(layer.source(),'chronology_read_only',layer.providerType(),options)
            if not source.isValid() or not source.setSubsetString(base or ""):
                raise RelatedFilterError(t('Impossibile leggere la vista originale.','Cannot read the original view.'))
        wanted=set(self._date_fields(layer))|{'OGD','OGT','i_livello','ii_livello','iii_livello'}
        fields=[f.name() for f in source.fields() if f.name() in wanted]
        request=QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry).setSubsetOfAttributes(fields,source.fields())
        iterator=source.getFeatures(request)
        try:rows={f.id():{name:f[name] for name in fields} for f in iterator}
        finally:iterator.close()
        self._rows[layer.id()]=rows;self._tokens[layer.id()]=stable
        return rows

    @staticmethod
    def _tokens_of(value):
        if value is None or (hasattr(value,'isNull') and value.isNull()):return []
        return [v.strip().strip('"') for v in str(value).strip('{}').split(',') if v.strip()]

    def _categories(self,*args):
        old = self._loading
        try:
            self._populate_categories()
        except (RelatedFilterError,RuntimeError) as exc:
            self._apply_timer.stop()
            self.status.setText(str(exc))
        finally:
            self._loading = old

    def _populate_categories(self):
        old=self._loading;self._loading=True;self.list_values.clear()
        field=self.cmb_them_field.currentData();values=set()
        for layer in self._live_sources():
            for row in self._read(layer).values():values.update(self._tokens_of(row.get(field)))
        names=labels(field) if field in ('OGD','OGT') else {}
        for raw in sorted(values,key=lambda v:names.get(v,v).casefold()):
            item=QListWidgetItem(names.get(raw,raw));item.setData(Qt.ItemDataRole.UserRole,raw)
            item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Unchecked);self.list_values.addItem(item)
        self._loading=old;self._schedule()

    def _mode_changed(self,*args):
        single=self.mode.currentData()=='year';self.spin_max.setVisible(not single);self.end_label.setVisible(not single)
        self.slider.setVisible(not single);self.year_slider.setVisible(single)
        self._spins()

    def _spins(self,*args):
        a,b=self.spin_min.value(),self.spin_max.value()
        if self.mode.currentData()=='year':b=a
        a,b=sorted((a,b))
        self.slider.blockSignals(True)
        self.slider.setMinimum(min(a,self.slider.minimum()));self.slider.setMaximum(max(b,self.slider.maximum()));self.slider.setRange(a,b)
        self.slider.blockSignals(False);self._schedule()
        self.year_slider.blockSignals(True);self.year_slider.setRange(self.slider.minimum(),self.slider.maximum());self.year_slider.setValue(self.spin_min.value());self.year_slider.blockSignals(False)

    def _slider(self,a,b):
        if self.mode.currentData()=='year':b=a
        for spin,value in ((self.spin_min,a),(self.spin_max,b)):
            spin.blockSignals(True);spin.setValue(value);spin.blockSignals(False)
        self._schedule()

    def _apply_filters(self):
        self._apply_timer.stop()
        if not self._live_sources():return
        start=time.perf_counter();a,b=sorted((self.spin_min.value(),self.spin_max.value()))
        if self.mode.currentData()=='year':a=b=self.spin_min.value()
        checked={self.list_values.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list_values.count()) if self.list_values.item(i).checkState()==Qt.CheckState.Checked}
        field=self.cmb_them_field.currentData();selections=[]
        try:
            for layer in self._live_sources():
                ini,fin=self._date_fields(layer);matches=[]
                is_mosi=gna_role(layer)=='mosi'
                for fid,row in self._read(layer).items():
                    start_value,end_value=row.get(ini),row.get(fin)
                    if is_mosi and year(start_value) is None and year(end_value)==0:
                        end_value=None  # documented GNA unknown chronology, not year zero
                    if self.chk_date_enable.isEnabled() and self.chk_date_enable.isChecked() and not overlap(start_value,end_value,a,b,self.chk_keep_null.isChecked()):continue
                    if checked and not (checked&set(self._tokens_of(row.get(field)))):continue
                    matches.append(fid)
                selections.append((layer,matches))
            session_for(self.plugin).apply_many(selections)
            self.status.setText(t('{n} schede visibili — {s:.2f} s. Padri/figli collegati aggiornati.',
                                  '{n} visible records — {s:.2f} s. Related parents/children updated.',n=sum(len(ids) for _,ids in selections),s=time.perf_counter()-start))
        except (RelatedFilterError,RuntimeError) as exc:
            self.status.setText(str(exc))

    def clear_all_filters(self):
        self._apply_timer.stop()
        try:session_for(self.plugin).clear();return True
        except RelatedFilterError as exc:self.status.setText(str(exc));return False

    def _clear_filter(self):
        if self.clear_all_filters():
            self._tokens.clear()
            self.reload_layers()
            self.status.setText(t('Filtri ripristinati.','Filters restored.'))
