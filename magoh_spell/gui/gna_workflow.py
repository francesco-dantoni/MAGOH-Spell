"""Single-window MOPR → conversion → official template workflow."""
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import tempfile
import json

from qgis.PyQt.QtCore import Qt, QSettings, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QPlainTextEdit, QCheckBox, QFileDialog,
    QTabWidget, QScrollArea, QStackedWidget,
)
from qgis.core import (QgsProject, QgsVectorLayer, QgsGeometry, QgsWkbTypes, QgsPointXY,
                       QgsTask, QgsApplication, QgsVectorLayerFeatureSource, QgsProviderRegistry)
from qgis.gui import QgsMapTool, QgsRubberBand
from ..conversion.gna_project import PERSONAL_CODE_RE, build_cpr, validate_cpr, GnaProjectBuilder
from ..conversion.mopr_filter import filter_to_mopr
from ..conversion.magoh_to_gna import MagohToGnaConverter, ConversionOptions
from .mopr_workspace import MoprWorkspace, REQUIRED, LABELS, PROFILE, PARENTS, value, checked_geometry, WORKSPACE_PROP
from .gna_layers import rows, find, load_gna_outputs
from ..i18n import _tf as t
from ..ui_theme import apply_teal_style
from .ui_helpers import fit_dialog_to_parent
from ..ui_theme import dialog_header, set_role, SPACE_S, SPACE_M, SPACE_L
from ..ui_theme import MagohMessageBox as QMessageBox  # stile MAGOH, solo per questo modulo
from .mopr_autofill import geography_fields, scan_geography, resolve_geography
from .mopr_guide import guide_button

CODE_SETTING = 'magoh_spell/gna/professional_code'
SOURCE_FIELDS = {'i': {'idscheda', 'ubicazione'}, 'r': {'id', 'idscheda', 'iii_livello'},
                 'e': {'id', 'idintervento', 'idiii_livello'}}


def button(text, slot, layout):
    b = QPushButton(text)
    b.setAutoDefault(False)
    b.clicked.connect(slot)
    layout.addWidget(b)
    return b


class PerimeterTool(QgsMapTool):
    finished = pyqtSignal(object)
    cancelled = pyqtSignal(bool)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.points = []
        self.band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.band.setColor(QColor(42, 130, 175, 95))
        self.band.setWidth(2)
        self.done = False

    def point(self, event):
        match = self.canvas().snappingUtils().snapToMap(event.pos())
        return QgsPointXY(match.point() if match.isValid() else self.toMapCoordinates(event.pos()))

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.points.append(self.point(event))
            self.render()
        elif event.button() == Qt.MouseButton.RightButton and len(self.points) >= 3:
            self.done = True
            self.finished.emit(QgsGeometry.fromPolygonXY([self.points + [self.points[0]]]))

    def canvasMoveEvent(self, event):
        self.render(self.point(event))

    def render(self, current=None):
        pts = self.points + ([current] if current is not None else [])
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        for p in pts: self.band.addPoint(p)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.done = True
            self.cancelled.emit(True)
        elif event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self.points:
            self.points.pop()
            self.render()

    def deactivate(self):
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        super().deactivate()
        if not self.done:
            self.done = True
            self.cancelled.emit(False)

    def dispose(self):
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self.canvas().scene().removeItem(self.band)


