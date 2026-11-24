# -*- coding: utf-8 -*-
"""Validazione della conversione MAGOH → GNA, Template 1.6.0.

Tre classi di esito, che non vanno confuse fra loro:

ERROR
    Il dato non è esportabile o è semanticamente incoerente: geometria assente,
    identificativo mancante o duplicato, valore fuori vocabolario, cronologia
    invertita. Il Geoportale lo rifiuterebbe comunque, e il problema sta nel
    dato MAGOH di partenza.

WARNING
    Il dato è esportabile ma incompleto o da verificare: fonte non determinabile,
    quota assente in un dato pregresso, DSCT ambiguo, NVCT non ricavabile.

INADMISSIBLE
    Il dato è valido, ma non soddisfa le condizioni per essere trasferito: una
    regola di ammissibilità lo esclude dal conferimento. Non è un difetto del
    dato MAGOH — il record è a posto — e non è un rinvio al Template, perché nel
    Template non ci arriverà proprio. Sono tre cose diverse e vanno contate
    separatamente: confonderle vorrebbe dire non sapere più se un numero misura
    la qualità del dato, il lavoro che resta da fare o l'ampiezza di un filtro.

DEFERRED_GNA
    Campo obbligatorio nel Template 1.6 che il convertitore **non deve** compilare
    perché dipende dal Modulo di Progetto: `CPR_MOPR`, `VRRP`, `ACCC`, `AMA`,
    e l'identificativo `DSCH` che il sistema GNA genera da sé. Non è un difetto
    della conversione e non deve farla fallire: va completato nel Template
    ufficiale, dove questi campi si compilano da soli a partire dal codice progetto.
"""

from __future__ import annotations

ERROR = "ERROR"
WARNING = "WARNING"
DEFERRED_GNA = "DEFERRED_GNA"
INADMISSIBLE = "INADMISSIBLE"

__all__ = ["blank", "validate_required", "validate_lengths", "Report", "Esito",
           "ERROR", "WARNING", "DEFERRED_GNA", "INADMISSIBLE",
           "normalizza_valore", "REPR_QT"]

import re

#: Firma di un oggetto Qt finito in un testo. Non deve mai uscire dal
#: convertitore: nel GNA sarebbe un dato sbagliato, non un dato brutto.
REPR_QT = re.compile(r"PyQt\d?\.|<PyQt|QVariant\(|QDateTime\(|QDate\(|QTime\(|QByteArray\(")

_FORMATI_QT = {
    "QDateTime": "yyyy-MM-dd HH:mm:ss",
    "QDate": "yyyy-MM-dd",
    "QTime": "HH:mm:ss",
}


def normalizza_valore(v):
    """Riduce un valore letto da QGIS a un tipo Python semplice.

    QGIS non restituisce sempre stringhe: un campo data arriva come `QDateTime`,
    un campo vuoto come `QVariant` nullo. Passandoli a `str()` si ottiene la
    loro rappresentazione Python — `PyQt5.QtCore.QDateTime(2020, 9, 10, ...)` —
    che finiva dentro campi di testo come BIBM. Qui le date diventano date e i
    valori nulli diventano vuoti, una volta sola e per tutti i campi.
    """
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return v
    modulo = getattr(type(v), "__module__", "") or ""
    nome = type(v).__name__
    if modulo.startswith("PyQt") or modulo.startswith("Qt") or nome.startswith("Q"):
        # valore nullo di QGIS, in tutte le forme in cui si presenta
        try:
            if v.isNull():
                return ""
        except Exception:
            pass
        formato = _FORMATI_QT.get(nome)
        if formato:
            try:
                return v.toString(formato)
            except Exception:
                return ""
        if nome == "QVariant":
            try:
                return normalizza_valore(v.value())
            except Exception:
                return ""
        if nome == "QByteArray":
            try:
                return bytes(v).decode("utf-8", "replace")
            except Exception:
                return ""
        # Qualunque altro oggetto Qt: `str()` darebbe la sua rappresentazione
        # Python, che come testo è peggio del vuoto.
        return ""
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    if isinstance(v, (list, tuple, dict)):
        return v
    testo = str(v)
    return "" if REPR_QT.search(testo) else testo


