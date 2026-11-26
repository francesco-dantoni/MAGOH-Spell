"""Closed, searchable geographic IDs with non-recursive Comune dependencies."""
from qgis.PyQt.QtCore import Qt, QSignalBlocker, QModelIndex, QTimer, QEvent
from qgis.PyQt import sip
from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QComboBox, QCheckBox, QMessageBox, QCompleter,QLineEdit
from qgis.gui import QgsGui, QgsAttributeForm, QgsEditorWidgetFactory, QgsEditorWidgetWrapper
from qgis.core import QgsFieldFormatter, QgsApplication, QgsVariantUtils, QgsProject
from ..geographic_lookups import by_municipality, municipality_label, municipality_rows, municipality_names, municipality_code, readable_names
from ..i18n import _tf as t
from .editor_registry import config_widget, field_value

WIDGET_ID = 'MAGOHStreetId'
TOPONYM_ID = 'MAGOHToponymId'
MUNICIPALITY_ID = 'MAGOHMunicipalityId'
_factories = []
_formatters = []


def clean(value):
    return '' if QgsVariantUtils.isNull(value) else str(value).strip()


def _attribute_form(widget):
    """Return the QgsAttributeForm that contains ``widget`` (or None).

    The form opened by QGIS (identify, add feature, attribute
    table) is created in C++. PyQt/SIP exposes such an ancestor as a plain
    QWidget unless Python already holds a typed reference, so an
    ``isinstance(parent, QgsAttributeForm)`` test is always False in real
    use and the dependent editors would never receive the Comune changes.
    Qt's own meta-object check is independent from the Python wrapper type.
    """
    current = widget
    while current is not None:
        try:
            if current.inherits('QgsAttributeForm'):
                form = current if isinstance(current, QgsAttributeForm) else \
                    sip.wrapinstance(sip.unwrapinstance(current), QgsAttributeForm)
                return form if isinstance(form, QgsAttributeForm) else sip.cast(current, QgsAttributeForm)
        except (RuntimeError, TypeError):
            return None
        current = current.parentWidget()
    return None


def _same(a, b):
    """Same C++ object, whatever Python wrapper represents it."""
    if a is None or b is None:
        return False
    try:
        return sip.unwrapinstance(a) == sip.unwrapinstance(b)
    except (RuntimeError, TypeError):
        return False


# Shared per-form flags live on the C++ object (Qt dynamic properties), so
# every wrapper sees them even if each holds a different Python wrapper.
def _flag(form, name, default=None):
    value = form.property(name)
    return default if value is None else value


def _set_flag(form, name, value):
    form.setProperty(name, value)


class _GeoCombo(QComboBox):
    """Editable combo that re-reads the live Comune before showing choices."""
    def __init__(self, wrapper, parent=None):
        super().__init__(parent)
        self._wrapper = wrapper

    def showPopup(self):
        wrapper = self._wrapper
        if wrapper is not None and not sip.isdeleted(wrapper):
            wrapper.sync_from_form()
        super().showPopup()

    def focusInEvent(self, event):
        # QComboBox forwards focus to its line edit without going through
        # event filters: handle it here.
        wrapper = self._wrapper
        if wrapper is not None and not sip.isdeleted(wrapper):
            wrapper.focus_in()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        wrapper = self._wrapper
        if wrapper is not None and not sip.isdeleted(wrapper) and not self.view().isVisible():
            wrapper.focus_out()