class MoprForm(QWidget):
    """Only mandatory template attributes; CPR components are entry helpers."""
    def __init__(self, vocab, parent=None):
        super().__init__(parent)
        self.vocab = vocab
        self.widgets = {}
        self.dirty_fields = set()
        outer = QVBoxLayout(self)
        notice = QLabel(t('Template di lavoro: puoi lasciare campi da completare in seguito. Il perimetro seleziona i livelli da convertire.',
                         'Working template: fields can be completed later. The perimeter selects the levels to convert.'))
        notice.setWordWrap(True)
        notice.setObjectName('MagohContext')
        outer.addWidget(notice)
        guide_row = QHBoxLayout()
        self.guide = guide_button(self, 'MagohMoprGuideForm')
        guide_row.addWidget(self.guide)
        guide_row.addStretch(1)
        outer.addLayout(guide_row)
        self.prefill_status = QLabel(t('I valori proposti dal progetto restano modificabili.', 'Values proposed from the project remain editable.'))
        self.prefill_status.setWordWrap(True)
        self.prefill_status.setObjectName('MagohHint')
        outer.addWidget(self.prefill_status)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        outer.addLayout(form)
        self.code = QLineEdit()
        self.code.setMaximumWidth(170)
        self.code.setPlaceholderText('00000-XX')
        self.code.setMaxLength(10)
        code_row = QHBoxLayout()
        code_row.addWidget(self.code)
        button(t('Salva codice', 'Save code'), self.remember_code, code_row)
        button(t('Dimentica', 'Forget'), self.forget_code, code_row)
        form.addRow(t('Codice professionista/ditta', 'Professional/company code'), code_row)
        self.code_status = QLabel()
        self.code_status.setWordWrap(True)
        form.addRow('', self.code_status)
        self.year = QLineEdit(str(datetime.now().year))
        self.progressive = QLineEdit()
        self.progressive.setPlaceholderText(t('Progressivo del progetto, es. 43', 'Project sequence number, e.g. 43'))
        form.addRow(t('Anno del progetto', 'Project year'), self.year)
        form.addRow(t('Progressivo', 'Sequence number'), self.progressive)
        for f in REQUIRED:
            if f in vocab.fields:
                w = QComboBox()
                w.setMaxVisibleItems(15)
                w.setMinimumWidth(270)
                w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                w.setMinimumContentsLength(24)
                self.widgets[f] = w
                self.fill(f)
                if f in PROFILE: w.setEnabled(False)
            elif f == 'DES':
                w = QPlainTextEdit()
                w.setFixedHeight(95)
            else:
                w = QLineEdit()
            self.widgets[f] = w
            if f == 'CPR': w.setReadOnly(True)
            if f in ('CMC', 'CMR'): w.setPlaceholderText('Cognome, Nome')
            form.addRow(LABELS[f], w)
        for field, widget in self.widgets.items():
            if isinstance(widget,QComboBox): widget.activated.connect(lambda _i,f=field:self.mark_dirty(f))
            elif isinstance(widget,QLineEdit): widget.textEdited.connect(lambda _text,f=field:self.mark_dirty(f))
        for parent in ('LCR', 'LCP', 'ENTE'):
            self.widgets[parent].currentIndexChanged.connect(lambda _i, p=parent: self.cascade(p))
        self.widgets['ENTE'].currentIndexChanged.connect(self.sync_ente)
        for w in (self.widgets['EMC'], self.code, self.year, self.progressive):
            w.textChanged.connect(self.update_cpr)
        self.reset()

    def mark_dirty(self, field):
        self.dirty_fields.add(field)
        for child, (parent, _) in PARENTS.items():
            if parent==field: self.mark_dirty(child)

    def sync_ente(self, *_args):
        if 'EMC' not in self.dirty_fields:
            self.widgets['EMC'].setText(self.vocab.label('ENTE',self.widgets['ENTE'].currentData()))

    def apply_proposals(self, attrs):
        applied=[]
        for field in ('LCR','LCP','LCC','ENTE','EMC','OGN','DES','CMC','CMR'):
            if field not in attrs or field in self.dirty_fields: continue
            if field=='EMC' and 'ENTE' in self.dirty_fields: continue
            widget=self.widgets[field]
            if isinstance(widget,QComboBox):
                if widget.currentData(): continue
                index=widget.findData(str(attrs[field]))
                if index<0: continue
                widget.setCurrentIndex(index)
            elif isinstance(widget,QPlainTextEdit):
                if widget.toPlainText(): continue
                widget.setPlainText(str(attrs[field]))
            else:
                if widget.text(): continue
                widget.setText(str(attrs[field]))
            applied.append(LABELS[field])
        return applied

    def fill(self, field, selected=''):
        w = self.widgets[field]
        w.blockSignals(True)
        w.clear()
        w.addItem(t('— Scegli —', '— Select —'), '')
        parent = self.widgets[PARENTS[field][0]].currentData() if field in PARENTS and PARENTS[field][0] in self.widgets else None
        for key, label in self.vocab.pairs(field, parent):
            if field not in PROFILE or key == PROFILE[field]: w.addItem(label, key)
        key = PROFILE.get(field, value(selected).strip('{}'))
        w.setCurrentIndex(max(0, w.findData(key)))
        w.blockSignals(False)

    def cascade(self, parent):
        for child, (p, _) in PARENTS.items():
            if p == parent:
                self.fill(child)
                self.cascade(child)

    def update_cpr(self):
        candidate=build_cpr(self.widgets['EMC'].text(), self.year.text(), self.code.text(), self.progressive.text())
        self.widgets['CPR'].setText(candidate if validate_cpr(candidate) else '')
        self.widgets['CPR'].setPlaceholderText(t('Sarà usato un codice tecnico MAGOH_BOZZA, modificabile nel template',
                                                'A technical MAGOH_BOZZA code will be used, editable in the template'))

    def remember_code(self):
        code = self.code.text().strip().upper()
        if not PERSONAL_CODE_RE.fullmatch(code):
            self.code_status.setText(t('Codice non valido: formato 00000-XX (da 2 a 4 lettere).', 'Invalid code: expected 00000-XX (2 to 4 letters).'))
            return
        self.code.setText(code)
        settings = QSettings()
        settings.setValue(CODE_SETTING, code)
        settings.sync()
        self.code_status.setText(t('Codice salvato solo nel profilo QGIS locale. Non è una credenziale di accesso.', 'Code saved only in the local QGIS profile. This is not a login credential.'))

    def forget_code(self):
        settings = QSettings()
        settings.remove(CODE_SETTING)
        settings.sync()
        self.code.clear()
        self.code_status.setText(t('Codice dimenticato. Le schede già salvate non sono state modificate.', 'Code forgotten. Previously saved records have not been changed.'))

    def reset(self, attrs=None):
        attrs = attrs or {}
        for f, w in self.widgets.items():
            if isinstance(w, QComboBox): self.fill(f, attrs.get(f, '1' if f in ('GEL', 'ADP') else ''))
            elif isinstance(w, QCheckBox): w.setChecked(attrs.get(f) in (True, 1, '1'))
            elif isinstance(w, QPlainTextEdit): w.setPlainText(value(attrs.get(f)))
            else: w.setText(value(attrs.get(f, str(datetime.now().year) if f == 'CMA' else '')))
        pieces = value(attrs.get('CPR')).split('_')
        self.year.setText(pieces[1] if len(pieces) == 4 else str(datetime.now().year))
        self.code.setText(pieces[2] if len(pieces) == 4 else value(QSettings().value(CODE_SETTING, '')))
        self.progressive.setText(pieces[3] if len(pieces) == 4 else '')
        self.code_status.setText(t('Memorizzazione facoltativa: premi “Salva codice” per i prossimi progetti.', 'Optional: press “Save code” to reuse it in future projects.'))
        self.update_cpr()
        self.dirty_fields.clear()

    def attributes(self):
        result = {}
        for f, w in self.widgets.items():
            if isinstance(w, QComboBox): result[f] = w.currentData()
            elif isinstance(w, QCheckBox): result[f] = w.isChecked()
            elif isinstance(w, QPlainTextEdit): result[f] = w.toPlainText().strip()
            else: result[f] = w.text().strip()
        return result