def blank(v):
    v = normalizza_valore(v)
    if v is None:
        return True
    if isinstance(v, (list, tuple, dict)):
        return not v
    s = str(v).strip()
    return s == "" or s.upper() == "NULL"


def validate_required(row, fields):
    return [f for f in fields if blank(row.get(f))]


def validate_lengths(row, lengths):
    out = []
    for k, v in row.items():
        if blank(v) or str(k).startswith("_"):
            continue
        lim = (lengths or {}).get(str(k).lower())
        if lim and len(str(v)) > int(lim):
            out.append((k, len(str(v)), int(lim)))
    return out


class Esito(object):
    """Una singola segnalazione, riferita a un record."""

    __slots__ = ("classe", "codice", "messaggio", "tabella", "campo", "record", "regola")

    def __init__(self, classe, codice, messaggio, tabella="", campo="", record="", regola=""):
        self.classe = classe
        self.codice = codice
        self.messaggio = messaggio
        self.tabella = tabella
        self.campo = campo
        self.record = record
        #: la regola che ha prodotto l'esito, quando ce n'è una (INADMISSIBLE)
        self.regola = regola

    def as_row(self):
        return {"classe": self.classe, "codice": self.codice, "tabella": self.tabella,
                "record_id": self.record, "campo": self.campo, "regola": self.regola,
                "messaggio": self.messaggio}


class Report(object):
    """Raccolta delle segnalazioni, con i conteggi per classe."""

    COLONNE = ["classe", "codice", "tabella", "record_id", "campo", "regola", "messaggio"]

    def __init__(self):
        self.esiti = []

    def add(self, classe, codice, messaggio, tabella="", campo="", record="", regola=""):
        self.esiti.append(Esito(classe, codice, messaggio, tabella, campo, record, regola))

    def error(self, codice, messaggio, **kw):
        self.add(ERROR, codice, messaggio, **kw)

    def warning(self, codice, messaggio, **kw):
        self.add(WARNING, codice, messaggio, **kw)

    def deferred(self, codice, messaggio, **kw):
        self.add(DEFERRED_GNA, codice, messaggio, **kw)

    def inammissibile(self, codice, messaggio, **kw):
        self.add(INADMISSIBLE, codice, messaggio, **kw)

    def di_classe(self, classe):
        return [e for e in self.esiti if e.classe == classe]

    @property
    def errori(self):
        return self.di_classe(ERROR)

    @property
    def avvisi(self):
        return self.di_classe(WARNING)

    @property
    def differiti(self):
        return self.di_classe(DEFERRED_GNA)

    @property
    def inammissibili(self):
        return self.di_classe(INADMISSIBLE)

    def conteggi(self):
        return {ERROR: len(self.errori), WARNING: len(self.avvisi),
                DEFERRED_GNA: len(self.differiti),
                INADMISSIBLE: len(self.inammissibili)}

    def righe(self):
        return [e.as_row() for e in self.esiti]

    def campi_differiti(self):
        """I nomi dei campi rimandati al Template GNA, senza ripetizioni."""
        visti = []
        for e in self.differiti:
            if e.campo and e.campo not in visti:
                visti.append(e.campo)
        return visti

    def riepilogo_classi(self, massimo=6):
        """Sintesi leggibile, raggruppata per codice: gli elenchi lunghi non servono."""
        blocchi = []
        for classe, titolo in ((ERROR, "ERRORI"), (WARNING, "AVVISI"),
                               (INADMISSIBLE, "ESCLUSI DAL TRASFERIMENTO"),
                               (DEFERRED_GNA, "DA COMPLETARE NEL TEMPLATE GNA")):
            gruppo = self.di_classe(classe)
            if not gruppo:
                continue
            per_codice = {}
            for e in gruppo:
                per_codice.setdefault(e.codice, []).append(e)
            blocchi.append("%s (%d)" % (titolo, len(gruppo)))
            for codice, elenco in sorted(per_codice.items(), key=lambda x: -len(x[1]))[:massimo]:
                blocchi.append("   %-28s %4d   %s" % (codice, len(elenco), elenco[0].messaggio[:90]))
            if len(per_codice) > massimo:
                blocchi.append("   … e altri %d tipi di segnalazione" % (len(per_codice) - massimo))
            blocchi.append("")
        return "\n".join(blocchi).rstrip()