class MunicipalityWrapper(QgsEditorWidgetWrapper):
    """A typing field, not a dropdown: explicit completion stores the source ID."""
    def __init__(self,layer,index,editor,parent):
        super().__init__(layer,index,editor,parent)
        self.edit=None
        self.selected=''
        self.display=''
        self._pending_completion=False

    def createWidget(self,parent):
        return QLineEdit(parent)

    def initWidget(self,editor):
        self.edit=editor
        counts={}
        for _,name,*_ in municipality_rows():counts[name]=counts.get(name,0)+1
        self.labels={}
        self.codes={}
        for code,name,istat,*_ in sorted(municipality_rows(),key=lambda row:(row[1].casefold(),row[2],row[0])):
            label=name if counts[name]==1 else name+' — ISTAT '+istat+' [ID: '+code+']'
            self.labels[code]=label;self.codes[label]=code
        completer=QCompleter(list(self.codes),editor)
        self.completer=completer
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.activated[QModelIndex].connect(self._complete)
        editor.setCompleter(completer)
        editor.editingFinished.connect(self._finish)
        editor.setClearButtonEnabled(True)
        editor.setPlaceholderText(t('Scrivi e seleziona un Comune','Type and select a municipality'))

    def _complete(self,index):
        code=self.codes.get(clean(index.data(Qt.ItemDataRole.DisplayRole)),'')
        if code:
            # QLineEdit also handles the completer activation. Apply the ID
            # after its internal text insertion, so rejecting a Comune change
            # cannot leave a new label paired with the previous code.
            self._pending_completion=True
            def commit():
                if sip.isdeleted(self) or self.edit is None or sip.isdeleted(self.edit):return
                self._pending_completion=False
                self.updateValues(code)
                self.emitValueChanged()
            QTimer.singleShot(0,commit)

    def _finish(self):
        if self._pending_completion or self.edit.completer().popup().isVisible():return
        text=self.edit.text().strip()
        if text!=self.display:
            code=municipality_code(text)
            if code:self.updateValues(code)
            elif text:
                self.updateValues(self.selected)
                self.edit.setToolTip(t('Seleziona un Comune tra i suggerimenti.', 'Select a municipality from the suggestions.'))
                return
        self.emitValueChanged()

    def valid(self):return self.edit is not None

    def value(self):
        return self.selected if self.edit is not None and self.edit.text()==self.display and self.selected else None

    def updateValues(self,value,additional_values=None):
        self.selected=clean(value)
        self.display=self.labels.get(self.selected,municipality_label(self.selected) or self.selected)
        if self.edit is not None:
            blocker=QSignalBlocker(self.edit)
            self.edit.setText(self.display)
            del blocker

    def setFeature(self, feature):
        # do not depend on the C++ base dispatching updateValues() to
        # Python (not reliable on every runtime): load the stored ID directly.
        try:
            self.setFormFeature(feature)
        except Exception:
            pass
        try:
            self.updateValues(field_value(feature, self.fieldIdx()))
        except Exception:
            self.updateValues(None)

    def showIndeterminateState(self):
        self.selected='';self.display='';self.edit.clear()


