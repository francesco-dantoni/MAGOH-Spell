import os
import traceback
import importlib
import sqlite3
import binascii
import shutil
import re
import html
import json
import math
import io
import zipfile
import xml.etree.ElementTree as ET
import time
import tempfile
import hashlib
from pathlib import Path
from . import local_codes
from .related_filters import session_for, RelatedFilterError
from .pdf_support import pdf_canvas
from .ui_theme import (
    brand_header, dock_header, dialog_frame, action_bar, section, card, hint, scroll_page,
    tool_row, set_role, segmented, key_value_grid, ui_icon, SPACE_XS, SPACE_S,
    SPACE_M, SPACE_L,
)

from qgis.PyQt.QtWidgets import (
    QInputDialog, QHeaderView,
    QToolButton,
    QAction,
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QPushButton,
    QHBoxLayout,
    QLabel,
    QComboBox,
    QFileDialog,
    QCheckBox,
    QGroupBox,
    QSpinBox,
    QListWidget,
    QListWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTabWidget,
    QSizePolicy,
    QTextBrowser,
    QDockWidget,
    QWidget,
    QProgressDialog,
    QProgressBar,
    QApplication,
    QGridLayout,
    QAbstractItemView,
)
from qgis.PyQt.QtGui import QIcon, QColor, QFont, QPainter, QTextDocument, QImage, QTextCursor
from qgis.PyQt.QtCore import QSettings, Qt, QRectF, QSize, QRect, QDateTime, QDate, QTime, QTimer, QVariant, QEvent
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsApplication,
    QgsTask,
    QgsFeature,
    QgsGeometry,
    QgsFeatureRequest,
    QgsPrintLayout,
    QgsLayoutExporter,
    QgsLayoutSize,
    QgsLayoutPoint,
    QgsLayoutItemLabel,
    QgsLayoutItemMap,
    QgsLayoutItemScaleBar,
    QgsLayoutItemHtml,
    QgsLayoutFrame,
    QgsUnitTypes,
    QgsWkbTypes,
    QgsRuleBasedRenderer,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsMapSettings,
    QgsMapRendererCustomPainterJob,
    QgsCoordinateTransform,
    QgsRectangle,
    QgsMapLayer,
    QgsMessageLog,
    QgsRelation,
    QgsAttributeEditorRelation,
    QgsAttributeEditorContainer,
    QgsEditorWidgetSetup,
    QgsFieldConstraints,
    QgsDefaultValue,
    QgsTextFormat,
    Qgis,
)
from qgis.gui import (
    QgsRelationEditorWidget,
    QgsAbstractRelationEditorWidgetFactory,
    QgsRelationEditorConfigWidget,
    QgsGui,
)

# Modelli delle viste del Relation Editor: servono a ricavare l'fid della riga
# selezionata. Non sono presenti in ogni build di QGIS, e l'assenza non deve
# impedire il caricamento del plugin: chi li usa lavora già in try/except.
try:
    from qgis.gui import QgsAttributeTableModel
except ImportError:  # pragma: no cover
    QgsAttributeTableModel = None
try:
    from qgis.gui import QgsFeatureListModel
except ImportError:  # pragma: no cover
    QgsFeatureListModel = None

from .api_client import MagohApiSettings, MagohApiClient
from .api_structure import ENTITY_SPECS, flatten_search_docs, feature_collection, entity_counts
from .api_template_import import CORE_IMPORT_SPECS, TemplateLookups, map_entity, tail_int
from .api_spatial import filter_page, record_identity, apply_intervention_labels
from .api_admin_index import plan_queries
from .api_geometry import for_layer as api_geometry_for_layer
from .template_integrity import ensure_archive_schema, CascadeCommitGuard
from .street_lookups import ensure_street_schema
from .gui.api_area import ApiAreaPanel
from .gui.ui_helpers import fit_dialog_to_parent
# Finestre di messaggio con lo stile MAGOH (solo per questo modulo, non per QGIS).
from .ui_theme import MagohMessageBox as QMessageBox
from .ui_theme import apply_teal_style
from .i18n import LANGUAGE_SETTING, _language, _t, _tf, _translate_widget_tree
from . import local_codes
from .related_filters import relation_graph, component


# Nome del progetto QGIS incorporato nel GeoPackage distribuito con il plugin.
# ATTENZIONE: nel gpkg il progetto si chiama "template MAGOH" (con lo SPAZIO).
# Le versioni <= v13 cercavano "template_MAGOH" e non lo trovavano mai.
DEFAULT_PROJECT_NAME = "template MAGOH"


def _metadata_version():
    """Versione letta da metadata.txt: una sola fonte per il numero di versione."""
    import configparser
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(os.path.join(os.path.dirname(__file__), "metadata.txt"), encoding="utf-8")
        return parser.get("general", "version", fallback="0")
    except (OSError, configparser.Error):
        return "0"


PLUGIN_VERSION = _metadata_version()

# Progetti che puntano a sorgenti remote (PostGIS/WFS): li segnaliamo prima di aprirli
# perche' offline bloccherebbero QGIS in attesa della connessione.
_REMOTE_PROJECT_HINTS = ("dbname=", "host=", "service=")


def migrate_legacy_settings():
    """V1.15: copy settings saved under the old plugin identifier, once.

    Old keys are left untouched, so an earlier version keeps working if it is
    reinstalled. Nothing is overwritten if a new key already exists.
    """
    try:
        settings = QSettings()
        if settings.value("magoh_spell/settings_migrated_from_legacy", False, type=bool):
            return 0
        settings.beginGroup("magoh_offline")
        keys = settings.allKeys()
        values = {key: settings.value(key) for key in keys}
        settings.endGroup()
        copied = 0
        for key, value in values.items():
            target = "magoh_spell/" + key
            if not settings.contains(target):
                settings.setValue(target, value)
                copied += 1
        settings.setValue("magoh_spell/settings_migrated_from_legacy", True)
        settings.sync()
        return copied
    except Exception:
        return 0


def _log(msg, level=Qgis.Info):
    """Log leggero nel pannello messaggi di QGIS (nessun popup)."""
    try:
        QgsMessageLog.logMessage(str(msg), "MAGOH Spell", level)
    except Exception:
        pass


MAGOH_RELATION_WIDGET_ID = "magoh_relation_editor"


class MagohRelationEditorWidget(QgsRelationEditorWidget):
    """Relation Editor MAGOH con navigazione esplicita padre -> figlio.

    QGIS permette nativamente al figlio di aprire il padre tramite il widget
    Relation Reference. Nel verso opposto il Relation Editor mostra i figli,
    ma non espone un pulsante "apri form" dedicato. Questo widget mantiene il
    Relation Editor standard e aggiunge soltanto un comando non modale per
    aprire la scheda completa del figlio selezionato. Il doppio clic su una
    riga della tabella esegue la stessa azione.
    """

    #: Etichette del pulsante per i figli riconosciuti. Il nome della scheda è
    #: più utile di un generico "figlio": chi lavora sui dati ragiona per schede.
    MAGOH_ETICHETTE_FIGLIO = (
        ("evidenza", ("Apri scheda di evidenza archeologica",
                      "Open archaeological evidence record")),
        ("ritrovament", ("Apri scheda di ritrovamento", "Open find record")),
        ("iii_livello", ("Apri scheda di ritrovamento", "Open find record")),
        ("intervento", ("Apri scheda di intervento", "Open intervention record")),
    )

    def __init__(self, config, parent=None, iface=None):
        super().__init__(config, parent)
        self._magoh_iface = iface
        self._magoh_bound_views = set()
        self._magoh_open_button = QPushButton(_t("Apri scheda figlio"), self)
        self._magoh_open_button.setToolTip(_tf(
            "Apre in una finestra autonoma la scheda completa del record figlio selezionato.",
            "Open the full form of the selected child record in a separate window."
        ))
        self._magoh_open_button.clicked.connect(self._magoh_open_selected_child)
        QTimer.singleShot(0, self._magoh_aggiorna_etichetta)
        try:
            lay = self.layout()
            if isinstance(lay, QGridLayout):
                cols = max(1, lay.columnCount())
                lay.addWidget(self._magoh_open_button, lay.rowCount(), 0, 1, cols)
            elif lay is not None:
                lay.addWidget(self._magoh_open_button)
        except Exception as e:
            _log(f"Pulsante apri figlio Relation Editor: {e}", Qgis.Warning)
        QTimer.singleShot(0, self._magoh_bind_double_click)

    def showEvent(self, event):
        try:
            super().showEvent(event)
        finally:
            QTimer.singleShot(0, self._magoh_bind_double_click)
            QTimer.singleShot(0, self._magoh_aggiorna_etichetta)

    # -- identità del layer figlio ---------------------------------------

    def _magoh_child_layer(self):
        """Il layer figlio della relazione, e nient'altro.

        Tutto quello che il pulsante apre deve venire da qui: è la sola
        garanzia che la scheda mostrata sia davvero quella del figlio.
        """
        try:
            relation = self.relation()
            if relation is None or not relation.isValid():
                return None
            layer = relation.referencingLayer()
            if isinstance(layer, QgsVectorLayer) and layer.isValid():
                return layer
        except Exception:
            pass
        return None

    def _magoh_aggiorna_etichetta(self):
        """Nomina il pulsante con la scheda che apre davvero."""
        try:
            layer = self._magoh_child_layer()
            if layer is None:
                return
            nome = str(layer.name() or "").lower()
            for chiave, (it, en) in self.MAGOH_ETICHETTE_FIGLIO:
                if chiave in nome:
                    self._magoh_open_button.setText(_tf(it + "…", en + "…"))
                    self._magoh_open_button.setToolTip(_tf(
                        "Apre in una finestra autonoma la scheda selezionata "
                        "del layer «%s»." % layer.name(),
                        "Open the selected record of layer “%s” in a separate window."
                        % layer.name()))
                    return
            self._magoh_open_button.setText(_tf("Apri scheda figlio…", "Open child record…"))
        except Exception:
            pass

    def _magoh_bind_double_click(self):
        """Collega una volta sola il doppio clic delle viste interne."""
        try:
            for view in self.findChildren(QAbstractItemView):
                key = id(view)
                if key in self._magoh_bound_views:
                    continue
                try:
                    view.doubleClicked.connect(self._magoh_open_selected_child)
                    self._magoh_bound_views.add(key)
                except Exception:
                    pass
        except Exception:
            pass

    def _magoh_fid_da_indice(self, index):
        """Il fid di una riga, qualunque modello stia usando la vista.

        Il Relation Editor annida modelli diversi (tabella o elenco, con
        eventuali proxy di filtro) e ognuno espone il fid a modo suo: si prova
        la strada diretta, poi i ruoli, poi la mappatura riga → id.
        """
        if index is None or not index.isValid():
            return None
        try:
            model = index.model()
        except Exception:
            model = None
        ruoli = []
        try:
            ruoli.append(QgsAttributeTableModel.FeatureIdRole)
        except Exception:
            pass
        try:
            ruoli.append(QgsFeatureListModel.FeatureInfoRole)
        except Exception:
            pass
        for ruolo in ruoli:
            try:
                v = index.data(ruolo)
            except Exception:
                continue
            if isinstance(v, int):
                return v
            fid = getattr(v, "id", None)
            if isinstance(fid, int):
                return fid
        # ultima strada: il modello sa convertire la riga in id
        origine, idx = model, index
        for _ in range(4):
            if origine is None:
                break
            try:
                return int(origine.rowToId(idx.row()))
            except Exception:
                pass
            try:
                idx = origine.mapToSource(idx)
                origine = idx.model()
            except Exception:
                break
        return None

    def _magoh_selected_child_fids(self):
        """Gli fid dei figli selezionati.

        Prima il gestore di selezione della relazione, che è la fonte
        autorevole; se è vuoto — succede quando la riga è evidenziata nella
        vista ma la selezione non è stata propagata — si leggono le righe
        selezionate nelle viste interne.
        """
        try:
            manager = self.featureSelectionManager()
            if manager is not None:
                ids = [int(x) for x in manager.selectedFeatureIds()]
                if ids:
                    return ids
        except Exception:
            pass
        fids = []
        try:
            for view in self.findChildren(QAbstractItemView):
                try:
                    sel = view.selectionModel()
                    if sel is None or not sel.hasSelection():
                        continue
                    for index in sel.selectedIndexes():
                        fid = self._magoh_fid_da_indice(index)
                        if fid is not None and fid not in fids:
                            fids.append(fid)
                except Exception:
                    continue
        except Exception:
            pass
        return fids

    def _magoh_fid_figli(self, layer):
        """Gli fid dei figli della relazione per il padre corrente.

        Non si usa `self.features()`: da QGIS 3.24 quel metodo restituisce le
        feature del **padre**, non dei figli. È esattamente da lì che nasceva
        il difetto per cui il pulsante apriva la scheda con gli attributi della
        scheda padre: la form era quella del layer figlio, ma la feature che
        veniva passata era quella del padre. Qui i figli si chiedono alla
        relazione e si rileggono dal layer figlio, che è l'unica strada che
        non può restituire un record di un altro layer.
        """
        try:
            relation = self.relation()
            padre = self.feature()
            if relation is None or not relation.isValid() or padre is None:
                return []
            return [int(f.id()) for f in relation.getRelatedFeatures(padre)]
        except Exception:
            pass
        # ripiego: la richiesta di filtro della relazione, applicata al figlio
        try:
            request = self.relation().getRelatedFeaturesRequest(self.feature())
            return [int(f.id()) for f in layer.getFeatures(request)]
        except Exception:
            return []

    def _magoh_open_selected_child(self, *_args):
        try:
            layer = self._magoh_child_layer()
            if layer is None:
                return
            fids = self._magoh_selected_child_fids()
            if not fids:
                # Senza selezione il comando resta utile se il figlio è uno solo.
                figli = self._magoh_fid_figli(layer)
                if len(figli) == 1:
                    fids = figli
                else:
                    QMessageBox.information(
                        self, _t("MAGOH – Strumenti"),
                        _t("Seleziona un record figlio nella tabella.")
                    )
                    return

            # La feature si rilegge sempre dal layer figlio a partire dall'fid:
            # così quello che si apre è per costruzione un record di quel layer.
            feature = layer.getFeature(int(fids[0]))
            if feature is None or not feature.isValid():
                _log("Apertura scheda figlio: fid %s non trovato in %s"
                     % (fids[0], layer.name()), Qgis.Warning)
                return
            # Controllo di sicurezza: se i campi non sono quelli del layer
            # figlio si sta per mostrare la scheda sbagliata. Meglio non aprire.
            try:
                if feature.fields().names() != layer.fields().names():
                    _log("Apertura scheda figlio annullata: i campi del record non "
                         "corrispondono al layer %s" % layer.name(), Qgis.Warning)
                    return
            except Exception:
                pass

            if self._magoh_iface is not None:
                # Non modale: la form padre resta aperta e si può navigare avanti/indietro.
                self._magoh_iface.openFeatureForm(layer, feature, False, False)
        except Exception as e:
            _log(f"Apertura scheda figlio: {e}", Qgis.Warning)


class MagohRelationEditorFactory(QgsAbstractRelationEditorWidgetFactory):
    """Factory del Relation Editor MAGOH basato sul widget standard QGIS."""

    def __init__(self, iface):
        super().__init__()
        self._iface = iface

    def type(self):
        return MAGOH_RELATION_WIDGET_ID

    def name(self):
        return "MAGOH Relation Editor"

    def create(self, config, parent):
        return MagohRelationEditorWidget(config, parent, self._iface)

    def configWidget(self, relation, parent):
        # Riusa integralmente la configurazione del Relation Editor nativo.
        return QgsRelationEditorConfigWidget(relation, parent)


class MagohSpellPlugin:
    def __init__(self, iface):
        migrate_legacy_settings()
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = "MAGOH Spell"

        self._relation_editor_factory = None
        self._gna_dialog = None
        self._workspace_connections = []
        self._cascade_guard = CascadeCommitGuard(self)

    # ---------- QGIS hooks ----------
    def initGui(self):
        """Two stable toolbar buttons; the same access is available in Plugin menu.

        V1.19.2: pulsanti e menu vengono creati per primi. Le operazioni di
        avvio successive (editor di campo, ripristino del progetto aperto…)
        sono indipendenti: se una fallisce, il plugin resta visibile e il
        motivo compare nella barra messaggi e nel Registro messaggi.
        """
        self._build_toolbar_and_menu()
        project = QgsProject.instance()

        def connect_project_signals():
            for signal, callback in ((project.readProject, self._restore_project_workspace),
                                     (project.cleared, self._clear_project_workspace),
                                     (project.writeProject, self._save_project_workspace),
                                     (project.readProject, self._repair_known_gna_forms),
                                     (project.layersAdded, self._repair_known_gna_forms)):
                signal.connect(callback)
                self._workspace_connections.append((signal, callback))

        def restore_open_project():
            if project.fileName() or project.mapLayers():
                self._restore_project_workspace()
                self._repair_known_gna_forms()

        def register(module_name):
            def step():
                module = importlib.import_module('.gui.' + module_name, __package__)
                module.register()
            return step

        def municipality_function():
            from .municipality_locator import register_expression_function
            register_expression_function()

        steps = (
            # v18: separa template/plugin dai dati utente; un GeoPackage
            # persistente di vecchie build resta tra i recenti, non viene riaperto.
            (_tf('migrazione delle impostazioni', 'settings migration'), self._migrate_legacy_state_once),
            (_tf('controllo copie precedenti', 'legacy copy check'), self._warn_legacy_copy),
            (_tf('editor delle relazioni', 'relation editor'), self._register_relation_editor_widget),
            (_tf('editor di indirizzi e toponimi', 'address and toponym editor'), register('geographic_editor')),
            (_tf('editor della cronologia', 'chronology editor'), register('chronology_editor')),
            (_tf('editor delle evidenze', 'evidence editor'), register('evidence_editor')),
            # V1.15: Comune proposto dalla geometria dei nuovi interventi.
            (_tf('Comune dalla geometria', 'municipality from geometry'), municipality_function),
            (_tf('collegamento al progetto QGIS', 'QGIS project connection'), connect_project_signals),
            (_tf('ripristino del progetto aperto', 'open project restore'), restore_open_project),
        )
        failed = []
        for label, step in steps:
            try:
                step()
            except Exception:
                failed.append(label)
                _log(f"Avvio, {label}: {traceback.format_exc()}", Qgis.Critical)
        if failed:
            try:
                self.iface.messageBar().pushWarning(
                    "MAGOH Spell",
                    _tf('Avvio incompleto ({x}). Dettagli in Visualizza → Pannelli → Registro messaggi, scheda «MAGOH Spell».',
                        'Incomplete start-up ({x}). Details in View → Panels → Log Messages, «MAGOH Spell» tab.',
                        x=', '.join(failed)))
            except Exception:
                pass
        _log(f"MAGOH Spell {PLUGIN_VERSION}: plugin caricato; due pulsanti e menu disponibili."
             + (f" Passaggi non riusciti: {', '.join(failed)}." if failed else ""),
             Qgis.Warning if failed else Qgis.Info)

    def _build_toolbar_and_menu(self):
        """Pulsanti della barra e voci del menu Plugin → MAGOH Spell."""
        def artwork_icon(name):
            icon = QIcon()
            for size in (16, 24, 32, 48, 64, 128, 256):
                icon.addFile(os.path.join(self.plugin_dir, "icons", f"{name}_{size}.png"), QSize(size, size))
            return icon

        icon = artwork_icon("hat_m")
        tools_icon = artwork_icon("hat_management")
        self.toolbar = self.iface.addToolBar("MAGOH Spell")
        self.toolbar.setIconSize(QSize(32, 32))
        self.toolbar.setObjectName("MAGOHSpellToolbar")

        a_launcher = QAction(icon, _tf("MAGOH Spell – Progetto e dati", "MAGOH Spell – Project and data"), self.iface.mainWindow())
        a_launcher.setStatusTip(_tf("Apri gestione progetto e sorgenti dati MAGOH", "Open MAGOH project and data management"))
        a_launcher.triggered.connect(self.open_launcher_dialog)
        self.toolbar.addAction(a_launcher)
        self.iface.addPluginToMenu(self.menu, a_launcher)
        self.actions.append(a_launcher)

        a_tools_toolbar = QAction(tools_icon, _tf("MAGOH Spell – Funzioni e tool", "MAGOH Spell – Functions and tools"), self.iface.mainWindow())
        a_tools_toolbar.setStatusTip(_tf("Gestione, attributi, cronologia, API, PDF e backup", "Management, attributes, chronology, API, PDF and backup"))
        a_tools_toolbar.triggered.connect(self.toggle_tools_dock)
        self.toolbar.addAction(a_tools_toolbar)
        self.iface.addPluginToMenu(self.menu, a_tools_toolbar)
        self.actions.append(a_tools_toolbar)

        # Menu: tutte le funzioni restano disponibili anche senza toolbar.
        a_new = QAction(icon, _t("Crea nuovo progetto MAGOH…"), self.iface.mainWindow())
        a_new.triggered.connect(self.create_new_project)
        self.iface.addPluginToMenu(self.menu, a_new)
        self.actions.append(a_new)

        a_existing = QAction(icon, _t("Apri progetto MAGOH esistente…"), self.iface.mainWindow())
        a_existing.triggered.connect(self.open_existing_magoh_project)
        self.iface.addPluginToMenu(self.menu, a_existing)
        self.actions.append(a_existing)

        for label, callback in [('Prefisso dei codici scheda…', self.edit_code_prefix),
                                ('Confronta codici con una copia offline…', self.compare_local_codes)]:
            action = QAction(icon,label,self.iface.mainWindow())
            action.triggered.connect(callback)
            self.iface.addPluginToMenu(self.menu,action)
            self.actions.append(action)

        a_pdf = QAction(tools_icon, _t("Esporta schede (PDF) – mappa + attributi…"), self.iface.mainWindow())
        a_pdf.triggered.connect(self.open_pdf_export_dialog)
        self.iface.addPluginToMenu(self.menu, a_pdf)
        self.actions.append(a_pdf)

        a_backup = QAction(tools_icon, _t("Crea backup del progetto"), self.iface.mainWindow())
        a_backup.triggered.connect(self.create_backup)
        self.iface.addPluginToMenu(self.menu, a_backup)
        self.actions.append(a_backup)

        a_toolbar = QAction(icon, _tf("Mostra barra MAGOH Spell", "Show MAGOH Spell toolbar"), self.iface.mainWindow())
        a_toolbar.triggered.connect(lambda: self.toolbar.show())
        self.iface.addPluginToMenu(self.menu, a_toolbar)
        self.actions.append(a_toolbar)

        a_info = QAction(icon, _t("Info MAGOH"), self.iface.mainWindow())
        a_info.triggered.connect(self.open_info_dialog)
        self.iface.addPluginToMenu(self.menu, a_info)
        self.actions.append(a_info)

        # Il collegamento GeoPackage resta disponibile dal menu Plugins.
        a_gpkg = QAction(tools_icon, _tf("Gestisci / collega GeoPackage…", "Manage / connect GeoPackage…"), self.iface.mainWindow())
        a_gpkg.triggered.connect(self.open_gpkg_link_dialog)
        self.iface.addPluginToMenu(self.menu, a_gpkg)
        self.actions.append(a_gpkg)

        self._set_plugin_menu_icon(icon)

    def _set_plugin_menu_icon(self, icon):
        """Mostra il cappello anche sul sottomenu "MAGOH Spell" del menu Plugins."""
        try:
            root = self.iface.pluginMenu()
            for action in root.actions():
                submenu = action.menu()
                if submenu is None:
                    continue
                if action.text().replace("&", "") == self.menu or submenu.title().replace("&", "") == self.menu:
                    action.setIcon(icon)
                    submenu.setIcon(icon)
        except Exception:
            pass

    def current_language(self):
        return _language()

    def set_language(self, code):
        """Translate existing widgets, preserving geometry, selections and filters."""
        code = "en" if str(code).lower().startswith("en") else "it"
        if code == self.current_language():
            return
        QSettings().setValue(LANGUAGE_SETTING, code)
        # QAction is not a QWidget: translate toolbar/menu labels explicitly.
        for action in list(self.actions):
            try:
                action.setText(_t(action.text()))
                if action.statusTip():
                    action.setStatusTip(_t(action.statusTip()))
            except Exception:
                pass
        QTimer.singleShot(0, self._rebuild_magoh_ui_for_language)

    def _rebuild_magoh_ui_for_language(self):
        # Historical method name retained for compatibility; no objects rebuilt.
        for attr in ("_launcher_dlg", "tools_dock", "_pdf_dlg", "_gpkg_dlg", "_info_dlg", "_api_dlg", "_gna_dialog"):
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            geometry = widget.geometry()
            _translate_widget_tree(widget)
            if hasattr(widget, "retranslate"):
                try:
                    widget.retranslate()
                except Exception:
                    pass
            if attr == "tools_dock":
                # Qt synchronizes the toggle action with the blank native title.
                # Restore only the menu label, not the title bar text.
                widget.toggleViewAction().setText(_tf("MAGOH Spell – Funzioni e tool", "MAGOH Spell – Functions and tools"))
            if widget.layout():
                widget.layout().activate()
            if not isinstance(widget, QDockWidget) or widget.isFloating():
                widget.setGeometry(geometry)
        launcher = getattr(self, "_launcher_dlg", None)
        if launcher is not None:
            launcher.b_it.setChecked(self.current_language() == "it")
            launcher.b_en.setChecked(self.current_language() == "en")
            launcher._refresh_status()

    def _repair_known_gna_forms(self, *args):
        try:
            from .gui.gna_presentation import repair_known_generated_forms
            repair_known_generated_forms()
        except Exception as exc:
            _log(f'GNA form presentation: {exc}', Qgis.Warning)

    def unload(self):
        dialog = self._gna_dialog
        if dialog is not None:
            try:
                dialog.close()
                dialog.deleteLater()
            except RuntimeError:
                pass
            self._gna_dialog = None
        from .gui.local_identity import unregister
        unregister()
        self._cascade_guard.dispose()
        try:
            from .municipality_locator import unregister_expression_function
            unregister_expression_function()
        except Exception:
            pass
        for signal, callback in self._workspace_connections:
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._workspace_connections = []
        # Temporary UI filters must not remain attached to user layers on disable.
        tools = getattr(self, "tools_dock", None)
        if tools is not None:
            tools.filter_panel.clear_all_filters()
            tools.relation_panel.clear_dataset_filter()
        for a in self.actions:
            try:
                self.iface.removePluginMenu(self.menu, a)
            except Exception:
                pass
            try:
                self.iface.removeToolBarIcon(a)
            except Exception:
                pass
        self.actions = []

        # Rimuovi davvero la toolbar dalla finestra principale:
        # il semplice "del self.toolbar" lasciava una toolbar orfana a ogni reload del plugin.
        try:
            tb = getattr(self, "toolbar", None)
            if tb is not None:
                self.iface.mainWindow().removeToolBar(tb)
                tb.deleteLater()
            self.toolbar = None
        except Exception:
            pass

        # Chiudi i dock se presenti
        for dock_attr in ("tools_dock",):
            try:
                d = getattr(self, dock_attr, None)
                if d:
                    self.iface.removeDockWidget(d)
                    d.deleteLater()
                    setattr(self, dock_attr, None)
            except Exception:
                pass

        # Chiudi le finestre modeless tenute in vita (filtri, dashboard)
        for attr in ("_launcher_dlg", "_pdf_dlg", "_gpkg_dlg", "_info_dlg", "_api_dlg"):
            try:
                dlg = getattr(self, attr, None)
                if dlg is not None:
                    if attr == '_api_dlg':
                        dlg.dispose()
                    dlg.close()
                    dlg.deleteLater()
                setattr(self, attr, None)
            except Exception:
                pass

        try:
            self._unregister_relation_editor_widget()
        except Exception:
            pass

    # ---------- Relation Editor MAGOH ----------
    def _register_relation_editor_widget(self):
        """Registra una sola volta il Relation Editor MAGOH nella GUI QGIS."""
        if self._relation_editor_factory is not None:
            return
        try:
            registry = QgsGui.relationWidgetRegistry()
            # Un reload anomalo del plugin può lasciare la factory precedente:
            # rimuovila prima di registrare la nuova istanza.
            try:
                registry.removeRelationWidget(MAGOH_RELATION_WIDGET_ID)
            except Exception:
                pass
            factory = MagohRelationEditorFactory(self.iface)
            registry.addRelationWidget(factory)
            self._relation_editor_factory = factory
        except Exception as e:
            _log(f"Registrazione Relation Editor MAGOH: {e}", Qgis.Warning)

    def _unregister_relation_editor_widget(self):
        if self._relation_editor_factory is None:
            return
        try:
            QgsGui.relationWidgetRegistry().removeRelationWidget(MAGOH_RELATION_WIDGET_ID)
        except Exception:
            pass
        self._relation_editor_factory = None

    # ---------- Helpers ----------
    def _warn_legacy_copy(self):
        """V1.15: the plugin folder is now magoh_spell; an old copy must be removed."""
        try:
            from qgis import utils as qgis_utils
            if "magoh_offline" in getattr(qgis_utils, "plugins", {}) or "magoh_offline" in getattr(qgis_utils, "active_plugins", []):
                self.iface.messageBar().pushWarning(
                    "MAGOH Spell",
                    _tf("È installata anche la versione precedente (cartella magoh_off" + "line): disinstallala da Plugin → Gestisci e installa plugin e riavvia QGIS.",
                        "The previous version (folder magoh_off" + "line) is also installed: uninstall it from Plugins → Manage and Install Plugins and restart QGIS."))
        except Exception:
            pass

    def _packaged_gpkg_path(self):
        return os.path.join(self.plugin_dir, "data", "magoh_spell.gpkg")

    def _ensure_workdir(self):
        # Profile folder (writable)
        base = QgsApplication.qgisSettingsDirPath()
        workdir = os.path.join(base, "magoh_spell")
        os.makedirs(workdir, exist_ok=True)
        return workdir

    def _legacy_work_gpkg_path(self):
        """Percorso usato dalle build <= v17 per il database persistente nascosto.

        Il file NON viene cancellato: può contenere dati dell'utente e viene
        semplicemente disaccoppiato dal nuovo flusso a progetti espliciti.
        """
        # Old location, kept verbatim for the one-time migration (not recreated).
        return os.path.join(QgsApplication.qgisSettingsDirPath(), "magoh_offline", "magoh_offline.gpkg")

    def _migrate_legacy_state_once(self):
        """Disattiva una sola volta il vecchio GeoPackage nascosto come sorgente attiva.

        In v17 il plugin poteva riusare automaticamente
        ``<profilo QGIS>/magoh_spell/magoh_spell.gpkg``. Se quel file era
        stato popolato in una sessione precedente, reinstallando il plugin le
        vecchie geometrie ricomparivano. Da v18 i dati utente devono vivere in
        una cartella progetto scelta esplicitamente.
        """
        settings = QSettings()
        if settings.value("magoh_spell/v18_legacy_state_migrated", False, type=bool):
            return
        try:
            legacy = os.path.normcase(os.path.abspath(self._legacy_work_gpkg_path()))
            candidates = [
                settings.value("magoh_spell/active_gpkg", "", type=str) or "",
                settings.value("magoh_spell/linked_gpkg", "", type=str) or "",
            ]
            for raw in candidates:
                if not raw:
                    continue
                try:
                    current = os.path.normcase(os.path.abspath(raw))
                except Exception:
                    current = raw
                if current == legacy:
                    if os.path.exists(raw):
                        self.add_recent_gpkg(raw)
                        settings.setValue("magoh_spell/legacy_gpkg_preserved", raw)
                    settings.setValue("magoh_spell/active_gpkg", "")
                    settings.setValue("magoh_spell/linked_gpkg", "")
                    break
        finally:
            settings.setValue("magoh_spell/v18_legacy_state_migrated", True)


    def get_active_gpkg(self) -> str:
        """Ritorna SOLO la sorgente scelta esplicitamente dall'utente.

        Nessun fallback al GeoPackage nascosto nel profilo QGIS: è il punto
        chiave che impedisce la ricomparsa di geometrie di vecchie sessioni.
        """
        s = QSettings()
        path = s.value("magoh_spell/active_gpkg", "", type=str) or ""
        if not path:
            # compatibilità con una sorgente esterna scelta nelle build vecchie
            path = s.value("magoh_spell/linked_gpkg", "", type=str) or ""
        if path and os.path.exists(path) and path.lower().endswith(".gpkg"):
            return os.path.abspath(path)
        return ""

    def _bind_project_workspace(self, path):
        project = QgsProject.instance()
        encoded = project.writePath(path) if path else ''
        old, _ = project.readEntry('MAGOHSpell', 'workspaceGpkg', '')
        if old != encoded:
            project.writeEntry('MAGOHSpell', 'workspaceGpkg', encoded)

    def _save_project_workspace(self, document=None):
        path = self.get_active_gpkg()
        self._bind_project_workspace(path)
        # writeProject is emitted after properties have been serialized. Update
        # this entry in the DOM too, so Save As preserves relative paths.
        if document is not None:
            node = (document.documentElement().firstChildElement('properties')
                    .firstChildElement('MAGOHSpell').firstChildElement('workspaceGpkg'))
            if not node.isNull():
                text = QgsProject.instance().writePath(path) if path else ''
                if node.firstChild().isNull():
                    node.appendChild(document.createTextNode(text))
                else:
                    node.firstChild().setNodeValue(text)

    def _clear_project_workspace(self, *_args):
        self._cascade_guard.dispose()
        api = getattr(self, '_api_dlg', None)
        if api is not None:
            api._cancel_import()
        settings = QSettings()
        for key in ('active_gpkg', 'linked_gpkg', 'project_root'):
            settings.setValue('magoh_spell/' + key, '')
        self._refresh_open_ui_status()

    def _restore_project_workspace(self, *_args):
        project = QgsProject.instance()
        stored, _ = project.readEntry('MAGOHSpell', 'workspaceGpkg', '')
        candidates = set()
        if stored:
            path = project.readPath(stored)
            if self._validate_magoh_gpkg(path)[0]:
                candidates.add(os.path.abspath(path))
        if not candidates:
            for layer in project.mapLayers().values():
                if isinstance(layer, QgsVectorLayer):
                    path, table = self._split_gpkg_source(layer.source())
                    if table in self.CORE_MAGOH_TABLES and self._validate_magoh_gpkg(path)[0]:
                        candidates.add(os.path.abspath(path))
        settings = QSettings()
        path = next(iter(candidates)) if len(candidates) == 1 else ''
        for key in ('active_gpkg', 'linked_gpkg'):
            settings.setValue('magoh_spell/' + key, path)
        settings.setValue('magoh_spell/project_root', self._infer_project_root(path) if path else '')
        if path:
            self._post_load_repair(path)
        self._refresh_open_ui_status()

    def set_active_gpkg(self, path: str):
        """Imposta o scollega esplicitamente la sorgente dati attiva."""
        path = (path or "").strip()
        if path:
            path = os.path.abspath(path)
            self.add_recent_gpkg(path)
            root = self._infer_project_root(path)
            QSettings().setValue("magoh_spell/project_root", root or '')
        else:
            QSettings().setValue("magoh_spell/project_root", "")
        QSettings().setValue("magoh_spell/active_gpkg", path)
        # compatibilità con le build precedenti
        QSettings().setValue("magoh_spell/linked_gpkg", path)
        self._bind_project_workspace(path)
        self._refresh_open_ui_status()

    CORE_MAGOH_TABLES = (
        "magoh_scheda_intervento",
        "magoh_ritrovamenti",
        "magoh_evidenza_archeologica",
        "magoh_evidenza_archeologica_no_geom",
    )

    def _validate_magoh_gpkg(self, gpkg_path: str):
        """Verifica minima e non invasiva della struttura MAGOH."""
        if not gpkg_path or not os.path.exists(gpkg_path):
            return False, list(self.CORE_MAGOH_TABLES)
        conn = None
        try:
            conn = sqlite3.connect(gpkg_path)
            existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in self.CORE_MAGOH_TABLES if t not in existing]
            return not missing, missing
        except Exception:
            return False, list(self.CORE_MAGOH_TABLES)
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    @staticmethod
    def _safe_project_name(name: str):
        name = re.sub(r'[<>:"/\\|?*]+', "_", str(name or "")).strip().strip(".")
        name = re.sub(r"\s+", " ", name)
        return name[:120]

    def _infer_project_root(self, gpkg_path: str):
        """Individua la cartella progetto senza imporre una struttura ai GPKG esterni."""
        if not gpkg_path:
            return ""
        gpkg_path = os.path.abspath(gpkg_path)
        parent = os.path.dirname(gpkg_path)
        if os.path.basename(parent).lower() == "data":
            candidate = os.path.dirname(parent)
            if os.path.exists(os.path.join(candidate, "MAGOH_project.json")):
                return candidate
            # Compatibilità con workspace MAGOH legacy: la struttura standard
            # data/ + exports/ + attachments/ + backups/ identifica la root
            # anche se il manifest non è ancora presente.
            standard_dirs = ("exports", "attachments", "backups")
            if all(os.path.isdir(os.path.join(candidate, d)) for d in standard_dirs):
                return candidate
        # per un GeoPackage esterno generico non inventiamo sottocartelle nella
        # sua directory: il root resta vuoto e gli export continuano a chiedere
        # esplicitamente la destinazione.
        stored = QSettings().value("magoh_spell/project_root", "", type=str) or ""
        if stored and os.path.isdir(stored):
            manifest = os.path.join(stored, "MAGOH_project.json")
            if os.path.exists(manifest):
                try:
                    with open(manifest, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    rel = meta.get("gpkg", "") or meta.get("geopackage", "")
                    if rel and os.path.normcase(os.path.abspath(os.path.join(stored, rel))) == os.path.normcase(gpkg_path):
                        return stored
                except Exception:
                    pass
        return ""

    def get_project_root(self):
        gpkg = self.get_active_gpkg()
        return self._infer_project_root(gpkg) if gpkg else ""

    def default_export_dir(self, kind):
        """Cartella output predefinita del progetto MAGOH attivo.

        Le cartelle vengono create solo dentro un workspace MAGOH riconosciuto.
        Nessuna directory viene inventata accanto a GeoPackage esterni generici.
        V1.23: restano solo le uscite in uso (pdf, gna, mopr); per ogni altro
        tipo non viene creata alcuna cartella.
        """
        root = self.get_project_root()
        if not root:
            return ""
        kind_l = str(kind or "").lower()
        if kind_l == 'mopr':
            path = os.path.join(root, 'data', 'mopr')
            os.makedirs(path, exist_ok=True)
            return path
        if kind_l not in ('pdf', 'gna'):
            return ""
        path = os.path.join(root, "exports", kind_l)
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            return ""
        return path

    def _write_project_manifest(self, root: str, project_name: str, gpkg_path: str, attached=False):
        """Scrive il manifest della cartella-progetto MAGOH.

        Il GeoPackage resta il database operativo, ma l'unità aperta
        dall'utente è la directory di progetto. Le chiavi aggiuntive sono
        retrocompatibili con i manifest v18/v19 che contenevano solo ``gpkg``.
        """
        rel = os.path.relpath(gpkg_path, root).replace("\\", "/")
        qgz_name = f"{project_name}.qgz"
        meta = {
            "format": "MAGOH Spell project",
            "format_version": 2,
            "name": project_name,
            "gpkg": rel,
            "geopackage": rel,
            "qgis_project": '' if attached else qgz_name,
            "mode": 'attached' if attached else 'standalone',
            "exports_pdf": "exports/pdf",
            "exports_gna": "exports/gna",
            "working_mopr": "data/mopr",
            "attachments": "attachments",
            "backups": "backups",
            "created": QDateTime.currentDateTime().toString(Qt.DateFormat.ISODate),
            "plugin_version": PLUGIN_VERSION,
        }
        with open(os.path.join(root, "MAGOH_project.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def _read_project_manifest(self, root: str):
        path = os.path.join(root, "MAGOH_project.json")
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            _log(f"Manifest MAGOH non leggibile: {e}", Qgis.Warning)
            return {}

    def _resolve_project_workspace(self, root: str):
        """Risoluzione deterministica di una cartella progetto MAGOH.

        L'utente sceglie SOLO la directory radice. Il plugin cerca il database
        in ``data/`` e il progetto QGIS nella root. Prima usa il manifest; per
        i workspace v18/v19 senza chiavi nuove applica una compatibilità sicura.
        Non seleziona mai silenziosamente tra più GeoPackage MAGOH validi.
        """
        if not root or not os.path.isdir(root):
            return None, "La directory selezionata non esiste."
        root = os.path.abspath(root)
        meta = self._read_project_manifest(root)

        gpkg = ""
        rel = meta.get("gpkg", "") or meta.get("geopackage", "")
        if rel:
            candidate = rel if os.path.isabs(rel) else os.path.join(root, rel)
            if os.path.isfile(candidate):
                gpkg = os.path.abspath(candidate)

        if not gpkg:
            data_dir = os.path.join(root, "data")
            if not os.path.isdir(data_dir):
                return None, "Manca la cartella obbligatoria 'data'."
            valid = []
            for name in sorted(os.listdir(data_dir)):
                if not name.lower().endswith(".gpkg"):
                    continue
                candidate = os.path.join(data_dir, name)
                ok, _missing = self._validate_magoh_gpkg(candidate)
                if ok:
                    valid.append(os.path.abspath(candidate))
            if not valid:
                return None, "Nella cartella 'data' non è stato trovato alcun GeoPackage con struttura MAGOH."
            if len(valid) > 1:
                return None, (
                    "Nella cartella 'data' sono presenti più GeoPackage MAGOH validi. "
                    "Un progetto standard deve avere un solo database operativo."
                )
            gpkg = valid[0]

        ok_schema, missing = self._validate_magoh_gpkg(gpkg)
        if not ok_schema:
            return None, "Il GeoPackage del progetto è incompleto. Mancano: " + ", ".join(missing)

        qgz = ""
        qrel = meta.get("qgis_project", "")
        if qrel:
            candidate = qrel if os.path.isabs(qrel) else os.path.join(root, qrel)
            if os.path.isfile(candidate):
                qgz = os.path.abspath(candidate)
        if not qgz:
            root_name = os.path.basename(root.rstrip(os.sep))
            preferred = os.path.join(root, f"{root_name}.qgz")
            if os.path.isfile(preferred):
                qgz = os.path.abspath(preferred)
            else:
                qgzs = sorted(
                    os.path.join(root, n) for n in os.listdir(root)
                    if n.lower().endswith((".qgz", ".qgs")) and os.path.isfile(os.path.join(root, n))
                )
                if len(qgzs) == 1:
                    qgz = os.path.abspath(qgzs[0])

        missing_dirs = [
            d for d in ("exports", "attachments", "backups")
            if not os.path.isdir(os.path.join(root, d))
        ]
        return {
            "root": root,
            "gpkg": gpkg,
            "qgis_project": qgz,
            "manifest": meta,
            "missing_dirs": missing_dirs,
        }, ""

    def _clean_operational_tables(self, gpkg_path: str):
        """Garantisce che un NUOVO progetto parta senza record/geometrie operative."""
        conn = None
        try:
            conn = sqlite3.connect(gpkg_path)
            cur = conn.cursor()
            existing = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            cur.execute("PRAGMA foreign_keys=OFF")
            cur.execute("BEGIN")
            for table in self.CORE_MAGOH_TABLES:
                if table in existing:
                    cur.execute(f'DELETE FROM "{table}"')
            if "sqlite_sequence" in existing:
                placeholders = ",".join("?" for _ in self.CORE_MAGOH_TABLES)
                cur.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", self.CORE_MAGOH_TABLES)
            conn.commit()
        except Exception:
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _normalize_embedded_projects_in_gpkg(self, gpkg_path: str):
        """Rende portabili anche i progetti salvati DENTRO il nuovo GeoPackage.

        È chiamato solo sul file appena creato dal template, quindi non altera
        GeoPackage esterni dell'utente.
        """
        conn = None
        try:
            conn = sqlite3.connect(gpkg_path)
            cur = conn.cursor()
            try:
                rows = cur.execute("SELECT name, content FROM qgis_projects").fetchall()
            except sqlite3.Error:
                return 0
            changed = 0
            for name, raw in rows:
                data = self._decode_project_blob(raw)
                if not data:
                    continue
                patched = self._normalize_project_bytes_for_gpkg(data, gpkg_path)
                if patched == data:
                    continue
                # Il template MAGOH usa stringhe HEX. Conserviamo il formato
                # originale quando possibile per massima compatibilità QGIS.
                if isinstance(raw, str):
                    stored = binascii.hexlify(patched).decode("ascii")
                elif isinstance(raw, (bytes, bytearray, memoryview)):
                    raw_b = bytes(raw)
                    try:
                        binascii.unhexlify(raw_b)
                        stored = binascii.hexlify(patched)
                    except Exception:
                        stored = patched
                else:
                    stored = patched
                cur.execute("UPDATE qgis_projects SET content=? WHERE name=?", (stored, name))
                changed += 1
            conn.commit()
            return changed
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def edit_code_prefix(self):
        path = self.get_active_gpkg()
        if not path or not os.path.isfile(path):
            return
        try:
            value, ok = QInputDialog.getText(self.iface.mainWindow(), 'MAGOH Spell',
                'Prefisso per le nuove schede (2–5 lettere)',text=local_codes.project_prefix(path))
            if ok:
                local_codes.project_prefix(path,value.strip().upper())
        except (ValueError,sqlite3.Error) as exc:
            QMessageBox.warning(self.iface.mainWindow(),'MAGOH Spell',str(exc))

    def compare_local_codes(self):
        path = self.get_active_gpkg()
        if not path or not os.path.isfile(path):
            return
        other,_ = QFileDialog.getOpenFileName(self.iface.mainWindow(),'Copia offline da confrontare','','GeoPackage (*.gpkg)')
        if other:
            try:
                conflicts = local_codes.collisions(path,other)
                result = ('Codici assegnati a schede diverse: ' + ', '.join(conflicts)
                          if conflicts else 'Nessun conflitto di codice fra i due registri.')
                QMessageBox.information(self.iface.mainWindow(),'MAGOH Spell',result)
            except sqlite3.Error as exc:
                QMessageBox.warning(self.iface.mainWindow(),'MAGOH Spell',str(exc))

    def _create_archive_files(self, project_name, parent_dir, attached=False, prefix=None):
        """Reserve a new owned directory, never overwrite an existing archive."""
        root = os.path.join(parent_dir, project_name)
        os.mkdir(root)
        for rel in ('data', 'data/api_cache', 'data/mopr', 'exports/pdf', 'exports/gna',
                    'attachments', 'backups'):
            os.makedirs(os.path.join(root, rel), exist_ok=True)
        gpkg = os.path.join(root, 'data', re.sub(r'\s+', '_', project_name) + '.gpkg')
        shutil.copy2(self._packaged_gpkg_path(), gpkg)
        self._clean_operational_tables(gpkg)
        self._normalize_embedded_projects_in_gpkg(gpkg)
        local_codes.install(gpkg, prefix or local_codes.suggest_prefix(project_name))
        self._write_project_manifest(root, project_name, gpkg, attached)
        return root, gpkg

    def add_archive_to_current_project(self, _checked=False, mode=None):
        project = QgsProject.instance()
        if not (project.fileName() or project.mapLayers() or project.isDirty()):
            QMessageBox.information(self.iface.mainWindow(), 'MAGOH Spell', _tf(
                'Apri prima il progetto QGIS a cui aggiungere il nuovo archivio MAGOH.',
                'Open the QGIS project to which you want to add the new MAGOH archive first.'))
            return
        reserved = {row[0] for row in self.MAGOH_RELATIONS}
        if reserved.intersection(project.relationManager().relations()) or any(
                isinstance(layer, QgsVectorLayer) and self._split_gpkg_source(layer.source())[1] in self.CORE_MAGOH_TABLES
                for layer in project.mapLayers().values()):
            QMessageBox.information(self.iface.mainWindow(), 'MAGOH Spell', _tf(
                'Il progetto contiene già un archivio o relazioni MAGOH. Usa l’archivio presente oppure un altro progetto QGIS.',
                'The project already contains a MAGOH archive or relations. Use the existing archive or another QGIS project.'))
            return
        if mode is None:
            choice=AttachArchiveDialog(self)
            fit_dialog_to_parent(choice,self.iface.mainWindow())
            if choice.exec()!=QDialog.DialogCode.Accepted:return
            mode=choice.mode
        workspace=None
        if mode=='existing':
            folder=QFileDialog.getExistingDirectory(self.iface.mainWindow(),_tf(
                'Collega archivio MAGOH esistente — scegli la cartella','Connect existing MAGOH archive — choose its folder'),self.get_project_root() or '')
            if not folder:return
            workspace,error=self._resolve_project_workspace(folder)
            if not workspace:
                QMessageBox.warning(self.iface.mainWindow(),'MAGOH Spell',error)
                return
        elif mode=='new':
            dialog = NewProjectDialog(self, attach=True)
            fit_dialog_to_parent(dialog, self.iface.mainWindow())
            if dialog.exec() != QDialog.DialogCode.Accepted:return
            name = self._safe_project_name(dialog.project_name())
            parent = dialog.parent_dir().strip()
            if not name or not os.path.isdir(parent):
                QMessageBox.warning(self.iface.mainWindow(), 'MAGOH Spell', _tf(
                    'Inserisci un nome e una directory contenitore validi.', 'Enter a valid name and parent directory.'))
                return
        else:
            return
        old_path = self.get_active_gpkg()
        old_ids = set(project.mapLayers())
        old_extent = QgsRectangle(self.iface.mapCanvas().extent())
        old_crs = project.crs()
        old_layer = self.iface.activeLayer()
        old_dirty = project.isDirty()
        old_groups = {group for group in project.layerTreeRoot().findGroups(True)}
        try:
            if workspace:
                root,gpkg=workspace['root'],workspace['gpkg']
            else:
                root, gpkg = self._create_archive_files(name, parent, attached=True, prefix=dialog.prefix_edit.text().upper())
            self.set_active_gpkg(gpkg)
            self.add_layers()
            if not set(self.CORE_MAGOH_TABLES).issubset(self._layers_for_gpkg(gpkg)):
                raise RuntimeError(_tf('Caricamento dei layer MAGOH incompleto.', 'Incomplete MAGOH layer loading.'))
            project.setDirty(True)
            self.iface.messageBar().pushSuccess('MAGOH Spell', _tf(
                'Archivio MAGOH collegato. Cartella: {root}', 'MAGOH archive connected. Folder: {root}', root=root))
        except Exception as exc:
            project.removeMapLayers(list(set(project.mapLayers()) - old_ids))
            for rid in reserved:
                project.relationManager().removeRelation(rid)
            # Delete only newly-created empty wrappers, never host groups.
            for group in reversed(project.layerTreeRoot().findGroups(True)):
                if group not in old_groups and not group.children():
                    group.parent().removeChildNode(group)
            self.set_active_gpkg(old_path)
            project.setDirty(old_dirty)
            QMessageBox.critical(self.iface.mainWindow(), 'MAGOH Spell', str(exc))
        finally:
            project.setCrs(old_crs)
            self.iface.mapCanvas().setExtent(old_extent)
            self.iface.setActiveLayer(old_layer)
            self.iface.mapCanvas().refresh()
            self._refresh_open_ui_status()

    def create_new_project(self):
        """Crea un progetto MAGOH autonomo in una directory scelta dall'utente."""
        dlg = NewProjectDialog(self)
        fit_dialog_to_parent(dlg, self.iface.mainWindow())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        project_name = self._safe_project_name(dlg.project_name())
        parent_dir = dlg.parent_dir().strip()
        if not project_name:
            QMessageBox.warning(self.iface.mainWindow(), "MAGOH – Nuovo progetto", "Inserisci un nome progetto valido.")
            return
        if not parent_dir or not os.path.isdir(parent_dir):
            QMessageBox.warning(self.iface.mainWindow(), "MAGOH – Nuovo progetto", "Seleziona una directory contenitore valida.")
            return

        project_root = os.path.join(parent_dir, project_name)
        if os.path.exists(project_root):
            QMessageBox.warning(
                self.iface.mainWindow(), "MAGOH – Nuovo progetto",
                f"La cartella esiste già e non è vuota:\n{project_root}\n\nScegli un altro nome o un'altra directory."
            )
            return

        if dlg.open_after_create() and not self._warn_unsaved():
            return

        try:
            project_root, gpkg_path = self._create_archive_files(project_name, parent_dir, prefix=dlg.prefix_edit.text().upper())
            self.set_active_gpkg(gpkg_path)
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "MAGOH – Nuovo progetto", f"Creazione non riuscita:\n\n{e}")
            return

        # Scrive anche un .qgz persistente nella root del progetto: l'utente può
        # riaprirlo direttamente da QGIS senza dipendere dal plugin.
        project_path = ""
        try:
            pname = self._resolve_project_name(gpkg_path)
            if pname:
                qgz = self._extract_qgz_from_gpkg(gpkg_path, pname)
                qgz = self._normalize_project_bytes_for_gpkg(qgz, gpkg_path) if qgz else None
                if qgz:
                    project_path = self._write_project_files(project_root, qgz, project_name)
        except Exception as e:
            _log(f"File progetto persistente non creato: {e}", Qgis.Warning)

        if dlg.open_after_create():
            try:
                ok = QgsProject.instance().read(project_path) if project_path else False
                if not ok:
                    self.set_active_gpkg(gpkg_path)
                    self.add_layers()
                else:
                    self.set_active_gpkg(gpkg_path)
                    self._post_load_repair(gpkg_path)
            except Exception as e:
                QMessageBox.warning(self.iface.mainWindow(), "MAGOH – Nuovo progetto", f"Progetto creato, ma apertura QGIS non completata:\n\n{e}")

        counts = self._table_counts(gpkg_path, self.CORE_MAGOH_TABLES)
        count_txt = ", ".join(f"{k}: {v}" for k, v in counts.items())
        self.iface.messageBar().pushSuccess("MAGOH Spell", f"Nuovo progetto creato: {project_name}")
        QMessageBox.information(
            self.iface.mainWindow(), "MAGOH – Nuovo progetto",
            f"Progetto creato correttamente.\n\nCartella:\n{project_root}\n\nGeoPackage:\n{gpkg_path}\n\nLayer operativi: {count_txt}"
        )

    def open_existing_magoh_project(self):
        """Apre un progetto MAGOH scegliendo la sola cartella radice.

        Da v20 l'utente non deve più conoscere o cercare il file .gpkg:
        MAGOH_project.json e la struttura standard della directory determinano
        automaticamente database, progetto QGIS e cartelle di output.
        """
        current_root = self.get_project_root()
        start = current_root or (os.path.dirname(current_root) if current_root else "")
        root = QFileDialog.getExistingDirectory(
            self.iface.mainWindow(),
            _tf("Apri progetto MAGOH esistente – scegli la cartella del progetto", "Open existing MAGOH project – choose the project folder"),
            start,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not root:
            return

        workspace, error = self._resolve_project_workspace(root)
        if not workspace:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "MAGOH – Progetto esistente",
                "La cartella selezionata non è un progetto MAGOH valido.\n\n" + error +
                "\n\nStruttura attesa: cartella progetto / data / <progetto>.gpkg."
            )
            return

        if not self._warn_unsaved():
            return

        gpkg = workspace["gpkg"]
        QSettings().setValue("magoh_spell/project_root", workspace["root"])
        self.set_active_gpkg(gpkg)

        try:
            opened = False
            project_path = workspace.get("qgis_project") or ""

            # Prima scelta: il .qgz salvato nella cartella progetto. In questo
            # modo si preservano layout, visibilità e modifiche del progetto
            # effettuate dall'utente nelle sessioni precedenti.
            if project_path and os.path.isfile(project_path):
                try:
                    opened = bool(QgsProject.instance().read(project_path))
                    if not opened:
                        QgsProject.instance().clear()
                except Exception as e:
                    _log(f"Progetto QGIS della workspace non aperto: {e}", Qgis.Warning)
                    opened = False

            # Fallback: progetto incorporato nel GeoPackage.
            if not opened:
                pname = self._resolve_project_name(gpkg)
                if pname:
                    qgz = self._extract_qgz_from_gpkg(gpkg, pname)
                    if not qgz:
                        raise RuntimeError(_tf("Progetto QGIS incorporato non leggibile", 'Embedded QGIS project unreadable'))
                    qgz = self._normalize_project_bytes_for_gpkg(qgz, gpkg)
                    project_name = (workspace.get("manifest") or {}).get("name") or os.path.basename(workspace["root"])
                    project_path = self._write_project_files(workspace["root"], qgz, project_name)
                    opened = bool(QgsProject.instance().read(project_path))
                    if not opened:
                        raise RuntimeError(_tf("QGIS non è riuscito ad aprire il progetto incorporato", 'QGIS could not open the embedded project'))

            if opened:
                self.set_active_gpkg(gpkg)
                self._post_load_repair(gpkg)
            else:
                self.add_layers()

            self._zoom_to_active_project_data(gpkg)
            self._refresh_open_ui_status()
            name = (workspace.get("manifest") or {}).get("name") or os.path.basename(workspace["root"])
            self.iface.messageBar().pushSuccess("MAGOH Spell", f"Progetto aperto: {name}")

            if workspace.get("missing_dirs"):
                _log(
                    "Workspace legacy: cartelle opzionali mancanti: " + ", ".join(workspace["missing_dirs"]),
                    Qgis.Warning,
                )
        except Exception as e:
            QMessageBox.critical(self.iface.mainWindow(), "MAGOH – Progetto esistente", f"Apertura non riuscita:\n\n{e}")

    def create_backup(self, _checked=False, choose_destination=False):
        """Backup manuale del solo GeoPackage attivo, mai ad ogni edit."""
        gpkg = self.get_active_gpkg()
        if not gpkg or not os.path.exists(gpkg):
            QMessageBox.warning(self.iface.mainWindow(), "MAGOH – Backup", "Nessun GeoPackage attivo.")
            return
        modified = []
        try:
            for lyr in self._layers_for_gpkg(gpkg).values():
                if lyr.isEditable() and lyr.isModified():
                    modified.append(lyr.name())
        except Exception:
            pass
        if modified:
            QMessageBox.warning(
                self.iface.mainWindow(), "MAGOH – Backup",
                "Prima del backup salva o annulla le modifiche in corso nei layer:\n\n" + "\n".join(modified)
            )
            return
        root = self._infer_project_root(gpkg)
        if root and not choose_destination:
            backup_dir = os.path.join(root, "backups")
            os.makedirs(backup_dir, exist_ok=True)
        else:
            backup_dir = QFileDialog.getExistingDirectory(
                self.iface.mainWindow(), "Scegli cartella backup", os.path.join(root, 'backups') if root else os.path.dirname(gpkg)
            )
            if not backup_dir:
                return
        stamp = QDateTime.currentDateTime().toString("yyyyMMdd_HHmmss_zzz")
        base = os.path.splitext(os.path.basename(gpkg))[0]
        out = None
        try:
            # Exclusive unique destination; SQLite's snapshot includes committed
            # WAL transactions, unlike a plain filesystem copy of the .gpkg.
            with tempfile.NamedTemporaryFile(
                prefix=f"{base}_backup_{stamp}_", suffix=".gpkg", dir=backup_dir, delete=False
            ) as reserved:
                out = reserved.name
            source = sqlite3.connect(Path(gpkg).resolve().as_uri() + "?mode=ro", uri=True)
            try:
                destination = sqlite3.connect(out)
                try:
                    source.backup(destination)
                    if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError(_tf("Verifica di integrita del backup non riuscita.", 'Backup integrity check failed.'))
                finally:
                    destination.close()
            finally:
                source.close()
            self.iface.messageBar().pushSuccess("MAGOH – Backup", f"Backup creato: {out}")
        except Exception as e:
            if out and os.path.isfile(out):
                os.remove(out)  # Only this attempt's incomplete, exclusively created file.
            QMessageBox.critical(self.iface.mainWindow(), "MAGOH – Backup", f"Backup non riuscito:\n\n{e}")

    # compatibilità API: le vecchie finestre chiamano ancora get/set_linked_gpkg
    def get_linked_gpkg(self) -> str:
        return self.get_active_gpkg()

    def set_linked_gpkg(self, path: str):
        self.set_active_gpkg(path)

    @staticmethod
    def _split_gpkg_source(source: str):
        """Normalizza una sorgente OGR GeoPackage in (percorso, layername)."""
        if not source or "|layername=" not in str(source):
            return None, None
        left, right = str(source).split("|layername=", 1)
        layername = right.split("|", 1)[0]
        try:
            path = os.path.normcase(os.path.abspath(left))
        except Exception:
            path = os.path.normcase(left)
        return path, layername

    def _layers_for_gpkg(self, gpkg_path: str):
        """Mappa layername -> QgsVectorLayer per una specifica sorgente GeoPackage."""
        out = {}
        if not gpkg_path:
            return out
        target = os.path.normcase(os.path.abspath(gpkg_path))
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer):
                continue
            pth, lname = self._split_gpkg_source(lyr.source())
            if pth == target and lname and lname not in out:
                out[lname] = lyr
        return out

    def _refresh_open_ui_status(self):
        """Aggiorna solo le finestre già aperte: costo nullo a riposo."""
        gna = getattr(self, '_gna_dialog', None)
        if gna is not None and gna.isVisible():
            try:
                gna.refresh()
            except RuntimeError:
                pass
        try:
            dlg = getattr(self, "_launcher_dlg", None)
            if dlg is not None and dlg.isVisible():
                dlg._refresh_status()
        except Exception:
            pass
        try:
            tools = getattr(self, "tools_dock", None)
            if tools is not None and tools.isVisible():
                tools.relation_panel.refresh_layer_browser()
                tools.filter_panel.reload_layers()
        except Exception:
            pass

    @staticmethod
    def _dependency_layer_for_field(field_name: str, opts: dict):
        """Ricava il layer di thesaurus da una configurazione ValueRelation."""
        name = ""
        try:
            name = (opts.get("LayerName").attrib.get("value") or "").strip()
        except Exception:
            pass
        if name:
            return name
        try:
            src = (opts.get("LayerSource").attrib.get("value") or "")
            if "|layername=" in src:
                return src.split("|layername=", 1)[1].split("|", 1)[0]
        except Exception:
            pass
        # fallback per le configurazioni storiche con Layer/LayerSource vuoti
        mapping = {
            "iv_livello": "thesaurus_iv_livello",
            "id_iv_livello": "thesaurus_iv_livello",
            "cronologia": "thesaurus_periodo",
            "cronologia_iniziale": "thesaurus_periodo",
            "cronologia_finale": "thesaurus_periodo",
            "affidabilita": "thesaurus_affidabilità",
            "id_affidabilita": "thesaurus_affidabilità",
            "id_i_livello": "I_livelli_new",
            "id_ii_livello": "II_livelli_new",
            "id_iii_livello": "dataset_completo",
            "crono_iniziale": "thesaurus_crono",
            "crono_finale": "thesaurus_crono",
            "range_inizio": "thesaurus_crono",
            "range_iniziale": "thesaurus_crono",
            "range_finale": "thesaurus_crono",
        }
        return mapping.get(str(field_name or ""), "")

    @staticmethod
    def _set_xml_option(map_node, opts: dict, name: str, value: str, opt_type="QString"):
        node = opts.get(name)
        if node is None and map_node is not None:
            node = ET.SubElement(map_node, "Option")
            node.set("name", name)
            opts[name] = node
        if node is not None:
            node.set("type", opt_type)
            node.set("value", str(value))

    def _patch_value_relation_xml(self, root, gpkg_path: str, layer_id_by_name: dict):
        """Rende portabili i ValueRelation eliminando vecchi path/Layer ID assoluti."""
        available = set(self._layer_names_from_gpkg(gpkg_path))
        gpkg_abs = os.path.abspath(gpkg_path).replace("\\", "/")
        changed = 0
        for field in root.findall('.//fieldConfiguration/field'):
            ew = field.find('editWidget')
            if ew is None or ew.attrib.get('type') != 'ValueRelation':
                continue
            all_opts = [o for o in ew.findall('.//Option') if o.attrib.get('name')]
            opts = {o.attrib.get('name'): o for o in all_opts}
            map_node = None
            for o in ew.findall('.//Option'):
                if o.attrib.get('type') == 'Map':
                    map_node = o
                    break
            dep = self._dependency_layer_for_field(field.attrib.get('name', ''), opts)
            if not dep or dep not in available or dep not in layer_id_by_name:
                continue
            self._set_xml_option(map_node, opts, 'Layer', layer_id_by_name[dep])
            self._set_xml_option(map_node, opts, 'LayerName', dep)
            self._set_xml_option(map_node, opts, 'LayerSource', f"{gpkg_abs}|layername={dep}")
            self._set_xml_option(map_node, opts, 'LayerProviderName', 'ogr')
            # Configurazione storicamente incompleta del campo id_iv_livello
            if field.attrib.get('name') == 'id_iv_livello':
                if not (opts.get('Key') is not None and opts['Key'].attrib.get('value')):
                    self._set_xml_option(map_node, opts, 'Key', 'id')
                if not (opts.get('Value') is not None and opts['Value'].attrib.get('value')):
                    self._set_xml_option(map_node, opts, 'Value', 'nome')

            # MAGOH/PostGIS salva crono_iniziale/crono_finale come ID della
            # voce di thesaurus_crono. Alcuni vecchi QML del template usavano
            # invece name_full come Key: i valori importati (es. 23, 80)
            # apparivano quindi come ``(23)``/``(80)`` pur essendo corretti.
            # Standardizziamo il modello offline sullo stesso schema PostGIS.
            if field.attrib.get('name') in ('crono_iniziale', 'crono_finale') and dep == 'thesaurus_crono':
                self._set_xml_option(map_node, opts, 'Key', 'id')
                self._set_xml_option(map_node, opts, 'Value', 'name_full')
                self._set_xml_option(map_node, opts, 'Group', 'id_periodo')
            changed += 1
        return changed

    MAGOH_DISPLAY_EXPRESSIONS = {
        "magoh_scheda_intervento": 'to_string("idscheda") || CASE WHEN coalesce("ubicazione", \'\') <> \'\' THEN \' – \' || "ubicazione" ELSE \'\' END',
        "magoh_ritrovamenti": 'to_string("id") || CASE WHEN coalesce("iii_livello", \'\') <> \'\' THEN \' – \' || "iii_livello" ELSE \'\' END',
        "magoh_evidenza_archeologica": 'to_string("id") || CASE WHEN coalesce("iv_livello", \'\') <> \'\' THEN \' – \' || "iv_livello" ELSE \'\' END',
        "magoh_evidenza_archeologica_no_geom": 'to_string("id") || CASE WHEN coalesce("iv_livello", \'\') <> \'\' THEN \' – \' || "iv_livello" ELSE \'\' END',
    }

    def _maplayer_nodes(self, root):
        """Restituisce i nodi maplayer senza duplicarli, includendo un QML root."""
        nodes = []
        if getattr(root, 'tag', None) == 'maplayer':
            nodes.append(root)
        nodes.extend(root.findall('.//projectlayers/maplayer'))
        if not nodes:
            nodes.extend(root.findall('.//maplayer'))
        # preserva ordine, elimina eventuali duplicati dello stesso elemento
        out, seen = [], set()
        for node in nodes:
            ident = id(node)
            if ident not in seen:
                out.append(node)
                seen.add(ident)
        return out

    def _patch_magoh_display_expressions_xml(self, root, layer_name_hint=None):
        """Corregge le espressioni usate da RelationReference.

        Il template storico usava ad es. ``id || ' - ' || ubicazione``: se
        ``ubicazione`` era NULL, l'intera etichetta diventava NULL e il record
        sembrava scollegato anche quando la foreign key era corretta. Inoltre il
        ritrovamento usava un campo ``iii_livello_txt`` non presente nel GPKG.
        """
        changed = 0
        nodes = self._maplayer_nodes(root)
        contexts = []
        if nodes:
            for ml in nodes:
                name_el = ml.find('layername')
                name = (name_el.text or '').strip() if name_el is not None else ''
                contexts.append((ml, name))
        elif layer_name_hint:
            # I QML salvati in layer_styles hanno root <qgis> e non contengono
            # necessariamente <layername>. Il nome arriva dalla colonna
            # f_table_name della stessa riga layer_styles.
            contexts.append((root, str(layer_name_hint)))

        for ml, name in contexts:
            expr = self.MAGOH_DISPLAY_EXPRESSIONS.get(name)
            if not expr:
                continue
            prev = ml.find('previewExpression')
            if prev is None:
                prev = ET.SubElement(ml, 'previewExpression')
            if (prev.text or '') != expr:
                prev.text = expr
                changed += 1
        return changed

    RELATION_REFERENCE_BY_FIELD = {
        ('magoh_ritrovamenti', 'idscheda'): 'magoh_ritr_idscheda_magoh_sche_idscheda',
        ('magoh_evidenza_archeologica', 'idiii_livello'): 'magoh_evid_idiii_livello_magoh_ritr_id',
        ('magoh_evidenza_archeologica', 'idintervento'): 'magoh_evid_idintervento_magoh_sche_idscheda',
        ('magoh_evidenza_archeologica_no_geom', 'idiii_livello'): 'magoh_evid_idiii_livello_magoh_ritr_id_1',
        ('magoh_evidenza_archeologica_no_geom', 'idintervento'): 'magoh_evid_idintervento_magoh_sche_idscheda_1',
    }

    def _patch_relation_reference_xml(self, root, gpkg_path: str, layer_id_by_name: dict, layer_name_hint=None):
        """Riallinea i RelationReference al layer/relazione realmente correnti.

        Non ci fidiamo dell'ID relazione salvato nel vecchio QML: alcune form
        storiche (soprattutto evidenza senza geometria) puntavano alla relazione
        dell'altro layer. La relazione viene quindi determinata da
        ``(layer corrente, campo FK)``.
        """
        gpkg_abs = os.path.abspath(gpkg_path).replace('\\', '/')
        parent_by_rel = {rid: parent for rid, _nm, parent, _pf, _child, _cf in self.MAGOH_RELATIONS}
        changed = 0

        nodes = self._maplayer_nodes(root)
        contexts = []
        if nodes:
            for ml in nodes:
                lname_el = ml.find('layername')
                lname = (lname_el.text or '').strip() if lname_el is not None else ''
                contexts.append((ml, lname))
        else:
            # layer_styles/QML: il nome non è nel documento, ma nella riga DB.
            contexts.append((root, str(layer_name_hint or '')))

        for ml, lname in contexts:
            for field in ml.findall('.//fieldConfiguration/field'):
                ew = field.find('editWidget')
                if ew is None or ew.attrib.get('type') != 'RelationReference':
                    continue
                field_name = (field.attrib.get('name') or '').strip()
                all_opts = [o for o in ew.findall('.//Option') if o.attrib.get('name')]
                opts = {o.attrib.get('name'): o for o in all_opts}
                map_node = next((o for o in ew.findall('.//Option') if o.attrib.get('type') == 'Map'), None)
                if map_node is None:
                    continue

                rid = self.RELATION_REFERENCE_BY_FIELD.get((lname, field_name), '')
                if not rid:
                    try:
                        rid = (opts.get('Relation').attrib.get('value') or '').strip()
                    except Exception:
                        rid = ''
                parent_name = parent_by_rel.get(rid)
                if not parent_name or parent_name not in layer_id_by_name:
                    continue

                self._set_xml_option(map_node, opts, 'Relation', rid)
                self._set_xml_option(map_node, opts, 'ReferencedLayerId', layer_id_by_name[parent_name])
                self._set_xml_option(map_node, opts, 'ReferencedLayerName', parent_name)
                # QGIS 3.40 usa esattamente ReferencedLayerDataSource. La vecchia
                # patch scriveva ReferencedLayerSource e lasciava in vita il path
                # assoluto del progetto di febbraio.
                self._set_xml_option(map_node, opts, 'ReferencedLayerDataSource', f"{gpkg_abs}|layername={parent_name}")
                self._set_xml_option(map_node, opts, 'ReferencedLayerProviderKey', 'ogr')
                self._set_xml_option(map_node, opts, 'ShowOpenFormButton', 'true', 'bool')
                changed += 1
        return changed

    def _patch_qgs_xml_for_gpkg(self, xml_bytes: bytes, gpkg_path: str) -> bytes:
        """Ricollega progetto, form e ValueRelation al GeoPackage scelto.

        Non modifica il GeoPackage dell'utente: la patch riguarda soltanto la
        copia temporanea del progetto estratto prima che QGIS la apra.
        """
        try:
            root = ET.fromstring(xml_bytes)
        except Exception:
            return xml_bytes
        available = set(self._layer_names_from_gpkg(gpkg_path))
        gpkg_abs = os.path.abspath(gpkg_path).replace("\\", "/")
        layer_id_by_name = {}
        for ml in root.findall('.//projectlayers/maplayer'):
            name_el = ml.find('layername')
            id_el = ml.find('id')
            ds_el = ml.find('datasource')
            name = (name_el.text or '').strip() if name_el is not None else ''
            lid = (id_el.text or '').strip() if id_el is not None else ''
            if name and lid:
                layer_id_by_name[name] = lid
            if name in available and ds_el is not None:
                ds_el.text = f"{gpkg_abs}|layername={name}"

        self._patch_value_relation_xml(root, gpkg_path, layer_id_by_name)
        self._patch_relation_reference_xml(root, gpkg_path, layer_id_by_name)
        self._patch_magoh_display_expressions_xml(root)

        # Le sezioni referencedLayers delle form contenevano copie di vecchi
        # percorsi e ID di layer: riallineiamo anche quelle.
        for rel in root.findall('.//referencedLayers/relation'):
            lname = (rel.attrib.get('layerName') or '').strip()
            if lname in available and lname in layer_id_by_name:
                rel.set('layerId', layer_id_by_name[lname])
                rel.set('referencedLayer', layer_id_by_name[lname])
                rel.set('providerKey', 'ogr')
                rel.set('dataSource', f"{gpkg_abs}|layername={lname}")

        try:
            return ET.tostring(root, encoding='utf-8', xml_declaration=True)
        except Exception:
            return xml_bytes

    def _normalize_project_bytes_for_gpkg(self, project_bytes: bytes, gpkg_path: str) -> bytes:
        """Patch in memoria di .qgs/.qgz per renderlo autoconsistente col gpkg attivo."""
        if not project_bytes:
            return project_bytes
        if project_bytes[:2] != b'PK':
            return self._patch_qgs_xml_for_gpkg(project_bytes, gpkg_path)
        src = io.BytesIO(project_bytes)
        dst = io.BytesIO()
        try:
            with zipfile.ZipFile(src, 'r') as zin, zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    data = zin.read(info.filename)
                    if info.filename.lower().endswith('.qgs'):
                        data = self._patch_qgs_xml_for_gpkg(data, gpkg_path)
                    zout.writestr(info, data)
            return dst.getvalue()
        except Exception as e:
            _log(f"Normalizzazione progetto non riuscita: {e}", Qgis.Warning)
            return project_bytes

    def _repair_gpkg_styles_for_loaded_layers(self, gpkg_path: str):
        """Patch della COPIA DI LAVORO dei QML salvati nel gpkg.

        Serve per evitare 'Missing layer form dependency' e l'autocaricamento di
        doppioni quando loadDefaultStyle() incontra vecchi ID/path assoluti.
        """
        loaded = self._layers_for_gpkg(gpkg_path)
        if not loaded:
            return 0
        layer_ids = {n: l.id() for n, l in loaded.items()}
        conn = None
        changed = 0
        try:
            conn = sqlite3.connect(gpkg_path)
            cur = conn.cursor()
            rows = cur.execute("SELECT id, f_table_name, styleQML FROM layer_styles WHERE styleQML IS NOT NULL").fetchall()
            for sid, table_name, qml in rows:
                if not qml:
                    continue
                try:
                    root = ET.fromstring(qml.encode('utf-8') if isinstance(qml, str) else qml)
                except Exception:
                    continue
                n = self._patch_value_relation_xml(root, gpkg_path, layer_ids)
                rr_n = self._patch_relation_reference_xml(root, gpkg_path, layer_ids, layer_name_hint=table_name)
                disp_n = self._patch_magoh_display_expressions_xml(root, layer_name_hint=table_name)
                gpkg_abs = os.path.abspath(gpkg_path).replace("\\", "/")
                rel_n = 0
                for rel in root.findall('.//referencedLayers/relation'):
                    lname = (rel.attrib.get('layerName') or '').strip()
                    if lname in layer_ids:
                        rel.set('layerId', layer_ids[lname])
                        rel.set('referencedLayer', layer_ids[lname])
                        rel.set('providerKey', 'ogr')
                        rel.set('dataSource', f"{gpkg_abs}|layername={lname}")
                        rel_n += 1
                if n or rr_n or disp_n or rel_n:
                    new_qml = ET.tostring(root, encoding='unicode')
                    cur.execute("UPDATE layer_styles SET styleQML=? WHERE id=?", (new_qml, sid))
                    changed += n + rr_n + disp_n + rel_n
            conn.commit()
        except Exception as e:
            _log(f"Riparazione stili GeoPackage non riuscita: {e}", Qgis.Warning)
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
        return changed

    MAGOH_RELATIONS = (
        ('magoh_ritr_idscheda_magoh_sche_idscheda', 'rel_interventi_ritrovamenti',
         'magoh_scheda_intervento', 'idscheda', 'magoh_ritrovamenti', 'idscheda'),
        ('magoh_evid_idiii_livello_magoh_ritr_id', 'rel_ritrovamenti_evidenze',
         'magoh_ritrovamenti', 'id', 'magoh_evidenza_archeologica', 'idiii_livello'),
        ('magoh_evid_idintervento_magoh_sche_idscheda', 'rel_interventi_evidenze',
         'magoh_scheda_intervento', 'idscheda', 'magoh_evidenza_archeologica', 'idintervento'),
        ('magoh_evid_idiii_livello_magoh_ritr_id_1', 'rel_ritrovamento_evidenze_no_geom',
         'magoh_ritrovamenti', 'id', 'magoh_evidenza_archeologica_no_geom', 'idiii_livello'),
        ('magoh_evid_idintervento_magoh_sche_idscheda_1', 'rel_interventi_evidenze_no_geom',
         'magoh_scheda_intervento', 'idscheda', 'magoh_evidenza_archeologica_no_geom', 'idintervento'),
    )

    def _ensure_magoh_relations(self, gpkg_path: str):
        """Ricostruisce le relazioni MAGOH quando i layer sono caricati manualmente."""
        project = QgsProject.instance()
        loaded = self._layers_for_gpkg(gpkg_path)
        manager = project.relationManager()
        made = 0
        for rid, name, parent_name, parent_field, child_name, child_field in self.MAGOH_RELATIONS:
            parent = loaded.get(parent_name)
            child = loaded.get(child_name)
            if not parent or not child:
                continue
            if parent.fields().indexOf(parent_field) < 0 or child.fields().indexOf(child_field) < 0:
                continue
            # Ricreiamo sempre la definizione: è un'operazione molto leggera (max 5
            # relazioni) e impedisce che restino nel progetto oggetti validi solo
            # formalmente ma legati a vecchie copie dei layer o field pair obsoleti.
            try:
                if rid in manager.relations():
                    manager.removeRelation(rid)
            except Exception:
                pass
            try:
                rel = QgsRelation()
                rel.setId(rid)
                rel.setName(name)
                rel.setReferencedLayer(parent.id())
                rel.setReferencingLayer(child.id())
                rel.addFieldPair(child_field, parent_field)
                try:
                    rel.updateRelationStatus()
                except Exception:
                    pass
                if rel.isValid():
                    manager.addRelation(rel)
                    made += 1
                else:
                    try:
                        err = rel.validationError()
                    except Exception:
                        err = "relazione non valida"
                    _log(f"Relazione {rid} non valida: {err}", Qgis.Warning)
            except Exception as e:
                _log(f"Relazione {rid} non ricostruita: {e}", Qgis.Warning)
        return made

    # ------------------------------------------------------------------
    # Regola topologica MAGOH: un record figlio spaziale puo' essere
    # associato a un padre areale solo quando la sua geometria e' interamente
    # contenuta nella geometria del padre. La regola e' volutamente applicata
    # SOLO ai rapporti in cui il layer padre e' poligonale: per una relazione
    # con padre puntuale (Ritrovamento -> Evidenza) il concetto di "dentro"
    # non e' geometricamente definito e la relazione attributiva resta
    # invariata.
    # ------------------------------------------------------------------
    def _spatial_constraint_expression(self, parent_layer, parent_field, child_layer, child_field):
        """Espressione QGIS hard constraint per la relazione child -> parent.

        Usa get_feature() sul layer padre gia' caricato. Il controllo viene
        rivalutato da QGIS quando cambiano attributi o geometria della feature
        figlia, senza listener/polling del plugin.
        """
        parent_ref = str(parent_layer.id()).replace("'", "''")
        pf = str(parent_field).replace("'", "''")
        cf = str(child_field).replace("'", "''")
        child_crs = child_layer.crs().authid() if child_layer.crs().isValid() else ""
        parent_crs = parent_layer.crs().authid() if parent_layer.crs().isValid() else ""
        child_geom = '$geometry'
        if child_crs and parent_crs and child_crs != parent_crs:
            child_geom = f"transform($geometry, '{child_crs}', '{parent_crs}')"
        # NULL resta consentito solo se il modello/form lo consente gia': gli
        # eventuali vincoli NOT NULL originari restano intatti e continuano a
        # governare la nullabilita'. Se la FK e' valorizzata, invece, la
        # geometria deve esistere e stare interamente dentro il padre scelto.
        return (
            f'"{cf}" IS NULL OR ('
            f'coalesce(NOT is_empty($geometry), false) AND '
            f'coalesce(within({child_geom}, geometry(get_feature(\'{parent_ref}\', \'{pf}\', attribute(@feature, \'{cf}\')))), false)'
            f')'
        )

    def _install_spatial_parent_constraints(self, gpkg_path: str):
        """Installa i vincoli spaziali hard sulle FK con padre poligonale.

        Non tocca lo schema del GeoPackage e non aggiunge segnali permanenti:
        usa i constraint nativi di QgsVectorLayer. Eventuali constraint
        espressione preesistenti vengono preservati e combinati con la regola
        MAGOH.
        """
        loaded = self._layers_for_gpkg(gpkg_path)
        applied = 0
        for _rid, _name, parent_name, parent_field, child_name, child_field in self.MAGOH_RELATIONS:
            parent = loaded.get(parent_name)
            child = loaded.get(child_name)
            if not parent or not child or not parent.isSpatial() or not child.isSpatial():
                continue
            if QgsWkbTypes.geometryType(parent.wkbType()) != QgsWkbTypes.PolygonGeometry:
                continue
            idx = child.fields().indexOf(child_field)
            if idx < 0 or parent.fields().indexOf(parent_field) < 0:
                continue
            try:
                expr_key = f"magoh/spatial_parent/original_expr/{child_field}"
                desc_key = f"magoh/spatial_parent/original_desc/{child_field}"
                init_key = f"magoh/spatial_parent/original_saved/{child_field}"
                if not bool(child.customProperty(init_key, False)):
                    child.setCustomProperty(expr_key, child.constraintExpression(idx) or "")
                    child.setCustomProperty(desc_key, child.constraintDescription(idx) or "")
                    child.setCustomProperty(init_key, True)

                original_expr = str(child.customProperty(expr_key, "") or "").strip()
                original_desc = str(child.customProperty(desc_key, "") or "").strip()
                spatial_expr = self._spatial_constraint_expression(
                    parent, parent_field, child, child_field
                )
                final_expr = f"({original_expr}) AND ({spatial_expr})" if original_expr else spatial_expr
                spatial_desc = (
                    f"MAGOH: la geometria deve essere interamente dentro "
                    f"{parent.name()} associato"
                )
                final_desc = f"{original_desc} | {spatial_desc}" if original_desc else spatial_desc
                child.setConstraintExpression(idx, final_expr, final_desc)
                child.setFieldConstraint(
                    idx,
                    QgsFieldConstraints.ConstraintExpression,
                    QgsFieldConstraints.ConstraintStrengthHard,
                )
                applied += 1
            except Exception as e:
                _log(f"Vincolo spaziale {child_name}.{child_field}: {e}", Qgis.Warning)
        return applied

    def _repair_display_expressions(self, gpkg_path: str):
        """Imposta etichette sempre leggibili per i record referenziati."""
        loaded = self._layers_for_gpkg(gpkg_path)
        changed = 0
        for lname, expr in self.MAGOH_DISPLAY_EXPRESSIONS.items():
            lyr = loaded.get(lname)
            if not lyr:
                continue
            if lyr.fields().indexOf('codice_locale') >= 0:
                continue  # il codice leggibile lo gestisce gui/local_identity.install_form
            try:
                if lyr.displayExpression() != expr:
                    lyr.setDisplayExpression(expr)
                    changed += 1
            except Exception as e:
                _log(f"Display expression {lname}: {e}", Qgis.Warning)
        return changed

    def _repair_iii_level_codes(self, gpkg_path: str):
        """Riallinea id_iii_livello al II livello della scheda.

        Gli import API precedenti cercavano il III livello per denominazione
        anche fuori dal II livello: «reperti mobili» di Frequentazione diventava
        il codice di Lavorazione argilla, e la form lo mostrava tra parentesi.
        Si cambia solo il codice, ricavato dal testo iii_livello; mai il testo.
        """
        layer = self._layers_for_gpkg(gpkg_path).get('magoh_ritrovamenti')
        names = ('id_ii_livello', 'id_iii_livello', 'iii_livello')
        if layer is None or layer.isEditable() or any(layer.fields().indexOf(n) < 0 for n in names):
            return 0
        lookups = TemplateLookups.from_gpkg(gpkg_path)
        target = layer.fields().indexOf('id_iii_livello')
        request = QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry) \
            .setSubsetOfAttributes(list(names), layer.fields())
        from .gui.geographic_editor import clean
        changes = {}
        for feature in layer.dataProvider().getFeatures(request):
            code = lookups.consistent_iii_code(*(clean(feature[n]) for n in names))
            if code:
                changes[feature.id()] = {target: code}
        if changes and layer.dataProvider().changeAttributeValues(changes):
            layer.reload()
            _log(f"III livello: {len(changes)} codici riallineati al II livello della scheda.")
        return len(changes)

    def _reinitialize_relation_form_elements(self, gpkg_path: str):
        """Re-inizializza i pannelli Relation Editor dopo la ricostruzione delle relazioni.

        Quando uno stile QML viene caricato prima che il RelationManager contenga
        le relazioni correnti, gli ``attributeEditorRelation`` della form possono
        restare legati a una relazione invalida anche se il manager viene poi
        corretto. QGIS espone ``QgsAttributeEditorRelation.init()`` proprio per
        riallineare questi elementi al RelationManager attuale.
        """
        manager = QgsProject.instance().relationManager()
        loaded = self._layers_for_gpkg(gpkg_path)
        touched = 0

        def walk(element):
            nonlocal touched
            if element is None:
                return
            try:
                if isinstance(element, QgsAttributeEditorRelation):
                    # Usa il Relation Editor MAGOH: stesso widget QGIS, con in più
                    # "Apri scheda figlio" e doppio clic per aprire la form completa.
                    try:
                        element.setRelationWidgetTypeId(MAGOH_RELATION_WIDGET_ID)
                    except Exception:
                        pass
                    if element.init(manager):
                        touched += 1
            except Exception as e:
                _log(f"Init elemento relazione form: {e}", Qgis.Warning)
            try:
                if isinstance(element, QgsAttributeEditorContainer):
                    for child in element.children():
                        walk(child)
                elif hasattr(element, 'children'):
                    # fallback per versioni API con classi SIP differenti
                    for child in element.children():
                        walk(child)
            except Exception:
                pass

        for lyr in loaded.values():
            try:
                cfg = lyr.editFormConfig()
                for tab in cfg.tabs():
                    walk(tab)
                # Reimpostare la config forza QGIS a usare le istanze appena inizializzate.
                lyr.setEditFormConfig(cfg)
            except Exception as e:
                _log(f"Form relation refresh {lyr.name()}: {e}", Qgis.Warning)
        return touched

    def _repair_loaded_widget_dependencies(self, gpkg_path: str):
        """Riallinea ValueRelation/RelationReference agli oggetti layer correnti."""
        loaded = self._layers_for_gpkg(gpkg_path)
        relation_by_field = {
            ('magoh_ritrovamenti', 'idscheda'): 'magoh_ritr_idscheda_magoh_sche_idscheda',
            ('magoh_evidenza_archeologica', 'idiii_livello'): 'magoh_evid_idiii_livello_magoh_ritr_id',
            ('magoh_evidenza_archeologica', 'idintervento'): 'magoh_evid_idintervento_magoh_sche_idscheda',
            ('magoh_evidenza_archeologica_no_geom', 'idiii_livello'): 'magoh_evid_idiii_livello_magoh_ritr_id_1',
            ('magoh_evidenza_archeologica_no_geom', 'idintervento'): 'magoh_evid_idintervento_magoh_sche_idscheda_1',
        }
        fixed = 0
        for lname, lyr in loaded.items():
            for i, fld in enumerate(lyr.fields()):
                try:
                    setup = lyr.editorWidgetSetup(i)
                    typ = setup.type()
                    cfg = dict(setup.config() or {})
                except Exception:
                    continue
                if typ == 'ValueRelation':
                    dep = cfg.get('LayerName') or ''
                    if not dep:
                        src = str(cfg.get('LayerSource') or '')
                        if '|layername=' in src:
                            dep = src.split('|layername=', 1)[1].split('|', 1)[0]
                    if not dep:
                        dep = self._dependency_layer_for_field(fld.name(), {})
                    target = loaded.get(dep)
                    if not target:
                        continue
                    cfg['Layer'] = target.id()
                    cfg['LayerName'] = dep
                    cfg['LayerSource'] = target.source()
                    if fld.name() == 'id_iv_livello':
                        cfg.setdefault('Key', 'id')
                        cfg.setdefault('Value', 'nome')
                    elif fld.name() in ('crono_iniziale', 'crono_finale') and dep == 'thesaurus_crono':
                        # Compatibilità reale con MAGOH/PostGIS: i campi crono_*
                        # contengono l'ID di thesaurus_crono, non name_full.
                        cfg['Key'] = 'id'
                        cfg['Value'] = 'name_full'
                        cfg['Group'] = 'id_periodo'
                        cfg['OrderByValue'] = True
                        if lname == 'magoh_ritrovamenti':
                            period_field = 'cronologia_iniziale' if fld.name() == 'crono_iniziale' else 'cronologia_finale'
                        elif lname in ('magoh_evidenza_archeologica', 'magoh_evidenza_archeologica_no_geom'):
                            period_field = 'cronologia' if fld.name() == 'crono_iniziale' else 'cronologia_finale'
                        else:
                            period_field = None
                        if period_field and lyr.fields().indexOf(period_field) >= 0:
                            cfg['FilterExpression'] = f'"id_periodo" = current_value(\'{period_field}\')'
                    elif fld.name() in ('cronologia', 'cronologia_iniziale', 'cronologia_finale') and dep == 'thesaurus_periodo':
                        # Anche il periodo generale usa lo stesso codice di MAGOH.
                        cfg['Key'] = 'cod_periodo'
                        cfg['Value'] = 'name_full'
                        cfg['Group'] = 'cod_periodo'
                        cfg['OrderByValue'] = True
                    try:
                        lyr.setEditorWidgetSetup(i, QgsEditorWidgetSetup('ValueRelation', cfg))
                        fixed += 1
                    except Exception:
                        pass
                elif typ == 'RelationReference':
                    rid = relation_by_field.get((lname, fld.name()))
                    if rid:
                        rel = QgsProject.instance().relationManager().relations().get(rid)
                        if rel and rel.isValid():
                            parent = QgsProject.instance().mapLayer(rel.referencedLayerId())
                        else:
                            parent = None
                        cfg['Relation'] = rid
                        if parent:
                            cfg['ReferencedLayerId'] = parent.id()
                            cfg['ReferencedLayerName'] = parent.name()
                            cfg['ReferencedLayerDataSource'] = parent.source()
                            cfg['ReferencedLayerProviderKey'] = parent.providerType()
                        cfg['ShowOpenFormButton'] = True
                        try:
                            lyr.setEditorWidgetSetup(i, QgsEditorWidgetSetup('RelationReference', cfg))
                            fixed += 1
                        except Exception:
                            pass
        from .gui.chronology_editor import install as install_chronology_editors
        install_chronology_editors(loaded)
        from .gui.evidence_editor import install as install_evidence_editors
        install_evidence_editors(loaded)
        return fixed

    def _deduplicate_gpkg_layers(self, gpkg_path: str, preferred_ids=None):
        """Rimuove SOLO copie esatte della stessa tabella dello stesso gpkg."""
        preferred_ids = set(preferred_ids or [])
        target = os.path.normcase(os.path.abspath(gpkg_path))
        buckets = {}
        for lyr in list(QgsProject.instance().mapLayers().values()):
            if not isinstance(lyr, QgsVectorLayer):
                continue
            pth, lname = self._split_gpkg_source(lyr.source())
            if pth == target and lname:
                buckets.setdefault(lname, []).append(lyr)
        removed = 0
        for arr in buckets.values():
            if len(arr) < 2:
                continue
            keep = next((x for x in arr if x.id() in preferred_ids), arr[0])
            for lyr in arr:
                if lyr.id() == keep.id():
                    continue
                try:
                    QgsProject.instance().removeMapLayer(lyr.id())
                    removed += 1
                except Exception:
                    pass
        return removed


    @staticmethod
    def _plain_value(value):
        try:
            if value is None or (hasattr(value, 'isNull') and value.isNull()):
                return None
            txt = str(value).strip()
            return None if (not txt or txt.upper() == 'NULL') else txt
        except Exception:
            return None


    def _ensure_performance_indexes(self, gpkg_path: str):
        """Crea indici SQLite non distruttivi sulle chiavi usate più spesso.

        Le prime versioni del template avevano gli RTree geometrici ma nessun
        indice sulle FK/ID attributive. RelationReference, get_feature(), browser
        gerarchico, constraint spaziali e filtro cronologico possono quindi
        costringere SQLite a scansioni complete. Gli indici vengono creati una
        volta per workspace e non alterano record, form o geometrie.
        """
        if not gpkg_path or not os.path.exists(gpkg_path):
            return 0
        specs = {
            "magoh_scheda_intervento": ("id", "idscheda"),
            "magoh_ritrovamenti": ("id", "idscheda", "crono_iniziale", "crono_finale"),
            "magoh_evidenza_archeologica": ("id", "idintervento", "idiii_livello", "crono_iniziale", "crono_finale"),
            "magoh_evidenza_archeologica_no_geom": ("id", "idintervento", "idiii_livello", "crono_iniziale", "crono_finale"),
            "thesaurus_crono": ("id", "id_periodo"),
        }
        created = 0
        con = sqlite3.connect(gpkg_path, timeout=5)
        try:
            existing_tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, fields in specs.items():
                if table not in existing_tables:
                    continue
                cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
                for field in fields:
                    if field not in cols:
                        continue
                    safe_table = re.sub(r'[^A-Za-z0-9_]+', '_', table)
                    safe_field = re.sub(r'[^A-Za-z0-9_]+', '_', field)
                    idx_name = f"idx_magoh_{safe_table}_{safe_field}"
                    before = con.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (idx_name,)
                    ).fetchone()
                    con.execute(
                        f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table}" ("{field}")'
                    )
                    if not before:
                        created += 1
            con.commit()
        finally:
            con.close()
        return created

    def _install_local_temporary_id_defaults(self, gpkg_path: str):
        """Riserva gli ID positivi agli oggetti MAGOH ufficiali.

        I nuovi record creati manualmente nel workspace offline ricevono ID
        negativi. In questo modo un record locale non puo' occupare per errore
        lo stesso identificativo di un oggetto gia' presente sul server MAGOH.
        Gli ID positivi importati via API vengono sempre impostati esplicitamente
        dal writer e non dipendono da questi default.
        """
        loaded = self._layers_for_gpkg(gpkg_path)
        specs = {
            'magoh_scheda_intervento': {
                'idscheda': 'if(coalesce(minimum("idscheda"),0) < 0, minimum("idscheda") - 1, -1)',
                # ``id`` e ``idscheda`` rappresentano lo stesso identificativo
                # nella scheda locale: usiamo la stessa sequenza temporanea.
                'id': 'if(coalesce(minimum("idscheda"),0) < 0, minimum("idscheda") - 1, -1)',
            },
            'magoh_ritrovamenti': {
                'id': 'if(coalesce(minimum("id"),0) < 0, minimum("id") - 1, -1)',
            },
            'magoh_evidenza_archeologica': {
                'id': 'if(coalesce(minimum("id"),0) < 0, minimum("id") - 1, -1)',
            },
            # Il no-geometry e' la seconda rappresentazione della stessa tabella
            # Evidenza. Gli assegniamo un intervallo negativo separato per evitare
            # collisioni fra le due tabelle offline.
            'magoh_evidenza_archeologica_no_geom': {
                'id': 'if(coalesce(minimum("id"),-1000000000) < -1000000000, minimum("id") - 1, -1000000001)',
            },
        }
        changed = 0
        for lname, fields in specs.items():
            layer = loaded.get(lname)
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            cfg = layer.editFormConfig()
            if layer.fields().indexOf('codice_locale') >= 0:
                from .gui.local_identity import technical_default
                expression = technical_default(layer,gpkg_path,lname)
                fields = {local_codes.TABLES[lname]:expression}
                if lname == 'magoh_scheda_intervento':
                    fields['id'] = '"idscheda"'
            for field_name, expression in fields.items():
                idx = layer.fields().indexOf(field_name)
                if idx < 0:
                    continue
                try:
                    layer.setDefaultValueDefinition(idx, QgsDefaultValue(expression, False))
                except Exception:
                    pass
                try:
                    cfg.setReadOnly(idx, True)
                except Exception:
                    pass
                changed += 1
            try:
                layer.setEditFormConfig(cfg)
            except Exception:
                pass
        return changed

    def _post_load_repair(self, gpkg_path: str, preferred_ids=None):
        """Riparazioni leggere dopo apertura/caricamento: relazioni, widget, doppioni."""
        try:
            loaded = self._layers_for_gpkg(gpkg_path)
            if not any(layer.isEditable() for layer in loaded.values()):
                folder = os.path.join(self._infer_project_root(gpkg_path) or os.path.dirname(gpkg_path), 'backups')
                upgraded=ensure_archive_schema(gpkg_path, folder)
                project_root = self._infer_project_root(gpkg_path)
                meta = self._read_project_manifest(project_root) if project_root else {}
                name = (meta or {}).get('name') or Path(gpkg_path).stem
                prefix = local_codes.suggest_prefix(name)
                if len(prefix) < 2:
                    with sqlite3.connect(gpkg_path) as conn:
                        configured = conn.execute("SELECT 1 FROM sqlite_master WHERE name='magoh_code_settings'").fetchone()
                    if not configured:
                        prefix, ok = QInputDialog.getText(self.iface.mainWindow(),'MAGOH Spell','Prefisso dei codici (2–5 lettere)')
                        if not ok:
                            raise ValueError('Impostazione del prefisso annullata.')
                        prefix = prefix.upper()
                code_upgrade = local_codes.install(gpkg_path,prefix,folder)
                if upgraded or code_upgrade:
                    for layer in loaded.values():
                        layer.reload();layer.updateFields()
                ensure_street_schema(gpkg_path, None if upgraded else folder)
                from .geographic_lookups import ensure_toponym_schema
                ensure_toponym_schema(gpkg_path, None if upgraded else folder)
            else:
                with sqlite3.connect(gpkg_path) as db:
                    ready=db.execute("SELECT 1 FROM sqlite_master WHERE name='magoh_spell_comune_insert_v2'").fetchone()
                if not ready:
                    raise RuntimeError('Salva o annulla le modifiche e riapri il progetto per aggiornare il template / Save or discard edits and reopen to upgrade the template.')
            lookup = loaded.get('magoh_comuni')
            if lookup is None:
                lookup = QgsVectorLayer(gpkg_path + '|layername=magoh_comuni', 'magoh_comuni', 'ogr')
                if lookup.isValid():
                    QgsProject.instance().addMapLayer(lookup, False)
                    group = QgsProject.instance().layerTreeRoot().findGroup('MAGOH')
                    group = group or QgsProject.instance().layerTreeRoot().addGroup('MAGOH')
                    group.addLayer(lookup)
            intervention = loaded.get('magoh_scheda_intervento')
            if intervention is not None and lookup is not None and lookup.isValid():
                index = intervention.fields().indexFromName('id_comune')
                if index >= 0:
                    intervention.setFieldAlias(index, 'Comune')
                    # QGIS 3.40 uses its native code-backed completer; newer
                    # runtimes use our explicit ID editor. Both store codice.
                    commune_widget='ValueRelation' if Qgis.QGIS_VERSION_INT<34400 else 'MAGOHMunicipalityId'
                    intervention.setEditorWidgetSetup(index, QgsEditorWidgetSetup(commune_widget, {
                        'Layer':lookup.id(), 'LayerName':lookup.name(), 'LayerSource':lookup.source(),
                        'LayerProviderName':'ogr', 'Key':'codice', 'Value':'nome', 'UseCompleter':True,
                        # A filter referring to id_comune itself rebuilds the
                        # completer on every keypress and can erase user input.
                        'FilterExpression': '"legacy"=0' if commune_widget=='ValueRelation' else '', 'CompleterMatchFlags':1,
                        'AllowNull':True, 'AllowMulti':False, 'OrderByValue':True}))
                    # V1.15: new interventions receive the Comune that contains
                    # their geometry (evaluated once, at creation; editable).
                    from .municipality_locator import DEFAULT_EXPRESSION
                    current = intervention.defaultValueDefinition(index)
                    if not current.expression() or current.expression() == DEFAULT_EXPRESSION:
                        intervention.setDefaultValueDefinition(index, QgsDefaultValue(DEFAULT_EXPRESSION, False))
            self._cascade_guard.bind(gpkg_path)
            streets=loaded.get('shape_str_comunali')
            if streets is None:
                streets=QgsVectorLayer(gpkg_path+'|layername=shape_str_comunali','shape_str_comunali','ogr')
                if streets.isValid():
                    QgsProject.instance().addMapLayer(streets,False)
                    group=QgsProject.instance().layerTreeRoot().findGroup('MAGOH')
                    group=group or QgsProject.instance().layerTreeRoot().addGroup('MAGOH')
                    group.addLayer(streets)
            if intervention is not None and streets is not None and streets.isValid():
                index=intervention.fields().indexFromName('indirizzo')
                if index>=0:
                    intervention.setEditorWidgetSetup(index,QgsEditorWidgetSetup('MAGOHStreetId',{
                        'Layer':streets.id(),'LayerName':streets.name(),'LayerSource':streets.source(),
                        'LayerProviderName':'ogr','Key':'id','Value':'den_uff',
                        'UseCompleter':False,'AllowNull':True,'AllowMulti':False,'OrderByValue':True,
                        'FilterExpression':'',
                        'MunicipalityLayer':lookup.id(),
                        'Description':'\'ID: \' || "id" || \' — \' || "comune"'}))
            toponyms = loaded.get('shape_toponimi')
            if toponyms is None:
                toponyms = QgsVectorLayer(gpkg_path+'|layername=shape_toponimi','shape_toponimi','ogr')
                if toponyms.isValid():
                    QgsProject.instance().addMapLayer(toponyms, False)
            if intervention is not None and toponyms is not None and toponyms.isValid():
                index = intervention.fields().indexFromName('toponimo')
                if index >= 0:
                    intervention.setFieldAlias(index, 'Toponimo')
                    intervention.setEditorWidgetSetup(index,QgsEditorWidgetSetup('MAGOHToponymId',{
                        'Layer':toponyms.id(),'Key':'pk_uid','Value':'toponimo',
                        'MunicipalityLayer':lookup.id()}))
        except Exception as exc:
            _log('Integrità archivio / Archive integrity: ' + str(exc), Qgis.Warning)
            self.iface.messageBar().pushWarning('MAGOH Spell',str(exc))
        try:
            created_indexes = self._ensure_performance_indexes(gpkg_path)
            if created_indexes:
                _log(f"Creati {created_indexes} indici attributivi MAGOH per migliorare le prestazioni.")
        except Exception as e:
            _log(f"Indici performance MAGOH: {e}", Qgis.Warning)
        try:
            removed = self._deduplicate_gpkg_layers(gpkg_path, preferred_ids)
            if removed:
                _log(f"Rimossi {removed} layer duplicati della stessa sorgente.")
        except Exception:
            pass
        try:
            self._ensure_magoh_relations(gpkg_path)
        except Exception as e:
            _log(f"Relazioni MAGOH: {e}", Qgis.Warning)
        try:
            self._install_local_temporary_id_defaults(gpkg_path)
        except Exception as e:
            _log(f"ID temporanei locali MAGOH: {e}", Qgis.Warning)
        try:
            self._install_spatial_parent_constraints(gpkg_path)
        except Exception as e:
            _log(f"Vincoli spaziali MAGOH: {e}", Qgis.Warning)
        try:
            self._repair_display_expressions(gpkg_path)
        except Exception as e:
            _log(f"Display expression MAGOH: {e}", Qgis.Warning)
        try:
            self._repair_loaded_widget_dependencies(gpkg_path)
        except Exception as e:
            _log(f"Widget form MAGOH: {e}", Qgis.Warning)
        try:
            self._repair_iii_level_codes(gpkg_path)
        except Exception as e:
            _log(f"III livello MAGOH: {e}", Qgis.Warning)
        # Guida MAGOH Wiki nei quattro moduli QGIS, sopra ID/Anno, e rimozione
        # del vecchio riquadro Info (anche nei progetti gia' esistenti).
        # Tutti i campi, gli editor, le schede e le relazioni rimangono invariati.
        try:
            from .gui.magoh_wiki import install as install_magoh_wiki
            install_magoh_wiki(self._layers_for_gpkg(gpkg_path))
        except Exception as e:
            _log(f"Pulsanti MAGOH Wiki: {e}", Qgis.Warning)
        try:
            self._reinitialize_relation_form_elements(gpkg_path)
        except Exception as e:
            _log(f"Relation editor form MAGOH: {e}", Qgis.Warning)
        # V1: no CTR is added automatically. Existing user basemaps are untouched.
        from .gui.layer_organization import normalize_magoh
        normalize_magoh(gpkg_path)
        try:
            # V1.23.2: GNA layers loaded by earlier versions get the IDs of the
            # official Template, so that the official GNA plugin can download here.
            from .gui.gna_presentation import upgrade_generated_ids
            if upgrade_generated_ids() is not None:
                _log("Pacchetto GNA ricaricato con gli identificativi del Template ufficiale", Qgis.Info)
        except Exception as e:
            _log(f"GNA: ricaricamento con gli identificativi ufficiali non riuscito: {e}", Qgis.Warning)
        own_intervention = self._layers_for_gpkg(gpkg_path).get('magoh_scheda_intervento')
        if own_intervention is not None:
            from .gui import precision_editor
            precision_editor.register()
            idx = own_intervention.fields().indexOf('precisione_di_georeferenziazione')
            if idx >= 0:
                own_intervention.setEditorWidgetSetup(idx,QgsEditorWidgetSetup(precision_editor.ID,{}))
        if own_intervention is not None and not own_intervention.customProperty('magoh/precision_canonical_v1', False):
            self.applica_simbologia_interventi([own_intervention], chiedi=False)
            own_intervention.setCustomProperty('magoh/precision_canonical_v1', True)
        for layer in self._layers_for_gpkg(gpkg_path).values():
            if layer.customProperty('magoh/api_idscheda_labels', False):
                apply_intervention_labels(layer)
        from .gui.local_identity import install_form
        for table,layer in self._layers_for_gpkg(gpkg_path).items():
            if table in local_codes.TABLES:
                install_form(layer,table)
        self._refresh_open_ui_status()

    # ----- lettura progetti QGIS incorporati nel GeoPackage -----
    @staticmethod
    def _decode_project_blob(raw):
        """Normalizza il contenuto di qgis_projects.content.

        A seconda di come e' stato scritto il GeoPackage il campo puo' essere:
        - una stringa esadecimale (caso del gpkg MAGOH distribuito);
        - un BLOB gia' binario;
        - direttamente XML .qgs non compresso.
        Ritorna bytes oppure None.
        """
        if raw is None:
            return None
        data = None
        if isinstance(raw, (bytes, bytearray, memoryview)):
            data = bytes(raw)
            if not data[:2] in (b"PK", b"<?"):
                try:
                    data = binascii.unhexlify(data)
                except Exception:
                    pass
        elif isinstance(raw, str):
            s = raw.strip()
            try:
                data = binascii.unhexlify(s)
            except Exception:
                data = s.encode("utf-8", "replace")
        if not data:
            return None
        # accettiamo solo zip (.qgz) o XML (.qgs)
        head = data.lstrip()[:5]
        if data[:2] == b"PK" or head.startswith(b"<?xml") or head.startswith(b"<qgis"):
            return data
        return None

    def list_gpkg_projects(self, gpkg_path):
        """Elenca i progetti QGIS contenuti nel GeoPackage.

        Ritorna una lista di dict: {name, remote} dove `remote` indica che il progetto
        contiene sorgenti di rete (PostGIS/WFS) non utilizzabili offline.
        Operazione leggera: legge solo i nomi, il contenuto viene ispezionato
        con una singola query per progetto.
        """
        out = []
        if not gpkg_path or not os.path.exists(gpkg_path):
            return out
        conn = None
        try:
            conn = sqlite3.connect(gpkg_path)
            cur = conn.cursor()
            cur.execute("SELECT name, content FROM qgis_projects ORDER BY name")
            for name, content in cur.fetchall() or []:
                if not name:
                    continue
                remote = False
                try:
                    data = self._decode_project_blob(content)
                    if data and data[:2] == b"PK":
                        import zipfile as _zf
                        import io as _io
                        with _zf.ZipFile(_io.BytesIO(data)) as z:
                            qgs = [n for n in z.namelist() if n.lower().endswith(".qgs")]
                            if qgs:
                                low = z.read(qgs[0])[:400000].lower()
                                remote = any(h.encode() in low for h in _REMOTE_PROJECT_HINTS)
                    elif data:
                        low = data[:400000].lower()
                        remote = any(h.encode() in low for h in _REMOTE_PROJECT_HINTS)
                except Exception:
                    remote = False
                out.append({"name": str(name), "remote": bool(remote)})
        except Exception as e:
            _log(f"Lettura qgis_projects fallita: {e}", Qgis.Warning)
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
        return out

    def _resolve_project_name(self, gpkg_path, wanted=DEFAULT_PROJECT_NAME):
        """Trova il nome reale del progetto nel gpkg tollerando spazi/underscore.

        Nel GeoPackage MAGOH il progetto si chiama "template MAGOH": cercare
        letteralmente "template_MAGOH" (come nelle build <= v13) falliva sempre.
        """
        names = [p["name"] for p in self.list_gpkg_projects(gpkg_path)]
        if not names:
            return None
        if wanted in names:
            return wanted

        def norm(s):
            return re.sub(r"[\s_\-]+", "", str(s)).lower()

        target = norm(wanted)
        for n in names:
            if norm(n) == target:
                return n
        # ultimo tentativo: un progetto che contenga "template"
        for n in names:
            if "template" in norm(n):
                return n
        return names[0]

    def _extract_qgz_from_gpkg(self, gpkg_path, project_name=DEFAULT_PROJECT_NAME):
        """Estrae il progetto QGIS dal GeoPackage. Ritorna bytes oppure None."""
        conn = None
        try:
            conn = sqlite3.connect(gpkg_path)
            cur = conn.cursor()
            cur.execute("SELECT content FROM qgis_projects WHERE name = ?", (project_name,))
            row = cur.fetchone()
            if not row:
                return None
            return self._decode_project_blob(row[0])
        except sqlite3.Error as e:
            raise RuntimeError(f"Il GeoPackage non contiene progetti QGIS leggibili: {e}") from e
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _write_project_files(self, workdir, qgz_bytes, project_filename="template_MAGOH.qgz"):
        # Sceglie l'estensione corretta in base al contenuto reale (.qgz zippato o .qgs XML)
        base = os.path.splitext(project_filename)[0]
        ext = ".qgz" if qgz_bytes[:2] == b"PK" else ".qgs"
        # nome file safe per Windows
        base = re.sub(r'[<>:"/\\|?*]+', "_", base).strip() or "MAGOH_Spell"
        project_path = os.path.join(workdir, base + ext)
        with open(project_path, "wb") as f:
            f.write(qgz_bytes)
        return project_path

    def _warn_unsaved(self):
        try:
            if QgsProject.instance().isDirty():
                r = QMessageBox.question(
                    self.iface.mainWindow(),
                    "MAGOH Spell",
                    _tf("Il progetto corrente ha modifiche non salvate. "
                        "Aprire MAGOH sovrascriverà il progetto corrente.\n\nContinuo?",
                        "The current project has unsaved changes. "
                        "Opening MAGOH will replace the current project.\n\nContinue?"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                return r == QMessageBox.StandardButton.Yes
        except Exception:
            pass
        return True

    def run_diagnostics(self):
        """Controlli leggeri pre-demo: nessun caricamento massivo, nessuna modifica al progetto."""
        checks = []

        packaged = self._packaged_gpkg_path()
        checks.append(("GeoPackage incluso", os.path.exists(packaged), packaged))
        try:
            import configparser
            metadata = configparser.ConfigParser()
            with open(os.path.join(self.plugin_dir, 'metadata.txt'), encoding='utf-8') as source:
                metadata.read_file(source)
            version = metadata.get('general', 'version')
            checks.append(("Metadati plugin", version == PLUGIN_VERSION, version))
        except Exception as e:
            checks.append(("Metadati plugin", False, str(e)))

        try:
            workdir = self._ensure_workdir()
            writable = os.path.isdir(workdir) and os.access(workdir, os.W_OK)
        except Exception as e:
            workdir = str(e)
            writable = False
        checks.append(("Cartella di lavoro scrivibile", writable, workdir))

        # Contenuto del GeoPackage: progetti presenti e conteggio record dei layer chiave.
        # Query sqlite puntuali: nessun caricamento di layer, nessuna modifica ai dati.
        try:
            gpkg_for_check = packaged if os.path.exists(packaged) else None

            if gpkg_for_check:
                projects = self.list_gpkg_projects(gpkg_for_check)
                if projects:
                    desc = ", ".join(p["name"] + (" [remoto]" if p["remote"] else "") for p in projects)
                    checks.append(("Progetti nel GeoPackage", True, desc))
                else:
                    checks.append(("Progetti nel GeoPackage", False, "tabella qgis_projects assente o vuota"))

                resolved = self._resolve_project_name(gpkg_for_check)
                checks.append((
                    "Progetto predefinito",
                    resolved is not None,
                    resolved or "non individuato",
                ))

                counts = self._table_counts(gpkg_for_check, [
                    "magoh_scheda_intervento",
                    "magoh_ritrovamenti",
                    "magoh_evidenza_archeologica",
                ])
                detail = "; ".join(f"{t}: {n if n is not None else 'assente'}" for t, n in counts.items())
                checks.append((
                    "Template operativo pulito",
                    all(n == 0 for n in counts.values() if n is not None),
                    detail,
                ))
        except Exception as e:
            checks.append(("Contenuto GeoPackage", False, str(e)))

        active_gpkg = self.get_active_gpkg()
        checks.append((
            "GeoPackage attivo",
            not active_gpkg or os.path.exists(active_gpkg),
            active_gpkg or "nessun progetto aperto (non è un errore di installazione)",
        ))

        try:
            project = QgsProject.instance()
            layer_count = len(project.mapLayers())
            checks.append(("Progetto QGIS", True, f"{layer_count} layer caricati"))

            # Se i layer MAGOH sono caricati, verifica anche le relazioni principali.
            active_layers = self._layers_for_gpkg(active_gpkg) if active_gpkg else {}
            if any(n in active_layers for n in ("magoh_scheda_intervento", "magoh_ritrovamenti", "magoh_evidenza_archeologica")):
                rels = project.relationManager().relations()
                expected = [r[0] for r in self.MAGOH_RELATIONS if r[2] in active_layers and r[4] in active_layers]
                missing = [rid for rid in expected if rid not in rels or not rels[rid].isValid()]
                checks.append((
                    "Relazioni MAGOH",
                    not missing,
                    "corrette" if not missing else "mancanti/non valide: " + ", ".join(missing),
                ))
        except Exception as e:
            checks.append(("Progetto QGIS", False, str(e)))

        try:
            al = self.iface.activeLayer()
            if al and isinstance(al, QgsVectorLayer):
                detail = f"{al.name()} — {al.featureCount()} record — {al.selectedFeatureCount()} selezionati"
            elif al:
                detail = al.name()
            else:
                detail = "Nessun layer attivo"
            checks.append(("Layer attivo", True, detail))
        except Exception as e:
            checks.append(("Layer attivo", False, str(e)))

        ok_all = all(ok for _, ok, _ in checks)
        lines = []
        for label, ok, detail in checks:
            icon = "✓" if ok else "✗"
            lines.append(f"{icon} {label}: {detail}")

        title = "Diagnostica MAGOH — OK" if ok_all else "Diagnostica MAGOH — controllare gli elementi segnati"
        QMessageBox.information(self.iface.mainWindow(), title, "\n".join(lines))
        return ok_all

    @staticmethod
    def _table_counts(gpkg_path, tables):
        """Conteggio record via sqlite (leggero, nessun caricamento di layer)."""
        out = {}
        conn = None
        try:
            conn = sqlite3.connect(gpkg_path)
            cur = conn.cursor()
            existing = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for t in tables:
                if t not in existing:
                    out[t] = None
                    continue
                try:
                    out[t] = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                except Exception:
                    out[t] = None
        except Exception:
            for t in tables:
                out.setdefault(t, None)
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
        return out

    def _zoom_to_active_project_data(self, gpkg_path: str, margin_ratio: float = 0.08):
        """Zoom leggero ai dati operativi del progetto MAGOH appena aperto.

        Il progetto template conserva volutamente un'estensione di riferimento
        (Pisa), utile quando si crea un progetto completamente vuoto. Quando si
        apre invece un progetto esistente con geometrie, l'estensione salvata nel
        template non deve prevalere sui dati reali: uniamo gli extent dei soli
        layer operativi MAGOH caricati dal GeoPackage e li trasformiamo nel CRS
        del canvas. Nessuna scansione delle feature: usa gli extent provider-side.
        """
        if not gpkg_path or not os.path.exists(gpkg_path):
            return False
        try:
            loaded = self._layers_for_gpkg(gpkg_path)
            dest_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
            project = QgsProject.instance()
            total = None
            used = 0
            for lname in self.CORE_MAGOH_TABLES:
                layer = loaded.get(lname)
                if not isinstance(layer, QgsVectorLayer) or not layer.isSpatial():
                    continue
                try:
                    if layer.featureCount() <= 0:
                        continue
                    rect = QgsRectangle(layer.extent())
                    if rect.isNull() or rect.isEmpty():
                        continue
                    if layer.crs().isValid() and dest_crs.isValid() and layer.crs() != dest_crs:
                        tr = QgsCoordinateTransform(layer.crs(), dest_crs, project.transformContext())
                        rect = tr.transformBoundingBox(rect)
                    if total is None:
                        total = QgsRectangle(rect)
                    else:
                        total.combineExtentWith(rect)
                    used += 1
                except Exception as e:
                    _log(f"Zoom progetto: extent non disponibile per {lname}: {e}", Qgis.Warning)
            if total is None or used == 0:
                return False
            # Un extent puntuale/lineare molto piccolo deve comunque essere visibile.
            w, h = abs(total.width()), abs(total.height())
            base = max(w, h)
            if base <= 0:
                base = 100.0
            grow = max(base * float(margin_ratio), 1e-9)
            if w <= 0:
                total.setXMinimum(total.xMinimum() - grow)
                total.setXMaximum(total.xMaximum() + grow)
            if h <= 0:
                total.setYMinimum(total.yMinimum() - grow)
                total.setYMaximum(total.yMaximum() + grow)
            total.grow(max(abs(total.width()), abs(total.height())) * float(margin_ratio))
            self.iface.mapCanvas().setExtent(total)
            self.iface.mapCanvas().refresh()
            return True
        except Exception as e:
            _log(f"Zoom automatico al progetto non riuscito: {e}", Qgis.Warning)
            return False

    def show_project_info(self):
        """Technical summary of the active workspace, kept separate from About MAGOH."""
        gpkg = self.get_active_gpkg()
        if not gpkg or not os.path.exists(gpkg):
            QMessageBox.information(
                self.iface.mainWindow(),
                _tf("MAGOH – Info progetto", "MAGOH – Project info"),
                _tf(
                    "Nessun progetto MAGOH attivo.\n\nCrea un nuovo progetto oppure aprine uno esistente.",
                    "No MAGOH project is active.\n\nCreate a new project or open an existing one."
                )
            )
            return
        ok_schema, missing = self._validate_magoh_gpkg(gpkg)
        root = self._infer_project_root(gpkg)
        counts = self._table_counts(gpkg, self.CORE_MAGOH_TABLES)
        loaded = self._layers_for_gpkg(gpkg)
        rels = QgsProject.instance().relationManager().relations()
        expected = [r for r in self.MAGOH_RELATIONS if r[2] in loaded and r[4] in loaded]
        valid = sum(1 for r in expected if r[0] in rels and rels[r[0]].isValid())
        qgis_project = QgsProject.instance().fileName() or "—"
        if _language() == "en":
            names = {
                "magoh_scheda_intervento": "Intervention Records",
                "magoh_ritrovamenti": "Findings",
                "magoh_evidenza_archeologica": "Evidence",
                "magoh_evidenza_archeologica_no_geom": "Evidence without geometry",
            }
            lines = [
                f"GeoPackage:\n{gpkg}",
                f"\nProject folder:\n{root or '— (external GeoPackage)'}",
                f"\nQGIS project:\n{qgis_project}",
                f"\nMAGOH structure: {'OK' if ok_schema else 'INCOMPLETE'}" + ("" if ok_schema else f" — missing: {', '.join(missing)}"),
                f"Valid QGIS relations: {valid}/{len(expected)}" if expected else "Valid QGIS relations: n/a",
                "\nRecords:",
            ]
            for table in self.CORE_MAGOH_TABLES:
                lines.append(f"  • {names.get(table, table)}: {counts.get(table, 'n/a')}")
            lines.append(f"\nMAGOH layers loaded in project: {len(loaded)}")
            title = "MAGOH – Project info"
        else:
            names = {
                "magoh_scheda_intervento": "Schede di Intervento",
                "magoh_ritrovamenti": "Ritrovamenti",
                "magoh_evidenza_archeologica": "Evidenze",
                "magoh_evidenza_archeologica_no_geom": "Evidenze senza geometria",
            }
            lines = [
                f"GeoPackage:\n{gpkg}",
                f"\nCartella progetto:\n{root or '— (GeoPackage esterno)'}",
                f"\nProgetto QGIS:\n{qgis_project}",
                f"\nStruttura MAGOH: {'OK' if ok_schema else 'INCOMPLETA'}" + ("" if ok_schema else f" — mancanti: {', '.join(missing)}"),
                f"Relazioni QGIS valide: {valid}/{len(expected)}" if expected else "Relazioni QGIS valide: n/d",
                "\nRecord:",
            ]
            for table in self.CORE_MAGOH_TABLES:
                lines.append(f"  • {names.get(table, table)}: {counts.get(table, 'n/d')}")
            lines.append(f"\nLayer MAGOH caricati nel progetto: {len(loaded)}")
            title = "MAGOH – Info progetto"
        QMessageBox.information(self.iface.mainWindow(), title, "\n".join(lines))

    # Ordine preferito dei layer MAGOH (quelli assenti vengono semplicemente saltati)
    PREFERRED_LAYERS = [
        "magoh_scheda_intervento",
        "magoh_ritrovamenti",
        "magoh_evidenza_archeologica",
        "magoh_evidenza_archeologica_no_geom",
        "dataset_completo",
        "credits",
        "I_livelli_new",
        "II_livelli_new",
        "thesaurus_crono",
        "thesaurus_periodo",
        "thesaurus_iv_livello",
        "thesaurus_affidabilità",
    ]

    def add_layers(self):
        """Aggiunge i layer MAGOH senza duplicati e ricostruisce relazioni/form.

        Strategia anti-warning/anti-rallentamento:
        1) crea prima TUTTI i layer necessari senza caricare gli stili;
        2) ricostruisce subito le relazioni sul RelationManager corrente;
        3) aggiorna ValueRelation/RelationReference nella copia di lavoro;
        4) carica gli stili quando le relazioni sono già disponibili;
        5) riallinea una seconda volta widget ed elementi relation editor.
        In questo modo QGIS non tenta di autocaricare vecchie dipendenze e le
        schede collegate non vengono inizializzate contro relazioni inesistenti.
        """
        gpkg_dst = self.get_active_gpkg()
        if not gpkg_dst or not os.path.exists(gpkg_dst):
            QMessageBox.information(
                self.iface.mainWindow(), "MAGOH Spell",
                "Non c'è un progetto MAGOH attivo.\n\nCrea un nuovo progetto oppure apri un GeoPackage MAGOH esistente."
            )
            return
        ok_schema, missing = self._validate_magoh_gpkg(gpkg_dst)
        if not ok_schema:
            QMessageBox.warning(
                self.iface.mainWindow(), "MAGOH Spell",
                "Il GeoPackage attivo non possiede la struttura MAGOH completa.\n\nMancano: " + ", ".join(missing)
            )
            return

        available = set(self._layer_names_from_gpkg(gpkg_dst))
        wanted = [n for n in self.PREFERRED_LAYERS if n in available]
        if not wanted:
            wanted = [n for n in sorted(available) if not str(n).startswith("gpkg_") and n != "layer_styles"]
        if not wanted:
            QMessageBox.warning(self.iface.mainWindow(), "MAGOH Spell", "Nessun layer trovato nel GeoPackage.")
            return

        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup("MAGOH") or root.addGroup("MAGOH")
        grp_layers = group.findGroup("Layers") or group.addGroup("Layers")
        grp_tables = group.findGroup("Tables") or group.addGroup("Tables")

        target_path = os.path.normcase(os.path.abspath(gpkg_dst))
        existing_by_name = {}
        for lyr in project.mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer):
                continue
            pth, lname = self._split_gpkg_source(lyr.source())
            if pth == target_path and lname and lname not in existing_by_name:
                existing_by_name[lname] = lyr

        loaded = dict(existing_by_name)
        new_layers = []
        skipped, failed = 0, []

        # Fase 1: carica le sorgenti, SENZA loadDefaultStyle.
        for name in wanted:
            if name in loaded:
                skipped += 1
                continue
            v = QgsVectorLayer(f"{gpkg_dst}|layername={name}", name, "ogr")
            if not v.isValid():
                failed.append(name)
                continue
            project.addMapLayer(v, False)
            (grp_layers if v.isSpatial() else grp_tables).addLayer(v)
            loaded[name] = v
            new_layers.append(v)

        # Fase 2: crea PRIMA le relazioni sui layer reali. È importante farlo
        # prima di loadDefaultStyle(): gli attributeEditorRelation delle form
        # vengono inizializzati quando il QML viene applicato e, se il
        # RelationManager è ancora vuoto, il pannello dei record collegati può
        # restare apparentemente scollegato anche dopo una riparazione tardiva.
        self.set_active_gpkg(gpkg_dst)
        try:
            self._ensure_magoh_relations(gpkg_dst)
        except Exception as e:
            _log(f"Pre-caricamento relazioni MAGOH: {e}", Qgis.Warning)

        # Fase 3: ripara i QML nella sola copia di lavoro usando gli ID reali
        # dei layer e il datasource RelationReference corretto.
        try:
            self._repair_gpkg_styles_for_loaded_layers(gpkg_dst)
        except Exception as e:
            _log(f"Preparazione stili: {e}", Qgis.Warning)

        # Fase 4: applica gli stili quando dipendenze E relazioni esistono già.
        for v in new_layers:
            try:
                v.loadDefaultStyle()
            except Exception as e:
                _log(f"Stile non caricato per {v.name()}: {e}", Qgis.Warning)

        # Fase 4b: vestizione della scheda Intervento per precisione di
        # georeferenziazione. Va dopo loadDefaultStyle, altrimenti lo stile del
        # GeoPackage la cancellerebbe; e non tocca mai il GeoPackage: vive solo
        # nel progetto aperto.
        try:
            self.applica_simbologia_interventi(new_layers)
        except Exception as e:
            _log(f"Vestizione scheda intervento: {e}", Qgis.Warning)

        # Fase 5: riallineamento finale e re-init degli elementi relazione.
        self._post_load_repair(gpkg_dst, preferred_ids=[l.id() for l in loaded.values()])

        added = len(new_layers)
        msg = f"Layer aggiunti: {added}"
        if skipped:
            msg += f" — già presenti: {skipped}"
        if failed:
            msg += f" — non caricabili: {', '.join(failed)}"
            self.iface.messageBar().pushWarning("MAGOH Spell", msg)
        else:
            self.iface.messageBar().pushSuccess("MAGOH Spell", msg)

        if added:
            self.iface.mapCanvas().refresh()

    #: nomi con cui si riconosce il layer della scheda Intervento
    NOMI_INTERVENTO = ("magoh_scheda_intervento", "scheda_intervento", "interventi")

    def applica_simbologia_interventi(self, layers=None, chiedi=True):
        """Veste la scheda Intervento in base alla precisione di georeferenziazione.

        Due cautele volute: la vestizione **non viene scritta nel GeoPackage**
        dell'utente — vive solo nel progetto aperto — e se il layer ha già uno
        stile suo non lo si sostituisce di nascosto, si chiede. Uno stile
        costruito a mano è lavoro di qualcuno.
        """
        from .gui.gna_symbology import ha_stile_proprio, simbologia_intervento

        if layers is None:
            layers = list(QgsProject.instance().mapLayers().values())
        candidati = []
        for lyr in layers:
            try:
                nome = "".join(ch for ch in str(lyr.name()).lower()
                               if ch.isalnum() or ch == "_")
            except Exception:
                continue
            if any(k in nome for k in self.NOMI_INTERVENTO):
                candidati.append(lyr)
        if not candidati:
            return 0

        vestiti, messaggi = 0, []
        for lyr in candidati:
            if chiedi and ha_stile_proprio(lyr):
                risposta = QMessageBox.question(
                    self.iface.mainWindow(), _t("MAGOH – Strumenti"),
                    _t("Il layer «%s» ha già uno stile proprio.\n\n"
                       "Sostituirlo con la vestizione per precisione di "
                       "georeferenziazione?") % lyr.name(),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if risposta != QMessageBox.StandardButton.Yes:
                    messaggi.append("Vestizione non applicata a «%s»: stile esistente conservato."
                                    % lyr.name())
                    continue
            fatto, msg = simbologia_intervento(lyr, os.path.dirname(os.path.abspath(__file__)))
            messaggi.extend(msg)
            if fatto:
                vestiti += 1
                try:
                    lyr.setCustomProperty("magoh/simbologia_intervento", True)
                except Exception:
                    pass
        for m in messaggi:
            _log(m, Qgis.Info)
        if vestiti:
            try:
                self.iface.messageBar().pushInfo(
                    "MAGOH Spell",
                    _t("Scheda intervento vestita per precisione di georeferenziazione."))
            except Exception:
                pass
        return vestiti


    # ---------- Report tool: entry point ----------
    def open_launcher_dialog(self):
        """Dashboard NON modale.

        Nelle build <= v13 la dashboard veniva aperta con exec_(): essendo modale
        bloccava l'input verso la finestra dei Filtri e verso il pannello laterale,
        che quindi risultavano aperti ma inutilizzabili (vedi workflow demo, punto 5).
        """
        dlg = getattr(self, "_launcher_dlg", None)
        if dlg is None:
            dlg = LauncherDialog(self.iface, self)
            self._launcher_dlg = dlg
            fit_dialog_to_parent(dlg, self.iface.mainWindow(), 620, 540)
        apply_teal_style(dlg)
        dlg.setModal(False)
        fit_dialog_to_parent(dlg, self.iface.mainWindow())
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        # stato aggiornato solo all'apertura, mai con listener permanenti
        try:
            dlg._refresh_status()
        except Exception:
            pass

    def toggle_tools_dock(self):
        """Pannello laterale per collegamenti, esportazioni e analisi.

        È un vero QDockWidget: il canvas resta sempre utilizzabile mentre il
        pannello è aperto.
        """
        d = getattr(self, "tools_dock", None)
        if d is not None and d.isVisible():
            d.hide()
            return
        if d is None:
            d = MagohToolsDockWidget(self.iface, self)
            self.tools_dock = d
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, d)
        d.show()
        d.raise_()
        try:
            d.relation_panel.refresh_layer_browser()
        except Exception:
            pass


    def open_gna_export_dialog(self, *_args):
        """Apre il convertitore MAGOH → GNA, riusando la stessa finestra."""
        from .gui.gna_workflow import GnaWorkflowDialog
        if self._gna_dialog is None:
            self._gna_dialog = GnaWorkflowDialog(self.iface, Path(self.plugin_dir), self)
        self._gna_dialog.refresh()
        self._gna_dialog.show()
        self._gna_dialog.raise_()
        self._gna_dialog.activateWindow()
        return self._gna_dialog

    def _show_modeless_dialog(self, attr_name, factory):
        """Mostra una finestra non modale e la mantiene viva.

        Permette di continuare a selezionare, zoomare e aprire le form dei layer
        nel canvas mentre gli strumenti MAGOH sono aperti.
        """
        dlg = getattr(self, attr_name, None)
        if dlg is None:
            dlg = factory()
            dlg.setModal(False)
            setattr(self, attr_name, dlg)
        try:
            if hasattr(dlg, "_update_linked_label"):
                dlg._update_linked_label()
            if hasattr(dlg, "refresh_layers"):
                dlg.refresh_layers()
            elif hasattr(dlg, "_populate_layers"):
                dlg._populate_layers()
        except Exception:
            pass
        fit_dialog_to_parent(dlg, self.iface.mainWindow())
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg


    def open_pdf_export_dialog(self):
        return self._show_modeless_dialog("_pdf_dlg", lambda: PdfExportDialog(self.iface, self))

    # ---------- Public API used by the UI (keep stable name) ----------
    def export_schede_pdf(
        self, layer, output_pdf, only_selected=False, fields_per_page=25,
        atlas_margin_percent=15, use_canvas_layers=True,
        isolate_current_feature=True, prefer_atlas=False, progress_callback=None,
    ):
        """Commit the finished PDF only; errors/cancellation preserve older files."""
        if not output_pdf:
            raise RuntimeError(_tf("Percorso output PDF mancante", 'Missing PDF output path'))
        output_pdf = os.path.abspath(output_pdf)
        if not output_pdf.lower().endswith('.pdf'):
            output_pdf += '.pdf'
        with tempfile.NamedTemporaryFile(prefix='.magoh_export_', suffix='.pdf',
                                         dir=os.path.dirname(output_pdf), delete=False) as tmp:
            temporary = tmp.name
        try:
            result = self._export_schede_pdf_to_file(
                layer, temporary, only_selected, fields_per_page,
                atlas_margin_percent, use_canvas_layers, isolate_current_feature,
                prefer_atlas, progress_callback,
            )
            if isinstance(result, dict) and result.get('cancelled'):
                return {'output': output_pdf, 'records': 0, 'cancelled': True, 'preserved': True}
            os.replace(temporary, output_pdf)
            if isinstance(result, dict):
                result['output'] = output_pdf
            return result
        finally:
            if os.path.isfile(temporary):
                os.remove(temporary)

    def _export_schede_pdf_to_file(
        self,
        layer: QgsVectorLayer,
        output_pdf: str,
        only_selected: bool = False,
        fields_per_page: int = 25,
        atlas_margin_percent: int = 15,
        use_canvas_layers: bool = True,
        isolate_current_feature: bool = True,
        prefer_atlas: bool = False,
        progress_callback=None,
    ):
        """Entry point chiamato dalla UI per l'export PDF.

        Manteniamo questo metodo per evitare regressioni: la UI del plugin chiama
        `export_schede_pdf(...)`. Qui facciamo dispatch al motore più robusto
        (direct) salvo richiesta esplicita di usare Atlas.

        `fields_per_page` è mantenuto per compatibilità: il motore "direct" non
        paginizza per numero di campi ma per spazio disponibile; viene ignorato.
        """

        if prefer_atlas:
            return self.export_schede_pdf_atlas(
                layer=layer,
                output_pdf=output_pdf,
                only_selected=only_selected,
                fields_per_page=fields_per_page,
                atlas_margin_percent=atlas_margin_percent,
                use_canvas_layers=use_canvas_layers,
                isolate_current_feature=isolate_current_feature,
            )

        return self.export_schede_pdf_direct(
            layer=layer,
            output_pdf=output_pdf,
            only_selected=only_selected,
            atlas_margin_percent=atlas_margin_percent,
            use_canvas_layers=use_canvas_layers,
            isolate_current_feature=isolate_current_feature,
            progress_callback=progress_callback,
        )


    def open_api_dialog(self):
        return self._show_modeless_dialog("_api_dlg", lambda: MagohApiDialog(self.iface, self))


    def open_gpkg_link_dialog(self):
        return self._show_modeless_dialog("_gpkg_dlg", lambda: GpkgLinkDialog(self))

    def open_info_dialog(self):
        return self._show_modeless_dialog("_info_dlg", lambda: InfoDialog(self.iface, self))

    # ---------- PDF export (mappa + scheda attributi) ----------
    def export_schede_pdf_atlas(
        self,
        layer: QgsVectorLayer,
        output_pdf: str,
        only_selected: bool = False,
        fields_per_page: int = 25,
        atlas_margin_percent: int = 15,
        use_canvas_layers: bool = True,
        isolate_current_feature: bool = True,
    ):
        """Esporta un PDF multipagina usando un layout Atlas.
        - Pagina 1: mappa di localizzazione (zoom sulla feature)
        - Pagine successive: scheda attributi (tutti i campi, spezzati su più pagine)

        Richieste chiave:
        - nessuna sovrapposizione: nella mappa viene disegnato SOLO il record corrente del layer di copertura
          (le altre feature dello stesso layer non vengono renderizzate).
        - gestisce selezione e sovrascrittura file.
        """

        if not layer or not layer.isValid():
            raise RuntimeError(_tf("Layer non valido", 'Invalid layer'))
        if not output_pdf:
            raise RuntimeError(_tf("Percorso output PDF mancante", 'Missing PDF output path'))
        if not output_pdf.lower().endswith(".pdf"):
            output_pdf = output_pdf + ".pdf"

        # Overwrite? (gestito anche nel dialog, ma qui restiamo robusti)
        if os.path.exists(output_pdf):
            try:
                os.remove(output_pdf)
            except OSError as exc:
                # se il file è aperto/lockato, fallisce qui invece di crashare più avanti
                raise RuntimeError(_tf("Impossibile sovrascrivere il PDF: il file sembra aperto o non scrivibile", 'Cannot overwrite the PDF: the file seems open or not writable')) from exc

        sel_ids = list(layer.selectedFeatureIds())
        if only_selected and not sel_ids:
            raise RuntimeError(_tf("Hai attivato 'solo selezionati', ma nel layer non ci sono feature selezionate", "'Selected only' is active, but the layer has no selected features"))

        # Campi
        qgs_fields = list(layer.fields())
        if not qgs_fields:
            raise RuntimeError(_tf("Il layer non ha campi", 'The layer has no fields'))
        if fields_per_page <= 0:
            fields_per_page = 25

        chunks = [qgs_fields[i : i + fields_per_page] for i in range(0, len(qgs_fields), fields_per_page)]
        if not chunks:
            chunks = [qgs_fields]

        # Campo ID "carino" per titolo
        id_field = None
        for candidate in ("idscheda", "id", "fid"):
            if layer.fields().indexOf(candidate) != -1:
                id_field = candidate
                break
        if not id_field:
            id_field = layer.fields()[0].name()

        project = QgsProject.instance()
        lm = project.layoutManager()

        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        layout_name = "MAGOH_schede_pdf_tmp"
        existing = {l.name() for l in lm.printLayouts()}
        if layout_name in existing:
            n = 1
            while f"{layout_name}_{n}" in existing:
                n += 1
            layout_name = f"{layout_name}_{n}"
        layout.setName(layout_name)
        lm.addLayout(layout)

        temp_layers = []
        try:
            # --- Pagine
            page_h_mm = 297.0  # A4 portrait
            pages = layout.pageCollection()

            # Forza A4 sulla prima pagina
            try:
                pages.page(0).setPageSize("A4")
            except Exception:
                pass

            # La prima pagina esiste già; aggiungo pagine attributi (1 per chunk)
            # (usa QgsLayoutItemPage per evitare comportamenti strani/copy non supportati)
            from qgis.core import QgsLayoutItemPage

            for _ in range(len(chunks)):
                p = QgsLayoutItemPage(layout)
                try:
                    p.setPageSize("A4")
                except Exception:
                    pass
                pages.addPage(p)

            # --- Atlas
            atlas = layout.atlas()
            atlas.setEnabled(True)
            atlas.setCoverageLayer(layer)

            # filtro su selezionati
            if only_selected:
                atlas.setFilterFeatures(True)
                atlas.setFilterExpression("$id IN (%s)" % ",".join(str(i) for i in sel_ids))
            else:
                atlas.setFilterFeatures(False)

            # --- Layers per mappa
            try:
                canvas_layers = self.iface.mapCanvas().layers() if use_canvas_layers else [layer]
            except Exception:
                canvas_layers = [layer]

            layers_for_map = list(canvas_layers)

            # Garantisce che il layer "stampato" sia sempre presente nella mappa
            if all((lyr is None) or (lyr.id() != layer.id()) for lyr in layers_for_map):
                layers_for_map.append(layer)

            if isolate_current_feature:
                # sostituisci il layer stampato con un clone filtrato via regola: $id = @atlas_featureid
                try:
                    clone = QgsVectorLayer(layer.source(), f"{layer.name()} (record corrente)", layer.providerType())
                    if clone and clone.isValid():
                        geom_type = clone.geometryType()
                        if geom_type == QgsWkbTypes.PolygonGeometry:
                            sym = QgsFillSymbol.createSimple({
                                "color": "255,165,0,90",
                                "outline_color": "255,0,0,255",
                                "outline_width": "0.8",
                            })
                        elif geom_type == QgsWkbTypes.LineGeometry:
                            sym = QgsLineSymbol.createSimple({
                                "color": "255,0,0,255",
                                "width": "1.2",
                            })
                        else:
                            sym = QgsMarkerSymbol.createSimple({
                                "name": "circle",
                                "color": "255,0,0,255",
                                "size": "3.0",
                                "outline_color": "255,255,255,255",
                                "outline_width": "0.6",
                            })

                        root = QgsRuleBasedRenderer.Rule(None)
                        rule = QgsRuleBasedRenderer.Rule(sym)
                        rule.setFilterExpression("$id = @atlas_featureid")
                        root.appendChild(rule)
                        clone.setRenderer(QgsRuleBasedRenderer(root))

                        # rimpiazza nella lista layers
                        for i, lyr in enumerate(layers_for_map):
                            if lyr and lyr.id() == layer.id():
                                layers_for_map[i] = clone
                        temp_layers.append(clone)
                except Exception:
                    # se qualcosa va storto, meglio stampare "troppo" che crashare
                    pass

            # --- Helper posizionamento su pagina N
            def _pt(x, y):
                return QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters)

            def _sz(w, h):
                return QgsLayoutSize(w, h, QgsUnitTypes.LayoutMillimeters)

            def _y(page_index, y_in_page):
                return page_h_mm * page_index + y_in_page

            def _set_layout_font(item, font):
                text_format = QgsTextFormat()
                text_format.setFont(font)
                text_format.setSize(font.pointSizeF())
                item.setTextFormat(text_format)

            # --- Pagina 0: localizzazione
            title = QgsLayoutItemLabel(layout)
            _set_layout_font(title, QFont("Arial", 12, QFont.Weight.Bold))
            title.setText(f"Localizzazione – {layer.name()}")
            title.adjustSizeToText()
            title.attemptMove(_pt(10, _y(0, 8)))
            layout.addLayoutItem(title)

            rec = QgsLayoutItemLabel(layout)
            _set_layout_font(rec, QFont("Arial", 10))
            rec.setText(f"Record: [% attribute(@atlas_feature, '{id_field}') %]")
            rec.adjustSizeToText()
            rec.attemptMove(_pt(10, _y(0, 15)))
            layout.addLayoutItem(rec)

            map_item = QgsLayoutItemMap(layout)
            map_item.setFrameEnabled(True)
            map_item.attemptMove(_pt(10, _y(0, 22)))
            map_item.attemptResize(_sz(190, 240))
            try:
                map_item.setLayers(layers_for_map)
                map_item.setKeepLayerSet(True)
            except Exception:
                pass

            # Atlas zoom + margine in percentuale
            try:
                map_item.setAtlasDriven(True)
                map_item.setAtlasScalingMode(QgsLayoutItemMap.Auto)
                map_item.setAtlasMargin(float(atlas_margin_percent))
            except Exception:
                pass
            layout.addLayoutItem(map_item)

            # Scalebar
            try:
                sb = QgsLayoutItemScaleBar(layout)
                sb.setStyle("Single Box")
                _set_layout_font(sb, QFont("Arial", 8))
                sb.setLinkedMap(map_item)
                sb.applyDefaultSize()
                sb.attemptMove(_pt(10, _y(0, 268)))
                sb.attemptResize(_sz(70, 8))
                layout.addLayoutItem(sb)
            except Exception:
                pass

            # --- Attributi: pagine 1..N
            def _escape_field_for_expr(n: str) -> str:
                return n.replace("'", "''")

            def _expr_value(field_name: str) -> str:
                fn = _escape_field_for_expr(field_name)
                base = f"coalesce(to_string(attribute(@atlas_feature, '{fn}')), '')"
                # escape minimo + newline -> <br/>
                return (
                    "replace(replace(replace(replace(" + base + ", '&', '&amp;'), '<', '&lt;'), '>', '&gt;'), "
                    "'\\n', '<br/>')"
                )

            def _html_table(fields_chunk):
                rows = []
                for f in fields_chunk:
                    name = f.name()
                    label = f.alias() or name
                    rows.append(
                        "<tr>"
                        f"<th>{html.escape(label)}</th>"
                        f"<td>[% {_expr_value(name)} %]</td>"
                        "</tr>"
                    )
                return (
                    "<html><head><meta charset='utf-8'/>"
                    "<style>"
                    "body{font-family:Arial; font-size:10pt;}"
                    "table{border-collapse:collapse; width:100%;}"
                    "th,td{border:0.2mm solid #d0d0d0; padding:2.0mm; vertical-align:top;}"
                    "th{background:#f5f5f5; width:36%; text-align:left;}"
                    "td{word-break:break-word; white-space:pre-wrap;}"
                    "</style></head><body>"
                    "<table>" + "".join(rows) + "</table>"
                    "</body></html>"
                )

            for i, chunk in enumerate(chunks):
                page_index = 1 + i

                header = QgsLayoutItemLabel(layout)
                _set_layout_font(header, QFont("Arial", 11, QFont.Weight.Bold))
                header.setText(
                    f"Scheda attributi – {layer.name()} – Record: [% attribute(@atlas_feature, '{id_field}') %] "
                    f"(pag. {i+1}/{len(chunks)})"
                )
                header.attemptMove(_pt(10, _y(page_index, 8)))
                header.attemptResize(_sz(190, 12))
                layout.addLayoutItem(header)

                # HTML multiframe + frame (più robusto rispetto a QLabel/HTML)
                html_mf = QgsLayoutItemHtml(layout)
                try:
                    html_mf.setContentMode(QgsLayoutItemHtml.ManualHtml)
                except Exception:
                    pass
                html_mf.setHtml(_html_table(chunk))
                try:
                    html_mf.loadHtml()
                except Exception:
                    pass

                frame = QgsLayoutFrame(layout, html_mf)
                html_mf.addFrame(frame)
                frame.setFrameEnabled(True)
                try:
                    frame.setBackgroundEnabled(True)
                    frame.setBackgroundColor(QColor(255, 255, 255))
                except Exception:
                    pass

                x, y = 10, _y(page_index, 22)
                w, h = 190, 260
                frame.attemptSetSceneRect(QRectF(x, y, w, h))
                layout.addLayoutItem(frame)

            # --- Export
            exporter = QgsLayoutExporter(layout)
            settings = QgsLayoutExporter.PdfExportSettings()
            # con basemap raster/WMS è più stabile non forzare vettoriale
            try:
                settings.forceVectorOutput = False
            except Exception:
                pass

            res, err = exporter.exportToPdf(layout.atlas(), output_pdf, settings)
            if res != QgsLayoutExporter.Success:
                raise RuntimeError(f"Export PDF fallito: {err or res}")

        finally:
            try:
                lm.removeLayout(layout)
            except Exception:
                pass
            # cleanup layer temporanei
            try:
                for tl in temp_layers:
                    try:
                        tl.deleteLater()
                    except Exception:
                        pass
            except Exception:
                pass

    def export_schede_pdf_direct(
        self,
        layer: QgsVectorLayer,
        output_pdf: str,
        only_selected: bool = False,
        atlas_margin_percent: int = 15,
        use_canvas_layers: bool = True,
        isolate_current_feature: bool = True,
        dpi: int = 150,
        progress_callback=None,
    ):
        """Esporta un PDF multipagina senza Atlas, disegnando direttamente su PDF.

        Vantaggi pratici:
        - impaginazione più stabile (niente HTML che "schiaccia" il testo);
        - numero pagine per record variabile (in base ai contenuti);
        - isolamento record garantito: in mappa viene renderizzata una layer temporanea con la sola geometria corrente.

        Nota: per basemap remote (XYZ/WMS) il rendering può richiedere più tempo, come nel canvas.
        """

        if not layer or not layer.isValid():
            raise RuntimeError(_tf("Layer non valido", 'Invalid layer'))
        if not output_pdf:
            raise RuntimeError(_tf("Percorso output PDF mancante", 'Missing PDF output path'))
        if not output_pdf.lower().endswith(".pdf"):
            output_pdf = output_pdf + ".pdf"

        # Overwrite (robusto)
        if os.path.exists(output_pdf):
            try:
                os.remove(output_pdf)
            except OSError as exc:
                raise RuntimeError(_tf("Impossibile sovrascrivere il PDF: il file sembra aperto o non scrivibile", 'Cannot overwrite the PDF: the file seems open or not writable')) from exc

        sel_ids = list(layer.selectedFeatureIds())
        if only_selected and not sel_ids:
            raise RuntimeError(_tf("Hai attivato 'solo selezionati', ma nel layer non ci sono feature selezionate", "'Selected only' is active, but the layer has no selected features"))

        # Features: usiamo un iteratore QGIS invece di list(...) per non caricare
        # in memoria l'intero layer (requisito di basso consumo di memoria).
        req = QgsFeatureRequest()
        if only_selected:
            req.setFilterFids(sel_ids)

        total_feats = len(sel_ids) if only_selected else layer.featureCount()
        if total_feats is None or total_feats < 0:
            total_feats = 0
        if total_feats == 0:
            raise RuntimeError(_tf("Nessun record da esportare", 'No records to export'))

        # Campo ID per intestazioni
        id_field = None
        _source, table_name = self._split_gpkg_source(layer.source())
        candidates = ("idscheda", "id", "fid") if table_name == 'magoh_scheda_intervento' else ("id", "idscheda", "fid")
        for candidate in candidates:
            if layer.fields().indexOf(candidate) != -1:
                id_field = candidate
                break
        if not id_field:
            id_field = layer.fields()[0].name()

        # Canvas layers
        try:
            base_layers = self.iface.mapCanvas().layers() if use_canvas_layers else [layer]
        except Exception:
            base_layers = [layer]

        with pdf_canvas(output_pdf, dpi) as (writer, painter):
            # mm -> px helper
            px_per_mm = writer.resolution() / 25.4
            def mm(x):
                return int(round(x * px_per_mm))

            page_w = writer.width()
            page_h = writer.height()

            # layout constants
            margin = mm(10)
            title_h = mm(10)
            subtitle_h = mm(7)
            gap = mm(3)
            map_h = mm(190)
            map_w = page_w - 2 * margin

            # table constants
            table_top = margin
            table_left = margin
            table_w = page_w - 2 * margin
            col1_w = int(table_w * 0.30)
            col2_w = table_w - col1_w
            cell_pad = mm(3)
            line_gap = mm(2)
            header_h = mm(12)

            font_title = QFont("Arial", 14, QFont.Weight.Bold)
            font_sub = QFont("Arial", 10)
            font_th = QFont("Arial", 11, QFont.Weight.Bold)
            font_td = QFont("Arial", 11)

            project = QgsProject.instance()

            def _safe(v):
                # Normalizza valori QGIS/PyQt in stringhe leggibili
                # (evita che nel PDF compaiano stringhe tipo PyQt5.QtCore.QDateTime(...))
                if v is None:
                    return ""
                try:
                    # QGIS NULL (QVariant nullo) e QDate/QDateTime non valide
                    if isinstance(v, QVariant):
                        if v.isNull() or not v.isValid():
                            return ""
                        v = v.value()
                        if v is None:
                            return ""
                    elif hasattr(v, "isNull") and v.isNull():
                        return ""
                except Exception:
                    pass
                if isinstance(v, bool):
                    return "Sì" if v else "No"
                try:
                    if isinstance(v, QDateTime):
                        return v.toString("dd/MM/yyyy HH:mm")
                    if isinstance(v, QDate):
                        return v.toString("dd/MM/yyyy")
                    if isinstance(v, QTime):
                        return v.toString("HH:mm")
                except Exception:
                    pass
                try:
                    if isinstance(v, float):
                        if math.isfinite(v):
                            return (f"{v:.3f}").rstrip("0").rstrip(".")
                except Exception:
                    pass
                try:
                    return str(v)
                except Exception:
                    return ""

            def _feature_label(ft):
                try:
                    v = ft[id_field]
                    s = _safe(v).strip()
                    return s if s else str(ft.id())
                except Exception:
                    return str(ft.id())

            def _friendly_layer_label():
                n = (layer.name() or "").strip()
                if n == "magoh_scheda_intervento":
                    return "Scheda Intervento"
                return n.replace("_", " ")

            def _expand_rect(rect: QgsRectangle, margin_percent: float):
                if rect is None:
                    return rect
                w = rect.width()
                h = rect.height()
                # estensione minima: punti (w=h=0) ma anche linee perfettamente
                # orizzontali/verticali, dove si annullava una sola dimensione.
                min_side = 0.001 if layer.crs().isGeographic() else 100.0
                if w <= 0 or h <= 0:
                    cx = (rect.xMinimum() + rect.xMaximum()) / 2.0
                    cy = (rect.yMinimum() + rect.yMaximum()) / 2.0
                    w = w if w > 0 else min_side
                    h = h if h > 0 else min_side
                    rect = QgsRectangle(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)
                m = max(w, h) * (float(margin_percent) / 100.0)
                if m > 0:
                    rect.grow(m)
                return rect

            def _render_map(ft):
                geom = ft.geometry()
                if geom is None or geom.isEmpty():
                    return None, None
                rect = _expand_rect(geom.boundingBox(), float(atlas_margin_percent))

                # temporary layer with ONLY current geometry (isolation)
                temp_layer = None
                layers_for_render = []

                if isolate_current_feature:
                    # remove original layer from base list and append temp layer at same position
                    idx = 0
                    for i, lyr in enumerate(base_layers):
                        if hasattr(lyr, "id") and lyr.id() == layer.id():
                            idx = i
                            break

                    # build memory layer
                    wkb_str = QgsWkbTypes.displayString(layer.wkbType())
                    # authid() e' vuoto per i CRS personalizzati: in quel caso il layer
                    # temporaneo finiva erroneamente in EPSG:4326 e la mappa risultava vuota.
                    crs_def = layer.crs().authid()
                    if not crs_def:
                        wkt = layer.crs().toWkt()
                        crs_def = wkt if wkt else "EPSG:4326"
                    temp_layer = QgsVectorLayer(f"{wkb_str}?crs={crs_def}", "__magoh_current_feature__", "memory")
                    if not temp_layer.isValid():
                        temp_layer = QgsVectorLayer(f"{wkb_str}?crs=EPSG:4326", "__magoh_current_feature__", "memory")
                    try:
                        temp_layer.setCrs(layer.crs())
                    except Exception:
                        pass
                    prov = temp_layer.dataProvider()
                    prov.addAttributes(layer.fields())
                    temp_layer.updateFields()
                    f2 = QgsFeature(temp_layer.fields())
                    f2.setGeometry(geom)
                    # copy attrs so label/expr works if needed
                    for i_f, fld in enumerate(layer.fields()):
                        try:
                            f2.setAttribute(i_f, ft[fld.name()])
                        except Exception:
                            pass
                    prov.addFeatures([f2])

                    # simple red style
                    try:
                        gtype = temp_layer.geometryType()
                        if gtype == QgsWkbTypes.PolygonGeometry:
                            sym = QgsFillSymbol.createSimple({
                                "color": "255,165,0,90",
                                "outline_color": "255,0,0,255",
                                "outline_width": "0.8",
                            })
                        elif gtype == QgsWkbTypes.LineGeometry:
                            sym = QgsLineSymbol.createSimple({
                                "color": "255,0,0,255",
                                "width": "1.2",
                            })
                        else:
                            sym = QgsMarkerSymbol.createSimple({
                                "name": "circle",
                                "color": "255,0,0,255",
                                "size": "3.0",
                                "outline_color": "255,255,255,255",
                                "outline_width": "0.6",
                            })
                        try:
                            # renderer di default = single symbol
                            temp_layer.renderer().setSymbol(sym)
                        except Exception:
                            pass
                    except Exception:
                        pass

                    # Ensure temp layer is renderable (transform context)
                    layers_for_render = []
                    for lyr in base_layers:
                        if hasattr(lyr, "id") and lyr.id() == layer.id():
                            continue
                        layers_for_render.append(lyr)
                    layers_for_render.insert(min(idx, len(layers_for_render)), temp_layer)
                else:
                    layers_for_render = list(base_layers)

                # Map settings
                ms = QgsMapSettings()
                ms.setLayers([l for l in layers_for_render if isinstance(l, QgsMapLayer) and l.isValid()])
                ms.setBackgroundColor(QColor(255, 255, 255))
                ms.setOutputDpi(writer.resolution())
                ms.setOutputSize(QSize(map_w, map_h))
                # Ensure extent is in destination CRS units
                rect_out = rect
                try:
                    if layer.crs().isValid() and project.crs().isValid() and layer.crs() != project.crs():
                        ct = QgsCoordinateTransform(layer.crs(), project.crs(), project.transformContext())
                        rect_out = ct.transformBoundingBox(rect)
                except Exception:
                    rect_out = rect
                ms.setExtent(rect_out)
                ms.setDestinationCrs(project.crs() if project.crs().isValid() else layer.crs())
                ms.setTransformContext(project.transformContext())

                # Render map synchronously (more robust with XYZ/WMS tiles)
                # NOTE: Use CustomPainterJob instead of ParallelJob to avoid blank maps when network tiles are needed.
                img = QImage(map_w, map_h, QImage.Format.Format_ARGB32_Premultiplied)
                if img.isNull():
                    return None, None
                img.fill(QColor(255, 255, 255))
                qp = QPainter(img)
                try:
                    if not qp.isActive():
                        return None, None
                    job = QgsMapRendererCustomPainterJob(ms, qp)
                    job.start()
                    job.waitForFinished()
                finally:
                    # end() sempre, anche se il rendering solleva: senza questo il QImage
                    # restava agganciato a un painter attivo e Qt emetteva warning a raffica
                    if qp.isActive():
                        qp.end()
                    # rilascio esplicito del layer temporaneo (evita accumulo di memory layer)
                    try:
                        ms.setLayers([])
                    except Exception:
                        pass
                    if temp_layer is not None:
                        try:
                            temp_layer.deleteLater()
                        except Exception:
                            pass
                return img, ms.scale()

            def _draw_map_page(ft, page_no, total):
                rec_id = _feature_label(ft)
                painter.setFont(font_title)
                painter.drawText(margin, margin + title_h, f"Localizzazione {_friendly_layer_label()} ID {rec_id}")
                painter.setFont(font_sub)
                painter.drawText(margin, margin + title_h + subtitle_h, f"Record: {rec_id}")

                # map — un errore di rendering non deve interrompere l'intero export
                y0 = margin + title_h + subtitle_h + gap
                try:
                    img, scale = _render_map(ft)
                    err = None
                except Exception as e:
                    img, scale, err = None, None, e
                    _log(f"Rendering mappa fallito per il record {rec_id}: {e}", Qgis.Warning)

                if img is not None:
                    target = QRect(margin, y0, map_w, map_h)
                    painter.drawImage(target, img)
                    painter.setPen(QColor(0, 0, 0))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(target)
                    # scale text
                    if scale and scale > 0:
                        painter.setFont(QFont("Arial", 8))
                        painter.drawText(margin, y0 + map_h + mm(8), f"Scala 1:{int(scale):,}".replace(",", "."))
                elif err is not None:
                    painter.drawText(margin, y0 + mm(10), f"(Mappa non disponibile per questo record: {err})")
                else:
                    painter.drawText(margin, y0 + mm(10), "(Geometria assente: impossibile renderizzare la mappa)")

            def _doc_height(text, font, width_px):
                doc = QTextDocument()
                doc.setDefaultFont(font)
                doc.setTextWidth(float(width_px))
                doc.setPlainText(text)
                return doc, int(math.ceil(doc.size().height()))

            def _split_to_fit(text, font, width_px, max_h):
                # ritorna (prefix, rest) dove prefix sta in max_h
                if not text:
                    return "", ""
                lo, hi = 1, len(text)
                best = 0
                while lo <= hi:
                    mid = (lo + hi) // 2
                    cand = text[:mid]
                    _, h = _doc_height(cand, font, width_px)
                    if h <= max_h:
                        best = mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                if best <= 0:
                    best = min(200, len(text))
                cut = best
                # prova a tagliare su spazio
                for i in range(best, max(best - 80, 0), -1):
                    if text[i-1].isspace():
                        cut = i
                        break
                return text[:cut].rstrip(), text[cut:].lstrip()

            def _draw_attr_pages(ft):
                """Stampa la scheda attributi su quante pagine servono.

                Layout identico alla build approvata: qui cambia SOLO la logica di
                avanzamento pagina. Nella v13 il controllo "siamo a inizio pagina?"
                confrontava y con margin+header_h+mm(12), valore che l'header supera
                sempre (aggiunge l'intestazione di tabella): il ramo di spezzatura del
                testo non veniva mai raggiunto e un valore piu' alto di una pagina
                generava pagine all'infinito. Ora l'inizio pagina e' memorizzato.
                """
                rec_id = _feature_label(ft)
                bottom = page_h - margin
                state = {"y": table_top, "top": table_top}

                def header(page_idx_within_record):
                    painter.setFont(font_title)
                    painter.drawText(table_left, margin + header_h, f"Scheda attributi — {layer.name()}")
                    painter.setFont(font_sub)
                    painter.drawText(table_left, margin + header_h + mm(7), f"Record: {rec_id}  |  Pagina: {page_idx_within_record}")
                    y = margin + header_h + mm(12)

                    # intestazione tabella
                    colhdr_h = mm(8)
                    r1 = QRect(table_left, y, col1_w, colhdr_h)
                    r2 = QRect(table_left + col1_w, y, col2_w, colhdr_h)
                    painter.setPen(QColor(170, 170, 170))
                    painter.setBrush(QColor(235, 235, 235))
                    painter.drawRect(r1)
                    painter.drawRect(r2)
                    painter.setFont(QFont("Arial", 11, QFont.Weight.Bold))
                    painter.drawText(r1.adjusted(cell_pad, 0, -cell_pad, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Campo")
                    painter.drawText(r2.adjusted(cell_pad, 0, -cell_pad, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Valore")
                    y += colhdr_h + line_gap
                    # y di partenza reale della prima riga di questa pagina
                    state["y"] = y
                    state["top"] = y

                def draw_row(th_doc, td_doc, row_h):
                    y = state["y"]
                    r1 = QRect(table_left, y, col1_w, row_h)
                    r2 = QRect(table_left + col1_w, y, col2_w, row_h)
                    painter.setPen(QColor(208, 208, 208))
                    painter.setBrush(QColor(245, 245, 245))
                    painter.drawRect(r1)
                    painter.setBrush(QColor(255, 255, 255))
                    painter.drawRect(r2)

                    painter.save()
                    painter.translate(r1.left() + cell_pad, r1.top() + cell_pad)
                    th_doc.drawContents(painter, QRectF(0, 0, col1_w - 2 * cell_pad, row_h - 2 * cell_pad))
                    painter.restore()

                    painter.save()
                    painter.translate(r2.left() + cell_pad, r2.top() + cell_pad)
                    td_doc.drawContents(painter, QRectF(0, 0, col2_w - 2 * cell_pad, row_h - 2 * cell_pad))
                    painter.restore()

                    state["y"] = y + row_h + line_gap

                page_idx = 1
                header(page_idx)

                for fld in layer.fields():
                    label = fld.alias() or fld.name()
                    value = _safe(ft[fld.name()])

                    first_chunk = True
                    remaining = value
                    # guardia anti-loop: nessun campo puo' occupare piu' pagine di questa soglia
                    guard = 0
                    while True:
                        guard += 1
                        if guard > 500:
                            _log(
                                f"Campo '{label}' del record {rec_id}: testo troncato dopo 500 pagine "
                                "(protezione anti-loop).",
                                Qgis.Warning,
                            )
                            break

                        th_text = label if first_chunk else f"{label} (continua)"
                        th_doc, th_h = _doc_height(th_text, font_th, col1_w - 2 * cell_pad)
                        td_doc, td_h = _doc_height(remaining, font_td, col2_w - 2 * cell_pad)
                        row_h = max(th_h, td_h) + 2 * cell_pad

                        avail = bottom - state["y"]
                        at_page_start = state["y"] <= state["top"]

                        if row_h <= avail:
                            draw_row(th_doc, td_doc, row_h)
                            break

                        # Non ci sta e NON siamo a inizio pagina: proviamo su una pagina nuova
                        if not at_page_start:
                            writer.newPage()
                            page_idx += 1
                            header(page_idx)
                            continue

                        # Siamo a inizio pagina e il valore non ci sta comunque: spezzalo
                        max_cell_h = avail - 2 * cell_pad
                        prefix, rest = _split_to_fit(remaining, font_td, col2_w - 2 * cell_pad, max_cell_h)
                        if not prefix:
                            # fallback estremo: garantisce sempre un avanzamento
                            cut = min(200, len(remaining)) or len(remaining)
                            prefix, rest = remaining[:cut], remaining[cut:]
                        if not prefix:
                            break

                        th_doc, th_h = _doc_height(th_text, font_th, col1_w - 2 * cell_pad)
                        td_doc, td_h = _doc_height(prefix, font_td, col2_w - 2 * cell_pad)
                        row_h = min(max(th_h, td_h) + 2 * cell_pad, max(avail, 1))
                        draw_row(th_doc, td_doc, row_h)

                        remaining = rest
                        first_chunk = False
                        if not remaining:
                            break

                        writer.newPage()
                        page_idx += 1
                        header(page_idx)

            cancelled = False
            exported = 0
            try:
                for i, ft in enumerate(layer.getFeatures(req)):
                    if progress_callback is not None:
                        try:
                            if progress_callback(i, total_feats, _feature_label(ft)) is False:
                                cancelled = True
                                break
                        except Exception:
                            pass
                    if i > 0:
                        writer.newPage()
                    _draw_map_page(ft, i + 1, total_feats)
                    writer.newPage()
                    _draw_attr_pages(ft)
                    exported += 1
            finally:
                if painter.isActive():
                    painter.end()

            if cancelled and exported == 0:
                # The context closes the writer before the public wrapper
                # removes its temporary file. Never unlink an active writer.
                raise RuntimeError(_tf("Export annullato dall'utente", 'Export cancelled by the user'))

            return {"output": output_pdf, "records": exported, "cancelled": cancelled}

    # get_linked_gpkg/set_linked_gpkg sono definiti tra gli helper come alias
    # compatibili della sorgente dati attiva.

    # ----- elenco GeoPackage recenti (QSettings) -----
    MAX_RECENT_GPKG = 8

    def get_recent_gpkgs(self):
        s = QSettings()
        raw = s.value("magoh_spell/recent_gpkg", "", type=str) or ""
        out = []
        for p in raw.split("|"):
            p = p.strip()
            if p and p not in out and os.path.exists(p):
                out.append(p)
        return out

    def add_recent_gpkg(self, path: str):
        if not path:
            return
        recent = [p for p in self.get_recent_gpkgs() if os.path.normcase(p) != os.path.normcase(path)]
        recent.insert(0, path)
        recent = recent[: self.MAX_RECENT_GPKG]
        QSettings().setValue("magoh_spell/recent_gpkg", "|".join(recent))

    def clear_recent_gpkgs(self):
        QSettings().setValue("magoh_spell/recent_gpkg", "")

    # ---------- Report tool: layer loading helpers ----------
    def _layer_names_from_gpkg(self, gpkg_path: str):
        """Return table names from gpkg_contents."""
        if not gpkg_path or not os.path.exists(gpkg_path):
            return []
        conn = None
        try:
            conn = sqlite3.connect(gpkg_path)
            cur = conn.cursor()
            cur.execute("SELECT table_name FROM gpkg_contents ORDER BY table_name")
            rows = cur.fetchall()
            return [r[0] for r in rows if r and r[0]]
        except Exception as e:
            _log(f"Lettura gpkg_contents fallita ({gpkg_path}): {e}", Qgis.Warning)
            return []
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass


class MagohApiDialog(QDialog):
    """Import MAGOH SearchController -> template MAGOH Spell.

    V1: l'interfaccia espone solo ambito territoriale, entita' e log.
    La configurazione di connessione esistente viene preservata. L'import e' append-only:
    non aggiorna o sovrascrive mai un ID ufficiale gia' presente.
    """

    CORE_ORDER = ('intervento', 'ritrovamento', 'iv_livello')
    MATCH_FIELDS = {
        'intervento': ('ubicazione', 'anno', 'tipologia_intervento', 'sigla'),
        'ritrovamento': ('idscheda', 'i_livello', 'ii_livello', 'iii_livello', 'datazione', 'datazione_finale'),
        'iv_livello': ('idiii_livello', 'idintervento', 'iv_livello', 'datazione_iniziale', 'datazione_finale'),
    }

    def __init__(self, iface, plugin):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.plugin = plugin
        self.setWindowTitle(_tf('MAGOH API – Importa nel template', 'MAGOH API – Import into template'))
        self._client = None
        self._facet_cache = {}
        self._full_records = []
        self._full_start = 0
        self._query_plan = []
        self._query_index = 0
        self._examined_queries = 0
        self._query_totals = 0
        self._full_total = None
        self._active_filters = {}
        self._selected_entity_keys = []
        self._temp_target_layers = []
        self._busy = False
        self._filter_task = None
        self._generation = 0
        self._spatial_area = None
        self._spatial_records = {key: {} for key in ENTITY_SPECS}
        self._spatial_stats = {}
        self._spatial_ids = {'_match_ids': set(), '_inherited_ids': set(), '_dependency_ids': set()}
        self._seen_pages = set()
        self._import_gpkg = None
        self._writing = False
        # Stato del caricamento gerarchico Provincia -> Comune.
        # Non usiamo la faccetta `comune` globale: in alcune risposte Solr
        # facet_counts non viene ristretto dalla Provincia. I Comuni vengono
        # quindi ricavati dai documenti search-adv realmente filtrati.
        self._municipality_load = None

        main, footer = dialog_frame(
            self, _tf('Importa da MAGOH API', 'Import from MAGOH API'),
            _tf('Connessione in sola lettura: i record già presenti nel template non vengono sovrascritti.',
                'Read-only connection: records already in the template are never overwritten.'))

        note = QLabel(_tf(
            'Importa dal database MAGOH le schede esposte da <b>search-adv</b> nel template MAGOH Spell attivo. '
            'L’operazione è in sola lettura verso il server e non sovrascrive record già presenti nel template. '
            'Gli ID MAGOH ufficiali restano invariati; i nuovi record creati offline usano ID temporanei negativi.',
            'Import records exposed by <b>search-adv</b> from MAGOH into the active MAGOH Spell template. '
            'The server connection is read-only and existing template records are never overwritten. '
            'Official MAGOH IDs are preserved; new offline records use temporary negative IDs.'
        ))
        note.setWordWrap(True)
        note.setObjectName('MagohHint')
        main.addWidget(note)

        # --- Ambito territoriale gerarchico: prima Provincia, poi Comune.
        # La Provincia e' obbligatoria; il Comune e' opzionale e viene popolato
        # dalle faccette restituite da search-adv gia' filtrato sulla Provincia.
        scope_box = QGroupBox(_tf('Ambito da importare', 'Import scope'))
        self.scope_box = scope_box
        self.filter_mode = QComboBox()
        self.filter_mode.setProperty('magoh_translate_choices', True)
        self.filter_mode.addItem(_tf('Provincia / Comune', 'Province / Municipality'), 'admin')
        self.filter_mode.addItem(_tf('Provincia / Comune + area geografica', 'Province / Municipality + geographic area'), 'both')
        main.addWidget(self.filter_mode)
        scope_form = QFormLayout(scope_box)
        self.cmb_province = QComboBox()
        self.cmb_province.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_province.setMinimumContentsLength(24)
        self.cmb_comune = QComboBox()
        self.cmb_comune.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_comune.setMinimumContentsLength(28)
        self.cmb_comune.setEnabled(False)
        scope_form.addRow(_tf('Provincia', 'Province'), self.cmb_province)
        scope_form.addRow(_tf('Comune', 'Municipality'), self.cmb_comune)
        self.btn_retry_provinces = QPushButton(_tf('Ricarica Province', 'Reload Provinces'))
        self.btn_retry_provinces.setAutoDefault(False)
        self.btn_retry_provinces.clicked.connect(self._retry_provinces)
        scope_form.addRow('', self.btn_retry_provinces)
        hint_scope = QLabel(_tf(
            'Seleziona prima la Provincia. Puoi poi importare l’intera Provincia oppure restringere l’import a un Comune.',
            'Select the Province first. You can then import the whole Province or restrict the import to one Municipality.'
        ))
        hint_scope.setWordWrap(True)
        hint_scope.setObjectName('MagohHint')
        scope_form.addRow('', hint_scope)
        main.addWidget(scope_box)
        self.area_panel = ApiAreaPanel(self, _tf)
        main.addWidget(self.area_panel)
        self.area_panel.hide()
        self.filter_mode.currentIndexChanged.connect(self._mode_changed)

        # --- Entita' core importabili nella prima release.
        entity_box = QGroupBox(_tf('Schede da importare nel template', 'Records to import into template'))
        entity_layout = QVBoxLayout(entity_box)
        self.entity_checks = {}
        for key in self.CORE_ORDER:
            spec = CORE_IMPORT_SPECS[key]
            chk = QCheckBox(_t(spec['label']))
            chk.setChecked(True)
            self.entity_checks[key] = chk
            entity_layout.addWidget(chk)
        dep_note = QLabel(_tf(
            'Le relazioni vengono conservate con gli identificativi originali MAGOH. Se importi un livello figlio senza il padre, '
            'il padre deve essere già presente nel template; in caso contrario il record viene saltato e segnalato nel log.',
            'Relationships preserve original MAGOH identifiers. If you import a child level without its parent, the parent must '
            'already exist in the template; otherwise the record is skipped and reported in the log.'
        ))
        dep_note.setWordWrap(True)
        dep_note.setObjectName('MagohHint')
        entity_layout.addWidget(dep_note)
        self.chk_preview_layers = QCheckBox(_tf(
            'Carica anche una copia di controllo', 'Also load a review copy'
        ))
        self.chk_preview_layers.setToolTip(_tf(
            'Carica anche una copia di controllo come layer esterni nel gruppo “MAGOH – API”',
            'Also load a review copy as external layers in the “MAGOH – API” group'
        ))
        self.chk_preview_layers.setChecked(False)
        entity_layout.addWidget(self.chk_preview_layers)
        main.addWidget(entity_box)

        future = QLabel(_tf(
            'Questa release scrive solo Intervento, Ritrovamento ed Evidenza archeologica. Sepolture, dati antropologici, '
            'Documentazione e Bibliografia restano già leggibili dal parser API e sono predisposti per future destinazioni nel template.',
            'This release writes only Intervention, Finding and Archaeological Evidence records. Burials, anthropological data, '
            'Documentation and Bibliography are already readable by the API parser and are reserved for future template destinations.'
        ))
        future.setWordWrap(True)
        future.setObjectName('MagohHint')
        main.addWidget(future)

        # V1 uses the existing connection configuration without exposing it.

        # --- Log sempre visibile: e' il principale strumento diagnostico.
        log_box = QGroupBox(_tf('Log importazione', 'Import log'))
        log_layout = QVBoxLayout(log_box)
        self.preview = QTextBrowser()
        self.preview.setMinimumHeight(145)
        self.preview.setPlainText(_tf(
            'Scegli il filtro e le schede, poi avvia l’import. La cache conserva i record scaricati; in modalità spaziale solo quelli selezionati e i padri necessari.',
            'Choose filters and record types, then start importing. The cache stores downloaded records; in spatial mode only selected records and required parents.'
        ))
        log_layout.addWidget(self.preview)
        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(0)
        self.download_progress.setMaximumWidth(120)
        self.download_progress.hide()
        main.addWidget(log_box, 1)

        row = footer
        row.addWidget(self.download_progress)
        self.btn_import = QPushButton(_tf('Importa nel template MAGOH', 'Import into MAGOH template'))
        set_role(self.btn_import, primary=True)
        self.btn_import.clicked.connect(self._start_import)
        self.btn_import.setDefault(True)
        b_close = QPushButton(_t('Chiudi'))
        b_close.setAutoDefault(False)
        b_close.clicked.connect(self.hide)
        self.btn_cancel = QPushButton(_tf('Interrompi', 'Cancel import'))
        self.btn_cancel.setAutoDefault(False)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_import)
        row.addStretch(1)
        row.addWidget(self.btn_cancel)
        row.addWidget(b_close)
        row.addWidget(self.btn_import)

        self.cmb_province.currentIndexChanged.connect(self._province_changed)
        apply_teal_style(self)
        fit_dialog_to_parent(self, iface.mainWindow(), preferred_width=600, preferred_height=620)
        QTimer.singleShot(0, self._load_provinces)

    # ------------------------------------------------------------------
    # Configurazione / filtri / faccette
    # ------------------------------------------------------------------
    def _settings_from_ui(self):
        return MagohApiSettings.load()

    def _mode_changed(self, *_args):
        spatial = self.filter_mode.currentData() != 'admin'
        self.scope_box.setVisible(True)
        self.area_panel.setVisible(spatial)

    def _cancel_import(self):
        if self._writing:
            return  # The existing write/preparation phase has its own cancellation.
        self._generation += 1
        self.area_panel.cancel_task()
        if self._client is not None:
            self._client.cancel()
        if self._filter_task is not None:
            self._filter_task.cancel()
            self._filter_task = None
        self._municipality_load = None
        self._full_records = []
        self._spatial_records = {key: {} for key in ENTITY_SPECS}
        if self._busy:
            self._append_status(_tf('Importazione interrotta prima della scrittura.', 'Import cancelled before writing.'))
        self._set_busy(False)
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(0)

    def hideEvent(self, event):
        if not self.area_panel.drawing:
            self._cancel_import()
        super().hideEvent(event)

    def dispose(self):
        self._cancel_import()
        self.area_panel.dispose()

    def _filters_from_ui(self):
        province = str(self.cmb_province.currentData() or '').strip()
        comune = str(self.cmb_comune.currentData() or '').strip()
        filters = {}
        if province:
            filters['provincia'] = province
        if comune:
            filters['comune'] = comune
        return filters

    def _ensure_client(self, cfg):
        if self._client is not None:
            self._client.cancel()
            self._client.deleteLater()
        self._client = MagohApiClient(cfg, self)
        self._client.succeeded.connect(self._api_ok)
        self._client.failed.connect(self._api_error)

    def _set_busy(self, busy):
        self._busy = bool(busy)
        self.download_progress.setVisible(busy)
        self.btn_import.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy and not self._writing)
        self.filter_mode.setEnabled(not busy)
        self.area_panel.setEnabled(not busy)
        self.chk_preview_layers.setEnabled(not busy)
        self.btn_retry_provinces.setEnabled(not busy)
        self.cmb_province.setEnabled(not busy)
        self.cmb_comune.setEnabled(not busy and bool(self.cmb_province.currentData()))
        for chk in self.entity_checks.values():
            chk.setEnabled(not busy)

    def _fill_combo_values(self, combo, values, placeholder=None, first_item=None):
        current = str(combo.currentData() or '').strip()
        clean = sorted({str(v).strip() for v in values if str(v).strip()}, key=lambda x: x.casefold())
        if combo is self.cmb_province:
            clean = [v for v in clean if v.casefold() != 'trapani']
        combo.blockSignals(True)
        combo.setEditable(False)
        combo.clear()
        if placeholder is not None:
            combo.addItem(placeholder, '')
        if first_item is not None:
            combo.addItem(first_item, '')
        for value in clean:
            combo.addItem(value, value)
        if current:
            i = combo.findData(current)
            if i >= 0:
                combo.setCurrentIndex(i)
        combo.blockSignals(False)
        return bool(clean)

    def _retry_provinces(self):
        if self._busy:
            return
        self._facet_cache.clear()
        self._municipality_load = None
        self._load_provinces()

    def _load_provinces(self):
        if self._busy:
            return
        cached = self._facet_cache.get('provincia') or []
        if cached:
            self._fill_combo_values(
                self.cmb_province, cached,
                placeholder=_tf('Seleziona Provincia…', 'Select Province…')
            )
            self._province_changed()
            return
        self.cmb_province.blockSignals(True)
        self.cmb_province.clear()
        self.cmb_province.addItem(_tf('Caricamento Province…', 'Loading Provinces…'), '')
        self.cmb_province.blockSignals(False)
        self.cmb_province.setEnabled(False)
        self.cmb_comune.clear()
        self.cmb_comune.addItem(_tf('Seleziona prima la Provincia', 'Select Province first'), '')
        self.cmb_comune.setEnabled(False)
        # Reading public menus must not prompt for a legacy credential migration.
        # Authenticated requests migrate credentials in MagohApiClient instead.
        cfg = self._settings_from_ui()
        self._ensure_client(cfg)
        self._client.get_json('facet::provincia', cfg.facet_url('provincia'))

    def _province_changed(self, *_args):
        if self._busy:
            return
        province = str(self.cmb_province.currentData() or '').strip()
        self.cmb_comune.blockSignals(True)
        self.cmb_comune.clear()
        if not province:
            self.cmb_comune.addItem(_tf('Seleziona prima la Provincia', 'Select Province first'), '')
            self.cmb_comune.setEnabled(False)
            self.cmb_comune.blockSignals(False)
            self._municipality_load = None
            return
        cache_key = 'comune::' + province.casefold()
        cached = self._facet_cache.get(cache_key) or []
        if cache_key in self._facet_cache:
            self.cmb_comune.blockSignals(False)
            self._fill_combo_values(
                self.cmb_comune, cached,
                first_item=_tf('Tutti i Comuni della Provincia', 'All Municipalities in Province')
            )
            self.cmb_comune.setEnabled(True)
            return
        self.cmb_comune.addItem(_tf('Caricamento Comuni…', 'Loading Municipalities…'), '')
        self.cmb_comune.setEnabled(False)
        self.cmb_comune.blockSignals(False)
        cfg = self._settings_from_ui()
        self._ensure_client(cfg)
        # IMPORTANTE: get-facet(comune) e facet_counts di alcune build Solr
        # possono restituire il vocabolario globale. Per una relazione
        # Provincia -> Comune affidabile ricaviamo i Comuni direttamente dalle
        # Schede di Intervento restituite da search-adv con filtro provincia,
        # paginando fino a numFound.
        self._municipality_load = {
            'province': province, 'start': 0, 'total': None, 'values': set(), 'seen_pages': set()
        }
        self._request_municipality_page()

    def _request_municipality_page(self):
        state = self._municipality_load or {}
        province = str(state.get('province') or '').strip()
        if not province:
            return
        cfg = self._settings_from_ui()
        rows = max(200, min(1000, int(cfg.page_size or 200)))
        start = int(state.get('start') or 0)
        self._client.get_json(
            f'municipalities::{province}::{start}',
            cfg.search_url({'provincia': province}, start=start, rows=rows)
        )

    @staticmethod
    def _municipality_from_doc(doc):
        if not isinstance(doc, dict):
            return ''
        for key in ('comune', 'comune_s', 'nome_comune'):
            value = doc.get(key)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ''
            txt = str(value or '').strip()
            if txt:
                return txt
        return ''

    def _facet_fallback(self, kind):
        cfg = self._settings_from_ui()
        url = cfg.search_url({}, start=0, rows=1)
        self._ensure_client(cfg)
        self._client.get_json('facet_search::' + kind, url)

    # ------------------------------------------------------------------
    # Download search-adv
    # ------------------------------------------------------------------
    def _assert_import_targets_stable(self, gpkg):
        """Check every loaded view, including duplicate views of a core table.

        A subset can hide existing IDs during duplicate detection. Do not clear
        user filters or discard edit buffers as a side effect of API import.
        """
        expected = os.path.normcase(os.path.realpath(gpkg))
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            path, table = self.plugin._split_gpkg_source(layer.source())
            if not path or table not in self.plugin.CORE_MAGOH_TABLES:
                continue
            if os.path.normcase(os.path.realpath(path)) != expected:
                continue
            if layer.isEditable():
                raise RuntimeError(_tf(
                    'Prima dell’import salva o annulla le modifiche del layer «{layer}». Nessun record è stato scritto.',
                    'Save or cancel edits on «{layer}» before importing. No records have been written.',
                    layer=layer.name()
                ))
            if layer.subsetString():
                raise RuntimeError(_tf(
                    'Prima dell’import rimuovi i filtri del layer «{layer}» (Mostra tutti per i filtri del gestore). '
                    'I filtri possono nascondere ID già presenti. Nessun record è stato scritto.',
                    'Remove filters on «{layer}» before importing (Show all for management filters). '
                    'Filters can hide existing IDs. No records have been written.',
                    layer=layer.name()
                ))

    def _start_import(self):
        gpkg = self.plugin.get_active_gpkg()
        ok, missing = self.plugin._validate_magoh_gpkg(gpkg) if gpkg else (False, [])
        if not ok:
            QMessageBox.warning(self, 'MAGOH API', _tf(
                'Apri prima un progetto MAGOH Spell valido.',
                'Open a valid MAGOH Spell project first.'
            ))
            return
        # L'import deve partire da uno stato stabile: non mescoliamo i record
        # API con modifiche locali ancora nell'edit buffer dei layer core.
        try:
            self._assert_import_targets_stable(gpkg)
        except RuntimeError as exc:
            QMessageBox.warning(self, 'MAGOH API', str(exc))
            return
        selected = [key for key in self.CORE_ORDER if self.entity_checks[key].isChecked()]
        if not selected:
            QMessageBox.information(self, 'MAGOH API', _tf(
                'Seleziona almeno un tipo di scheda da importare.',
                'Select at least one record type to import.'
            ))
            return
        filters = self._filters_from_ui()
        spatial = self.filter_mode.currentData() != 'admin'
        if not filters.get('provincia'):
            QMessageBox.information(self, 'MAGOH API', _tf(
                'Seleziona prima la Provincia da importare.',
                'Select the Province to import first.'
            ))
            return

        if spatial and self.area_panel.area is None:
            QMessageBox.information(self, 'MAGOH API', _tf('Definisci prima l’area geografica.', 'Define the geographic area first.'))
            return
        fast_admin = spatial and self.area_panel.fast_admin.isChecked()

        cfg = self._settings_from_ui(); cfg.save()
        self._run_settings = cfg
        if spatial:
            self._run_settings.page_size = min(200, cfg.page_size)
        self._generation += 1
        self._import_gpkg = os.path.normcase(os.path.realpath(gpkg))
        self._spatial_area = QgsGeometry(self.area_panel.area) if spatial else None
        self._spatial_records = {key: {} for key in ENTITY_SPECS}
        self._spatial_stats = {}
        self._spatial_ids = {'_match_ids': set(), '_inherited_ids': set(), '_dependency_ids': set()}
        self._seen_pages = set()
        self._requested_entity_keys = list(selected)
        self._include_parents = spatial and self.area_panel.include_parents.isChecked()
        self._municipality_load = None
        self._active_filters = filters
        self._original_filters = dict(filters)
        self._query_plan = [dict(filters)]
        self._query_index = 0
        self._examined_queries = 0
        self._query_totals = 0
        self._fast_admin = fast_admin
        self._selected_entity_keys = selected
        if self._include_parents:
            self._selected_entity_keys = list(self.CORE_ORDER)
        self._full_records = []
        self._full_start = 0
        self._full_total = None
        self._set_busy(True)
        self.preview.setPlainText(_tf(
            'Download MAGOH tramite search-adv avviato. Il server viene interrogato esclusivamente in GET; nessun dato viene inviato a MAGOH.',
            'MAGOH download via search-adv started. The server is queried using GET only; no data are sent to MAGOH.'
        ))
        if filters:
            self._append_status(_tf('Filtro: {f}', 'Filter: {f}', f=', '.join(f'{k}={v}' for k, v in filters.items())))
        else:
            self._append_status(_tf('Filtro: nessuno (tutto MAGOH)', 'Filter: none (all MAGOH)'))
        self._append_status(_tf('Schede selezionate: {s}', 'Selected records: {s}', s=', '.join(CORE_IMPORT_SPECS[k]['label'] for k in selected)))
        self._ensure_client(cfg)
        if fast_admin and not filters.get('comune'):
            self._append_status(_tf('Verifica dei nomi API per l’indice geografico…', 'Checking API names for the geographic index…'))
            self._client.get_json('index::municipalities', cfg.facet_url('comune'))
        else:
            self._next_page()

    def _begin_indexed_queries(self, payload):
        if not self._busy:
            return
        try:
            if not isinstance(payload, dict) or not isinstance(payload.get('terms'), list):
                raise ValueError('Municipality facet unavailable')
            names = [term['name'] for term in payload['terms']
                     if isinstance(term, dict) and isinstance(term.get('name'), str)]
            self._query_plan, reason = plan_queries(
                self._spatial_area, QgsProject.instance().transformContext(), self._original_filters,
                self._facet_cache.get('provincia', []), names)
            if reason.startswith('widened_provinces='):
                explanation = _tf('ricerca allargata a {n} province per confini o nomi incompleti',
                                  'search widened to {n} provinces for incomplete boundaries or names',
                                  n=reason.split('=', 1)[1])
            else:
                explanation = _tf('ricerca completa nel filtro scelto: indice non sufficiente per restringerla',
                                  'complete search within the chosen filter: index insufficient to narrow it')
            self._append_status(_tf('Indice locale: {n} interrogazioni; {reason}.',
                                    'Local index: {n} queries; {reason}.', n=len(self._query_plan), reason=explanation))
        except Exception as exc:
            self._query_plan = [dict(self._original_filters)]
            self._append_status(_tf('Indice non utilizzabile: ricerca completa nel filtro scelto. {e}',
                                    'Index unavailable: complete search within the chosen filter. {e}', e=str(exc)))
        self._active_filters = self._query_plan[0]
        if self._query_plan != [self._original_filters]:
            self._append_status(_tf(
                'Ricerca rapida attiva: schede attribuite ad altri Comuni possono non essere trovate. '
                'Disattiva l’opzione per verificare la completezza.',
                'Fast search active: records assigned to other Municipalities may not be found. '
                'Disable this option to check completeness.'))
        self._next_page()

    def _search_kind(self):
        if len(self._query_plan) > 1:
            return f'search::{self._query_index}::{self._full_start}'
        return f'search::{self._full_start}'

    def _complete_query(self):
        self._examined_queries += self._full_start
        if self._query_totals is not None:
            self._query_totals = (self._query_totals + self._full_total
                                  if self._full_total is not None else None)
        if self._query_index + 1 < len(self._query_plan):
            self._query_index += 1
            self._active_filters = self._query_plan[self._query_index]
            self._full_start, self._full_total = 0, None
            self._seen_pages = set()
            self._next_page()
        else:
            self._full_start, self._full_total = self._examined_queries, self._query_totals
            self._active_filters = dict(getattr(self, '_original_filters', self._active_filters))
            self._finish_download()

    def _next_page(self):
        if not self._busy:
            return
        cfg = self._run_settings
        url = cfg.search_url(self._active_filters, start=self._full_start, rows=cfg.page_size)
        if len(self._query_plan) > 1 and self._full_start == 0:
            self._append_status(_tf('Ambito {i}/{n}: {f}', 'Scope {i}/{n}: {f}',
                                    i=self._query_index + 1, n=len(self._query_plan), f=str(self._active_filters)))
        self._append_status(_tf('search-adv: record da {start}…', 'search-adv: records from {start}…', start=self._full_start))
        self._client.get_json(self._search_kind(), url)

    def _api_ok(self, kind, payload):
        kind = str(kind)
        if kind == 'index::municipalities':
            self._begin_indexed_queries(payload)
            return
        if kind.startswith('facet::') or kind.startswith('facet_search::'):
            facet = kind.split('::', 1)[1]
            values = MagohApiClient.extract_facet_values(payload, facet)
            if values:
                self._facet_cache[facet] = values
                if facet == 'provincia':
                    self._fill_combo_values(
                        self.cmb_province, values,
                        placeholder=_tf('Seleziona Provincia…', 'Select Province…')
                    )
                    self.cmb_province.setEnabled(not self._busy)
                    self._province_changed()
                return
            if kind.startswith('facet::'):
                self._facet_fallback(facet)
                return
            if facet == 'provincia':
                self.cmb_province.clear()
                self.cmb_province.addItem(_tf('Province non disponibili', 'Provinces unavailable'), '')
                self.cmb_province.setEnabled(False)
                self._append_status(_tf(
                    'AVVISO: impossibile ottenere l’elenco Province dalle faccette API.',
                    'WARNING: could not retrieve the Province list from API facets.'
                ))
            return

        if kind.startswith('municipalities::'):
            parts = kind.split('::', 2)
            province = parts[1] if len(parts) > 1 else ''
            try:
                start = int(parts[2]) if len(parts) > 2 else 0
            except Exception:
                start = 0
            state = self._municipality_load or {}
            # Una risposta lenta di una Provincia selezionata in precedenza non
            # deve contaminare il menu della Provincia corrente.
            if str(state.get('province') or '') != province:
                return
            docs = MagohApiClient.extract_search_docs(payload)
            total = MagohApiClient.extract_search_total(payload)
            response = payload.get('response', payload) if isinstance(payload, dict) else {}
            try:
                valid_offset = int(response.get('start', start)) == start
            except (TypeError, ValueError, AttributeError):
                valid_offset = False
            if (not valid_offset or (not docs and total is None) or (total is not None and total < 0)
                    or (state.get('total') is not None and total is not None and total != state['total'])):
                self._api_error(kind, _tf('Paginazione dell’elenco Comuni non valida.', 'Invalid Municipality list pagination.'))
                return
            fingerprint = hashlib.sha256(json.dumps(docs, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
            seen = state.setdefault('seen_pages', set())
            if docs and fingerprint in seen:
                self._api_error(kind, _tf('Pagina ripetuta nell’elenco Comuni.', 'Repeated Municipality list page.'))
                return
            seen.add(fingerprint)
            values = state.setdefault('values', set())
            for doc in docs:
                comune = self._municipality_from_doc(doc)
                if comune:
                    values.add(comune)
            if state.get('total') is None and total is not None:
                state['total'] = int(total)
            cfg = self._settings_from_ui()
            rows = max(200, min(1000, int(cfg.page_size or 200)))
            consumed = start + len(docs)
            expected = state.get('total')
            if not docs and expected is not None and consumed < int(expected):
                self._api_error(kind, _tf('Pagina vuota prima della fine dell’elenco Comuni.', 'Empty page before the end of the Municipality list.'))
                return
            more = consumed < int(expected) if expected is not None else len(docs) >= rows
            if more and docs:
                state['start'] = consumed
                self._municipality_load = state
                self._request_municipality_page()
                return

            clean = sorted({str(v).strip() for v in values if str(v).strip()}, key=str.casefold)
            cache_key = 'comune::' + province.casefold()
            self._facet_cache[cache_key] = clean
            current_province = str(self.cmb_province.currentData() or '').strip()
            if current_province == province:
                self._fill_combo_values(
                    self.cmb_comune, clean,
                    first_item=_tf('Tutti i Comuni della Provincia', 'All Municipalities in Province')
                )
                self.cmb_comune.setEnabled(not self._busy)
                self._append_status(_tf(
                    '{n} Comuni caricati per la Provincia {p}.',
                    '{n} Municipalities loaded for Province {p}.',
                    n=len(clean), p=province
                ))
                if not clean:
                    self._append_status(_tf(
                        'AVVISO: nessun Comune restituito per la Provincia {p}; resta disponibile l’import dell’intera Provincia.',
                        'WARNING: no Municipality returned for Province {p}; importing the whole Province remains available.',
                        p=province
                    ))
            self._municipality_load = None
            return

        if not kind.startswith('search::') or not self._busy:
            return
        if kind != self._search_kind():
            return
        docs = MagohApiClient.extract_search_docs(payload)
        total = MagohApiClient.extract_search_total(payload)
        if self._full_total is None and total is not None:
            self._full_total = total
        response = payload.get('response', payload) if isinstance(payload, dict) else {}
        if isinstance(response, dict) and response.get('start') is not None:
            try:
                wrong_start = int(response['start']) != self._full_start
            except (ValueError, TypeError):
                wrong_start = True
            if wrong_start:
                self._api_error(kind, _tf('Offset di pagina API inatteso: import annullato.', 'Unexpected API page offset: import aborted.'))
                return
        if (not docs and total is None) or (total is not None and total < 0):
            self._api_error(kind, _tf('Risposta di ricerca incompleta o non valida.', 'Incomplete or invalid search response.'))
            return
        if not docs:
            if self._full_total is not None and self._full_start < self._full_total:
                self._api_error(kind, _tf('Pagina vuota prima della fine: import annullato.', 'Empty page before the end: import aborted.'))
                return
            self._complete_query()
            return
        fingerprint = hashlib.sha256(json.dumps(docs, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        if fingerprint in self._seen_pages:
            self._api_error(kind, _tf('Pagina API ripetuta: import annullato per evitare risultati incompleti.', 'Repeated API page: import aborted to avoid incomplete results.'))
            return
        self._seen_pages.add(fingerprint)
        if total is not None and total != self._full_total:
            self._api_error(kind, _tf('Il numero di record è cambiato durante la lettura. Riprova.', 'The record count changed while reading. Please retry.'))
            return
        consumed = len(docs)
        if self._spatial_area is not None:
            generation = self._generation
            def complete(exception, result):
                if generation != self._generation or not self._busy:
                    return
                self._filter_task = None
                if exception is not None or result is None:
                    self._api_error(kind, str(exception or _tf('Filtro interrotto.', 'Filter interrupted.')))
                    return
                flat, stats = result
                for key, records in flat.items():
                    for record in records:
                        ident = record_identity(record, key)
                        if ident in self._spatial_records[key]:
                            self._spatial_stats['duplicates'] = self._spatial_stats.get('duplicates', 0) + 1
                        self._spatial_records[key].setdefault(ident, record)
                for key, value in stats.items():
                    if key in self._spatial_ids:
                        self._spatial_ids[key].update(tuple(i) for i in value)
                    else:
                        self._spatial_stats[key] = self._spatial_stats.get(key, 0) + value
                self._spatial_stats['matches'] = len(self._spatial_ids['_match_ids'])
                self._spatial_stats['inherited_matches'] = len(self._spatial_ids['_inherited_ids'])
                self._spatial_stats['dependency_parents'] = len(self._spatial_ids['_dependency_ids'] - self._spatial_ids['_match_ids'])
                self._page_complete(consumed)
            self._filter_task = QgsTask.fromFunction(
                'MAGOH: spatial intersection', filter_page, docs, bytes(self._spatial_area.asWkb()),
                tuple(self._requested_entity_keys), self._include_parents, on_finished=complete)
            QgsApplication.taskManager().addTask(self._filter_task)
        else:
            self._full_records.extend(docs)
            self._page_complete(consumed)

    def _page_complete(self, consumed):
        self._full_start += consumed
        if self._full_total is not None:
            self.download_progress.setRange(0, max(1, self._full_total))
            self.download_progress.setValue(self._full_start)
        else:
            self.download_progress.setRange(0, 0)
        more = self._full_start < self._full_total if self._full_total is not None else consumed >= int(self._run_settings.page_size)
        if more:
            self._next_page()
        else:
            self._complete_query()

    def _api_error(self, kind, message):
        kind = str(kind)
        if kind == 'index::municipalities':
            self._begin_indexed_queries(None)
            return
        if kind.startswith('facet::'):
            self._append_status(_tf('AVVISO elenco Province (get-facet): {m}',
                                    'Province list warning (get-facet): {m}', m=message))
            self._facet_fallback(kind.split('::', 1)[1])
            return
        if kind.startswith('facet_search::'):
            facet = kind.split('::', 1)[1]
            if facet == 'provincia':
                self.cmb_province.clear()
                self.cmb_province.addItem(_tf('Errore caricamento Province — vedi log', 'Province loading error — see log'), '')
                self.cmb_province.setEnabled(False)
            self._append_status(_tf('AVVISO menu territoriale: {m}', 'Territorial menu warning: {m}', m=message))
            return
        if kind.startswith('municipalities::'):
            parts = kind.split('::', 2)
            province = parts[1] if len(parts) > 1 else ''
            if str(self.cmb_province.currentData() or '').strip() == province:
                self.cmb_comune.clear()
                self.cmb_comune.addItem(_tf('Tutti i Comuni della Provincia', 'All Municipalities in Province'), '')
                self.cmb_comune.setEnabled(True)
            self._municipality_load = None
            self._append_status(_tf(
                'AVVISO elenco Comuni per {p}: {m}. Puoi comunque importare l’intera Provincia.',
                'Municipality list warning for {p}: {m}. You can still import the whole Province.',
                p=province, m=message
            ))
            return
        self._append_status(_tf('ERRORE API: {m}', 'API ERROR: {m}', m=message))
        self._set_busy(False)

    # ------------------------------------------------------------------
    # Snapshot / normalizzazione / import nel template
    # ------------------------------------------------------------------
    @staticmethod
    def _slug(text):
        value = re.sub(r'[^A-Za-z0-9_-]+', '_', str(text or '')).strip('_')
        return value[:80] or 'tutto'

    def _snapshot_suffix(self):
        if not self._active_filters:
            return 'tutto'
        return self._slug('_'.join(f'{k}-{v}' for k, v in sorted(self._active_filters.items())))

    def _finish_download(self):
        try:
            self._check_import_destination()
        except RuntimeError as exc:
            self._api_error('search', str(exc))
            return
        records = list(self._full_records)
        flattened = ({key: list(values.values()) for key, values in self._spatial_records.items()}
                     if self._spatial_area is not None else flatten_search_docs(records))
        if self._spatial_area is not None:
            self._append_status(_tf(
                'Esaminati {n} interventi. Corrispondenze: {m}; per relazione senza geometria: {i}; '
                'padri di supporto: {p}; geometrie non valide escluse: {bad}; duplicati rimossi: {d}.',
                'Examined {n} interventions. Matches: {m}; inherited without geometry: {i}; '
                'supporting parents: {p}; invalid geometries excluded: {bad}; duplicates removed: {d}.',
                n=self._full_start, m=self._spatial_stats.get('matches', 0),
                i=self._spatial_stats.get('inherited_matches', 0), p=self._spatial_stats.get('dependency_parents', 0),
                bad=self._spatial_stats.get('invalid_geometry', 0), d=self._spatial_stats.get('duplicates', 0)))
            if not any(flattened.values()):
                message = _tf('Nessuna scheda selezionata interseca l’area (direttamente o tramite il padre).',
                              'No selected records intersect the area (directly or through their parent).')
                if getattr(self, '_fast_admin', False):
                    message += _tf(' Risultato limitato agli ambiti interrogati: riprova con la ricerca completa.',
                                   ' Result limited to queried scopes: retry with complete search.')
                self._append_status(message)
                QMessageBox.information(self, 'MAGOH API', message)
                self._set_busy(False)
                return
        cache = self._cache_dir()
        raw_path = os.path.join(cache, f'magoh_api_search_adv_import_{self._snapshot_suffix()}_{time.time_ns()}.json')
        snapshot = {'numFound': self._full_total, 'docs': records}
        if self._spatial_area is not None:
            snapshot = {'numFound': self._full_total, 'examined': self._full_start, 'records': flattened,
                        'spatialFilter': json.loads(self._spatial_area.asJson()), 'crs': 'EPSG:4326',
                        'administrativeFilters': self._active_filters, 'statistics': self._spatial_stats,
                        'administrativeQueryPlan': self._query_plan,
                        'fastAdministrativeSearch': getattr(self, '_fast_admin', False),
                        'includeRequiredParents': self._include_parents}
        try:
            with open(raw_path, 'w', encoding='utf-8') as f:
                json.dump(snapshot, f, ensure_ascii=False)
        except Exception as exc:
            self._append_status(_tf('ERRORE salvataggio JSON: {e}', 'ERROR saving JSON: {e}', e=exc))

        counts = entity_counts(flattened)
        self._append_status('')
        self._append_status(_tf(
            'Download completato: {n} Schede di Intervento{tot}.',
            'Download complete: {n} Intervention records{tot}.',
            n=len(flattened['intervento']), tot=(f' / {self._full_total}' if self._full_total is not None else '')
        ))
        for key in self.CORE_ORDER:
            c = counts.get(key, {'records': 0, 'with_geometry': 0})
            self._append_status(_tf('{label}: {n} record; {g} con geometria', '{label}: {n} records; {g} with geometry',
                                    label=_t(CORE_IMPORT_SPECS[key]['label']), n=c['records'], g=c['with_geometry']))

        if self.chk_preview_layers.isChecked():
            for key in self._selected_entity_keys:
                try:
                    self._save_load_entity(key, flattened.get(key, []))
                except Exception as exc:
                    self._append_status(_tf('AVVISO snapshot {k}: {e}', 'Snapshot warning {k}: {e}', k=key, e=exc))

        try:
            self._writing = True
            self.btn_cancel.setEnabled(False)
            self._import_into_template(flattened, raw_path)
        except Exception as exc:
            self._append_status(_tf('ERRORE IMPORT TEMPLATE: {e}', 'TEMPLATE IMPORT ERROR: {e}', e=exc))
            QMessageBox.critical(self, 'MAGOH API', str(exc))
            self._set_busy(False)
        finally:
            self._writing = False
            self._set_busy(False)
            self._temp_target_layers = []

    def _check_import_destination(self):
        current = self.plugin.get_active_gpkg()
        if self._import_gpkg and (not current or os.path.normcase(os.path.realpath(current)) != self._import_gpkg):
            raise RuntimeError(_tf('Il progetto di destinazione è cambiato: import annullato.',
                                   'The destination project changed: import aborted.'))

    def _label_imported_interventions(self, gpkg):
        if 'intervento' not in self._selected_entity_keys:
            return
        layer = self.plugin._layers_for_gpkg(gpkg).get('magoh_scheda_intervento')
        if layer is not None and not apply_intervention_labels(layer):
            self._append_status(_tf('Etichette non configurate: campo idscheda assente.',
                                    'Labels not configured: idscheda field missing.'))

    def _target_layer(self, lname):
        gpkg = self.plugin.get_active_gpkg()
        loaded = self.plugin._layers_for_gpkg(gpkg).get(lname) if gpkg else None
        if isinstance(loaded, QgsVectorLayer) and loaded.isValid():
            return loaded
        if not gpkg:
            return None
        lyr = QgsVectorLayer(f'{gpkg}|layername={lname}', lname, 'ogr')
        if lyr.isValid():
            self._temp_target_layers.append(lyr)
            return lyr
        return None

    @staticmethod
    def _plain(value):
        return MagohSpellPlugin._plain_value(value)

    def _existing_feature_map(self, layer, field):
        out = {}
        if not isinstance(layer, QgsVectorLayer) or layer.fields().indexOf(field) < 0:
            return out
        req = QgsFeatureRequest().setSubsetOfAttributes([field], layer.fields())
        for feat in layer.getFeatures(req):
            ident = tail_int(self._plain(feat[field]))
            if ident is not None and ident not in out:
                out[ident] = feat.id()
        return out

    @staticmethod
    def _cmp_value(value):
        if value is None:
            return ''
        textv = str(value).strip().replace('’', "'")
        return re.sub(r'\s+', ' ', textv).casefold()

    def _same_existing_record(self, entity_key, attrs, layer, fid):
        """Riconosce un record pre-v32.7 identico per evitare falsi conflitti.

        Non basta che l'ID coincida: confrontiamo almeno due campi sostanziali.
        Se un vecchio record locale positivo e' diverso, resta una collisione e
        non viene toccato automaticamente.
        """
        try:
            feat = layer.getFeature(int(fid))
        except Exception:
            return False
        compared = 0
        for name in self.MATCH_FIELDS.get(entity_key, ()):
            if layer.fields().indexOf(name) < 0:
                continue
            incoming = attrs.get(name)
            try:
                existing = feat[name]
            except Exception:
                existing = None
            a = self._cmp_value(incoming); b = self._cmp_value(self._plain(existing))
            if not a or not b:
                continue
            compared += 1
            if a != b:
                return False
        return compared >= 2

    def _registry_path(self):
        return os.path.join(self._cache_dir(), 'magoh_api_import_registry.json')

    def _load_registry(self):
        data = {'version': 1, 'entities': {key: [] for key in self.CORE_ORDER}}
        path = self._registry_path()
        try:
            if os.path.isfile(path):
                with open(path, 'r', encoding='utf-8') as registry_file:
                    obj = json.load(registry_file)
                if isinstance(obj, dict):
                    data.update(obj)
        except Exception:
            pass
        entities = data.setdefault('entities', {})
        return {key: set(str(x) for x in entities.get(key, [])) for key in self.CORE_ORDER}

    def _save_registry(self, registry):
        path = self._registry_path()
        payload = {
            'version': 1,
            'entities': {key: sorted(set(str(x) for x in registry.get(key, set())), key=lambda x: (len(x), x)) for key in self.CORE_ORDER},
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    def _create_preimport_backup(self, gpkg):
        """Crea un punto di ripristino prima di scrivere nel GeoPackage locale."""
        root = self.plugin._infer_project_root(gpkg)
        if root:
            backup_dir = os.path.join(root, 'backups')
        else:
            backup_dir = os.path.join(os.path.dirname(gpkg), 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        stamp = QDateTime.currentDateTime().toString('yyyyMMdd_HHmmss')
        base = os.path.splitext(os.path.basename(gpkg))[0]
        path = os.path.join(backup_dir, f'{base}_pre_API_import_{stamp}_{time.time_ns()}.gpkg')
        # SQLite backup also captures any committed WAL pages; copying only the
        # main file could produce an incomplete backup while QGIS holds it open.
        source = sqlite3.connect(Path(gpkg).resolve().as_uri() + '?mode=ro', uri=True)
        destination = sqlite3.connect(path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return path

    def _coerce_value(self, field, value):
        if value is None:
            return None
        t = field.type()
        try:
            if t in (QVariant.Int, QVariant.UInt, QVariant.LongLong, QVariant.ULongLong):
                return int(float(str(value).replace(',', '.')))
            if t == QVariant.Double:
                return float(str(value).replace(',', '.'))
            if t == QVariant.DateTime:
                if isinstance(value, QDateTime):
                    return value
                qdt = QDateTime.fromString(str(value), Qt.DateFormat.ISODate)
                if qdt.isValid():
                    return qdt
            if t == QVariant.Date:
                if isinstance(value, QDate):
                    return value
                txt = str(value)[:10]
                qd = QDate.fromString(txt, 'yyyy-MM-dd')
                if qd.isValid():
                    return qd
        except Exception:
            return value
        return value

    def _make_feature(self, layer, attrs, geom_obj):
        feat = QgsFeature(layer.fields())
        for field in layer.fields():
            name = field.name()
            if name.lower() == 'fid' or name not in attrs or attrs.get(name) is None:
                continue
            try:
                feat.setAttribute(name, self._coerce_value(field, attrs.get(name)))
            except Exception:
                pass

        note = None
        if layer.isSpatial() and geom_obj:
            # V1.19.1: lettore condiviso (api_geometry.py). Rispetta il membro
            # «crs» del GeoJSON e restituisce sempre il motivo di un rifiuto.
            geom, note, error = api_geometry_for_layer(geom_obj, layer, QgsProject.instance().transformContext())
            if geom is None:
                return None, error, None
            feat.setGeometry(geom)
        return feat, None, note

    def _import_into_template(self, flattened, raw_path):
        self._check_import_destination()
        gpkg = self.plugin.get_active_gpkg()
        self._assert_import_targets_stable(gpkg)
        lookups = TemplateLookups.from_gpkg(gpkg)
        registry = self._load_registry()

        # Apriamo sempre i tre livelli core: servono per verificare le relazioni
        # anche quando l'utente decide di importare soltanto un livello figlio.
        int_layer = self._target_layer('magoh_scheda_intervento')
        find_layer = self._target_layer('magoh_ritrovamenti')
        ev_layer = self._target_layer('magoh_evidenza_archeologica')
        ev_ng_layer = self._target_layer('magoh_evidenza_archeologica_no_geom')
        if not all(isinstance(x, QgsVectorLayer) and x.isValid() for x in (int_layer, find_layer, ev_layer, ev_ng_layer)):
            raise RuntimeError(_tf('Le tabelle core del template MAGOH non sono disponibili.', 'MAGOH core template tables are unavailable.'))

        layers_by_entity = {'intervento': int_layer, 'ritrovamento': find_layer}
        existing = {
            'intervento': self._existing_feature_map(int_layer, 'idscheda'),
            'ritrovamento': self._existing_feature_map(find_layer, 'id'),
        }
        ev_existing_a = self._existing_feature_map(ev_layer, 'id')
        ev_existing_b = self._existing_feature_map(ev_ng_layer, 'id')
        existing['iv_livello'] = dict(ev_existing_a)
        for k, v in ev_existing_b.items():
            existing['iv_livello'].setdefault(k, v)

        # Mappatura una volta sola. Conserviamo anche gli attributi dei padri non
        # selezionati per poter riconoscere record MAGOH gia' presenti nel template.
        mapped = {key: {} for key in self.CORE_ORDER}
        lookup_warnings = {
            'intervento': {}, 'ritrovamento': {}, 'iv_livello': {}
        }

        def note_lookup(entity, label, source_value, mapped_value):
            if source_value in (None, '', [], {} ) or mapped_value not in (None, ''):
                return
            bucket = lookup_warnings[entity].setdefault(label, {'count': 0, 'values': set()})
            bucket['count'] += 1
            if len(bucket['values']) < 8:
                bucket['values'].add(str(source_value))

        progress = QProgressDialog(_tf('Preparazione import MAGOH API…', 'Preparing MAGOH API import…'), _t('Annulla'), 0,
                                   sum(len(flattened.get(k, [])) for k in self.CORE_ORDER), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal); progress.setMinimumDuration(500)
        done = 0
        for key in self.CORE_ORDER:
            for record in flattened.get(key, []):
                if progress.wasCanceled():
                    progress.close(); self._set_busy(False); return
                attrs, geom = map_entity(key, record, lookups)
                if key == 'intervento':
                    note_lookup(key, 'Comune', record.get('comune'), attrs.get('id_comune'))
                    note_lookup(key, 'Indirizzo', record.get('indirizzo'), attrs.get('indirizzo'))
                    note_lookup(key, 'Toponimo', record.get('toponimo'), attrs.get('toponimo'))
                    note_lookup(key, 'OGD', record.get('intervento_ogd'), attrs.get('ogd'))
                    note_lookup(key, 'OGM', record.get('intervento_ogm'), attrs.get('ogm'))
                elif key == 'ritrovamento':
                    note_lookup(key, 'II livello', record.get('ritrovamento_ii_livello'), attrs.get('id_ii_livello'))
                    note_lookup(key, 'III livello', record.get('ritrovamento_iii_livello'), attrs.get('id_iii_livello'))
                    note_lookup(key, 'affidabilità quota', record.get('ritrovamento_affidabilita_quota'), attrs.get('id_affidabilita'))
                    note_lookup(key, 'cronologia iniziale', record.get('ritrovamento_cronologia_inizio'), attrs.get('crono_iniziale'))
                    note_lookup(key, 'cronologia finale', record.get('ritrovamento_cronologia_fine'), attrs.get('crono_finale'))
                elif key == 'iv_livello':
                    note_lookup(key, 'tipologia IV livello', record.get('iv_livello_iv_livello'), attrs.get('id_iv_livello'))
                    note_lookup(key, 'affidabilità quota', record.get('iv_livello_affidabilita_quota'), attrs.get('affidabilita'))
                    note_lookup(key, 'cronologia iniziale', record.get('iv_livello_cronologia_inizio'), attrs.get('crono_iniziale'))
                    note_lookup(key, 'cronologia finale', record.get('iv_livello_cronologia_fine'), attrs.get('crono_finale'))
                ident_field = CORE_IMPORT_SPECS[key]['id_field']
                ident = tail_int(attrs.get(ident_field))
                if ident is not None:
                    mapped[key][ident] = (attrs, geom, record)
                done += 1
                if done % 250 == 0:
                    progress.setValue(done); QApplication.processEvents()
        progress.setValue(progress.maximum()); progress.close()

        # Fid reali delle evidenze possono trovarsi in uno dei due layer split.
        def existing_layer_fid(entity, ident):
            if entity != 'iv_livello':
                layer = layers_by_entity[entity]
                fid = existing[entity].get(ident)
                return layer, fid
            if ident in ev_existing_a:
                return ev_layer, ev_existing_a[ident]
            if ident in ev_existing_b:
                return ev_ng_layer, ev_existing_b[ident]
            return None, None

        trusted = {key: set() for key in self.CORE_ORDER}
        collisions = {key: set() for key in self.CORE_ORDER}
        already = {key: 0 for key in self.CORE_ORDER}
        matched_old = {key: 0 for key in self.CORE_ORDER}

        # Classifica gli ID gia' presenti. Registry = record importato da questa
        # funzione; in sua assenza accettiamo come MAGOH solo un record che
        # coincida su almeno due campi sostanziali.
        for key in self.CORE_ORDER:
            for ident, (attrs, _geom, _record) in mapped[key].items():
                if ident not in existing[key]:
                    continue
                if str(ident) in registry[key]:
                    trusted[key].add(ident); already[key] += 1; continue
                lyr, fid = existing_layer_fid(key, ident)
                if lyr is not None and fid is not None and self._same_existing_record(key, attrs, lyr, fid):
                    trusted[key].add(ident); registry[key].add(str(ident)); matched_old[key] += 1
                else:
                    collisions[key].add(ident)

        selected = set(self._selected_entity_keys)
        prepared_by_table = {
            'magoh_scheda_intervento': [],
            'magoh_ritrovamenti': [],
            'magoh_evidenza_archeologica': [],
            'magoh_evidenza_archeologica_no_geom': [],
        }
        prepared_ids = {key: set() for key in self.CORE_ORDER}
        missing_parent = {key: 0 for key in self.CORE_ORDER}
        geom_errors = {key: 0 for key in self.CORE_ORDER}
        # V1.19.1: motivi dei rifiuti e ipotesi dichiarate, con ID di esempio.
        geom_reasons = {key: {} for key in self.CORE_ORDER}
        geom_notes = {key: {} for key in self.CORE_ORDER}

        def tally(bucket, entity, message, ident):
            entry = bucket[entity].setdefault(message, [0, []])
            entry[0] += 1
            if len(entry[1]) < 3:
                entry[1].append(str(ident))

        # Interventi
        for ident, (attrs, geom, _record) in mapped['intervento'].items():
            if 'intervento' not in selected or ident in existing['intervento']:
                continue
            feat, err, note = self._make_feature(int_layer, attrs, geom)
            if feat is None:
                geom_errors['intervento'] += 1; tally(geom_reasons, 'intervento', err, ident); continue
            if note:
                tally(geom_notes, 'intervento', note, ident)
            prepared_by_table['magoh_scheda_intervento'].append(feat)
            prepared_ids['intervento'].add(ident)
        available_interventions = trusted['intervento'] | prepared_ids['intervento']

        # Ritrovamenti
        for ident, (attrs, geom, _record) in mapped['ritrovamento'].items():
            if 'ritrovamento' not in selected or ident in existing['ritrovamento']:
                continue
            parent = tail_int(attrs.get('idscheda'))
            if parent is None or parent not in available_interventions:
                missing_parent['ritrovamento'] += 1; continue
            feat, err, note = self._make_feature(find_layer, attrs, geom)
            if feat is None:
                geom_errors['ritrovamento'] += 1; tally(geom_reasons, 'ritrovamento', err, ident); continue
            if note:
                tally(geom_notes, 'ritrovamento', note, ident)
            prepared_by_table['magoh_ritrovamenti'].append(feat)
            prepared_ids['ritrovamento'].add(ident)
        available_findings = trusted['ritrovamento'] | prepared_ids['ritrovamento']

        # Evidenze IV: la destinazione viene scelta in base alla presenza della geometria.
        for ident, (attrs, geom, _record) in mapped['iv_livello'].items():
            if 'iv_livello' not in selected or ident in existing['iv_livello']:
                continue
            parent_int = tail_int(attrs.get('idintervento'))
            parent_find = tail_int(attrs.get('idiii_livello'))
            if parent_int is None or parent_int not in available_interventions or parent_find is None or parent_find not in available_findings:
                missing_parent['iv_livello'] += 1; continue
            target = ev_layer if geom else ev_ng_layer
            tname = 'magoh_evidenza_archeologica' if geom else 'magoh_evidenza_archeologica_no_geom'
            feat, err, note = self._make_feature(target, attrs, geom)
            if feat is None:
                geom_errors['iv_livello'] += 1; tally(geom_reasons, 'iv_livello', err, ident); continue
            if note:
                tally(geom_notes, 'iv_livello', note, ident)
            prepared_by_table[tname].append(feat)
            prepared_ids['iv_livello'].add(ident)

        # Collisioni dei padri non devono mai far collegare i figli alla scheda
        # locale sbagliata: poiche' non sono in ``trusted``, i figli risultano
        # automaticamente tra i missing_parent e vengono saltati.
        new_counts = {
            'intervento': len(prepared_ids['intervento']),
            'ritrovamento': len(prepared_ids['ritrovamento']),
            'iv_livello': len(prepared_ids['iv_livello']),
        }
        total_new = sum(new_counts.values())
        self._append_status('')
        self._append_status(_tf('Pre-verifica import nel template:', 'Template import pre-check:'))
        for key in self.CORE_ORDER:
            if key not in selected:
                continue
            self._append_status(
                f"  {CORE_IMPORT_SPECS[key]['label']}: nuovi={new_counts[key]}, gia_presenti={already[key] + matched_old[key]}, "
                f"collisioni_non_sicure={len(collisions[key])}, padre_mancante={missing_parent[key]}, errori_geometria={geom_errors[key]}"
            )
            for message, (count, samples) in sorted(geom_reasons[key].items(), key=lambda x: -x[1][0]):
                self._append_status(_tf('    geometria rifiutata ({n}): {m}. ID di esempio: {ids}',
                                        '    geometry rejected ({n}): {m}. Sample IDs: {ids}',
                                        n=count, m=message, ids=', '.join(samples)))
            for message, (count, samples) in sorted(geom_notes[key].items(), key=lambda x: -x[1][0]):
                self._append_status(_tf('    nota ({n}): {m}. ID di esempio: {ids}',
                                        '    note ({n}): {m}. Sample IDs: {ids}',
                                        n=count, m=message, ids=', '.join(samples)))
        if missing_parent['ritrovamento'] or missing_parent['iv_livello']:
            self._append_status(_tf(
                '  padre_mancante: la scheda padre non è nel template né tra i record importabili '
                '(se il padre ha una geometria rifiutata, il motivo è indicato sopra).',
                '  padre_mancante: the parent record is neither in the template nor importable '
                '(if the parent geometry was rejected, the reason is listed above).'))

        warning_lines = []
        for key in self.CORE_ORDER:
            if key not in selected:
                continue
            for label, info in lookup_warnings.get(key, {}).items():
                vals = ', '.join(sorted(info['values'], key=lambda x: x.casefold()))
                warning_lines.append(f"  {CORE_IMPORT_SPECS[key]['label']} – {label}: {info['count']} valore/i senza codice nel thesaurus locale ({vals})")
        if warning_lines:
            self._append_status(_tf(
                'AVVISO thesauri: il testo originale viene conservato, ma per i seguenti valori il codice lookup resta vuoto:',
                'Thesaurus warning: original text is preserved, but the lookup code remains empty for these values:'
            ))
            for line in warning_lines:
                self._append_status(line)

        if not total_new:
            self._append_status(_tf('Nessun nuovo record valido da scrivere nel template.', 'No new valid records to write into the template.'))
            self._save_registry(registry)
            self._label_imported_interventions(gpkg)
            self._set_busy(False)
            return

        question = _tf(
            'Importare {n} nuovi record nel template MAGOH?\n\nInterventi: {i}\nRitrovamenti: {r}\nEvidenze: {e}\n\n'
            'Gli ID già presenti non saranno sovrascritti. Le collisioni non riconoscibili come dati MAGOH vengono saltate.',
            'Import {n} new records into the MAGOH template?\n\nInterventions: {i}\nFindings: {r}\nEvidence: {e}\n\n'
            'Existing IDs will not be overwritten. Collisions that cannot be recognized as MAGOH data are skipped.',
            n=total_new, i=new_counts['intervento'], r=new_counts['ritrovamento'], e=new_counts['iv_livello']
        )
        answer = QMessageBox.question(self, 'MAGOH API', question, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self._append_status(_tf('Import annullato dall’utente; snapshot JSON conservato: {p}', 'Import cancelled by user; JSON snapshot kept: {p}', p=raw_path))
            self._set_busy(False)
            return

        # Download and confirmation run through the event loop: the user may
        # have started editing since _start_import's initial safety check.
        self._check_import_destination()
        self._assert_import_targets_stable(gpkg)

        try:
            backup_path = self._create_preimport_backup(gpkg)
            self._append_status(_tf(
                'Backup automatico pre-import creato: {p}',
                'Automatic pre-import backup created: {p}', p=backup_path
            ))
        except Exception as exc:
            raise RuntimeError(_tf(
                'Backup di sicurezza non riuscito; import non avviato: {e}',
                'Safety backup failed; import not started: {e}', e=exc
            )) from exc

        target_by_table = {
            'magoh_scheda_intervento': int_layer,
            'magoh_ritrovamenti': find_layer,
            'magoh_evidenza_archeologica': ev_layer,
            'magoh_evidenza_archeologica_no_geom': ev_ng_layer,
        }
        states = {}
        try:
            for table_name, feats in prepared_by_table.items():
                if not feats:
                    continue
                layer = target_by_table[table_name]
                started_here = False
                if not layer.isEditable():
                    if not layer.startEditing():
                        raise RuntimeError(_tf('Impossibile avviare la modifica di {l}.', 'Could not start editing {l}.', l=layer.name()))
                    started_here = True
                try:
                    layer.beginEditCommand(_tf('Import MAGOH API', 'MAGOH API import'))
                except Exception:
                    pass
                states[table_name] = (layer, started_here)

            for table_name, feats in prepared_by_table.items():
                if not feats:
                    continue
                layer = states[table_name][0]
                if not layer.addFeatures(feats):
                    raise RuntimeError(_tf('QGIS non ha accettato i record in {l}.', 'QGIS rejected records in {l}.', l=layer.name()))

            for layer, _started in states.values():
                try: layer.endEditCommand()
                except Exception: pass

            for layer, started_here in states.values():
                if started_here and not layer.commitChanges():
                    errors = '; '.join(layer.commitErrors()) if hasattr(layer, 'commitErrors') else '?'
                    raise RuntimeError(_tf('Commit non riuscito per {l}: {e}', 'Commit failed for {l}: {e}', l=layer.name(), e=errors))
                layer.updateExtents(); layer.triggerRepaint()
        except Exception:
            for layer, started_here in states.values():
                try:
                    if started_here and layer.isEditable():
                        layer.rollBack()
                except Exception:
                    pass
            raise

        for key in self.CORE_ORDER:
            registry[key].update(str(x) for x in prepared_ids[key])
        registry_path = self._save_registry(registry)
        try:
            self.plugin._post_load_repair(gpkg, preferred_ids=[l.id() for l in self.plugin._layers_for_gpkg(gpkg).values()])
        except Exception:
            pass

        self._append_status('')
        self._append_status(_tf(
            'IMPORT COMPLETATO: {n} nuovi record scritti nel template. Registry ID API: {r}',
            'IMPORT COMPLETE: {n} new records written into the template. API ID registry: {r}',
            n=total_new, r=registry_path
        ))
        self._label_imported_interventions(gpkg)
        if any(collisions[k] for k in selected):
            self._append_status(_tf(
                'ATTENZIONE: sono presenti collisioni con vecchi ID positivi non riconosciuti come record MAGOH. Non sono state modificate automaticamente.',
                'WARNING: collisions exist with old positive IDs not recognized as MAGOH records. They were not modified automatically.'
            ))
        self._set_busy(False)
        self.iface.messageBar().pushSuccess('MAGOH API', _tf(
            'Import nel template completato: {n} nuovi record.',
            'Template import complete: {n} new records.', n=total_new
        ))

    # ------------------------------------------------------------------
    # Snapshot QGIS opzionali
    # ------------------------------------------------------------------
    def _api_group_at_top(self):
        project = QgsProject.instance(); root = project.layerTreeRoot()
        group = root.findGroup('MAGOH – API')
        if group is None:
            group = root.insertGroup(0, 'MAGOH – API')
        else:
            try:
                children = list(root.children())
                if children and children[0] is not group:
                    clone = group.clone(); root.insertChildNode(0, clone); root.removeChildNode(group); group = clone
            except Exception:
                pass
        try: group.setExpanded(True)
        except Exception: pass
        return group

    def _remove_old_api_entity(self, entity_key):
        project = QgsProject.instance()
        for old in list(project.mapLayers().values()):
            try:
                if old.customProperty('magoh/api_entity', '') == entity_key:
                    project.removeMapLayer(old.id())
            except Exception:
                pass

    def _save_load_entity(self, entity_key, records):
        spec = ENTITY_SPECS.get(entity_key)
        if not spec:
            return False
        path = os.path.join(self._cache_dir(), f'magoh_api_{entity_key}.geojson')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(feature_collection(records, entity_key), f, ensure_ascii=False)
        self._remove_old_api_entity(entity_key)
        layer = QgsVectorLayer(path, 'MAGOH API – ' + _t(spec['label']), 'ogr')
        if not layer.isValid():
            return False
        layer.setCustomProperty('magoh/api_entity', entity_key)
        layer.setCustomProperty('magoh/api_source', 'search-adv')
        try:
            if hasattr(layer, 'setReadOnly'): layer.setReadOnly(True)
        except Exception:
            pass
        project = QgsProject.instance(); group = self._api_group_at_top()
        project.addMapLayer(layer, False); group.addLayer(layer)
        return True

    def _cache_dir(self):
        root = self.plugin.get_project_root()
        if root:
            path = os.path.join(root, 'data', 'api_cache')
        else:
            home = QgsProject.instance().homePath() or ''
            path = os.path.join(home, 'MAGOH_API') if home and os.path.isdir(home) else os.path.join(QgsApplication.qgisSettingsDirPath(), 'magoh_spell', 'api_cache')
        os.makedirs(path, exist_ok=True)
        return path

    def _append_status(self, text):
        cursor = self.preview.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.preview.document().isEmpty():
            cursor.insertText('\n')
        cursor.insertText(str(text))
        try:
            sb = self.preview.verticalScrollBar(); sb.setValue(sb.maximum())
        except Exception:
            pass


class AttachArchiveDialog(QDialog):
    """Choose a new or existing archive without replacing the host QGIS project."""
    def __init__(self,plugin):
        super().__init__(plugin.iface.mainWindow())
        self.mode=None
        self.setWindowTitle('MAGOH Spell')
        body,footer=dialog_frame(self,_tf('Aggiungi al progetto corrente','Add to the current project'),
            _tf('Crea un archivio vuoto oppure collega un archivio MAGOH già esistente. Il progetto QGIS aperto viene mantenuto.',
                'Create an empty archive or connect an existing MAGOH archive. The open QGIS project is preserved.'),scroll=False)
        self.archive_buttons=[]
        for mode,it,en in (('new','Crea nuovo archivio','Create new archive'),('existing','Collega archivio esistente','Connect existing archive')):
            button=QPushButton(_tf(it,en));set_role(button,primary=True)
            button.clicked.connect(lambda _checked=False,mode=mode:self.choose(mode))
            body.addWidget(button);self.archive_buttons.append(button)
        cancel=QPushButton(_tf('Annulla','Cancel'));cancel.clicked.connect(self.reject)
        footer.addStretch(1);footer.addWidget(cancel);apply_teal_style(self)
    def choose(self,mode):
        self.mode=mode;self.accept()


class NewProjectDialog(QDialog):
    """Wizard minimo per creare un progetto realmente separato dal plugin."""

    def __init__(self, plugin: MagohSpellPlugin, attach=False):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.setWindowTitle("MAGOH – Crea nuovo progetto")
        self.setMinimumWidth(480)
        body, footer = dialog_frame(
            self, (_tf('Nuovo archivio nel progetto corrente', 'New archive in the current project') if attach else
                   _tf("Crea nuovo progetto", "Create new project")),
            _tf("Un nuovo progetto usa una cartella propria e un GeoPackage pulito. "
                "Il plugin non riutilizzerà dati o geometrie di progetti precedenti.",
                "A new project uses its own folder and a clean GeoPackage. "
                "The plugin will not reuse data or geometries from previous projects."),
            scroll=False)

        box, box_layout = card()
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.name_edit = QLineEdit()

        if attach:
            self.name_edit.setText(Path(QgsProject.instance().fileName()).stem or 'Archivio_MAGOH')
        form.addRow("Nome progetto", self.name_edit)
        self.prefix_edit = QLineEdit(local_codes.suggest_prefix(self.name_edit.text()))
        self.prefix_edit.setMaxLength(5)
        self._prefix_manual = False
        self.prefix_edit.textEdited.connect(lambda text: setattr(self, '_prefix_manual', bool(text)))
        self.name_edit.textChanged.connect(lambda text: None if self._prefix_manual else
                                          self.prefix_edit.setText(local_codes.suggest_prefix(text)))
        form.addRow("Prefisso codici scheda", self.prefix_edit)

        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText(_tf("Directory che conterrà la nuova cartella progetto",
                                             "Directory that will contain the new project folder"))
        browse = QPushButton("Sfoglia…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.setSpacing(SPACE_S)
        row.addWidget(self.dir_edit, 1)
        row.addWidget(browse)
        form.addRow("Directory contenitore", row)
        box_layout.addLayout(form)
        body.addWidget(box)

        structure = hint(_tf(
            "Struttura creata: <progetto>/data · exports/pdf · exports/gna · attachments · backups",
            "Created structure: <project>/data · exports/pdf · exports/gna · attachments · backups"))
        structure.setTextFormat(Qt.TextFormat.PlainText)
        body.addWidget(structure)

        self.chk_open = QCheckBox(_tf("Apri il progetto QGIS dopo la creazione", "Open the QGIS project after creation"))
        self.chk_open.setChecked(True)
        if attach:
            self.chk_open.setChecked(False)
            self.chk_open.hide()
        body.addWidget(self.chk_open)
        body.addStretch(1)

        footer.addStretch(1)
        cancel = QPushButton("Annulla")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        create = QPushButton("Crea progetto")
        set_role(create, primary=True)
        create.setDefault(True)
        create.clicked.connect(self._accept_checked)
        footer.addWidget(cancel)
        footer.addWidget(create)
        apply_teal_style(self)

    def _browse(self):
        p = QFileDialog.getExistingDirectory(self, "Scegli directory contenitore", self.dir_edit.text().strip())
        if p:
            self.dir_edit.setText(p)

    def _accept_checked(self):
        if not self.project_name():
            QMessageBox.warning(self, "MAGOH – Nuovo progetto", "Inserisci un nome progetto.")
            return
        try:
            local_codes.validate(self.prefix_edit.text().upper())
        except ValueError as exc:
            QMessageBox.warning(self, 'MAGOH Spell', str(exc))
            return
        if not self.parent_dir() or not os.path.isdir(self.parent_dir()):
            QMessageBox.warning(self, "MAGOH – Nuovo progetto", "Seleziona una directory contenitore esistente.")
            return
        self.accept()

    def project_name(self):
        return self.name_edit.text().strip()

    def parent_dir(self):
        return self.dir_edit.text().strip()

    def open_after_create(self):
        return self.chk_open.isChecked()


class GpkgLinkDialog(QDialog):
    """Collega un GeoPackage e (opzionale) apri layer/progetti contenuti."""

    def __init__(self, plugin: MagohSpellPlugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.setWindowTitle("MAGOH – GeoPackage")
        layout, footer = dialog_frame(
            self, _tf("GeoPackage del progetto", "Project GeoPackage"),
            _tf("Il GeoPackage scelto diventa la sorgente dati attiva del plugin. "
                "Puoi aprire un progetto QGIS salvato nel gpkg oppure caricare singoli layer.",
                "The chosen GeoPackage becomes the plugin's active data source. "
                "You can open a QGIS project saved in the gpkg or load individual layers."),
            scroll=False)
        self.magoh_header.setToolTip(_tf(
            "Nessun database viene più copiato o riutilizzato implicitamente dal profilo QGIS.",
            "No database is copied or implicitly reused from the QGIS profile."))

        # --- Path row
        source, source_layout = card()
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        source_layout.addLayout(form)

        self.path_edit = QLineEdit()
        self.path_edit.setText(self.plugin.get_linked_gpkg())
        self.path_edit.setPlaceholderText(_tf("Seleziona un file .gpkg", "Select a .gpkg file"))
        btn_browse = QPushButton("Sfoglia…")
        btn_browse.clicked.connect(self._browse)
        # Prima era collegato a textChanged: apriva una connessione sqlite a OGNI
        # carattere digitato. Ora si aggiorna solo a fine digitazione.
        self.path_edit.editingFinished.connect(self._refresh_lists)

        row = QHBoxLayout()
        row.setSpacing(SPACE_S)
        row.addWidget(self.path_edit, 1)
        row.addWidget(btn_browse)
        form.addRow("GeoPackage", row)

        # GeoPackage usati di recente
        self.cmb_recent = QComboBox()
        self._fill_recent()
        self.cmb_recent.activated.connect(self._pick_recent)
        btn_forget = QPushButton(_tf("Svuota elenco", "Clear list"))
        btn_forget.clicked.connect(self._forget_recent)
        row_recent = QHBoxLayout()
        row_recent.setSpacing(SPACE_S)
        row_recent.addWidget(self.cmb_recent, 1)
        row_recent.addWidget(btn_forget)
        form.addRow("Recenti", row_recent)
        layout.addWidget(source)

        # --- Contents tabs
        self.tabs = QTabWidget()
        self.tabs.setObjectName("MagohSubTabs")

        # projects tab
        tab_p = QWidget(); vp = QVBoxLayout(tab_p)
        vp.setContentsMargins(0, SPACE_S, 0, 0)
        vp.setSpacing(SPACE_S)
        self.lst_projects = QListWidget(); self.lst_projects.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        vp.addWidget(hint(_tf("Progetti QGIS salvati nel GeoPackage", "QGIS projects saved in the GeoPackage")))
        vp.addWidget(self.lst_projects, 1)
        btn_open_prj = QPushButton(_tf("Apri progetto selezionato", "Open selected project"))
        btn_open_prj.clicked.connect(self._open_selected_project)
        vp.addWidget(btn_open_prj, 0, Qt.AlignmentFlag.AlignRight)
        self.tabs.addTab(tab_p, _tf("Progetti", "Projects"))

        # layers tab
        tab_l = QWidget(); vl = QVBoxLayout(tab_l)
        vl.setContentsMargins(0, SPACE_S, 0, 0)
        vl.setSpacing(SPACE_S)
        self.lst_layers = QListWidget(); self.lst_layers.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        vl.addWidget(hint(_tf("Layer nel GeoPackage: selezionane uno o più", "Layers in the GeoPackage: select one or more")))
        vl.addWidget(self.lst_layers, 1)
        btn_add_layers = QPushButton(_tf("Aggiungi layer selezionati al progetto", "Add selected layers to project"))
        btn_add_layers.clicked.connect(self._add_selected_layers)
        vl.addWidget(btn_add_layers, 0, Qt.AlignmentFlag.AlignRight)
        self.tabs.addTab(tab_l, "Layer")

        layout.addWidget(self.tabs, 1)

        # --- Buttons bottom
        btn_clear = QPushButton(_tf("Scollega GeoPackage attivo", "Disconnect active GeoPackage"))
        btn_clear.setAutoDefault(False)
        btn_clear.clicked.connect(self._clear)
        footer.addWidget(btn_clear)
        footer.addStretch(1)
        btn_cancel = QPushButton("Chiudi")
        btn_cancel.setAutoDefault(False)
        btn_cancel.clicked.connect(self.accept)
        footer.addWidget(btn_cancel)
        btn_save = QPushButton(_tf("Imposta come attivo", "Set as active"))
        btn_save.setToolTip(_tf("Imposta come GeoPackage attivo", "Set as active GeoPackage"))
        set_role(btn_save, primary=True)
        btn_save.clicked.connect(self._save)
        btn_save.setDefault(True)
        footer.addWidget(btn_save)

        apply_teal_style(self)
        self._refresh_lists()

    def _fill_recent(self):
        self.cmb_recent.blockSignals(True)
        self.cmb_recent.clear()
        recents = self.plugin.get_recent_gpkgs()
        if not recents:
            self.cmb_recent.addItem(_tf("(nessun GeoPackage recente)", "(no recent GeoPackage)"), "")
            self.cmb_recent.setEnabled(False)
        else:
            self.cmb_recent.setEnabled(True)
            self.cmb_recent.addItem(_tf("(scegli…)", "(choose…)"), "")
            for p in recents:
                self.cmb_recent.addItem(f"{os.path.basename(p)}  —  {os.path.dirname(p)}", p)
        self.cmb_recent.blockSignals(False)

    def _pick_recent(self, _index):
        p = self.cmb_recent.currentData()
        if p:
            self.path_edit.setText(p)
            self._refresh_lists()

    def _forget_recent(self):
        self.plugin.clear_recent_gpkgs()
        self._fill_recent()

    def _browse(self):
        start_dir = os.path.dirname(self.path_edit.text().strip()) or ""
        path, _ = QFileDialog.getOpenFileName(self, "Seleziona GeoPackage", start_dir, "GeoPackage (*.gpkg)")
        if path:
            self.path_edit.setText(path)
            self._refresh_lists()

    def _clear(self):
        self.plugin.set_active_gpkg("")
        self.path_edit.clear()
        self._refresh_lists()
        self.plugin.iface.messageBar().pushInfo(
            "MAGOH – GeoPackage", "GeoPackage scollegato. Nessun database viene ora usato implicitamente."
        )

    def _save(self):
        path = self.path_edit.text().strip()
        if path and (not os.path.exists(path)):
            QMessageBox.warning(self, "MAGOH – GeoPackage", "Il file selezionato non esiste.")
            return
        if path and (not path.lower().endswith(".gpkg")):
            QMessageBox.warning(self, "MAGOH – GeoPackage", "Seleziona un file con estensione .gpkg")
            return
        self.plugin.set_active_gpkg(path)
        self.path_edit.setText(self.plugin.get_active_gpkg())
        self._fill_recent()
        QMessageBox.information(
            self, "MAGOH – GeoPackage",
            f"Sorgente dati attiva:\n{self.plugin.get_active_gpkg()}"
        )

    def _gpkg_path(self):
        p = self.path_edit.text().strip()
        return p if p and os.path.exists(p) and p.lower().endswith('.gpkg') else ""

    def _refresh_lists(self):
        gpkg = self._gpkg_path()
        self.lst_projects.clear()
        self.lst_layers.clear()
        if not gpkg:
            return

        # progetti: segnaliamo quelli con sorgenti remote (PostGIS/WFS),
        # che offline farebbero attendere QGIS su una connessione irraggiungibile
        try:
            for p in self.plugin.list_gpkg_projects(gpkg):
                label = p["name"] + ("   ⚠ sorgenti remote (non utilizzabile offline)" if p["remote"] else "")
                it = QListWidgetItem(label)
                it.setData(Qt.ItemDataRole.UserRole, p["name"])
                it.setData(Qt.ItemDataRole.UserRole + 1, bool(p["remote"]))
                self.lst_projects.addItem(it)
        except Exception as e:
            _log(f"Elenco progetti non disponibile: {e}", Qgis.Warning)

        # layers (+ conteggio record: fa emergere subito i layer vuoti)
        conn = None
        try:
            conn = sqlite3.connect(gpkg)
            cur = conn.cursor()
            cur.execute("SELECT table_name, data_type FROM gpkg_contents ORDER BY table_name")
            rows = cur.fetchall()
            for tn, dt in rows or []:
                if not tn:
                    continue
                # escludi tabelle interne
                if str(tn).startswith('gpkg_'):
                    continue
                try:
                    n = cur.execute(f'SELECT COUNT(*) FROM "{tn}"').fetchone()[0]
                    cnt = f"{n} record"
                except Exception:
                    cnt = "n/d"
                label = f"{tn} ({dt}) — {cnt}" if dt else f"{tn} — {cnt}"
                it = QListWidgetItem(label)
                it.setData(Qt.ItemDataRole.UserRole, str(tn))
                self.lst_layers.addItem(it)
        except Exception as e:
            _log(f"Elenco layer non disponibile: {e}", Qgis.Warning)
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _open_selected_project(self):
        gpkg = self._gpkg_path()
        if not gpkg:
            QMessageBox.warning(self, "MAGOH – GeoPackage", "Seleziona un GeoPackage valido")
            return
        item = self.lst_projects.currentItem()
        if not item:
            QMessageBox.warning(self, "MAGOH – GeoPackage", "Seleziona un progetto dalla lista")
            return
        name = (item.data(Qt.ItemDataRole.UserRole) or item.text()).strip()
        if not name:
            return

        if bool(item.data(Qt.ItemDataRole.UserRole + 1)):
            r = QMessageBox.question(
                self,
                "MAGOH – GeoPackage",
                f"Il progetto '{name}' contiene layer con sorgenti remote (database/servizi web).\n"
                "Senza connessione QGIS potrebbe restare in attesa a lungo.\n\nAprirlo comunque?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        try:
            if self.plugin._warn_unsaved() is False:
                return
            workdir = self.plugin._ensure_workdir()
            project_bytes = self.plugin._extract_qgz_from_gpkg(gpkg, project_name=name)
            if not project_bytes:
                raise RuntimeError(_tf("Progetto non trovato o non leggibile nel GeoPackage", 'Project not found or unreadable in the GeoPackage'))

            # Non copiamo il GeoPackage dell'utente: il progetto temporaneo viene
            # riallineato in memoria alla sorgente originale scelta.
            project_bytes = self.plugin._normalize_project_bytes_for_gpkg(project_bytes, gpkg)
            project_path = self.plugin._write_project_files(workdir, project_bytes, project_filename=name)
            ok = QgsProject.instance().read(project_path)
            if not ok:
                raise RuntimeError(_tf("QGIS non è riuscito ad aprire il progetto", 'QGIS could not open the project'))
            self.plugin.set_active_gpkg(gpkg)
            self.plugin._post_load_repair(gpkg)
            self.plugin.iface.messageBar().pushSuccess("MAGOH – GeoPackage", f"Progetto aperto: {name}")
        except Exception as e:
            QMessageBox.critical(self, "MAGOH – GeoPackage", f"Errore aprendo il progetto:\n\n{e}")

    def _add_selected_layers(self):
        gpkg = self._gpkg_path()
        if not gpkg:
            QMessageBox.warning(self, "MAGOH – GeoPackage", "Seleziona un GeoPackage valido")
            return
        items = self.lst_layers.selectedItems()
        if not items:
            QMessageBox.warning(self, "MAGOH – GeoPackage", "Seleziona uno o più layer")
            return

        target = os.path.normcase(os.path.abspath(gpkg))
        existing = {}
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer):
                continue
            pth, lname = self.plugin._split_gpkg_source(lyr.source())
            if pth == target and lname:
                existing[lname] = lyr

        added_layers = []
        skipped = 0
        for it in items:
            tn = it.data(Qt.ItemDataRole.UserRole)
            if not tn:
                continue
            if tn in existing:
                skipped += 1
                continue
            lyr = QgsVectorLayer(f"{gpkg}|layername={tn}", tn, "ogr")
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                existing[tn] = lyr
                added_layers.append(lyr)

        self.plugin.set_active_gpkg(gpkg)
        # Se il gpkg ha struttura MAGOH, ricostruiamo relazioni e dipendenze
        # senza modificare fisicamente il file esterno dell'utente.
        self.plugin._post_load_repair(gpkg, preferred_ids=[l.id() for l in existing.values()])

        if added_layers or skipped:
            msg = f"Layer aggiunti: {len(added_layers)}"
            if skipped:
                msg += f" — già presenti: {skipped}"
            self.plugin.iface.messageBar().pushSuccess("MAGOH – GeoPackage", msg)
        else:
            QMessageBox.warning(self, "MAGOH – GeoPackage", "Nessun layer aggiunto (verifica che siano caricabili).")


# ============================================================
# UI: Export PDF (schede: mappa + attributi)
# ============================================================


class PdfExportDialog(QDialog):
    def __init__(self, iface, plugin: MagohSpellPlugin):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.plugin = plugin
        self.setWindowTitle("MAGOH – Export PDF")
        self.setMinimumWidth(520)

        main, footer = dialog_frame(
            self, _t("Esporta schede (PDF)"),
            _tf("PDF multipagina: localizzazione cartografica e scheda completa degli attributi.",
                "Multi-page PDF: map location and the complete attribute record."))

        box = QGroupBox("Parametri")
        form = QFormLayout()

        self.cmb_method = QComboBox()
        self.cmb_method.addItem(_tf("Metodo stabile (consigliato)", "Stable method (recommended)"), "direct")
        self.cmb_method.addItem(_tf("Metodo Atlas (più veloce)", "Atlas method (faster)"), "atlas")
        self.cmb_method.setCurrentIndex(0)

        self.cmb_layer = QComboBox()
        self._populate_layers()
        layer_row = QHBoxLayout()
        layer_row.addWidget(self.cmb_layer, 1)
        self.btn_refresh_layers = QPushButton()
        self.btn_refresh_layers.setIcon(ui_icon("refresh"))
        self.btn_refresh_layers.setToolTip(_t("Aggiorna / usa layer attivo"))
        self.btn_refresh_layers.setAccessibleName(_t("Aggiorna / usa layer attivo"))
        self.btn_refresh_layers.clicked.connect(self._populate_layers)
        layer_row.addWidget(self.btn_refresh_layers)

        self.chk_selected = QCheckBox("Esporta solo feature selezionate")
        self.chk_selected.setChecked(False)

        self.spin_fields = QSpinBox()
        self.spin_fields.setRange(5, 200)
        self.spin_fields.setValue(25)
        self.spin_fields.setToolTip(
            "Usato solo dal metodo Atlas. Il metodo stabile impagina in base allo spazio disponibile."
        )
        self.cmb_method.currentIndexChanged.connect(self._on_method_changed)

        self.spin_margin = QSpinBox()
        self.spin_margin.setRange(0, 80)
        self.spin_margin.setValue(15)

        self.chk_use_canvas = QCheckBox("Usa i layer visibili nel canvas per la mappa")
        self.chk_use_canvas.setChecked(True)

        self.chk_isolate = QCheckBox(_t("Isola il record corrente"))
        self.chk_isolate.setToolTip(_tf("Niente altre feature dello stesso layer nella mappa",
                                        "No other features from the same layer on the map"))
        self.chk_isolate.setChecked(True)

        out_row = QHBoxLayout()
        self.le_out = QLineEdit()
        self._output_uses_project_default = True
        default_pdf_dir = self.plugin.default_export_dir("pdf")
        if default_pdf_dir:
            self.le_out.setText(os.path.join(default_pdf_dir, "schede_MAGOH.pdf"))
        btn_browse = QPushButton("Sfoglia…")
        btn_browse.clicked.connect(self._browse)
        out_row.setSpacing(SPACE_S)
        out_row.addWidget(self.le_out, 1)
        out_row.addWidget(btn_browse)

        form.addRow("Metodo", self.cmb_method)
        form.addRow("Layer", layer_row)
        form.addRow("", self.chk_selected)
        form.addRow("Campi per pagina (scheda attributi)", self.spin_fields)
        form.addRow("Margine mappa (%)", self.spin_margin)
        form.addRow("", self.chk_use_canvas)
        form.addRow("", self.chk_isolate)
        form.addRow("Output PDF", out_row)

        box.setLayout(form)
        main.addWidget(box)

        main.addStretch(1)
        footer.addStretch(1)
        b_cancel = QPushButton("Annulla")
        b_cancel.setAutoDefault(False)
        b_cancel.clicked.connect(self.reject)
        b_run = QPushButton("Esporta")
        set_role(b_run, primary=True)
        b_run.clicked.connect(self._run)
        footer.addWidget(b_cancel)
        footer.addWidget(b_run)

        apply_teal_style(self)
        self._on_method_changed()

    def showEvent(self, event):
        # La finestra è non modale: riallinea l'elenco al layer attivo quando la riapri.
        try:
            self._populate_layers()
            root = self.plugin.get_project_root()
            if root != getattr(self, '_directory_root', None):
                self._directory_root = root
                self._output_uses_project_default = True
            if getattr(self, "_output_uses_project_default", True):
                default_pdf_dir = self.plugin.default_export_dir("pdf")
                if default_pdf_dir:
                    self.le_out.setText(os.path.join(default_pdf_dir, "schede_MAGOH.pdf"))
        except Exception:
            pass
        super().showEvent(event)

    def _on_method_changed(self, *_):
        """Il metodo 'direct' non usa 'campi per pagina': lo disabilitiamo per chiarezza."""
        is_atlas = self.cmb_method.currentData() == "atlas"
        self.spin_fields.setEnabled(is_atlas)

    def _populate_layers(self):
        self.cmb_layer.clear()
        layers = [
            lyr
            for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid() and lyr.isSpatial()
        ]
        layers.sort(key=lambda l: l.name().lower())
        for lyr in layers:
            self.cmb_layer.addItem(lyr.name(), lyr.id())

        # default: layer attivo
        try:
            al = self.iface.activeLayer()
            if al and isinstance(al, QgsVectorLayer):
                idx = self.cmb_layer.findData(al.id())
                if idx >= 0:
                    self.cmb_layer.setCurrentIndex(idx)
        except Exception:
            pass

    def _browse(self):
        start = self.plugin.default_export_dir("pdf")
        suggested = os.path.join(start, "schede_MAGOH.pdf") if start else ""
        out, _ = QFileDialog.getSaveFileName(
            self,
            "Salva PDF",
            suggested,
            "PDF (*.pdf)",
        )
        if out:
            if not out.lower().endswith(".pdf"):
                out += ".pdf"
            self.le_out.setText(out)
            self._output_uses_project_default = False

    def _get_layer(self):
        lid = self.cmb_layer.currentData()
        if not lid:
            return None
        return QgsProject.instance().mapLayer(lid)

    def _run(self):
        layer = self._get_layer()
        if not layer or not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            QMessageBox.warning(self, "MAGOH – Export PDF", "Seleziona un layer valido")
            return

        out_pdf = (self.le_out.text() or "").strip()
        if not out_pdf:
            QMessageBox.warning(self, "MAGOH – Export PDF", "Seleziona un percorso di output")
            return
        if not out_pdf.lower().endswith(".pdf"):
            out_pdf += ".pdf"
            self.le_out.setText(out_pdf)

        out_dir = os.path.dirname(os.path.abspath(out_pdf))
        if out_dir and not os.path.isdir(out_dir):
            QMessageBox.warning(self, "MAGOH – Export PDF", f"La cartella di destinazione non esiste:\n{out_dir}")
            return

        if self.chk_selected.isChecked() and layer.selectedFeatureCount() == 0:
            QMessageBox.warning(
                self,
                "MAGOH – Export PDF",
                "È attiva l'opzione 'solo feature selezionate' ma nel layer non c'è alcuna selezione.",
            )
            return

        if os.path.exists(out_pdf):
            r = QMessageBox.question(
                self,
                "MAGOH – Export PDF",
                "Il file esiste già. Vuoi sovrascriverlo?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        # Il metodo scelto dall'utente veniva ignorato nelle build precedenti:
        # export_schede_pdf partiva sempre col motore "direct".
        prefer_atlas = self.cmb_method.currentData() == "atlas"

        progress = QProgressDialog("Preparazione export…", "Annulla", 0, 100, self)
        progress.setWindowTitle("MAGOH – Export PDF")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)

        def _on_progress(done, total, label):
            if total:
                progress.setMaximum(int(total))
            progress.setValue(int(done))
            progress.setLabelText(f"Record {done + 1} di {total} — ID {label}")
            QApplication.processEvents()
            return not progress.wasCanceled()

        try:
            result = self.plugin.export_schede_pdf(
                layer=layer,
                output_pdf=out_pdf,
                only_selected=self.chk_selected.isChecked(),
                fields_per_page=int(self.spin_fields.value()),
                atlas_margin_percent=int(self.spin_margin.value()),
                use_canvas_layers=self.chk_use_canvas.isChecked(),
                isolate_current_feature=self.chk_isolate.isChecked(),
                prefer_atlas=prefer_atlas,
                progress_callback=None if prefer_atlas else _on_progress,
            )
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "MAGOH – Export PDF", f"Errore durante l'export:\n\n{e}")
            return
        finally:
            progress.close()

        if isinstance(result, dict) and result.get("cancelled"):
            QMessageBox.information(
                self,
                "MAGOH – Export PDF",
                "Export annullato. Nessun PDF parziale salvato; l'eventuale file precedente è stato conservato."
                if result.get('preserved') else f"Export interrotto: sono stati scritti {result.get('records', 0)} record.\n{out_pdf}",
            )
        else:
            n = result.get("records") if isinstance(result, dict) else None
            extra = f" ({n} record)" if n else ""
            QMessageBox.information(self, "MAGOH – Export PDF", f"Export completato{extra}:\n{out_pdf}")
        self.accept()


# ============================================================
# UI: Launcher (dashboard principale)
# ============================================================


MAGOH_INFO_HTML_IT = """
<h2>MAGOH (Managing Archaeological data for a sustainable GOvernance of the Heritage)</h2>
<p><b>2020–2022</b><br/>
<b>Coordinatore:</b> MAPPALab – Dip. Civiltà e Forme del Sapere, Università di Pisa<br/>
<b>Consorzio:</b> Soprintendenza Archeologia, Belle Arti e Paesaggio per la città metropolitana di Firenze e per le province di Pistoia e Prato; Soprintendenza Archeologia, Belle Arti e Paesaggio per le province di Pisa e Livorno; INERA Srl; Miningful Studio.</p>

<p>MAGOH è uno dei progetti vincitori del bando <i>“100 assegni per la Cultura”</i> promosso da Regione Toscana.
Il progetto prevede la creazione di una piattaforma integrata che permetta di gestire l’intero ciclo della documentazione archeologica relativa alla Provincia di Pisa e all’area metropolitana di Firenze,
consentendo l’accessibilità ai dati per una più efficace attività di ricerca, tutela e valorizzazione.</p>

<p>MAGOH è coordinato dal MAPPALab, finanziato da Regione Toscana e co-finanziato dal Dip. di Civiltà e Forme del Sapere dell’Università di Pisa e dalle Soprintendenze ABAP coinvolte.
Fanno parte del consorzio la ditta di sviluppo informatico INERA SRL e la ditta di data science Miningful Studio.</p>

<p>MAGOH ha intrecciato una stretta collaborazione tra il MAPPALab e le Soprintendenze coinvolte, ponendosi tra i principali obbiettivi quello di creare uno strumento che agevoli il lavoro quotidiano dei funzionari
senza modificarne il consolidato <i>modus operandi</i>. Il sistema è pensato per permettere sia l’accessibilità degli operatori impegnati nei compiti di tutela e valorizzazione (con privilegi e profili diversi),
sia l’accesso ai contenuti da parte dell’utenza esterna.</p>

<p>Grazie al know-how del MAPPALab, l’eterogeneità dei dati archeologici è stata trattata mediante una fase di formalizzazione e categorizzazione,
con l’aggiornamento e la creazione di thesauri scientifici validati e strumenti di controllo dell’affidabilità e della verificabilità del dato.</p>

<p>Partendo dagli strumenti sviluppati nei progetti MAPPA e ArchAIDE, MAGOH ha sviluppato una piattaforma web-based che integra:</p>
<ol>
  <li>un database relazionale di archiviazione e gestione dei dati archeologici;</li>
  <li>un sistema webGIS per acquisizione, geolocalizzazione vettoriale, analisi spaziale, visualizzazione e accessibilità dei ritrovamenti;</li>
  <li>preservazione a lungo termine e disseminazione come open data tramite il repository MOD (MAPPA Open Data archive) nella Digital Library del Dip. Civiltà e Forme del Sapere (UniPi).</li>
</ol>

<p>Gli interventi archeologici sono estrapolati dalla documentazione di archivio fino al 2016 (per alcune aree fino al 2022). I dati sono in costante aggiornamento da parte delle Soprintendenze ABAP Firenze e Pisa-Livorno e da MAPPA Lab.</p>

<p>Metodologia (schema dati): ogni intervento archeologico è suddiviso, dove possibile, per unità di tempo e di spazio. All’interno di un intervento possono esserci uno o più ritrovamenti identificati sia sulla base del periodo,
sia sulla destinazione d’uso (tre livelli informativi). Il IV livello rappresenta l’evidenza archeologica (livello più specifico: ceramica, strutture murarie, fornace, ecc.).</p>

<p><b>Front-end del progetto MAGOH:</b><br/>
<a href="https://magoh.cfs.unipi.it/web/guest">https://magoh.cfs.unipi.it/web/guest</a><br/>
<small>Il servizio web può risultare temporaneamente non disponibile; MAGOH Spell mantiene separata la consultazione locale in QGIS.</small></p>

<p><b>MAGOH Wiki</b> (documentazione del modello dati):<br/>
<a href="https://magoh-wiki.cfs.unipi.it/index.html">https://magoh-wiki.cfs.unipi.it/index.html</a></p>
"""

MAGOH_INFO_HTML_EN = """
<h2>MAGOH (Managing Archaeological data for a sustainable GOvernance of the Heritage)</h2>
<p><b>2020–2022</b><br/>
<b>Coordinator:</b> MAPPALab – Department of Civilisations and Forms of Knowledge, University of Pisa<br/>
<b>Consortium:</b> Superintendency of Archaeology, Fine Arts and Landscape for the metropolitan city of Florence and the provinces of Pistoia and Prato; Superintendency of Archaeology, Fine Arts and Landscape for the provinces of Pisa and Livorno; INERA Srl; Miningful Studio.</p>

<p>MAGOH is one of the winning projects of the <i>“100 assegni per la Cultura”</i> call promoted by the Tuscany Region.
The project creates an integrated platform for managing the entire archaeological documentation cycle for the Province of Pisa and the Florence metropolitan area,
making the data accessible for more effective research, heritage protection and enhancement.</p>

<p>MAGOH is coordinated by MAPPALab, funded by the Tuscany Region and co-funded by the Department of Civilisations and Forms of Knowledge of the University of Pisa and by the ABAP Superintendencies involved.
The consortium also includes the software development company INERA SRL and the data science company Miningful Studio.</p>

<p>MAGOH established a close collaboration between MAPPALab and the Superintendencies involved. One of its main goals was to create a tool that supports the officers’ daily work
without changing their established <i>modus operandi</i>. The system is designed to give access both to the operators responsible for heritage protection and enhancement (with different privileges and profiles)
and to external users.</p>

<p>Building on MAPPALab’s know-how, the heterogeneity of archaeological data was addressed through a phase of formalisation and categorisation,
with the updating and creation of validated scientific thesauri and of tools to check data reliability and verifiability.</p>

<p>Starting from the tools developed in the MAPPA and ArchAIDE projects, MAGOH developed a web-based platform that integrates:</p>
<ol>
  <li>a relational database for storing and managing archaeological data;</li>
  <li>a webGIS system for the acquisition, vector geolocation, spatial analysis, visualisation and accessibility of findings;</li>
  <li>long-term preservation and dissemination as open data through the MOD repository (MAPPA Open Data archive) in the Digital Library of the Department of Civilisations and Forms of Knowledge (UniPi).</li>
</ol>

<p>Archaeological interventions are extracted from archival documentation up to 2016 (up to 2022 for some areas). The data are constantly updated by the ABAP Superintendencies of Florence and Pisa-Livorno and by MAPPA Lab.</p>

<p>Methodology (data model): each archaeological intervention is divided, where possible, into units of time and space. An intervention may contain one or more findings, identified both by period
and by intended use (three information levels). The fourth level represents the archaeological evidence (the most specific level: pottery, masonry structures, kilns, etc.).</p>

<p><b>MAGOH project front-end:</b><br/>
<a href="https://magoh.cfs.unipi.it/en-US/web/guest">https://magoh.cfs.unipi.it/en-US/web/guest</a><br/>
<small>The web service may currently be temporarily unavailable. MAGOH Spell provides separate local access to the project data model in QGIS.</small></p>

<p><b>MAGOH Wiki</b> (data model documentation):<br/>
<a href="https://magoh-wiki.cfs.unipi.it/en/index.html">https://magoh-wiki.cfs.unipi.it/en/index.html</a></p>
"""


class InfoDialog(QDialog):
    def __init__(self, iface, plugin=None):
        super().__init__(iface.mainWindow())
        self.plugin = plugin
        self.setWindowTitle(_t("Info MAGOH"))
        self.setMinimumWidth(520)
        v, row = dialog_frame(self, _t("MAGOH – Informazioni"),
                              "Managing Archaeological data for a sustainable GOvernance of the Heritage",
                              scroll=False)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setMinimumHeight(260)
        self.browser = browser
        self.retranslate()
        v.addWidget(browser, 1)

        row.addStretch(1)
        b = QPushButton(_t("Chiudi"))
        set_role(b, primary=True)
        b.clicked.connect(self.accept)
        row.addWidget(b)

        apply_teal_style(self)

    def retranslate(self, language=None):
        """V1.15.1: the long project text follows the language switch too."""
        language = language or _language()
        self.browser.setHtml(MAGOH_INFO_HTML_EN if language == "en" else MAGOH_INFO_HTML_IT)

    def showEvent(self, event):
        try:
            self.retranslate()
        except Exception:
            pass
        super().showEvent(event)

    def sizeHint(self):
        return QSize(640, 560)


class MagohRelationNavigator(QWidget):
    """Esplora le schede MAGOH con un unico albero gerarchico.

    v23:
    - un solo blocco di navigazione (niente browser + relazioni separati);
    - la ricerca ID produce una vista padre -> figli -> nipoti;
    - i quattro dataset MAGOH vengono filtrati davvero ai soli record del
      contesto cercato, lasciando invariati basemap e altri layer di contesto;
    - il filtro è reversibile con ``Mostra tutti`` e conserva eventuali subset
      filter che l'utente aveva impostato prima di MAGOH.
    """

    MAIN_LAYERS = (
        'magoh_scheda_intervento',
        'magoh_ritrovamenti',
        'magoh_evidenza_archeologica',
        'magoh_evidenza_archeologica_no_geom',
    )

    FRIENDLY = {
        'magoh_scheda_intervento': 'Schede di Intervento',
        'magoh_ritrovamenti': 'Ritrovamenti',
        'magoh_evidenza_archeologica': 'Evidenze archeologiche',
        'magoh_evidenza_archeologica_no_geom': 'Evidenze senza geometria',
    }
    SINGULAR = {
        'magoh_scheda_intervento': 'Scheda di Intervento',
        'magoh_ritrovamenti': 'Scheda di Ritrovamento',
        'magoh_evidenza_archeologica': 'Evidenza archeologica',
        'magoh_evidenza_archeologica_no_geom': 'Evidenza senza geometria',
    }
    MAX_BROWSER_RECORDS = 500

    def __init__(self, iface, plugin, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.plugin = plugin
        self._current_layer_id = None
        self._current_fid = None
        self._selection_signal_layer = None
        self._filtered_mode = False

        v = QVBoxLayout(self)
        v.setContentsMargins(SPACE_M, SPACE_M, SPACE_M, SPACE_S)
        v.setSpacing(SPACE_M - 2)

        hint_top = hint(_tf(
            "Cerca un valore o un ID: il filtro si estende ai layer padre e figlio collegati.",
            "Search a value or ID: the filter extends to related parent and child layers."
        ))
        hint_top.setToolTip(_tf(
            "Scegli un layer e cerca una parola, un valore o un ID. Puoi limitare la ricerca a un attributo. "
            "Il filtro si propaga automaticamente ai layer padre e figlio collegati.",
            "Choose a layer and search a word, value or ID. You can search one attribute or all attributes. "
            "The filter automatically propagates to related parent and child layers."
        ))
        v.addWidget(hint_top)

        search_box, search_card = card()
        search_layout = QGridLayout()
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setHorizontalSpacing(SPACE_M - 2)
        search_layout.setVerticalSpacing(SPACE_S + 1)
        search_card.addLayout(search_layout)
        self.search_layer_combo = QComboBox()
        self.search_layer_combo.setMinimumWidth(120)
        self.search_layer_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.search_layer_combo.setMinimumContentsLength(8)
        self.search_id = QLineEdit()
        self.search_id.setPlaceholderText(_tf("Cerca in tutti gli attributi…", "Search all attributes…"))
        self.search_id.setToolTip(_tf(
            "Nei campi a elenco (thesauri, cronologie) si cercano sia il codice salvato sia l'etichetta mostrata "
            "nella scheda, senza distinguere maiuscole, accenti e punteggiatura.",
            "In list fields (thesauri, chronologies) both the stored code and the label shown in the form are "
            "searched, ignoring case, accents and punctuation."))
        self.search_id.setClearButtonEnabled(True)
        self.search_id.returnPressed.connect(self.search_by_id)
        self.chk_exact_id = QCheckBox(_tf("ID esatto", "Exact ID"))
        self.chk_exact_id.setChecked(False)
        self.chk_exact_id.setToolTip(_tf(
            "Mantiene la vecchia ricerca per identificativo logico del layer.",
            "Use the legacy exact search on the layer logical identifier."
        ))
        self.search_field_combo = QComboBox()
        self.search_field_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.search_field_combo.setMinimumContentsLength(8)
        lbl_layer = QLabel(_t("Layer"))
        lbl_layer.setObjectName("MagohKey")
        lbl_field = QLabel(_tf("Attributo", "Attribute"))
        lbl_field.setObjectName("MagohKey")
        search_layout.addWidget(lbl_layer, 0, 0)
        search_layout.addWidget(self.search_layer_combo, 0, 1, 1, 2)
        search_layout.addWidget(lbl_field, 1, 0)
        search_layout.addWidget(self.search_field_combo, 1, 1, 1, 2)
        search_layout.addWidget(self.search_id, 2, 0, 1, 2)
        search_layout.addWidget(self.chk_exact_id, 2, 2)
        search_layout.setColumnStretch(1, 1)
        self.search_layer_combo.currentIndexChanged.connect(self._refresh_search_fields)
        self.chk_exact_id.toggled.connect(lambda checked: self.search_field_combo.setEnabled(not checked))

        # Applica filtro e Mostra tutti subito sotto la scelta del filtro.
        b_search = QPushButton(_tf("Applica filtro", "Apply filter"))
        b_search.setToolTip(_tf(
            "La ricerca non tocca basemap o raster e sostituisce il filtro cronologico.",
            "Search does not affect basemaps or rasters and replaces the chronology filter."
        ))
        b_search.clicked.connect(self.search_by_id)
        b_all = QPushButton(_tf("Mostra tutti", "Show all"))
        b_all.setToolTip(_tf(
            "Toglie tutti i filtri (ricerca, cronologia e categorie) dai layer MAGOH e da quelli collegati, "
            "compresi i filtri rimasti da sessioni precedenti.",
            "Removes every filter (search, chronology and categories) from MAGOH and related layers, "
            "including filters left over from previous sessions."
        ))
        b_all.clicked.connect(self.show_all_records)
        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, SPACE_XS, 0, 0)
        filter_row.setSpacing(SPACE_S)
        for button in (b_search, b_all):
            set_role(button, primary=button is b_search, compact=True)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            filter_row.addWidget(button, 1)
        search_card.addLayout(filter_row)
        v.addWidget(search_box)

        self.lbl_context = QLabel(_tf(
            "Scegli una scheda o cerca un ID.",
            "Choose a record or search an ID."
        ))
        self.lbl_context.setWordWrap(True)
        self.lbl_context.setObjectName("MagohContext")
        v.addWidget(self.lbl_context)

        # Unico albero: prima mostra layer/record, dopo una ricerca mostra la
        # gerarchia filtrata Intervento -> Ritrovamento -> Evidenza.
        self.browser = QTreeWidget()
        self.browser.setHeaderLabels([_tf("Scheda / collegamento", "Record / connection"), _t("Record")])
        self.browser.setAlternatingRowColors(True)
        self.browser.setMinimumHeight(150)
        self.browser.setUniformRowHeights(True)
        self.browser.itemExpanded.connect(self._populate_layer_node)
        self.browser.itemClicked.connect(self._browser_item_clicked)
        self.browser.itemDoubleClicked.connect(self._browser_item_double_clicked)
        try:
            header = self.browser.header()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        except Exception:
            pass
        v.addWidget(self.browser, 1)

        # Barra fissa sotto lo scorrimento: navigazione del record sempre visibile.
        self.action_bar, actions = action_bar(self)
        actions.setContentsMargins(SPACE_M, SPACE_S + 2, SPACE_M, SPACE_S + 2)
        b_zoom = QPushButton(_t("Zoom"))
        b_zoom.clicked.connect(self.zoom_current)
        b_open = QPushButton(_t("Apri scheda"))
        b_open.clicked.connect(self.open_current)
        for button in (b_zoom, b_open):
            set_role(button, compact=True)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            actions.addWidget(button, 1)
        self.b_search, self.b_show_all, self.b_zoom, self.b_open = b_search, b_all, b_zoom, b_open

        v.addWidget(self.action_bar)

        self.refresh_layer_browser()

    def _refresh_search_fields(self, *_args):
        previous = self.search_field_combo.currentData()
        self.search_field_combo.clear()
        self.search_field_combo.addItem(_tf("Tutti gli attributi", "All attributes"), "")
        layer = self._search_selected_layer()
        if layer is not None:
            for field in layer.fields():
                self.search_field_combo.addItem(field.displayName(), field.name())
        idx = self.search_field_combo.findData(previous)
        if idx >= 0:
            self.search_field_combo.setCurrentIndex(idx)

    def _layer_name(self, layer):
        try:
            _p, lname = self.plugin._split_gpkg_source(layer.source())
            return lname or layer.name()
        except Exception:
            return layer.name() if layer else ""

    @staticmethod
    def _value(feat, field):
        try:
            v = feat[field]
            if v is None or (hasattr(v, 'isNull') and v.isNull()):
                return None
            txt = str(v).strip()
            return txt if txt and txt.upper() != 'NULL' else None
        except Exception:
            return None

    @staticmethod
    def _expr_equal_text(field, value):
        val = str(value).replace("'", "''")
        fld = str(field).replace('"', '""')
        return f'to_string("{fld}") = \'{val}\''

    def _describe(self, lname, feat):
        if str(lname).upper().startswith(('DSC_', 'MOSI_')):
            from .gna_display import describe
            return describe('dsc' if str(lname).upper().startswith('DSC_') else 'mosi', feat)
        if lname == 'magoh_scheda_intervento':
            # V1.23: ID scheda al posto dell'anno, come per Ritrovamenti ed Evidenze.
            ident = self._value(feat, 'codice_locale') or self._value(feat, 'idscheda') or feat.id()
            place = self._value(feat, 'ubicazione') or ''
            return f"ID {ident}" + (f" – {place}" if place else '')
        if lname == 'magoh_ritrovamenti':
            ident = self._value(feat, 'codice_locale') or self._value(feat, 'id') or feat.id()
            extra = self._value(feat, 'iii_livello') or self._value(feat, 'ii_livello') or ''
            return f"ID {ident}" + (f" – {extra}" if extra else '')
        if lname in ('magoh_evidenza_archeologica', 'magoh_evidenza_archeologica_no_geom'):
            ident = self._value(feat, 'codice_locale') or self._value(feat, 'id') or feat.id()
            extra = self._value(feat, 'iv_livello') or ''
            return f"ID {ident}" + (f" – {extra}" if extra else '')
        # Layer GNA/generici: prova identificativi noti e una breve descrizione.
        candidates = ('MOPR_ID', 'DSCH', 'NVCT', 'MOSI_ID', 'gid', 'id', 'fid')
        ident = None
        for field in candidates:
            ident = self._value(feat, field)
            if ident is not None:
                break
        if ident is None:
            ident = feat.id()
        extra = ''
        for field in ('OGTD', 'OGTN', 'DSCI', 'DSCD', 'ubicazione', 'denominazione', 'nome'):
            extra = self._value(feat, field) or ''
            if extra:
                break
        return f"ID {ident}" + (f" – {extra[:90]}" if extra else '')

    def _search_selected_layer(self):
        try:
            layer_id = self.search_layer_combo.currentData()
            if layer_id in (self.GNA_MOSI_KEY, self.GNA_DSC_KEY):
                group = self._search_selected_layers()
                return group[0] if group else None
            layer = QgsProject.instance().mapLayer(str(layer_id)) if layer_id else None
            return layer if isinstance(layer, QgsVectorLayer) else None
        except Exception:
            return None

    def _logical_id_field(self, lname, layer):
        candidates = {
            'magoh_scheda_intervento': ('idscheda', 'id', 'fid'),
            'magoh_ritrovamenti': ('id', 'fid'),
            'magoh_evidenza_archeologica': ('id', 'fid'),
            'magoh_evidenza_archeologica_no_geom': ('id', 'fid'),
        }.get(lname, ('MOPR_ID', 'DSCH', 'NVCT', 'MOSI_ID', 'gid', 'id', 'fid'))
        from .gna_identity import gna_role
        role = gna_role(layer)
        if role:
            fields = {f.name().upper(): f.name() for f in layer.fields()}
            return fields.get('DSCH' if role == 'dsc' else 'ACCC')
        names = {f.name() for f in layer.fields()}
        # prima confronto case-sensitive, poi case-insensitive per i Template GNA
        for field in candidates:
            if field in names:
                return field
        lower = {n.casefold(): n for n in names}
        for field in candidates:
            if field.casefold() in lower:
                return lower[field.casefold()]
        return None

    def _loaded_layers(self):
        gpkg = self.plugin.get_active_gpkg()
        return self.plugin._layers_for_gpkg(gpkg) if gpkg and os.path.exists(gpkg) else {}

    def _is_gna_layer(self, layer):
        from .gna_identity import gna_role
        return bool(gna_role(layer))

    GNA_MOSI_KEY = 'magoh:gna_mosi_all'
    GNA_DSC_KEY = 'magoh:gna_dsc_all'

    def _gna_role(self, layer):
        from .gna_identity import gna_role
        return gna_role(layer)

    def _search_selected_layers(self):
        """Layers searched together: every MOSI for the single «MOSI» option."""
        try:
            key = self.search_layer_combo.currentData()
        except Exception:
            return []
        if key in (self.GNA_MOSI_KEY, self.GNA_DSC_KEY):
            role = 'mosi' if key == self.GNA_MOSI_KEY else 'dsc'
            return [l for l in self._searchable_layers() if self._gna_role(l) == role]
        layer = self._search_selected_layer()
        return [layer] if layer is not None else []

    def _searchable_layers(self):
        """Layer MAGOH core + layer vettoriali del gruppo/schema GNA.

        V1.15: the MOPR is never filtered by the panel."""
        out = []
        seen = set()
        loaded = self._loaded_layers()
        for lname in self.MAIN_LAYERS:
            lyr = loaded.get(lname)
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid() and lyr.id() not in seen:
                out.append(lyr); seen.add(lyr.id())
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.id() in seen or not isinstance(lyr, QgsVectorLayer) or not lyr.isValid():
                continue
            if self._is_gna_layer(lyr) and self._gna_role(lyr) != 'mopr':
                out.append(lyr); seen.add(lyr.id())
        return out

    def refresh_layer_browser(self):
        """Mostra la vista completa dei layer senza alterare l'eventuale filtro."""
        self.browser.clear()
        self._set_browser_columns()
        loaded = self._loaded_layers()
        previous_layer_id = self.search_layer_combo.currentData() if self.search_layer_combo.count() else None
        self.search_layer_combo.blockSignals(True)
        self.search_layer_combo.clear()
        mosi_layers = []
        dsc_layers = []
        for layer in self._searchable_layers():
            lname = self._layer_name(layer)
            label = _t(self.FRIENDLY.get(lname, layer.name()))
            if self._is_gna_layer(layer):
                # V1.15: GNA offers only DSC and one «MOSI» entry (all MOSI layers).
                role = self._gna_role(layer)
                if role == 'mosi':
                    mosi_layers.append(layer)
                    continue
                if role == 'dsc':
                    dsc_layers.append(layer)
                    continue
                label = _tf('GNA – {name}', 'GNA – {name}', name=layer.name())
            self.search_layer_combo.addItem(label, layer.id())
        if dsc_layers:
            self.search_layer_combo.addItem('GNA – DSC', self.GNA_DSC_KEY)
        if mosi_layers:
            self.search_layer_combo.addItem('GNA – MOSI', self.GNA_MOSI_KEY)
            self.search_layer_combo.setItemData(
                self.search_layer_combo.count() - 1,
                _tf('Filtra insieme tutti i MOSI: ', 'Filters every MOSI together: ') + ', '.join(l.name() for l in mosi_layers),
                Qt.ItemDataRole.ToolTipRole)
        if previous_layer_id:
            idx = self.search_layer_combo.findData(previous_layer_id)
            if idx >= 0:
                self.search_layer_combo.setCurrentIndex(idx)
        self.search_layer_combo.blockSignals(False)
        self._refresh_search_fields()

        self._filtered_mode = False
        for lname in self.MAIN_LAYERS:
            layer = loaded.get(lname)
            if not isinstance(layer, QgsVectorLayer):
                continue
            node = QTreeWidgetItem([_t(self.FRIENDLY.get(lname, layer.name())), str(layer.featureCount())])
            node.setData(0, Qt.ItemDataRole.UserRole, layer.id())
            node.setData(0, Qt.ItemDataRole.UserRole + 20, 'layer')
            node.setData(0, Qt.ItemDataRole.UserRole + 21, False)
            node.addChild(QTreeWidgetItem([_tf("Espandi per caricare le schede…", "Expand to load records…"), ""]))
            self.browser.addTopLevelItem(node)
        if self.browser.topLevelItemCount() == 0:
            node = QTreeWidgetItem([_tf("Nessun layer MAGOH caricato", "No MAGOH layer loaded"), ""])
            node.setFlags(Qt.ItemFlag.NoItemFlags)
            self.browser.addTopLevelItem(node)
        try:
            self.browser.resizeColumnToContents(0)
        except Exception:
            pass

    def _populate_layer_node(self, node):
        if self._filtered_mode:
            return
        if node.data(0, Qt.ItemDataRole.UserRole + 20) != 'layer' or bool(node.data(0, Qt.ItemDataRole.UserRole + 21)):
            return
        layer = QgsProject.instance().mapLayer(str(node.data(0, Qt.ItemDataRole.UserRole)))
        if not isinstance(layer, QgsVectorLayer):
            return
        lname = self._layer_name(layer)
        node.takeChildren()
        limit = self.MAX_BROWSER_RECORDS
        try:
            for feat in layer.getFeatures(QgsFeatureRequest().setLimit(limit)):
                item = self._record_item(layer, lname, feat)
                node.addChild(item)
            if layer.featureCount() > limit:
                more = QTreeWidgetItem([_tf(
                    "… visualizzati i primi {limit} di {total} record",
                    "… showing the first {limit} of {total} records",
                    limit=limit, total=layer.featureCount()), ""])
                more.setFlags(Qt.ItemFlag.NoItemFlags)
                node.addChild(more)
        except Exception as e:
            node.addChild(QTreeWidgetItem([_tf("Errore caricando i record", "Error loading records"), str(e)]))
        node.setData(0, Qt.ItemDataRole.UserRole + 21, True)

    PREVIEW_ID_WIDTH = 92

    def _set_browser_columns(self, role=''):
        # V1.23: risultati di Intervento, DSC e MOSI in due colonne «ID scheda» e «Ubicazione».
        self._gna_preview_role = role
        self._dsc_locations = None
        self.browser.setColumnCount(2)
        self.browser.setHeaderLabels(
            [_tf('ID scheda', 'Record ID'), _tf('Ubicazione', 'Location')] if role else
            [_tf('Scheda / collegamento', 'Record / connection'), _t('Record')])
        header = self.browser.header()
        if role:
            # Colonna ID stretta (valore completo nel tooltip), ubicazione con lo spazio restante.
            header.setStretchLastSection(True)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
            self.browser.setColumnWidth(0, self.PREVIEW_ID_WIDTH)
        else:
            header.setStretchLastSection(False)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)

    def _dsc_location_map(self):
        """DSCH -> ubicazione (DSCV) dei DSC presenti nel progetto, letta una volta per elenco."""
        if getattr(self, '_dsc_locations', None) is None:
            from .gna_identity import gna_role
            found = {}
            for layer in QgsProject.instance().mapLayers().values():
                if gna_role(layer) != 'dsc':
                    continue
                names = {f.name().upper(): f.name() for f in layer.fields()}
                key, place = names.get('DSCH'), names.get('DSCV') or names.get('DSVC')
                if not key or not place:
                    continue
                request = QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry).setSubsetOfAttributes([key, place], layer.fields())
                for f in layer.getFeatures(request):
                    k, v = self._value(f, key), self._value(f, place)
                    if k and v:
                        found.setdefault(k, v)
            self._dsc_locations = found
        return self._dsc_locations

    PREVIEW_FIELDS = {'dsc': ('OSS', 'DSCH', 'DSCV', 'DSVC'),
                      'mosi': ('DES', 'GNA', 'id_viarch', 'ACCC', 'DSCH', 'OGN', 'PVL')}

    def _preview_feature(self, role, layer, feat):
        """La ricerca testuale GNA restituisce record ridotti: rilegge per fid solo i campi dell'anteprima."""
        names = [n for n in self.PREVIEW_FIELDS.get(role, ()) if layer.fields().lookupField(n) >= 0]
        if not names or all(feat.fields().lookupField(n) >= 0 for n in names):
            return feat
        try:
            request = QgsFeatureRequest(feat.id()).setFlags(QgsFeatureRequest.NoGeometry).setSubsetOfAttributes(names, layer.fields())
            full = next(layer.getFeatures(request), None)
        except Exception:
            full = None
        return full if full is not None and full.isValid() else feat

    def _record_item(self, layer, lname, feat, label=None):
        role = getattr(self, '_gna_preview_role', '')
        if role:
            from .gna_display import preview_identity, preview_location
            shown = self._preview_feature(role, layer, feat)
            ident, origin = preview_identity(role, shown)
            place = preview_location(role, shown, self._dsc_location_map() if role == 'mosi' else None)
            columns = [ident, place]
            tips = ['%s\n%s\n%s' % (ident, origin, layer.name()), place]
        else:
            columns = [
                _t(label or self.SINGULAR.get(lname, self.FRIENDLY.get(lname, layer.name()))),
                self._describe(lname, feat),
            ]
            tips = columns
        item = QTreeWidgetItem(columns)
        for index, text in enumerate(tips):
            item.setToolTip(index, text)
        item.setData(0, Qt.ItemDataRole.UserRole, layer.id())
        item.setData(0, Qt.ItemDataRole.UserRole + 1, int(feat.id()))
        item.setData(0, Qt.ItemDataRole.UserRole + 20, 'record')
        return item

    # ---------- filtro temporaneo dei dataset MAGOH ----------
    def _restore_base_subsets(self, discard=False):
        """Shared session restores only filters still owned by this plugin."""
        try:
            session_for(self.plugin).clear()
            return True
        except RelatedFilterError as exc:
            QMessageBox.warning(self, _t("MAGOH – Filtri"), str(exc))
            return False

    def clear_dataset_filter(self):
        tools = getattr(self.plugin, 'tools_dock', None)
        if tools is not None:
            tools.filter_panel._apply_timer.stop()
        if not self._restore_base_subsets(discard=True):
            return
        self._filtered_mode = False
        self._current_layer_id = None
        self._current_fid = None
        for layer in self._searchable_layers():
            if isinstance(layer, QgsVectorLayer):
                try:
                    layer.removeSelection()
                except Exception:
                    pass
        self.lbl_context.setText(_tf(
            "Filtro MAGOH rimosso: sono nuovamente disponibili tutti i record.",
            "MAGOH filter removed: all records are available again."
        ))
        self.refresh_layer_browser()
        try:
            self.iface.mapCanvas().refresh()
        except Exception:
            pass


    def _layers_to_unfilter(self):
        """Layer MAGOH/GNA cercabili, quelli collegati da relazioni e quelli filtrati per cronologia."""
        targets = {}
        for layer in self._searchable_layers():
            if isinstance(layer, QgsVectorLayer):
                targets[layer.id()] = layer
        try:
            layers, edges = relation_graph(QgsProject.instance(), self.plugin.MAGOH_RELATIONS)
            for lid in list(targets):
                for other in component(lid, edges):
                    if other in layers:
                        targets[other] = layers[other]
        except Exception:
            pass
        tools = getattr(self.plugin, 'tools_dock', None)
        states = getattr(getattr(tools, 'filter_panel', None), '_layer_state', {}) or {}
        for lid in states:
            layer = QgsProject.instance().mapLayer(lid)
            if isinstance(layer, QgsVectorLayer):
                targets[lid] = layer
        return targets

    def show_all_records(self):
        """«Mostra tutti»: rimozione forzata di ogni filtro dai layer MAGOH.

        Azzera il filtro dei layer MAGOH/GNA, di quelli collegati e di quelli
        filtrati dal plugin, anche se rimasto da una sessione precedente o
        modificato fuori dal plugin. I layer in modifica non vengono toccati e
        sono segnalati. Lo scaricamento del plugin continua invece a usare
        clear_dataset_filter(), che ripristina i filtri di partenza.
        """
        tools = getattr(self.plugin, 'tools_dock', None)
        if tools is not None:
            tools.filter_panel._apply_timer.stop()
        # Non serve ripristinare i filtri "di partenza": vengono comunque tolti.
        targets = self._layers_to_unfilter()
        try:
            for lid in list(session_for(self.plugin).bases):
                layer = QgsProject.instance().mapLayer(lid)
                if isinstance(layer, QgsVectorLayer):
                    targets[lid] = layer
        except Exception:
            pass
        cleared, blocked = [], []
        canvas = self.iface.mapCanvas()
        frozen = canvas.isFrozen()
        canvas.freeze(True)
        try:
            for layer in targets.values():
                if not layer.subsetString():
                    continue
                if layer.isEditable():
                    blocked.append(layer.name())
                    continue
                if layer.setSubsetString(''):
                    cleared.append(layer.name())
                    layer.triggerRepaint()
                else:
                    blocked.append(layer.name())
        finally:
            canvas.freeze(frozen)
        # Nessun filtro da ripristinare in seguito: la sessione riparte pulita.
        try:
            session = session_for(self.plugin)
            session.bases.clear()
            session.applied.clear()
            session.last_counts = {}
        except Exception:
            pass
        if tools is not None:
            for st in (getattr(tools.filter_panel, '_layer_state', {}) or {}).values():
                if isinstance(st, dict):
                    st["subset_string"] = ""
        self._filtered_mode = False
        self._current_layer_id = None
        self._current_fid = None
        for layer in self._searchable_layers():
            try:
                layer.removeSelection()
            except Exception:
                pass
        if blocked:
            self.lbl_context.setText(_tf(
                "Filtri rimossi, tranne che da: {names}. Salva o annulla le modifiche di questi layer e premi di nuovo «Mostra tutti».",
                "Filters removed, except from: {names}. Save or discard edits on these layers and press ‘Show all’ again.",
                names=", ".join(blocked)))
            self.iface.messageBar().pushWarning("MAGOH", self.lbl_context.text())
        else:
            self.lbl_context.setText(_tf(
                "Tutti i filtri rimossi: sono nuovamente visibili tutti i record.",
                "All filters removed: every record is visible again."))
        _log(f"Mostra tutti: filtri azzerati su {len(cleared)} layer; non modificabili: {len(blocked)}.")
        self.refresh_layer_browser()
        try:
            canvas.refresh()
        except Exception:
            pass

    def _matches(self, layer, field, value):
        if not isinstance(layer, QgsVectorLayer) or value is None or layer.fields().indexOf(field) < 0:
            return []
        try:
            req = QgsFeatureRequest().setFilterExpression(self._expr_equal_text(field, value))
            return [f for f in layer.getFeatures(req)]
        except Exception:
            return []

    def _valid_child_of_intervention(self, child_layer, child_feat, intervention_layer, intervention_feat):
        """Validita' relazionale per il browser MAGOH.

        Il browser segue deliberatamente le chiavi padre/figlio e NON scarta
        i dati storici importati solo per una lieve incoerenza geometrica.
        La regola spaziale resta hard nelle form QGIS per la creazione/modifica
        manuale dei nuovi record; le anomalie topologiche dei dati preesistenti
        restano da verificare (il Controllo qualità è sospeso dalla V1.19).
        """
        return True

    def _first_intervention(self, loaded, key, child_layer=None, child_feat=None):
        layer = loaded.get('magoh_scheda_intervento')
        for feat in self._matches(layer, 'idscheda', key):
            if child_layer is not None and child_feat is not None:
                if not self._valid_child_of_intervention(child_layer, child_feat, layer, feat):
                    continue
            return layer, feat
        return None, None

    def _first_finding(self, loaded, key, intervention_layer=None, intervention_feat=None):
        layer = loaded.get('magoh_ritrovamenti')
        for feat in self._matches(layer, 'id', key):
            if intervention_layer is not None and intervention_feat is not None:
                if not self._valid_child_of_intervention(layer, feat, intervention_layer, intervention_feat):
                    continue
            return layer, feat
        return None, None

    def _clear_main_selections(self, loaded):
        for lname in self.MAIN_LAYERS:
            layer = loaded.get(lname)
            if isinstance(layer, QgsVectorLayer):
                try:
                    layer.removeSelection()
                except Exception:
                    pass

    def _build_intervention_context(self, loaded, int_layer, int_feat):
        key = self._value(int_feat, 'idscheda')
        finding_layer = loaded.get('magoh_ritrovamenti')
        ev_layer = loaded.get('magoh_evidenza_archeologica')
        evng_layer = loaded.get('magoh_evidenza_archeologica_no_geom')

        findings = [f for f in self._matches(finding_layer, 'idscheda', key)
                    if self._valid_child_of_intervention(finding_layer, f, int_layer, int_feat)]
        evidences = [f for f in self._matches(ev_layer, 'idintervento', key)
                     if self._valid_child_of_intervention(ev_layer, f, int_layer, int_feat)]
        evidences_ng = self._matches(evng_layer, 'idintervento', key)

        fid_map = {
            'magoh_scheda_intervento': [int_feat.id()],
            'magoh_ritrovamenti': [f.id() for f in findings],
            'magoh_evidenza_archeologica': [f.id() for f in evidences],
            'magoh_evidenza_archeologica_no_geom': [f.id() for f in evidences_ng],
        }

        root = self._record_item(int_layer, 'magoh_scheda_intervento', int_feat)
        self.browser.addTopLevelItem(root)
        used_ev, used_ng = set(), set()
        for finding in findings:
            fitem = self._record_item(finding_layer, 'magoh_ritrovamenti', finding)
            root.addChild(fitem)
            fkey = self._value(finding, 'id')
            for ev in evidences:
                if self._value(ev, 'idiii_livello') == fkey:
                    fitem.addChild(self._record_item(ev_layer, 'magoh_evidenza_archeologica', ev))
                    used_ev.add(int(ev.id()))
            for ev in evidences_ng:
                if self._value(ev, 'idiii_livello') == fkey:
                    fitem.addChild(self._record_item(evng_layer, 'magoh_evidenza_archeologica_no_geom', ev))
                    used_ng.add(int(ev.id()))

        # Evidenze collegate direttamente all'intervento ma non a uno dei
        # ritrovamenti filtrati restano comunque visibili come figli del padre.
        for ev in evidences:
            if int(ev.id()) not in used_ev:
                root.addChild(self._record_item(ev_layer, 'magoh_evidenza_archeologica', ev))
        for ev in evidences_ng:
            if int(ev.id()) not in used_ng:
                root.addChild(self._record_item(evng_layer, 'magoh_evidenza_archeologica_no_geom', ev))
        return fid_map, root

    def _build_finding_context(self, loaded, finding_layer, finding_feat):
        int_key = self._value(finding_feat, 'idscheda')
        int_layer, int_feat = self._first_intervention(loaded, int_key, finding_layer, finding_feat)
        finding_key = self._value(finding_feat, 'id')
        ev_layer = loaded.get('magoh_evidenza_archeologica')
        evng_layer = loaded.get('magoh_evidenza_archeologica_no_geom')
        evidences = self._matches(ev_layer, 'idiii_livello', finding_key)
        if int_layer is not None and int_feat is not None:
            evidences = [f for f in evidences if self._valid_child_of_intervention(ev_layer, f, int_layer, int_feat)]
        evidences_ng = self._matches(evng_layer, 'idiii_livello', finding_key)

        fid_map = {
            'magoh_scheda_intervento': [int_feat.id()] if int_feat is not None else [],
            'magoh_ritrovamenti': [finding_feat.id()],
            'magoh_evidenza_archeologica': [f.id() for f in evidences],
            'magoh_evidenza_archeologica_no_geom': [f.id() for f in evidences_ng],
        }
        if int_feat is not None:
            root = self._record_item(int_layer, 'magoh_scheda_intervento', int_feat)
            self.browser.addTopLevelItem(root)
            fitem = self._record_item(finding_layer, 'magoh_ritrovamenti', finding_feat)
            root.addChild(fitem)
        else:
            fitem = self._record_item(finding_layer, 'magoh_ritrovamenti', finding_feat)
            self.browser.addTopLevelItem(fitem)
            root = fitem
        for ev in evidences:
            fitem.addChild(self._record_item(ev_layer, 'magoh_evidenza_archeologica', ev))
        for ev in evidences_ng:
            fitem.addChild(self._record_item(evng_layer, 'magoh_evidenza_archeologica_no_geom', ev))
        return fid_map, fitem

    def _build_evidence_context(self, loaded, ev_layer, ev_feat, lname):
        int_key = self._value(ev_feat, 'idintervento')
        if lname == 'magoh_evidenza_archeologica':
            int_layer, int_feat = self._first_intervention(loaded, int_key, ev_layer, ev_feat)
        else:
            int_layer, int_feat = self._first_intervention(loaded, int_key)
        finding_key = self._value(ev_feat, 'idiii_livello')
        finding_layer, finding_feat = self._first_finding(
            loaded, finding_key, int_layer, int_feat
        )
        if int_feat is None and finding_feat is not None:
            int_layer, int_feat = self._first_intervention(loaded, self._value(finding_feat, 'idscheda'))

        fid_map = {
            'magoh_scheda_intervento': [int_feat.id()] if int_feat is not None else [],
            'magoh_ritrovamenti': [finding_feat.id()] if finding_feat is not None else [],
            'magoh_evidenza_archeologica': [ev_feat.id()] if lname == 'magoh_evidenza_archeologica' else [],
            'magoh_evidenza_archeologica_no_geom': [ev_feat.id()] if lname == 'magoh_evidenza_archeologica_no_geom' else [],
        }
        if int_feat is not None:
            root = self._record_item(int_layer, 'magoh_scheda_intervento', int_feat)
            self.browser.addTopLevelItem(root)
            parent_item = root
            if finding_feat is not None:
                fitem = self._record_item(finding_layer, 'magoh_ritrovamenti', finding_feat)
                root.addChild(fitem)
                parent_item = fitem
        elif finding_feat is not None:
            root = self._record_item(finding_layer, 'magoh_ritrovamenti', finding_feat)
            self.browser.addTopLevelItem(root)
            parent_item = root
        else:
            root = self._record_item(ev_layer, lname, ev_feat)
            self.browser.addTopLevelItem(root)
            return fid_map, root

        eitem = self._record_item(ev_layer, lname, ev_feat)
        parent_item.addChild(eitem)
        return fid_map, eitem

    def _refresh_for_feature(self, layer, feat):
        if not isinstance(layer, QgsVectorLayer) or not feat or not feat.isValid():
            return
        lname = self._layer_name(layer)
        if lname not in set(self.MAIN_LAYERS):
            self.lbl_context.setText(_tf(
                "Il layer non è una delle schede principali MAGOH.",
                "The layer is not one of the main MAGOH record layers."
            ))
            return

        # Riparti sempre dal dataset precedente al filtro MAGOH, così una nuova
        # ricerca non viene limitata dal risultato della ricerca precedente.
        tools = getattr(self.plugin, 'tools_dock', None)
        if tools is not None:
            tools.filter_panel._apply_timer.stop()
        if not self._apply_search_subset(layer, [feat]):
            return
        source_path, _ = self.plugin._split_gpkg_source(layer.source())
        loaded = self.plugin._layers_for_gpkg(source_path) if source_path else self._loaded_layers()
        if not loaded:
            self.lbl_context.setText(_tf(
                "Nessun set MAGOH riconosciuto nel GeoPackage attivo.",
                "No MAGOH dataset recognised in the active GeoPackage."
            ))
            return

        # Dopo il restore recuperiamo la feature dal layer corrente per essere
        # certi che appartenga al datasource non filtrato.
        try:
            feat = layer.getFeature(int(feat.id()))
        except Exception:
            pass
        if not feat or not feat.isValid():
            return

        self.browser.clear()
        self._filtered_mode = True
        self._set_browser_columns()
        self._current_layer_id = layer.id()
        self._current_fid = int(feat.id())

        if lname == 'magoh_scheda_intervento':
            fid_map, focus_item = self._build_intervention_context(loaded, layer, feat)
        elif lname == 'magoh_ritrovamenti':
            fid_map, focus_item = self._build_finding_context(loaded, layer, feat)
        else:
            fid_map, focus_item = self._build_evidence_context(loaded, layer, feat, lname)

        self._clear_main_selections(loaded)
        try:
            layer.selectByIds([feat.id()])
            self.iface.setActiveLayer(layer)
        except Exception:
            pass
        self.lbl_context.setText(_tf(
            "Filtro attivo — {layer}: {record}. Mostrati soltanto il record cercato e i collegamenti pertinenti.",
            "Active filter — {layer}: {record}. Only the requested record and relevant connections are shown.",
            layer=_t(self.FRIENDLY.get(lname, layer.name())), record=self._describe(lname, feat)
        ))
        try:
            self.browser.expandAll()
            self.browser.setCurrentItem(focus_item)
            self.browser.scrollToItem(focus_item)
            self.browser.resizeColumnToContents(0)
        except Exception:
            pass

    LABEL_WIDGETS = ('ValueRelation', 'ValueMap')

    def _label_lookup(self, layer, indexes):
        """V1.23.1: etichetta mostrata nella scheda per i campi a elenco (thesauri, ValueMap).

        Il valore salvato può essere un codice (crono_iniziale = 41) mentre la
        scheda mostra «Eta' altomedievale > VIII sec. d.C.». L'etichetta si
        calcola con il formattatore di QGIS una sola volta per valore distinto
        e si confronta in forma normalizzata (minuscole, senza accenti né
        punteggiatura). Solo lettura: nessun dato viene modificato.
        """
        from qgis.core import QgsApplication
        from .conversion.entity_summary import normalize
        registry = QgsApplication.fieldFormatterRegistry()
        specs = {}
        for index in indexes:
            setup = layer.editorWidgetSetup(index)
            if setup.type() in self.LABEL_WIDGETS:
                specs[index] = (registry.fieldFormatter(setup.type()), setup.config(), {})
        if not specs:
            return None

        def label(index, value):
            spec = specs.get(index)
            if spec is None:
                return ''
            formatter, config, memo = spec
            key = str(value)
            if key not in memo:
                try:
                    memo[key] = normalize(formatter.representValue(layer, index, config, None, value))
                except Exception:
                    memo[key] = ''
            return memo[key]
        return label

    def _search_all_attributes(self, layer, text):
        """Ricerca case-insensitive su tutti gli attributi, senza leggere geometrie.

        V1.23.1: per i campi a elenco vale anche l'etichetta mostrata nella scheda.
        """
        from .conversion.entity_summary import normalize
        field_name = self.search_field_combo.currentData()
        field_index = layer.fields().indexOf(str(field_name)) if field_name else -1
        indexes = [field_index] if field_index >= 0 else list(range(layer.fields().count()))
        label = self._label_lookup(layer, indexes) if normalize(text) else None
        if self._is_gna_layer(layer):
            from .search_index import TextIndex
            if not hasattr(self, '_gna_text_index'):
                self._gna_text_index = TextIndex()
            view = session_for(self.plugin).base_view(layer)
            return self._gna_text_index.find(view, text, field_name or '', cache_id=layer.id(), label=label)
        needle = str(text or '').strip().casefold()
        if not needle:
            return []
        norm_needle = normalize(text)
        matches = []
        req = QgsFeatureRequest()
        req.setFlags(QgsFeatureRequest.NoGeometry)
        if field_index >= 0:
            display_fields = {'codice_locale','idscheda', 'id', 'fid', 'anno', 'ubicazione', 'indirizzo', 'iii_livello', 'ii_livello',
                              'iv_livello', 'MOPR_ID', 'DSCH', 'NVCT', 'MOSI_ID', 'gid', 'OGTD', 'OGTN',
                              'DSCI', 'DSCD', 'denominazione', 'nome'}
            indexes = {field_index} | {i for i, field in enumerate(layer.fields()) if field.name() in display_fields}
            req.setSubsetOfAttributes(sorted(indexes))
        # Il dataset piu' grande MAGOH e' nell'ordine delle decine di migliaia:
        # una scansione su richiesta e' preferibile a costruire un OR SQL enorme
        # su decine di campi e resta indipendente dal provider (MAGOH/GNA).
        for feat in layer.getFeatures(req):
            found = False
            pairs = [(field_index, feat[field_index])] if field_index >= 0 else enumerate(feat.attributes())
            for index, value in pairs:
                if value is None:
                    continue
                try:
                    if isinstance(value, QVariant) and value.isNull():
                        continue
                except Exception:
                    pass
                txt = str(value).strip()
                if txt and needle in txt.casefold():
                    found = True
                    break
                if txt and label is not None and norm_needle in label(index, value):
                    found = True
                    break
            if found:
                matches.append(feat)
        return matches

    def _apply_search_subset(self, layer, matches):
        """Filter all matches and their parents/children, not just the first."""
        try:
            session_for(self.plugin).apply(layer, matches=[f.id() for f in matches])
            return True
        except RelatedFilterError as exc:
            QMessageBox.warning(self, _t("MAGOH – Filtri"), str(exc))
            return False

    def search_by_id(self):
        """Ricerca unica: tutti gli attributi oppure ID logico esatto."""
        if self.search_layer_combo.currentData() in (self.GNA_MOSI_KEY, self.GNA_DSC_KEY):
            return self._search_gna_group(self._search_selected_layers())
        layer = self._search_selected_layer()
        value = self.search_id.text().strip()
        if not isinstance(layer, QgsVectorLayer):
            self.lbl_context.setText(_tf('Scegli un layer MAGOH o GNA.', 'Choose a MAGOH or GNA layer.'))
            return
        if not value:
            self.lbl_context.setText(_tf('Inserisci un testo o un ID da cercare.', 'Enter text or an ID to search.'))
            return
        lname = self._layer_name(layer)
        exact_id = bool(self.chk_exact_id.isChecked())
        from .gna_identity import gna_role
        self._set_browser_columns('intervento' if lname == 'magoh_scheda_intervento' else (gna_role(layer) or ''))
        tools = getattr(self.plugin, 'tools_dock', None)
        if tools is not None:
            tools.filter_panel._apply_timer.stop()
        if not self._restore_base_subsets(discard=False):
            return
        try:
            if exact_id:
                field = self._logical_id_field(lname, layer)
                if not field:
                    self.lbl_context.setText(_tf(
                        'Il layer non possiede un campo ID riconosciuto.',
                        'The layer has no recognised ID field.'
                    ))
                    return
                expr = self._expr_equal_text(field,value)
                if layer.fields().indexOf('codice_locale') >= 0:
                    expr += ' OR ' + self._expr_equal_text('codice_locale',value.upper())
                matches = list(layer.getFeatures(QgsFeatureRequest().setFilterExpression(expr)))
            else:
                matches = self._search_all_attributes(layer, value)
        except Exception as e:
            self.lbl_context.setText(_tf('Ricerca non riuscita: {error}', 'Search failed: {error}', error=e))
            return

        if not matches:
            if not self._apply_search_subset(layer, []):
                return
            self.browser.clear()
            self._current_layer_id = layer.id()
            self._current_fid = None
            layer.removeSelection()
            self.lbl_context.setText(_tf(
                'Nessun record corrisponde a “{value}” in {layer}.',
                'No records match “{value}” in {layer}.',
                value=value, layer=layer.name()
            ))
            return

        # Con un solo risultato MAGOH manteniamo il comportamento storico:
        # ricostruzione immediata della gerarchia padre/figli.
        if len(matches) == 1 and lname in set(self.MAIN_LAYERS):
            self._refresh_for_feature(layer, matches[0])
            return

        if not self._apply_search_subset(layer, matches):
            return
        self.browser.clear()
        self._set_browser_columns('intervento' if lname == 'magoh_scheda_intervento' else (gna_role(layer) or ''))
        self._filtered_mode = False
        self._current_layer_id = layer.id()
        self._current_fid = None
        max_show = min(len(matches), self.MAX_BROWSER_RECORDS)
        for feat in matches[:max_show]:
            self.browser.addTopLevelItem(self._record_item(layer, lname, feat))
        if len(matches) > max_show:
            more = QTreeWidgetItem([_tf(
                '… {shown} di {total} risultati visualizzati',
                '… showing {shown} of {total} results',
                shown=max_show, total=len(matches)), ''])
            more.setFlags(Qt.ItemFlag.NoItemFlags)
            self.browser.addTopLevelItem(more)
        try:
            layer.selectByIds([f.id() for f in matches[:500]])
            self.iface.setActiveLayer(layer)
            self.iface.mapCanvas().refresh()
        except Exception:
            pass
        scope = 'MAGOH' if lname in set(self.MAIN_LAYERS) else ('GNA' if self._is_gna_layer(layer) else layer.name())
        self.lbl_context.setText(_tf(
            '{n} risultati in {layer} ({scope}). Filtrati anche padri e figli collegati; “Mostra tutti” ripristina i filtri precedenti.',
            '{n} results in {layer} ({scope}). Related parents and children are filtered too; “Show all” restores the previous filters.',
            n=len(matches), layer=layer.name(), scope=scope
        ))

    def _search_gna_group(self, layers):
        """Search one logical GNA family across all its geometry variants."""
        family = 'DSC' if self.search_layer_combo.currentData() == self.GNA_DSC_KEY else 'MOSI'
        value = self.search_id.text().strip()
        if not layers:
            self.lbl_context.setText(_tf('Nessun layer {family} caricato.', 'No {family} layer loaded.', family=family))
            return
        if not value:
            self.lbl_context.setText(_tf('Inserisci un testo o un ID da cercare.', 'Enter text or an ID to search.'))
            return
        tools = getattr(self.plugin, 'tools_dock', None)
        if tools is not None:
            tools.filter_panel._apply_timer.stop()
        exact_id = bool(self.chk_exact_id.isChecked())
        found = []
        try:
            for layer in layers:
                lname = self._layer_name(layer)
                if exact_id:
                    field = self._logical_id_field(lname, layer)
                    view = session_for(self.plugin).base_view(layer)
                    matches = list(view.getFeatures(QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry).setFilterExpression(
                        self._expr_equal_text(field, value)))) if field else []
                else:
                    matches = self._search_all_attributes(layer, value)
                found.append((layer, matches))
        except Exception as e:
            self.lbl_context.setText(_tf('Ricerca non riuscita: {error}', 'Search failed: {error}', error=e))
            return
        try:
            session_for(self.plugin).apply_many([(layer, [f.id() for f in matches]) for layer, matches in found])
        except RelatedFilterError as exc:
            QMessageBox.warning(self, _t("MAGOH – Filtri"), str(exc))
            return
        self.browser.clear()
        self._set_browser_columns(family.lower())
        self._filtered_mode = False
        self._current_layer_id = None
        self._current_fid = None
        total = sum(len(m) for _, m in found)
        shown = 0
        for layer, matches in found:
            lname = self._layer_name(layer)
            try:
                layer.selectByIds([f.id() for f in matches[:500]])
            except Exception:
                pass
            for feat in matches:
                if shown >= self.MAX_BROWSER_RECORDS:
                    break
                self.browser.addTopLevelItem(self._record_item(layer, lname, feat, label=layer.name()))
                shown += 1
        if total > shown:
            more = QTreeWidgetItem([_tf('… {shown} di {total} risultati visualizzati', '… showing {shown} of {total} results',
                                        shown=shown, total=total), ''])
            more.setFlags(Qt.ItemFlag.NoItemFlags)
            self.browser.addTopLevelItem(more)
        try:
            self.iface.mapCanvas().refresh()
        except Exception:
            pass
        detail = ', '.join('%s: %d' % (layer.name(), len(m)) for layer, m in found)
        if total:
            self.lbl_context.setText(_tf(
                '{n} risultati nei {family} ({detail}). Filtrati anche i DSC/MOSI collegati; MOPR escluso. “Mostra tutti” ripristina.',
                '{n} results in {family} ({detail}). Related DSC/MOSI are filtered too; MOPR excluded. “Show all” restores.',
                n=total, family=family, detail=detail))
        else:
            self.lbl_context.setText(_tf('Nessun {family} corrisponde a “{value}”.', 'No {family} matches “{value}”.', family=family, value=value))

    def _browser_item_clicked(self, item, _column=0):
        if item.data(0, Qt.ItemDataRole.UserRole + 20) != 'record':
            return
        layer = QgsProject.instance().mapLayer(str(item.data(0, Qt.ItemDataRole.UserRole)))
        try:
            feat = layer.getFeature(int(item.data(0, Qt.ItemDataRole.UserRole + 1))) if isinstance(layer, QgsVectorLayer) else None
        except Exception:
            feat = None
        if not layer or not feat or not feat.isValid():
            return
        lname = self._layer_name(layer)
        if not self._filtered_mode:
            if lname in set(self.MAIN_LAYERS):
                self._refresh_for_feature(layer, feat)
            else:
                # GNA/generico: il filtro dinamico resta sul layer interrogato,
                # il click seleziona il record senza inventare relazioni MAGOH.
                self._current_layer_id = layer.id()
                self._current_fid = int(feat.id())
                try:
                    self.iface.setActiveLayer(layer)
                    layer.selectByIds([feat.id()])
                except Exception:
                    pass
            return
        self._current_layer_id = layer.id()
        self._current_fid = int(feat.id())
        try:
            self.iface.setActiveLayer(layer)
            layer.selectByIds([feat.id()])
        except Exception:
            pass

    def _browser_item_double_clicked(self, item, _column=0):
        if item.data(0, Qt.ItemDataRole.UserRole + 20) != 'record':
            return
        self._browser_item_clicked(item, _column)
        self.open_current()

    def _disconnect_selection_signal(self):
        layer = self._selection_signal_layer
        self._selection_signal_layer = None
        if layer is not None:
            try:
                layer.selectionChanged.disconnect(self._on_map_selection_changed)
            except Exception:
                pass


    def _on_map_selection_changed(self, *args):
        layer = self._selection_signal_layer
        if not isinstance(layer, QgsVectorLayer):
            self._disconnect_selection_signal()
            return
        try:
            ids = layer.selectedFeatureIds()
            if len(ids) == 1:
                feat = layer.getFeature(ids[0])
                if feat and feat.isValid():
                    self._disconnect_selection_signal()
                    self._refresh_for_feature(layer, feat)
        except Exception:
            self._disconnect_selection_signal()


    def _selected_target(self):
        item = self.browser.currentItem()
        if item and item.data(0, Qt.ItemDataRole.UserRole + 20) == 'record':
            layer = QgsProject.instance().mapLayer(str(item.data(0, Qt.ItemDataRole.UserRole)))
            if isinstance(layer, QgsVectorLayer):
                try:
                    feat = layer.getFeature(int(item.data(0, Qt.ItemDataRole.UserRole + 1)))
                    if feat and feat.isValid():
                        return layer, feat
                except Exception:
                    pass
        if self._current_layer_id is not None and self._current_fid is not None:
            layer = QgsProject.instance().mapLayer(str(self._current_layer_id))
            if isinstance(layer, QgsVectorLayer):
                feat = layer.getFeature(int(self._current_fid))
                if feat and feat.isValid():
                    return layer, feat
        return None, None

    def zoom_current(self):
        layer, feat = self._selected_target()
        if not layer or not feat or not feat.isValid():
            return
        self.iface.setActiveLayer(layer)
        try:
            layer.selectByIds([feat.id()])
            if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.NullGeometry:
                self.iface.mapCanvas().zoomToSelected(layer)
                self.iface.mapCanvas().refresh()
        except Exception:
            pass

    def open_current(self):
        layer, feat = self._selected_target()
        if not layer or not feat or not feat.isValid():
            return
        self.iface.setActiveLayer(layer)
        layer.selectByIds([feat.id()])
        try:
            self.iface.openFeatureForm(layer, feat, False)
        except Exception as e:
            QMessageBox.warning(self, _tf("MAGOH – Collegamenti", "MAGOH – Connections"), _tf(
                "Impossibile aprire la scheda:\n{error}",
                "Could not open the record:\n{error}", error=e
            ))

class MagohToolsDockWidget(QDockWidget):
    """V1: Management (records + chronology), API, PDF and backup."""

    def sizeHint(self):
        # Comfortable initial width, while retaining user resizing/docking.
        return QSize(440, 640)

    def __init__(self, iface, plugin):
        super().__init__("", iface.mainWindow())
        # Keep the native dock controls without duplicating the internal header.
        self.setAccessibleName(_tf("MAGOH Spell – Funzioni e tool", "MAGOH Spell – Functions and tools"))
        self.toggleViewAction().setText(_tf("MAGOH Spell – Funzioni e tool", "MAGOH Spell – Functions and tools"))
        self.iface = iface
        self.plugin = plugin
        self.setObjectName("MagohToolsDock")
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.setMinimumWidth(280)

        root = QWidget()
        root.setObjectName("MagohSurface")
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.setTitleBarWidget(dock_header(
            self, _tf("Funzioni e tool", "Functions and tools"),
            _tf("Sgancia pannello", "Float panel"), _tf("Chiudi pannello", "Close panel"),
            dock_label=_tf("Riaggancia pannello", "Dock panel"), translate=_t))

        self.tabs = tabs = QTabWidget()
        tabs.setObjectName("MagohMainTabs")
        self.management_tabs = QTabWidget()
        self.management_tabs.setObjectName("MagohSubTabs")
        self.relation_panel = MagohRelationNavigator(iface, plugin)
        from .gui.chronology_panel import ChronologyPanel
        self.filter_panel = ChronologyPanel(iface, plugin)

        def page_with_footer(body, footer):
            """Scrolling body + fixed footer: commands never scroll away."""
            page = QWidget()
            page.setObjectName("MagohPage")
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            scroll, _body, _layout = scroll_page(body)
            layout.addWidget(scroll, 1)
            layout.addWidget(footer)
            return page

        # Gestione → Schede e attributi / Cronologia e categorie
        self.management_tabs.addTab(
            page_with_footer(self.relation_panel, self.relation_panel.action_bar),
            _tf("Schede e attributi", "Records and attributes"))
        self.management_tabs.addTab(
            page_with_footer(self.filter_panel, self.filter_panel.action_bar),
            _tf("Cronologia e categorie", "Chronology and categories"))
        self.management_tabs.setToolTip(_tf(
            "Applicare una ricerca sostituisce i filtri cronologici e viceversa. «Mostra tutti» toglie ogni filtro dai layer MAGOH e collegati.",
            "Applying a search replaces chronology filters and vice versa. ‘Show all’ removes every filter from MAGOH and related layers."
        ))
        tabs.addTab(self.management_tabs, _tf("Gestione", "Management"))

        # Dati e conversione: one row per tool, purpose stated once.
        scroll, _body, ld = scroll_page(spacing=SPACE_M)
        api_row, b_api = tool_row(
            _tf("Importa da MAGOH API", "Import from MAGOH API"),
            _tf("Importa le schede MAGOH scegliendo Provincia e Comune.", "Import MAGOH records by Province and Municipality."),
            _tf("Importa nel template…", "Import into template…"),
            plugin.open_api_dialog, primary=True,
            tooltip=_tf(
                "Importa da MAGOH tramite search-adv nel template locale, preservando gli ID ufficiali e controllando duplicati e relazioni. La connessione al server è in sola lettura.",
                "Import from MAGOH via search-adv into the local template, preserving official IDs and checking duplicates and relations. The server connection is read-only."
            ))
        ld.addWidget(api_row)
        gna_row, b_gna = tool_row(
            _tf("MAGOH → GNA", "MAGOH → GNA"),
            _tf("Prima prepara e salva il MOPR, poi avvia la conversione.", "Prepare and save the MOPR before starting conversion."),
            _tf("Convertitore MAGOH-GNA", "MAGOH-GNA converter"),
            plugin.open_gna_export_dialog,
            tooltip=_tf("Prepara un template di lavoro con i livelli interni al MOPR", "Prepare a working template with levels inside MOPR"))
        # V1.14: the official MOPR guide is reachable before opening the converter too.
        try:
            from .gui.mopr_guide import guide_button
            gna_guide = guide_button(gna_row, 'MagohMoprGuideDock')
            set_role(gna_guide, compact=True)
            guide_line = QHBoxLayout()
            guide_line.setContentsMargins(0, 0, 0, 0)
            guide_line.addWidget(gna_guide)
            guide_line.addStretch(1)
            gna_row.layout().addLayout(guide_line)
        except Exception as exc:
            _log(f"Guida MOPR nel pannello: {exc}", Qgis.Warning)
        ld.addWidget(gna_row)
        ld.addStretch(1)
        tabs.addTab(scroll, _tf("Dati e conversione", "Data and conversion"))

        # Esportazioni
        scroll, _body, le = scroll_page(spacing=SPACE_M)
        pdf_row, b_pdf = tool_row(
            _tf("Schede PDF", "PDF records"),
            _tf("PDF multipagina: localizzazione cartografica e scheda completa degli attributi.",
                "Multi-page PDF: map location and the complete attribute record."),
            "Esporta schede PDF", plugin.open_pdf_export_dialog, primary=True)
        le.addWidget(pdf_row)
        backup_row, b_backup = tool_row(
            _tf("Backup del progetto", "Project backup"),
            _tf("Copia coerente del GeoPackage salvato. Non include allegati o file QGIS esterni.",
                "Consistent copy of the saved GeoPackage. Does not include attachments or external QGIS files."),
            "Crea backup del progetto", plugin.create_backup)
        le.addWidget(backup_row)
        b_backup_elsewhere = QPushButton(_tf('Backup in un’altra cartella…', 'Backup to another folder…'))
        b_backup_elsewhere.clicked.connect(lambda: plugin.create_backup(choose_destination=True))
        le.addWidget(b_backup_elsewhere)
        le.addStretch(1)
        tabs.addTab(scroll, _t("Esportazioni"))

        v.addWidget(tabs, 1)
        self.setWidget(root)
        apply_teal_style(root)


class LauncherDialog(QDialog):
    """Project window: create/open first, then session status; same language as the dock."""

    def __init__(self, iface, plugin):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.plugin = plugin
        # Con un titolo vuoto Windows mostra il nome dell'applicazione (QGIS3).
        self.setWindowTitle("MAGOH Spell")
        self.setAccessibleName(_tf("MAGOH Spell – Progetto", "MAGOH Spell – Project"))
        self.setMinimumSize(460, 360)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- Brand header: artist's hat, name, language and MAGOH info.
        header = brand_header(_tf("Progetto e dati", "Project and data"), self, logo_size=52)
        logo = header.findChild(QLabel, "MagohPanelLogo")
        logo.setObjectName("MagohLauncherLogo")
        self._artwork_logo = logo
        self._credits_dialog = None
        logo.setCursor(Qt.CursorShape.PointingHandCursor)
        logo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        logo.setAccessibleName('Crediti immagine / Image credits: Vittoria Bellon, @icestorming')
        logo.setToolTip('Crediti dell’immagine / Image credits — Vittoria Bellon (@icestorming)')
        logo.installEventFilter(self)
        tools = QVBoxLayout()
        tools.setSpacing(SPACE_S)
        # Etichette fisse e bilingui nei suggerimenti: non cambiano larghezza
        # al cambio lingua e non si deformano sui pulsanti checkable.
        lang_box, (b_it, b_en) = segmented(("IT", "EN"), header)
        self.b_it, self.b_en = b_it, b_en
        lang_box.setToolTip("Lingua / Language")
        b_it.setToolTip("Italiano")
        b_en.setToolTip("English")
        b_it.setAccessibleName("Lingua: Italiano / Language: Italian")
        b_en.setAccessibleName("Lingua: inglese / Language: English")
        b_it.setChecked(self.plugin.current_language() == "it")
        b_en.setChecked(self.plugin.current_language() == "en")

        def _choose_language(code):
            # Mantiene sempre esattamente un pulsante selezionato, anche se
            # l'utente clicca di nuovo sulla lingua già attiva.
            b_it.setChecked(code == "it")
            b_en.setChecked(code == "en")
            self.plugin.set_language(code)

        b_it.clicked.connect(lambda _checked=False: _choose_language("it"))
        b_en.clicked.connect(lambda _checked=False: _choose_language("en"))
        tools.addWidget(lang_box, 0, Qt.AlignmentFlag.AlignRight)
        b_info = QPushButton(_t("Info MAGOH"), header)
        b_info.setIcon(ui_icon("info_white"))
        b_info.setToolTip(_tf("Informazioni sul progetto MAGOH e collegamento al front-end",
                              "About the MAGOH project and link to the front-end"))
        b_info.setAutoDefault(False)
        set_role(b_info, primary=True, compact=True)
        b_info.clicked.connect(self.plugin.open_info_dialog)
        tools.addWidget(b_info, 0, Qt.AlignmentFlag.AlignRight)
        header.layout().addLayout(tools)
        outer.addWidget(header)

        # --- Body: scrolls only when the window is shorter than its content.
        scroll, self.content, v = scroll_page(margins=(SPACE_L + 2, SPACE_L, SPACE_L + 2, SPACE_L), spacing=SPACE_L)
        outer.addWidget(scroll, 1)

        subtitle = hint(_tf("Crea o riapri un progetto MAGOH, oppure collega un archivio nuovo o esistente al progetto QGIS corrente.",
                            "Create or reopen a MAGOH project, or connect a new or existing archive to the current QGIS project."))
        v.addWidget(subtitle)

        start = QVBoxLayout()
        start.setSpacing(SPACE_M)
        b_new_proj = QPushButton(_tf("Crea nuovo progetto", "Create new project"))
        b_new_proj.setIcon(ui_icon("plus_white"))
        b_new_proj.setToolTip(_tf("Cartella propria e GeoPackage pulito: nessun dato dei progetti precedenti viene riutilizzato.",
                                  "Own folder and a clean GeoPackage: no data from previous projects is reused."))
        b_new_proj.clicked.connect(self.plugin.create_new_project)
        b_existing = QPushButton(_tf("Apri progetto esistente", "Open existing project"))
        b_existing.setIcon(ui_icon("folder_white"))
        b_existing.setToolTip(_tf("Scegli la cartella radice del progetto: il GeoPackage viene individuato automaticamente",
                                  "Choose the project root folder: the GeoPackage is detected automatically"))
        b_existing.clicked.connect(self.plugin.open_existing_magoh_project)
        b_add = QPushButton(_tf('Aggiungi al progetto corrente', 'Add to the current project'))
        b_add.setIcon(ui_icon('plus_white'))
        b_add.clicked.connect(self.plugin.add_archive_to_current_project)
        self.project_buttons = (b_new_proj, b_existing, b_add)
        for button in self.project_buttons:
            set_role(button, primary=True, hero=True)
            button.setIconSize(QSize(20, 20))
            button.setAutoDefault(False)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            start.addWidget(button)
        v.addLayout(start)

        # --- Session status as a readable key/value card.
        status_section, status_layout = section(_t("Stato sessione"))
        self.status_toggle = QToolButton(self)
        self.status_toggle.setText(_t('Stato sessione'))
        self.status_toggle.setCheckable(True)
        self.status_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.status_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.status_toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        set_role(self.status_toggle, primary=True)
        status_section.caption.hide()
        status_section.hide()
        def toggle_status(expanded):
            self.status_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
            status_section.setVisible(expanded)
            if expanded:
                self._refresh_status()
        self.status_toggle.toggled.connect(toggle_status)
        self.status_section = status_section
        status_grid, self._status_values = key_value_grid((
            ("qgis", "QGIS"),
            ("project", _tf("Progetto QGIS", "QGIS project")),
            ("magoh", _tf("Progetto MAGOH", "MAGOH project")),
            ("records", _tf("Schede", "Records")),
            ("relations", _tf("Relazioni", "Relations")),
            ("folder", _tf("Cartella", "Folder")),
            ("active", _tf("Layer attivo", "Active layer")),
        ))
        status_layout.addWidget(status_grid)
        # Testo completo conservato per compatibilità (copia/diagnostica).
        self.lbl_status = QLabel()
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setVisible(False)
        status_layout.addWidget(self.lbl_status)
        status_buttons = QHBoxLayout()
        status_buttons.setContentsMargins(0, SPACE_S, 0, 0)
        status_buttons.setSpacing(SPACE_S)
        b_project_info = QPushButton(_tf("Info progetto attivo", "Active project info"))
        b_project_info.setToolTip("Mostra percorso, database, conteggi e stato delle relazioni del progetto aperto")
        b_project_info.clicked.connect(self.plugin.show_project_info)
        b_refresh_status = QPushButton("Aggiorna stato")
        b_refresh_status.clicked.connect(self._refresh_status)
        b_diagnostics = QPushButton("Verifica installazione")
        b_diagnostics.clicked.connect(self.plugin.run_diagnostics)
        for button in (b_project_info, b_refresh_status, b_diagnostics):
            # Verde petrolio MAGOH su tutti i comandi della finestra Progetto.
            set_role(button, primary=True, compact=True)
            button.setAutoDefault(False)
            status_buttons.addWidget(button)
        status_buttons.addStretch(1)
        status_layout.addLayout(status_buttons)
        v.addWidget(self.status_toggle)
        v.addWidget(status_section)
        v.addStretch(1)

        # --- Close stays reachable at any size.
        bar, close_row = action_bar(self, margins=(SPACE_L + 2, SPACE_M, SPACE_L + 2, SPACE_M))
        close_row.addStretch(1)
        b_close = QPushButton("Chiudi")
        set_role(b_close, primary=True)
        b_close.setAutoDefault(False)
        b_close.clicked.connect(self.hide)
        close_row.addWidget(b_close)
        outer.addWidget(bar)

        apply_teal_style(self)
        self._refresh_status()

    def sizeHint(self):
        return QSize(620, 520)

    def eventFilter(self, watched, event):
        if watched is self._artwork_logo:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                event.accept()
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                if self._artwork_logo.rect().contains(event.pos()):
                    self._show_artwork_credits()
                return True
            if event.type() == QEvent.Type.KeyPress and event.key() in (
                    Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                self._show_artwork_credits()
                return True
        return super().eventFilter(watched, event)

    def _show_artwork_credits(self):
        from .gui.artwork_credits import ArtworkCreditsDialog
        if self._credits_dialog is None:
            self._credits_dialog = ArtworkCreditsDialog(self, _language())
        self._credits_dialog.retranslate(_language())
        self._credits_dialog.show()
        self._credits_dialog.raise_()
        self._credits_dialog.activateWindow()

    def hideEvent(self, event):
        if self._credits_dialog is not None:
            self._credits_dialog.close()
        super().hideEvent(event)

    def showEvent(self, event):
        self.status_toggle.setChecked(False)
        super().showEvent(event)

    def _refresh_status(self):
        en = _language() == "en"
        if self._credits_dialog is not None:
            self._credits_dialog.retranslate(_language())
        try:
            project = QgsProject.instance()
            project_name = os.path.basename(project.fileName()) if project.fileName() else ("Unsaved project" if en else "Progetto non salvato")
            n_layers = len(project.mapLayers())
        except Exception:
            project_name = "Unavailable" if en else "Non disponibile"
            n_layers = 0

        try:
            al = self.iface.activeLayer()
            if al and isinstance(al, QgsVectorLayer):
                active_txt = (
                    f"{al.name()} ({al.featureCount()} records; {al.selectedFeatureCount()} selected)"
                    if en else
                    f"{al.name()} ({al.featureCount()} record; {al.selectedFeatureCount()} selezionati)"
                )
            elif al:
                active_txt = al.name()
            else:
                active_txt = "none" if en else "nessuno"
        except Exception:
            active_txt = "unavailable" if en else "non disponibile"

        linked = self.plugin.get_active_gpkg()
        linked_txt = os.path.basename(linked) if linked else ("none" if en else "nessuno")
        if linked and not os.path.exists(linked):
            linked_txt += " (file not found!)" if en else " (file non trovato!)"
        root = self.plugin.get_project_root() if linked else ""
        root_txt = root if root else "—"
        counts_txt = ""
        if linked and os.path.exists(linked):
            try:
                cc = self.plugin._table_counts(linked, self.plugin.CORE_MAGOH_TABLES[:3])
                labels = (
                    (("magoh_scheda_intervento", "Interventions"), ("magoh_ritrovamenti", "Findings"), ("magoh_evidenza_archeologica", "Evidence"))
                    if en else
                    (("magoh_scheda_intervento", "Interventi"), ("magoh_ritrovamenti", "Ritrovamenti"), ("magoh_evidenza_archeologica", "Evidenze"))
                )
                counts_txt = "  •  " + " / ".join(f"{label}: {cc.get(tbl, 'n/a' if en else 'n/d')}" for tbl, label in labels)
            except Exception:
                counts_txt = ""

        rel_txt = "n/a" if en else "n/d"
        try:
            loaded = self.plugin._layers_for_gpkg(linked) if linked else {}
            expected = [r for r in self.plugin.MAGOH_RELATIONS if r[2] in loaded and r[4] in loaded]
            if expected:
                rels = QgsProject.instance().relationManager().relations()
                valid = sum(1 for r in expected if r[0] in rels and rels[r[0]].isValid())
                rel_txt = f"{valid}/{len(expected)}"
            elif loaded:
                rel_txt = "0 (partial schema)" if en else "0 (schema parziale)"
        except Exception:
            rel_txt = "unavailable" if en else "non disponibile"

        try:
            qgis_ver = Qgis.QGIS_VERSION
        except Exception:
            qgis_ver = "3.x"

        if en:
            self.lbl_status.setText(
                f"QGIS {qgis_ver}  •  QGIS project: {project_name}  •  Layers: {n_layers}\n"
                f"MAGOH project: {linked_txt}  •  Relations: {rel_txt}{counts_txt}\n"
                f"Project folder: {root_txt}\n"
                f"Active layer: {active_txt}"
            )
        else:
            self.lbl_status.setText(
                f"QGIS {qgis_ver}  •  Progetto QGIS: {project_name}  •  Layer: {n_layers}\n"
                f"Progetto MAGOH: {linked_txt}  •  Relazioni: {rel_txt}{counts_txt}\n"
                f"Cartella progetto: {root_txt}\n"
                f"Layer attivo: {active_txt}"
            )

        # Vista leggibile chiave/valore; il testo completo resta in lbl_status.
        values = getattr(self, "_status_values", {})
        rows = {
            "qgis": qgis_ver,
            "project": f"{project_name}  ·  " + (f"{n_layers} layers" if en else f"{n_layers} layer"),
            "magoh": linked_txt,
            "records": counts_txt.replace("  •  ", "", 1).strip() or ("n/a" if en else "n/d"),
            "relations": rel_txt,
            "folder": root_txt,
            "active": active_txt,
        }
        for key, text in rows.items():
            if key in values:
                values[key].setText(text)


