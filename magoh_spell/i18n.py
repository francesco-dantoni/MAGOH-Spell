"""Interfaccia bilingue IT/EN di MAGOH Spell.

Modulo autonomo (solo Qt): i moduli gui/ lo importano senza dipendere da
magoh_spell.py. La lingua scelta è salvata in QSettings e applicata alla
costruzione dei widget; nomi di layer, ID e valori archeologici non vengono
mai tradotti, perché solo le etichette note dell'interfaccia sono nel dizionario.
"""
from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QGroupBox, QLabel, QLineEdit, QPushButton, QTabWidget,
    QToolButton, QTreeWidget, QWidget,
)

LANGUAGE_SETTING = "magoh_spell/language"

EN_TRANSLATIONS = {
    # Toolbar/menu/dashboard
    "MAGOH – Strumenti": "MAGOH – Tools",
    "Crea nuovo progetto MAGOH…": "Create new MAGOH project…",
    "Apri progetto MAGOH esistente…": "Open existing MAGOH project…",
    "Esporta schede (PDF) – mappa + attributi…": "Export records (PDF) – map + attributes…",
    "Info MAGOH": "About MAGOH",
    "MAGOH – Informazioni": "MAGOH – About",
    "Stato sessione": "Session status",
    "Aggiorna stato": "Refresh status",
    "Verifica installazione": "Check installation",
    "Progetto": "Project",
    "Chiudi": "Close",
    # Project/new project
    "Nome progetto": "Project name",
    "Directory contenitore": "Parent directory",
    "Sfoglia…": "Browse…",
    "Crea progetto": "Create project",
    "Annulla": "Cancel",
    # Tools/navigator
    "Collegamenti": "Connections",
    "Esportazioni": "Exports",
    "Layer": "Layer",
    "ID": "ID",
    "Cerca": "Search",
    "Record": "Record",
    "Relazione": "Relation",
    "Zoom": "Zoom",
    "Apri scheda": "Open record",
    "Apri scheda figlio": "Open child record",
    "Seleziona un record figlio nella tabella.": "Select a child record in the table.",
    "Schede di Intervento": "Intervention Records",
    "Ritrovamenti": "Findings",
    "Evidenze archeologiche": "Archaeological Evidence",
    "Evidenze senza geometria": "Evidence without geometry",
    "Scheda di Intervento": "Intervention Record",
    "Scheda di Ritrovamento": "Finding Record",
    "Esporta schede PDF": "Export records to PDF",
    "Crea backup del progetto": "Create project backup",
    "Dati": "Data",
    "Sorgente": "Source",
    "Controllo": "Check",
    # Generic export/report/filter UI
    "GeoPackage": "GeoPackage",
    "Recenti": "Recent",
    "Aggiungi layer": "Add layers",
    "Apri progetto": "Open project",
    "Esporta": "Export",
    "Usa i layer visibili nel canvas": "Use visible canvas layers",
    "Isola il record corrente": "Isolate current record",
    "Aggiorna / usa layer attivo": "Refresh / use active layer",
    "Metodo stabile (consigliato)": "Stable method (recommended)",
    "Metodo Atlas": "Atlas method",
    "Scegli…": "Choose…",
    "Tutti": "All",
    # Additional dialogs
    "Output": "Output",
    "Cartella di output": "Output folder",
    "Aggiorna": "Refresh",
    "MAGOH – Crea nuovo progetto": "MAGOH – Create new project",
    "MAGOH – GeoPackage": "MAGOH – GeoPackage",
    "MAGOH – Export PDF": "MAGOH – PDF export",
    "Esporta schede (PDF)": "Export records (PDF)",
    "Parametri": "Parameters",
    "Metodo": "Method",
    "Metodo Atlas (più veloce)": "Atlas method (faster)",
    "Esporta solo feature selezionate": "Export selected features only",
    "Campi per pagina (scheda attributi)": "Fields per page (attribute sheet)",
    "Margine mappa (%)": "Map margin (%)",
    "Usa i layer visibili nel canvas per la mappa": "Use visible canvas layers for the map",
    "Output PDF": "Output PDF",
    "Progetto QGIS": "QGIS project",
}
IT_TRANSLATIONS = {v: k for k, v in EN_TRANSLATIONS.items()}


def _language():
    try:
        value = str(QSettings().value(LANGUAGE_SETTING, "it") or "it").lower()
    except Exception:
        value = "it"
    return "en" if value.startswith("en") else "it"


def _t(text):
    """Translate a static UI string according to the current preference."""
    if text is None:
        return text
    text = str(text)
    if _language() == "en":
        return EN_TRANSLATIONS.get(text, text)
    # Allows an already-open action to be translated back to Italian.
    return IT_TRANSLATIONS.get(text, text)


def _tf(it_text, en_text, **kwargs):
    try:
        it = it_text.format(**kwargs)
        en = en_text.format(**kwargs)
    except Exception:
        it, en = it_text, en_text
    # Keep the pair so existing widgets can change language without rebuilding.
    EN_TRANSLATIONS[it] = en
    IT_TRANSLATIONS[en] = it
    return en if _language() == "en" else it


def _translate_widget_tree(root):
    """Translate static text of an already-created Qt widget hierarchy.

    This keeps localisation independent from QGIS rendering/data code. Dynamic
    archaeological values are untouched because only known UI strings map.
    """
    if root is None:
        return
    try:
        if hasattr(root, "windowTitle") and hasattr(root, "setWindowTitle"):
            root.setWindowTitle(_t(root.windowTitle()))
    except Exception:
        pass
    widgets = [root]
    try:
        widgets += list(root.findChildren(QWidget))
    except Exception:
        pass
    seen = set()
    for w in widgets:
        if id(w) in seen:
            continue
        seen.add(id(w))
        try:
            if isinstance(w, QLabel):
                w.setText(_t(w.text()))
            elif isinstance(w, (QPushButton, QToolButton)):
                w.setText(_t(w.text()))
            elif isinstance(w, QCheckBox):
                w.setText(_t(w.text()))
            elif isinstance(w, QGroupBox):
                w.setTitle(_t(w.title()))
            if hasattr(w, "toolTip") and hasattr(w, "setToolTip") and w.toolTip():
                w.setToolTip(_t(w.toolTip()))
            if isinstance(w, QLineEdit) and w.placeholderText():
                w.setPlaceholderText(_t(w.placeholderText()))
            if isinstance(w, QTabWidget):
                for i in range(w.count()):
                    w.setTabText(i, _t(w.tabText(i)))
            if isinstance(w, QComboBox):
                for i in range(w.count()):
                    # Never translate layer names, field names or attribute values.
                    if w.itemData(i) not in (None, "") and not w.property('magoh_translate_choices'):
                        continue
                    txt = w.itemText(i)
                    translated = _t(txt)
                    if translated != txt:
                        w.setItemText(i, translated)
            if isinstance(w, QTreeWidget):
                header = w.headerItem()
                if header:
                    for i in range(w.columnCount()):
                        header.setText(i, _t(header.text(i)))
        except Exception:
            pass