class GnaWorkflowDialog(QDialog):
    def __init__(self, iface, plugin_dir, plugin=None):
        super().__init__(iface.mainWindow())
        self.iface, self.plugin_dir, self.plugin = iface, Path(plugin_dir), plugin
        self.workspace = MoprWorkspace(plugin_dir)
        self.mopr_geometry = None
        self.edit_fid = None
        self.draw_tool = None
        self.previous_tool = None
        self.last = ''
        self.bound_workspace = None
        self._directory_root = None
        self._output_default = ''
        self._prefill_task = None
        self._form_generation = 0
        self._geometry_source = None
        self._spatial_filter = {}
        self.setWindowTitle(t('Convertitore MAGOH-GNA', 'MAGOH-GNA converter'))
        self.resize(850, 730)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # Solo presentazione: il flusso (MOPR prima della conversione) è invariato.
        outer.addWidget(dialog_header(
            t('Convertitore MAGOH-GNA', 'MAGOH-GNA converter'),
            t('Prima prepara e salva il MOPR, poi avvia la conversione.', 'Prepare and save the MOPR before starting conversion.'),
            self))
        self.tabs = QTabWidget()
        self.tabs.setObjectName('MagohMainTabs')
        tabs_holder = QWidget()
        tabs_holder.setObjectName('MagohPage')
        tabs_layout = QVBoxLayout(tabs_holder)
        tabs_layout.setContentsMargins(SPACE_L, SPACE_S, SPACE_L, SPACE_M)
        # same guide, also inside the MOPR compilation page and in the
        # native QGIS form of the MOPR layer (see gui/mopr_guide.py).
        self.mopr_help = guide_button(tabs_holder, 'MagohMoprGuideTop')
        tabs_layout.addWidget(self.mopr_help)
        tabs_layout.addWidget(self.tabs)
        outer.addWidget(tabs_holder, 1)
        page = QWidget()
        layout = QVBoxLayout(page)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)
        choose = QWidget()
        choices = QVBoxLayout(choose)
        intro = QLabel(t('1. Prepara il MOPR\nScegli o disegna il perimetro. La scheda si aprirà automaticamente; puoi completarla in seguito.',
                         '1. Prepare MOPR\nSelect or draw the perimeter. The form opens automatically; you can complete it later.'))
        intro.setWordWrap(True)
        intro.setObjectName('MagohHint')
        choices.setSpacing(SPACE_M - 2)
        choices.addWidget(intro)
        self.path_label = QLabel(t('MOPR non ancora caricato.', 'No MOPR loaded yet.'))
        self.path_label.setWordWrap(True)
        self.path_label.setObjectName('MagohContext')
        self.geometry_buttons = (
            button(t('Seleziona una geometria esistente','Select an existing geometry'), self.open_existing_geometry, choices),
            button(t('Disegna una geometria','Draw a geometry'), self.draw, choices))
        for b in self.geometry_buttons: set_role(b,primary=True)
        existing = QWidget()
        ex = QVBoxLayout(existing)
        self.source = QComboBox()
        ex.addWidget(self.source)
        self.selected_only = QCheckBox(t('Usa solo le geometrie selezionate', 'Use selected geometries only'))
        # MOD-04: mai preselezionata, anche se nel layer c'è già una selezione QGIS.
        self.selected_only.setChecked(False)
        ex.addWidget(self.selected_only)
        button(t('Copia geometria e compila MOPR', 'Copy geometry and fill MOPR'), self.copy_source, ex)
        button(t('Indietro','Back'),lambda:self.stack.setCurrentIndex(0),ex)
        self.saved = QComboBox()
        self.saved.currentIndexChanged.connect(self.select_mopr)
        saved_row = QHBoxLayout()
        button(t('Aggiorna / usa selezione QGIS', 'Refresh / use QGIS selection'), self.refresh_saved, saved_row)
        button(t('Modifica scheda', 'Edit record'), self.edit_saved, saved_row)
        choices.addStretch()
        self.stack.addWidget(choose)
        editor = QWidget()
        edit_layout = QVBoxLayout(editor)
        self.form = MoprForm(self.workspace.vocab)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form)
        edit_layout.addWidget(scroll)
        edit_row = QHBoxLayout()
        set_role(button(t('Salva e seleziona MOPR', 'Save and select MOPR'), self.save_form, edit_row), primary=True)
        button(t('Annulla compilazione', 'Cancel editing'), lambda: self.stack.setCurrentIndex(0), edit_row)
        edit_layout.addLayout(edit_row)
        self.stack.addWidget(editor)
        self.stack.addWidget(existing)
        self.tabs.addTab(page, '1 · MOPR')
        conversion = QWidget()
        conv = QVBoxLayout(conversion)
        instructions = QLabel(t('2. Seleziona le sorgenti MAGOH e verifica\nSi leggono i record visibili dei layer scelti: i filtri QGIS attivi vengono rispettati. Nessun dato MAGOH viene modificato.',
                                '2. Select MAGOH sources and validate\nVisible records are read from the chosen layers: active QGIS filters are respected. MAGOH data is never modified.'))
        instructions.setWordWrap(True)
        instructions.setObjectName('MagohHint')
        conv.setSpacing(SPACE_M - 2)
        conv.addWidget(self.path_label)
        conv.addWidget(QLabel(t('MOPR salvato da usare nella conversione:','Saved MOPR to use for conversion:')))
        conv.addWidget(self.saved)
        conv.addLayout(saved_row)
        conv.addWidget(instructions)
        sources = QFormLayout()
        self.inputs = {}
        for key, label in [('i', 'Interventi'), ('r', 'Ritrovamenti'), ('e', 'Evidenze')]:
            combo = QComboBox()
            self.inputs[key] = combo
            sources.addRow(label, combo)
        conv.addLayout(sources)
        self.evidences = QCheckBox(t('Includi le evidenze archeologiche', 'Include archaeological evidence'))
        self.evidences.setChecked(True)
        conv.addWidget(self.evidences)
        button(t('Aggiorna elenco layer', 'Refresh layer list'), self.refresh, conv)
        button(t('Verifica conversione senza scrivere file', 'Validate conversion without writing files'), self.validate_only, conv)
        conv.addWidget(QLabel(t('Cartella di destinazione (ogni conversione crea una nuova sottocartella):', 'Output folder (each conversion creates a new subfolder):')))
        output_row = QHBoxLayout()
        self.output = QLineEdit()
        output_row.addWidget(self.output)
        button(t('Scegli…', 'Choose…'), self.choose_output, output_row)
        conv.addLayout(output_row)
        self.run_button = button(t('Genera il pacchetto completo GNA', 'Generate complete GNA package'), self.run, conv)
        set_role(self.run_button, primary=True)
        conv.addStretch()
        self.tabs.addTab(conversion, t('2 · Conversione', '2 · Conversion'))
        result = QWidget()
        res = QVBoxLayout(result)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        res.addWidget(self.log)
        self.open_button = button(t('Apri cartella risultato', 'Open result folder'), self.open_output, res)
        self.open_button.setEnabled(False)
        self.tabs.addTab(result, t('3 · Risultato', '3 · Result'))
        apply_teal_style(self)
        self.setStyleSheet(self.styleSheet() + ' QComboBox:disabled { color: #43505a; background: #edf1f3; }')
        fit_dialog_to_parent(self, iface.mainWindow(), preferred_width=850, preferred_height=730)
        self.refresh()

    def error(self, exc):
        QMessageBox.warning(self, 'MAGOH → GNA', str(exc))

    def refresh(self):
        self.sync_project_directories()
        layers = [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)]
        current = self.source.currentData()
        self.source.clear()
        for l in layers:
            if l.geometryType() == QgsWkbTypes.PolygonGeometry and not l.customProperty(WORKSPACE_PROP, False):
                self.source.addItem(l.name(), l.id())
        index = self.source.findData(current)
        if index >= 0: self.source.setCurrentIndex(index)
        for kind, combo in self.inputs.items():
            current = combo.currentData()
            default = find(kind)
            combo.clear()
            combo.addItem('—', '')
            for l in layers:
                fields = {f.name().lower() for f in l.fields()}
                if SOURCE_FIELDS[kind].issubset(fields) and l.isSpatial() and not l.customProperty(WORKSPACE_PROP, False):
                    combo.addItem(l.name(), l.id())
            combo.setCurrentIndex(max(0, combo.findData(current or (default.id() if default else ''))))
        try:
            workspace_present = self.workspace.layer is not None and self.workspace.layer.id() in QgsProject.instance().mapLayers()
        except RuntimeError:  # QGIS deleted the previous project's C++ layer.
            workspace_present = False
        if not workspace_present:
            self.workspace.layer = None
            self.path_label.setText(t('MOPR non ancora caricato.', 'No MOPR loaded yet.'))
            self.stack.setCurrentIndex(0)
        if self.workspace.layer is None:
            matches = [l for l in layers if l.customProperty(WORKSPACE_PROP, False) and set(REQUIRED).issubset(l.fields().names())]
            folder = self.default_mopr_directory()
            if folder:
                expected = (Path(folder) / 'MOPR.gpkg').resolve()
                matches = [layer for layer in matches if Path(layer.source().split('|', 1)[0]).resolve() == expected]
            if len(matches) == 1:
                self.workspace.layer = matches[0]
                self.workspace.configure()
        self.refresh_saved()

    def sync_project_directories(self):
        root = self.plugin.get_project_root() if self.plugin is not None else ''
        if root != self._directory_root:
            self.stop_drawing()
            self._directory_root = root
            # Never carry a previous project's MOPR into a different archive.
            self.workspace.layer = None
            self.mopr_geometry = None
            self._form_generation += 1
            if self._prefill_task is not None:
                self._prefill_task.cancel(); self._prefill_task=None
            self.edit_fid = None
            self.last = ''
            self.open_button.setEnabled(False)
            self._output_default = self.plugin.default_export_dir('gna') if root else ''
            self.output.setText(self._output_default)

    def project_name(self):
        """Nome del progetto MAGOH Spell, usato anche per il pacchetto GNA."""
        project = QgsProject.instance()
        name = ''
        try:
            if self.plugin is not None and self.plugin.get_project_root():
                name = (self.plugin._read_project_manifest(self.plugin.get_project_root()) or {}).get('name') or ''
        except Exception:
            name = ''
        return name or project.title() or Path(project.fileName()).stem or 'Progetto_GNA'

    def default_mopr_directory(self):
        if self.plugin is None or not self.plugin.get_project_root():
            return ''
        project = QgsProject.instance()
        archive, _ = project.readEntry('MAGOHSpell', 'moprArchive', '')
        folder, _ = project.readEntry('MAGOHSpell', 'moprDirectory', '')
        if archive and folder and Path(project.readPath(archive)).resolve() == Path(self.plugin.get_active_gpkg()).resolve():
            return project.readPath(folder)
        return self.plugin.default_export_dir('mopr')

    def choose_workspace(self):
        start = self.default_mopr_directory()
        folder = QFileDialog.getExistingDirectory(self, t('Cartella per il MOPR di lavoro', 'Working MOPR folder'), start or QgsProject.instance().homePath())
        if folder:
            try:
                self.workspace.open(folder)
                if self.plugin is not None and self.plugin.get_project_root():
                    project = QgsProject.instance()
                    project.writeEntry('MAGOHSpell', 'moprArchive', project.writePath(self.plugin.get_active_gpkg()))
                    project.writeEntry('MAGOHSpell', 'moprDirectory', project.writePath(folder))
                self.refresh_saved()
            except Exception as exc: self.error(exc)

    def ensure_workspace(self):
        try:
            if self.workspace.layer is not None and self.workspace.layer.id() not in QgsProject.instance().mapLayers():
                self.workspace.layer = None
        except RuntimeError:
            self.workspace.layer = None
        self.sync_project_directories()
        if self.workspace.layer is None:
            folder = self.default_mopr_directory()
            if folder:
                try:
                    self.workspace.open(folder)
                    self.refresh_saved()
                except Exception as exc:
                    self.error(exc)
            else:
                self.choose_workspace()
        return self.workspace.layer is not None

    def refresh_saved(self):
        current = self.saved.currentData()
        self.saved.blockSignals(True)
        self.saved.clear()
        self.saved.addItem(t('— Seleziona MOPR —', '— Select MOPR —'), None)
        layer = self.workspace.layer
        if layer is None:
            self.saved.blockSignals(False)
            return
        if self.bound_workspace is not layer:
            if self.bound_workspace is not None:
                try: self.bound_workspace.selectionChanged.disconnect(self.selection_changed)
                except (RuntimeError, TypeError): pass
            self.bound_workspace = layer
            layer.selectionChanged.connect(self.selection_changed)
        self.path_label.setText(layer.source().split('|')[0])
        for f in layer.getFeatures():
            self.saved.addItem(value(f['CPR']) + ' — ' + value(f['OGN']), f.id())
        ids = layer.selectedFeatureIds()
        selected = ids[0] if len(ids) == 1 else current
        idx = self.saved.findData(selected)
        if idx >= 0: self.saved.setCurrentIndex(idx)
        self.saved.blockSignals(False)

    def selection_changed(self, *_args):
        layer = self.workspace.layer
        if layer is None: return
        ids = layer.selectedFeatureIds()
        self.saved.blockSignals(True)
        self.saved.setCurrentIndex(max(0, self.saved.findData(ids[0] if len(ids) == 1 else None)))
        self.saved.blockSignals(False)

    def select_mopr(self, *_args):
        layer = self.workspace.layer
        if layer is not None:
            fid = self.saved.currentData()
            layer.selectByIds([fid] if fid is not None else [])

    def open_existing_geometry(self):
        """Apre «Seleziona una geometria esistente» con la casella sempre deselezionata (MOD-04)."""
        self.selected_only.setChecked(False)
        self.stack.setCurrentIndex(2)

    def copy_source(self):
        try:
            source = QgsProject.instance().mapLayer(self.source.currentData())
            if source is None: raise ValueError(t('Scegliere un layer poligonale.', 'Choose a polygon layer.'))
            if source.isEditable(): raise ValueError(t('Salvare o annullare prima le modifiche del layer sorgente.', 'Save or discard the edits of the source layer first.'))
            if self.selected_only.isChecked():
                # MOD-04: con la casella spuntata si usano solo gli elementi selezionati;
                # senza selezione si avvisa e non si ripiega mai sull'intero layer.
                features = source.selectedFeatures()
                if not features:
                    raise ValueError(t('Nel layer «{layer}» non ci sono geometrie selezionate. Selezionare il perimetro in QGIS oppure deselezionare «Usa solo le geometrie selezionate» per usare l\'intero layer.',
                                       'There are no selected geometries in layer «{layer}». Select the perimeter in QGIS or untick «Use selected geometries only» to use the whole layer.',
                                       layer=source.name()))
            else:
                features = list(source.getFeatures())
                if not features:
                    raise ValueError(t('Il layer «{layer}» non contiene geometrie.', 'Layer «{layer}» contains no geometries.', layer=source.name()))
            geometries = [checked_geometry(f.geometry(), source.crs()) for f in features]
            geom = QgsGeometry.unaryUnion(geometries)
            from qgis.core import QgsCoordinateReferenceSystem
            geom = checked_geometry(geom, QgsCoordinateReferenceSystem('EPSG:4326'))
            self._geometry_source = source.id()
            self.begin_form(geom)
        except Exception as exc: self.error(exc)

    def begin_form(self, geometry, attrs=None, fid=None):
        self._form_generation += 1
        if self._prefill_task is not None:
            self._prefill_task.cancel(); self._prefill_task=None
        self.mopr_geometry, self.edit_fid = geometry, fid
        self.form.reset(attrs)
        if attrs is None:
            project=QgsProject.instance()
            meta=self.plugin._read_project_manifest(self.plugin.get_project_root()) if self.plugin is not None else {}
            name=meta.get('name') or project.title() or Path(project.fileName()).stem
            self.form.apply_proposals({'OGN':name or '', 'DES':project.metadata().abstract() or ''})
            progressives=[]
            if self.workspace.layer is not None:
                for feature in self.workspace.layer.getFeatures():
                    last=value(feature['CPR']).rsplit('_',1)[-1]
                    if last.isdigit(): progressives.append(int(last))
            self.form.progressive.setText(str(max(progressives,default=0)+1))
            self.start_autofill()
        self.stack.setCurrentIndex(1)
        self.tabs.setCurrentIndex(0)
        self.show()
        self.raise_()

    def start_autofill(self):
        sources=[]
        project=QgsProject.instance()
        selected=project.mapLayer(self.source.currentData())
        layers=list(project.mapLayers().values())
        allowed=self.plugin._layers_for_gpkg(self.plugin.get_active_gpkg()) if self.plugin is not None else {}
        for layer in layers:
            if not isinstance(layer,QgsVectorLayer) or not layer.isSpatial() or not layer.crs().isValid(): continue
            if layer.customProperty(WORKSPACE_PROP,False): continue
            if self.plugin is not None and layer is not selected and layer not in allowed.values() and not any(x in layer.name().casefold() for x in ('comun','provinc','region')): continue
            fields=geography_fields(layer)
            if fields:
                fids=layer.selectedFeatureIds() if layer.id()==self._geometry_source and self.selected_only.isChecked() else None
                sources.append((QgsVectorLayerFeatureSource(layer),layer.crs(),fields,fids))
        generation=self._form_generation
        def done(exception,result):
            if generation!=self._form_generation: return
            self._prefill_task=None
            if exception is not None or not result or result['limited']:
                self.form.prefill_status.setText(t('Localizzazione non deducibile con certezza: compila i campi mancanti.', 'Location could not be inferred confidently: fill missing fields.'))
                return
            proposals=resolve_geography(self.workspace.vocab,result['observations'])
            applied=self.form.apply_proposals(proposals)
            self.form.prefill_status.setText(t('Proposte modificabili: {fields}. Verifica i campi mancanti.',
                'Editable proposals: {fields}. Check missing fields.',fields=', '.join(applied) or '—'))
        self._prefill_task=QgsTask.fromFunction('MAGOH: proposta localizzazione MOPR',scan_geography,sources,
             bytes(self.mopr_geometry.asWkb()),project.transformContext(),on_finished=done)
        QgsApplication.taskManager().addTask(self._prefill_task)

    def draw(self):
        self._geometry_source = None
        if self.draw_tool is not None: return
        canvas = self.iface.mapCanvas()
        self.previous_tool = canvas.mapTool()
        self.draw_tool = PerimeterTool(canvas)
        self.draw_tool.finished.connect(self.finish_drawing)
        self.draw_tool.cancelled.connect(self.cancel_drawing)
        self.hide()
        self.iface.messageBar().pushInfo('MOPR', t('Disegna con clic sinistro. Clic destro: termina. Backspace: rimuove l’ultimo vertice. Esc: annulla.', 'Left click to draw. Right click to finish. Backspace removes the last vertex. Esc cancels.'))
        canvas.setMapTool(self.draw_tool)

    def stop_drawing(self, restore=True):
        tool, self.draw_tool = self.draw_tool, None
        if tool is None: return
        tool.done = True
        canvas = self.iface.mapCanvas()
        if restore and canvas.mapTool() is tool:
            if self.previous_tool is not None: canvas.setMapTool(self.previous_tool)
            else: canvas.unsetMapTool(tool)
        tool.dispose()
        tool.deleteLater()
        self.previous_tool = None

    def cancel_drawing(self, restore=True):
        self.stop_drawing(restore)
        self.show()

    def finish_drawing(self, geometry):
        try:
            geom = checked_geometry(geometry, self.iface.mapCanvas().mapSettings().destinationCrs())
            self.stop_drawing()
            self.begin_form(geom)
        except Exception as exc:
            self.stop_drawing()
            self.show()
            self.error(exc)

    def save_form(self):
        try:
            attrs = self.form.attributes()
            if not self.ensure_workspace(): return
            if self.edit_fid is not None:
                # Retain stable record identity; optional fields remain untouched.
                attrs['MOPR_ID'] = self.workspace.layer.getFeature(self.edit_fid)['MOPR_ID']
                attrs['presa_visione'] = self.workspace.layer.getFeature(self.edit_fid)['presa_visione']
            self.workspace.save(attrs, self.mopr_geometry, self.edit_fid)
            self.refresh_saved()
            self.stack.setCurrentIndex(0)
            self.tabs.setCurrentIndex(1)
        except Exception as exc: self.error(exc)

    def edit_saved(self):
        try:
            fid = self.saved.currentData()
            if fid is None: raise ValueError(t('Selezionare un MOPR salvato.', 'Select a saved MOPR.'))
            layer = self.workspace.layer
            if layer.isEditable(): raise ValueError(t('Salvare o annullare prima le modifiche QGIS del MOPR.', 'Save or discard the QGIS edits of the MOPR first.'))
            f = layer.getFeature(fid)
            if not f.isValid(): raise ValueError(t('La scheda non esiste più: aggiornare l’elenco.', 'The record no longer exists: refresh the list.'))
            self.begin_form(checked_geometry(f.geometry(), layer.crs()), {field.name(): f[field.name()] for field in layer.fields()}, fid)
        except Exception as exc: self.error(exc)

    def convert(self):
        if self.stack.currentIndex() == 1: raise ValueError(t('Salvare o annullare la compilazione MOPR prima della conversione.', 'Save or cancel the MOPR form before converting.'))
        fid = self.saved.currentData()
        if fid is None: raise ValueError(t('Selezionare una scheda MOPR salvata.', 'Select a saved MOPR record.'))
        mopr = self.workspace.read(fid)  # Always read geometry AND attributes from disk.
        layers = {k: QgsProject.instance().mapLayer(c.currentData()) for k, c in self.inputs.items()}
        if layers['i'] is None or layers['r'] is None: raise ValueError(t('Selezionare i layer Interventi e Ritrovamenti MAGOH.', 'Select the MAGOH Interventions and Findings layers.'))
        if layers['i'] is layers['r']: raise ValueError(t('Interventi e Ritrovamenti devono essere layer distinti.', 'Interventions and Findings must be different layers.'))
        if not self.evidences.isChecked(): layers['e'] = None
        for kind, layer in layers.items():
            if layer is not None and not SOURCE_FIELDS[kind].issubset({f.name().lower() for f in layer.fields()}):
                raise ValueError(t('Schema della sorgente MAGOH non riconosciuto: ', 'MAGOH source schema not recognised: ') + layer.name())
        if any(l.isEditable() for l in layers.values() if l is not None):
            raise ValueError(t('Salvare o annullare le modifiche pendenti dei layer MAGOH prima di convertire.', 'Save or discard pending edits of the MAGOH layers before converting.'))
        options = ConversionOptions(working_template=True, regione=mopr.regione, comune_fisso='', usa_evidenze=self.evidences.isChecked(),
            geo_default={f: self.workspace.vocab.label(f.upper(), getattr(mopr, f)) for f in ('gpt', 'gpm', 'gpbb')})
        converter = MagohToGnaConverter(self.plugin_dir / 'conversion/mapping.json')
        records = {k: rows(l) if l is not None else [] for k, l in layers.items()}
        # The archive splits fourth-level evidence into spatial/non-spatial
        # tables. Both contribute source information; only spatial rows can
        # contribute a geometry. Do not silently omit the second table.
        if layers['e'] is not None:
            def identity(layer):
                if layer is None or layer.providerType() != 'ogr': return None, None
                uri = QgsProviderRegistry.instance().decodeUri('ogr', layer.source())
                path = uri.get('path')
                return (Path(path).resolve() if path else None), uri.get('layerName')
            path, table = identity(layers['e'])
            finding_path, _ = identity(layers['r'])
            if path and path == finding_path and table == 'magoh_evidenza_archeologica':
                companion = next((l for l in QgsProject.instance().mapLayers().values()
                                  if isinstance(l, QgsVectorLayer) and identity(l) == (path, 'magoh_evidenza_archeologica_no_geom')), None)
                if companion is None:
                    load_options = QgsVectorLayer.LayerOptions(); load_options.loadDefaultStyle = False
                    companion = QgsVectorLayer(str(path)+'|layername=magoh_evidenza_archeologica_no_geom', 'MAGOH evidence metadata', 'ogr', load_options)
                if companion.isValid():
                    if companion.isEditable(): raise ValueError(t('Salvare o annullare prima le modifiche delle evidenze senza geometria.', 'Save or discard the edits of the evidence without geometry first.'))
                    parents = {str(r.get('id')) for r in records['r']}
                    existing = {str(e.get('id')) for e in records['e']}
                    for evidence in rows(companion):
                        key = str(evidence.get('id'))
                        if str(evidence.get('idiii_livello')) in parents and key not in existing:
                            records['e'].append(evidence); existing.add(key)
        if not records['i'] or not records['r']: raise ValueError(t('I layer scelti non contengono Interventi/Ritrovamenti visibili. Controllare i filtri.', 'The chosen layers contain no visible Interventions/Findings. Check the filters.'))
        result = converter.convert(records['i'], records['r'], records['e'], options)
        result, self._spatial_filter = filter_to_mopr(result,mopr.geometry)
        return converter, result, options, mopr, {k: len(v) for k, v in records.items()}

    def validate_only(self):
        try:
            _, result, _, _, counts = self.convert()
            self.log.setPlainText('VERIFICA — nessun file scritto\nRecord letti: ' + str(counts) + '\n\n' + result.report.riepilogo_classi().replace('ERRORI','DA COMPLETARE').replace('Errori bloccanti','Campi da completare') +
                                  '\n\nFiltro MOPR: ' + json.dumps(self._spatial_filter,ensure_ascii=False))
            self.tabs.setCurrentIndex(2)
        except Exception as exc: self.error(exc)

    def choose_output(self):
        directory = QFileDialog.getExistingDirectory(self, t('Destinazione del pacchetto GNA', 'GNA package destination'), self.output.text())
        if directory: self.output.setText(directory)

    def run(self):
        destination = None
        self.run_button.setEnabled(False)
        try:
            base = Path(self.output.text().strip())
            if not self.output.text().strip() or not base.is_dir(): raise ValueError(t('Scegliere una cartella di destinazione esistente.', 'Choose an existing destination folder.'))
            converter, result, options, mopr, counts = self.convert()
            destination = Path(tempfile.mkdtemp(prefix='MAGOH_GNA_' + datetime.now().strftime('%Y%m%d_%H%M%S_'), dir=str(base)))
            self.last = str(destination)
            staging = converter.export(result, destination, asdict(options))
            builder = GnaProjectBuilder(self.plugin_dir)
            package = builder.build(result, mopr, Path(staging['cartella']), spatial_filter=self._spatial_filter,
                                    project_name=self.project_name())
            try:
                _, load_errors = load_gna_outputs(staging['cartella'], package['gpkg'])
                # Rebind the wizard to the single exported MOPR. The original
                # workspace file stays on disk; no stale C++ layer is reused.
                from .mopr_single import ORIGIN, mopr_layers
                canonical = next((layer for layer in mopr_layers()
                                  if str(Path(layer.source().split('|')[0]).resolve()) == str(Path(package['gpkg']).resolve())), None)
                if canonical is not None:
                    canonical.setCustomProperty(ORIGIN, QgsProject.instance().writePath(str(Path(self.default_mopr_directory())/'MOPR.gpkg')))
                    canonical.setCustomProperty('magoh/gna_mopr_workspace', True)
                    self.workspace.layer = canonical
                    self.workspace.configure()
                    selected = [f.id() for f in canonical.getFeatures() if value(f['MOPR_ID']) == mopr.mopr_id]
                    canonical.selectByIds(selected)
                    self.refresh_saved()
            except Exception as exc:
                load_errors = ['Pacchetto creato; caricamento anteprime non riuscito: ' + str(exc)]
            report = ('TEMPLATE DI LAVORO MAGOH-GNA GENERATO\n\n' + package['project_zip'] +
                      '\n\nSchede nel template: ' + str(package['counts']) + '\nRecord MAGOH letti: ' + str(counts) +
                      '\n\nREPORT DELLA CONVERSIONE SORGENTE (prima della compilazione del template)\n' + staging['testo'] + '\n\n' + result.report.riepilogo_classi())
            report += '\n\nIl pacchetto contiene i codici e i collegamenti derivati dal MOPR. Il file conversion_log.csv nel pacchetto collega ogni scheda GNA alla scheda MAGOH di origine; il report non certifica la validità finale delle schede.'
            report += '\n\nFiltro MOPR: ' + json.dumps(self._spatial_filter,ensure_ascii=False)
            for title, entries in [('Da completare', package.get('da_completare')), ('Record non inseriti', package.get('errors')),
                                   ('Avvisi', package.get('warnings')), ('Caricamento layer', load_errors)]:
                if entries: report += '\n\n' + title + ':\n' + str(entries)
            report += '\n\nApri il progetto .qgz del pacchetto, controlla le schede e usa il plugin GNA ufficiale per scaricare i MOSI già presenti, verificare duplicati e inviare. Nessun invio è stato effettuato da MAGOH.'
            self.log.setPlainText(report.replace('Errori bloccanti','Campi da completare').replace('ERRORI (','DA COMPLETARE ('))
            self.open_button.setEnabled(True)
            self.tabs.setCurrentIndex(2)
        except Exception as exc:
            if destination is not None:
                self.log.setPlainText('PACCHETTO NON COMPLETATO\n' + str(exc) + '\n\nFile parziali conservati per controllo in:\n' + str(destination))
                self.open_button.setEnabled(True)
                self.tabs.setCurrentIndex(2)
            self.error(exc)
        finally: self.run_button.setEnabled(True)

    def open_output(self):
        if self.last: QDesktopServices.openUrl(QUrl.fromLocalFile(self.last))

    def closeEvent(self, event):
        self._form_generation += 1
        if self._prefill_task is not None:
            self._prefill_task.cancel(); self._prefill_task=None
        self.stop_drawing()
        super().closeEvent(event)