class GeographicWrapper(QgsEditorWidgetWrapper):
    def __init__(self, layer, index, editor, parent, kind='street'):
        super().__init__(layer, index, editor, parent)
        self.kind = kind
        self.combo = None
        self.unassigned = None
        self.commune = ''
        self.form = None

    def createWidget(self, parent):
        widget = QWidget(parent)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        self.combo = _GeoCombo(self, widget)
        layout.addWidget(self.combo)
        if self.kind == 'toponym':
            self.unassigned = QCheckBox(t('Includi toponimi senza Comune', 'Include unassigned toponyms'), widget)
            layout.addWidget(self.unassigned)
        return widget

    def initWidget(self, editor):
        combo = self.combo
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setMinimumContentsLength(12)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        combo.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        combo.completer().activated[QModelIndex].connect(self._completion)
        combo.activated.connect(lambda *_: self.emitValueChanged())
        combo.lineEdit().editingFinished.connect(self.emitValueChanged)
        if self.unassigned is not None:
            self.unassigned.toggled.connect(lambda *_: self._populate(self.value()))
        combo.lineEdit().installEventFilter(self)
        combo.lineEdit().setPlaceholderText(t('Scrivi per cercare e seleziona', 'Type to search and select'))
        self._populate('')
        QTimer.singleShot(0, self._bind_form)

    def eventFilter(self, watched, event):
        # Typing in the line edit: make sure the choices match the live Comune.
        try:
            if self.combo is not None and watched is self.combo.lineEdit():
                if event.type() == QEvent.Type.FocusIn:
                    self.focus_in()
                elif event.type() == QEvent.Type.FocusOut:
                    self.focus_out()
        except RuntimeError:
            pass
        return False

    def focus_in(self):
        self.sync_from_form()
        # Start typing directly: the empty choice "—" is not search text.
        if self.combo.currentIndex() == 0 and self.combo.lineEdit().text() == '—':
            blocker = QSignalBlocker(self.combo.lineEdit())
            self.combo.lineEdit().clear()
            del blocker

    def focus_out(self):
        if not self.combo.lineEdit().text().strip() and self.combo.currentIndex() <= 0:
            blocker = QSignalBlocker(self.combo)
            line_blocker = QSignalBlocker(self.combo.lineEdit())
            self.combo.setCurrentIndex(0)
            self.combo.setEditText('—')
            del line_blocker, blocker

    def _bind_form(self):
        """Connect to the containing form; retried until the widget is placed."""
        if self.combo is None or sip.isdeleted(self.combo):
            return False
        if self.form is not None:
            try:
                if not sip.isdeleted(self.form):
                    return True
            except RuntimeError:
                pass
            self.form = None
        form = _attribute_form(self.combo)
        if form is None:
            return False
        self.form = form
        form.widgetValueChanged.connect(self._form_changed)
        # A Comune chosen before the binding must not be lost.
        QTimer.singleShot(0, self.sync_from_form)
        return True

    def _commune_wrappers(self):
        if self.form is None:
            return []
        try:
            index = self.layer().fields().indexFromName('id_comune')
            return [w for w in self.form.findChildren(QgsEditorWidgetWrapper)
                    if w.fieldIdx() == index and w.layer() is not None and w.layer().id() == self.layer().id()]
        except RuntimeError:
            return []

    def _live_commune(self):
        """Current, not yet saved Comune value in the form (None if unknown)."""
        for wrapper in self._commune_wrappers():
            try:
                return clean(wrapper.value())
            except Exception:
                continue
        return None

    def _commune_uses_value_relation(self):
        index = self.layer().fields().indexFromName('id_comune')
        return index >= 0 and self.layer().editorWidgetSetup(index).type() == 'ValueRelation'

    def sync_from_form(self):
        """Pull the live Comune: a missed signal can never leave the list empty."""
        if self.combo is None or sip.isdeleted(self.combo):
            return
        if not self._bind_form():
            return
        if _flag(self.form, 'magohGeoBusy', False):
            return
        commune = self._live_commune()
        if commune is None or commune == self.commune:
            return
        if not commune and self._commune_uses_value_relation():
            return  # interim NULL while the native completer is being typed in
        # Same path as a received signal: compatible values are kept, and
        # incompatible ones are cleared only after the single confirmation.
        self._form_changed('id_comune', commune, True)

    def _siblings(self):
        if self.form is None:
            return [self]
        result = [w for w in self.form.findChildren(GeographicWrapper) if _same(w.form, self.form)]
        if self not in result:
            result.append(self)
        return result

    def _label(self, code):
        lookup = QgsProject.instance().mapLayer(self.config().get('MunicipalityLayer', ''))
        return municipality_label(code, lookup)

    def _choices(self, commune=None):
        label = self._label(self.commune if commune is None else commune)
        rows = list(by_municipality(self.kind).get(label.casefold(), ())) if label else []
        if self.kind == 'toponym' and self.unassigned.isChecked():
            rows.extend(by_municipality(self.kind).get('', ()))
        return rows

    def _completion(self, index):
        key = index.data(Qt.ItemDataRole.UserRole)
        if key is not None:
            self.combo.setCurrentIndex(self.combo.findData(key))
            self.emitValueChanged()

    def valid(self):
        return self.combo is not None

    def value(self):
        if self.combo is None:
            return None
        index = self.combo.currentIndex()
        if index >= 0 and self.combo.currentText() == self.combo.itemText(index):
            return self.combo.itemData(index)
        return None

    def _populate(self, value):
        if self.combo is None:
            return
        value = clean(value)
        blocker = QSignalBlocker(self.combo)
        line_blocker = QSignalBlocker(self.combo.lineEdit())
        self.combo.clear()
        self.combo.addItem('—', None)
        # readable names as in the other fields; the ID stays the stored
        # value (tooltip only). Homonymous segments get a discreet ordinal.
        names = readable_names(self.kind)
        choices = [(ident, names.get(ident, label)) for ident, label in self._choices()]
        totals = {}
        for _, label in choices:
            totals[label] = totals.get(label, 0) + 1
        seen = {}
        for ident, label in choices:
            text = label
            if totals[label] > 1:
                seen[label] = seen.get(label, 0) + 1
                text = label + ' (' + t('tratto', 'segment') + ' ' + str(seen[label]) + ')'
            self.combo.addItem(text, ident)
            self.combo.setItemData(self.combo.count() - 1, 'ID ' + ident, Qt.ItemDataRole.ToolTipRole)
        index = self.combo.findData(value) if value else 0
        if value and index < 0:
            known = names.get(value)
            self.combo.addItem((known + ' (' + t('altro Comune', 'other municipality') + ')') if known else value, value)
            self.combo.setItemData(self.combo.count() - 1, 'ID ' + value, Qt.ItemDataRole.ToolTipRole)
            index = self.combo.count() - 1
        self.combo.setCurrentIndex(index)
        current_id = self.combo.itemData(index) if index >= 0 else None
        self.combo.setToolTip(t('Seleziona un suggerimento: viene salvato il codice, non il testo.', 'Select a suggestion: the ID is stored, not the label.')
                              + (('\nID: ' + str(current_id)) if current_id else ''))
        del line_blocker, blocker

    def updateValues(self, value, additional_values=None):
        self._populate(value)

    def setFeature(self, feature):
        self._bind_form()
        self.commune = clean(field_value(feature, feature.fields().indexOf('id_comune')))
        self.updateValues(field_value(feature, self.fieldIdx()))

    def _form_changed(self, name, value, changed):
        if name != 'id_comune' or self.form is None or _flag(self.form, 'magohGeoBusy', False):
            return
        commune = clean(value)
        if not commune and self._commune_uses_value_relation():
            return # Native 3.40 completer emits interim NULL while typing.
        if commune == _flag(self.form, 'magohGeoRejected', None):
            _set_flag(self.form, 'magohGeoBusy', True)
            try:self._restore_commune()
            finally:_set_flag(self.form, 'magohGeoBusy', False)
            return
        if changed and self._commune_popup_open():
            # The user is still choosing among suggestions (e.g. "Lucca" while
            # "Lucca Sicula" is listed): decide once, when the choice is final.
            if not _flag(self.form, 'magohGeoDeferred', False):
                _set_flag(self.form, 'magohGeoDeferred', True)
                QTimer.singleShot(150, self._deferred_commune)
            return
        if commune == self.commune:
            return
        wrappers = self._siblings()
        invalid = [w for w in wrappers if clean(w.value()) and clean(w.value()) not in {ident for ident, _ in w._choices(commune)}]
        _set_flag(self.form, 'magohGeoBusy', True)
        try:
            if invalid and changed:
                answer = QMessageBox.question(self.form, 'MAGOH Spell',
                    t('Il nuovo Comune non contiene l’indirizzo o il toponimo selezionato. Svuotare i campi non compatibili?',
                    'The selected address or toponym does not belong to this municipality. Clear incompatible fields?'),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    _set_flag(self.form, 'magohGeoRejected', commune)
                    self._restore_commune()
                    # QgsAttributeForm may finish propagating the outer change
                    # after this slot returns. Restore once more afterwards.
                    form = self.form
                    def restore():
                        if form is None or sip.isdeleted(form):return
                        _set_flag(form, 'magohGeoBusy', True)
                        try:self._restore_commune()
                        finally:
                            _set_flag(form, 'magohGeoBusy', False)
                            _set_flag(form, 'magohGeoRejected', None)
                    QTimer.singleShot(0,restore)
                    return
            for wrapper in wrappers:
                old = wrapper.value()
                wrapper.commune = commune
                wrapper._populate(None if wrapper in invalid and changed else old)
                if wrapper in invalid and changed:
                    wrapper.emitValueChanged()
        finally:
            _set_flag(self.form, 'magohGeoBusy', False)

    def _commune_popup_open(self):
        for wrapper in self._commune_wrappers():
            try:
                widget = wrapper.widget()
                edit = widget if isinstance(widget, QLineEdit) else (widget.findChild(QLineEdit) if widget is not None else None)
                completer = edit.completer() if edit is not None else None
                if completer is not None and completer.popup() is not None and completer.popup().isVisible():
                    return True
            except RuntimeError:
                continue
        return False

    def _deferred_commune(self):
        form = self.form
        if form is None or sip.isdeleted(form) or sip.isdeleted(self):
            return
        if self._commune_popup_open():
            QTimer.singleShot(150, self._deferred_commune)
            return
        _set_flag(form, 'magohGeoDeferred', False)
        commune = self._live_commune()
        if commune is not None:
            self._form_changed('id_comune', commune, True)

    def _restore_commune(self):
        for wrapper in self._commune_wrappers():
            if isinstance(wrapper,MunicipalityWrapper):wrapper.updateValues(self.commune or None)
            else:wrapper.setValues(self.commune or None, [])
            wrapper.emitValueChanged()

    def showIndeterminateState(self):
        if self.combo is not None:
            self.combo.setCurrentIndex(-1)


class GeographicFactory(QgsEditorWidgetFactory):
    def __init__(self, kind):
        super().__init__('MAGOH — ' + {'street':'Indirizzo','toponym':'Toponimo','municipality':'Comune'}[kind] + ' (ID)')
        self.kind = kind

    def create(self, layer, index, editor, parent):
        if self.kind=='municipality':return MunicipalityWrapper(layer,index,editor,parent)
        return GeographicWrapper(layer, index, editor, parent, self.kind)

    def fieldScore(self, layer, index):
        return 0

    def configWidget(self, layer, index, parent):
        return config_widget(layer, index, parent)


class GeographicFormatter(QgsFieldFormatter):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind

    def id(self):
        return {'street':WIDGET_ID,'toponym':TOPONYM_ID,'municipality':MUNICIPALITY_ID}[self.kind]

    def representValue(self, layer, index, config, cache, value):
        text = clean(value)
        if self.kind == 'municipality':
            return municipality_names().get(text, text)
        return readable_names(self.kind).get(text, text)


def _adopt(factory):
    """Make a factory registered before a plugin update build current wrappers.

    QGIS keeps editor-widget factories for the whole session and offers no way
    to replace one with the same ID. After "Install from ZIP" without a QGIS
    restart, the earlier factory would otherwise keep creating its own wrappers.
    Only a factory created by this module (same IDs) is touched.
    """
    try:
        if factory is None or isinstance(factory, GeographicFactory):
            return False
        namespace = getattr(getattr(type(factory), 'create', None), '__globals__', None)
        if not namespace or namespace is globals() or namespace.get('WIDGET_ID') != WIDGET_ID:
            return False
        namespace['GeographicWrapper'] = GeographicWrapper
        namespace['MunicipalityWrapper'] = MunicipalityWrapper
        _factories.append(factory)
        return True
    except Exception:
        return False


def register():
    registry = QgsGui.editorWidgetRegistry()
    formatters = QgsApplication.fieldFormatterRegistry()
    for kind, ident in (('street', WIDGET_ID), ('toponym', TOPONYM_ID),('municipality',MUNICIPALITY_ID)):
        if ident not in registry.factories():
            factory = GeographicFactory(kind)
            _factories.append(factory)
            registry.registerWidget(ident, factory)
        else:
            _adopt(registry.factory(ident))
        existing = formatters.fieldFormatter(ident)
        if existing.id() == ident and not isinstance(existing, GeographicFormatter):
            # a formatter left in memory before a plugin update (even
            # one whose folder was uninstalled without restarting QGIS) would
            # keep reading its own, possibly deleted, data files. Replace it.
            try:
                formatters.removeFieldFormatter(ident)
            except Exception:
                try:
                    formatters.removeFieldFormatter(existing)
                except Exception:
                    pass
        if formatters.fieldFormatter(ident).id() != ident:
            formatter = GeographicFormatter(kind)
            _formatters.append(formatter)
            formatters.addFieldFormatter(formatter)
