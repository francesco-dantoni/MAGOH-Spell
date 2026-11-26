# -*- coding: utf-8 -*-
"""Conversione MAGOH → GNA, allineata al Template GNA 1.6.0.

Produce un pacchetto di **staging**: dati pronti per essere trasferiti e validati
nel Template GNA ufficiale, non un conferimento diretto.

Il criterio che governa tutto: non si inventano valori per superare i controlli.
Dove la corrispondenza non esiste o è incerta il campo resta vuoto e la ragione
finisce nel report di validazione.

Tre scelte di fondo, che vale la pena dire qui:

* **Niente identificativi finti.** `DSCH`, `VRRP` e `ACCC` non vengono più
  costruiti da MAGOH (`magoh_<idscheda>`, `magoh_ritr`, `magoh_ritr_<id>`):
  nel GNA 1.6 sono generati dal sistema a partire dal Modulo di Progetto. Restano
  vuoti e il validatore li classifica `DEFERRED_GNA`. L'identificativo MAGOH
  originale non si perde: è nel log di conversione.
* **I vocabolari stanno fuori dal codice**, in `gna_vocabularies_1_6.json` e
  `mapping_magoh_gna_1_6.json`. Qui non c'è nessun elenco di lemmi.
* **`FNTP` è un campo nuovo e obbligatorio**, ricavato dalla fonte
  dell'informazione della scheda Intervento.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from ..text_cleaning import is_magoh_id,scientific_description,with_provenance,fit_with_references,magoh_provenance
from ..street_lookups import street_names
from ..geographic_lookups import municipality_names,toponym_names,readable_label

from .validators import (Report, blank, normalizza_valore, validate_lengths, validate_required)

# ---------------------------------------------------------------- utilità

def F(r, *names):
    """Primo valore non vuoto fra i nomi indicati, ignorando maiuscole.

    È l'unica porta d'ingresso ai valori MAGOH, e per questo è qui che si
    normalizza: quello che esce di qui è sempre un tipo Python semplice, mai un
    oggetto Qt. Sanare a valle, campo per campo, vorrebbe dire dimenticarsene
    in uno.
    """
    for n in names:
        if n in r and not blank(r.get(n)):
            return normalizza_valore(r.get(n))
        ln = str(n).lower()
        for k, v in r.items():
            if str(k).lower() == ln and not blank(v):
                return normalizza_valore(v)
    return ""


def nv(v):
    """Confronto tollerante: accenti sciolti, punteggiatura e spazi compressi."""
    if blank(v):
        return ""
    s = unicodedata.normalize("NFD", str(v))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def lookup(m, v):
    if blank(v) or not isinstance(m, dict):
        return ""
    if v in m:
        return m[v]
    sv = str(v)
    if sv in m:
        return m[sv]
    n = nv(v)
    for k, x in m.items():
        if str(k).startswith("_"):
            continue
        if nv(k) == n:
            return x
    return ""


def num(v):
    if blank(v):
        return None
    m = re.search(r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?", str(v))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def year_of(v):
    if blank(v):
        return ""
    m = re.search(r"(\d{4})", str(v))
    return m.group(1) if m else ""


def anni_in(v):
    """Tutti gli anni a quattro cifre contenuti nel valore."""
    return re.findall(r"\d{4}", str(v or ""))


def data_breve(v):
    """Solo la data, senza orario: nel GNA il protocollo si cita per data."""
    if blank(v):
        return ""
    s = str(normalizza_valore(v)).strip()
    m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", s)
    if m:
        return m.group(0)
    m = re.search(r"\d{1,2}[-/]\d{1,2}[-/]\d{4}", s)
    return m.group(0) if m else s


CODA_NSC = "[segue in OSS]"
TESTA_OSS = "Continuazione della descrizione (NSC), eccedente il limite di %d caratteri:"


def taglia_su_parola(testo, limite):
    """Taglia entro il limite senza spezzare una parola, se si può."""
    if len(testo) <= limite:
        return testo, ""
    taglio = testo.rfind(" ", 0, limite)
    if taglio < limite * 0.6:      # nessuno spazio utile: si taglia netto
        taglio = limite
    return testo[:taglio].rstrip(), testo[taglio:].lstrip()


def is_codice(v):
    return False if blank(v) else bool(re.fullmatch(r"\s*[+-]?\d+(?:[.,]0+)?\s*", str(v)))


def sigla_valida(v):
    return bool(re.fullmatch(r"[A-Za-z]{2}", str(v or "").strip()))


def compose_bibm(r):
    proto = ", ".join(str(x) for x in [F(r, "fonte_informazione"), F(r, "numero_protocollo"),
                                       data_breve(F(r, "data_protocollo"))] if not blank(x))
    bib = ", ".join(str(x) for x in [F(r, "autore"), F(r, "titolo"), F(r, "contenuto_in"),
                                     F(r, "luogo_data_pubblicazione"), F(r, "pagina")] if not blank(x))
    if not proto and not bib:
        return ""
    return proto + "; " + bib


def crono_by_value(mapping, v):
    if blank(v):
        return None
    for c in mapping.get("crono_vocab") or []:
        if isinstance(c, dict) and (str(c.get("id", "")) == str(v).strip()
                                    or nv(c.get("name_full")) == nv(v)):
            return c
    return None


# ---------------------------------------------------------------- geometrie

def gtype(g):
    return g.get("type", "") if isinstance(g, dict) else ""


def to_multipolygon(g):
    if not isinstance(g, dict):
        return None
    if gtype(g) == "MultiPolygon":
        return copy.deepcopy(g)
    if gtype(g) == "Polygon":
        return {"type": "MultiPolygon", "coordinates": [copy.deepcopy(g.get("coordinates", []))]}
    return None


def _coords(c):
    if isinstance(c, list) and len(c) >= 2 and all(isinstance(x, (int, float)) for x in c[:2]):
        yield c
    elif isinstance(c, list):
        for x in c:
            for y in _coords(x):
                yield y


def to_multipoint(g):
    if not isinstance(g, dict):
        return None
    if gtype(g) == "MultiPoint":
        return copy.deepcopy(g)
    if gtype(g) == "Point":
        return {"type": "MultiPoint", "coordinates": [copy.deepcopy(g.get("coordinates", []))]}
    pts = list(_coords(g.get("coordinates", [])))
    if not pts:
        return None
    c = [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)]
    return {"type": "MultiPoint", "coordinates": [c]}


# ---------------------------------------------------------------- opzioni

@dataclass
class ConversionOptions:
    working_template: bool = False
    regione: str = "Toscana"
    comune_fisso: str = ""
    profondita: str = "auto"
    geo_default: dict = field(default_factory=lambda: {"gpt": "", "gpm": "", "gpbb": ""})
    usa_evidenze: bool = True
    full_timestamp: bool = False
    source_interventi: str = "magoh_scheda_intervento"
    source_ritrovamenti: str = "magoh_ritrovamenti"


@dataclass
class ConversionResult:
    dsc: list = field(default_factory=list)
    mosi_multipolygon: list = field(default_factory=list)
    mosi_multilinea: list = field(default_factory=list)
    mosi_multipoint: list = field(default_factory=list)
    report: Report = field(default_factory=Report)
    #: interventi validi ma esclusi dal trasferimento: non entrano in nessun
    #: layer, ma non spariscono — restano nel log e nel report di valutazione.
    dsc_inammissibili: list = field(default_factory=list)
    #: ritrovamenti con I livello «Non luogo» (nessun MOSI)
    mosi_esclusi_non_luogo: list = field(default_factory=list)

    @property
    def all_tables(self):
        d = {}
        if self.dsc:
            d["GNA_DSC"] = self.dsc
        if self.mosi_multipolygon:
            d["GNA_MOSI_multipolygon"] = self.mosi_multipolygon
        if self.mosi_multilinea:
            d["GNA_MOSI_multilinea"] = self.mosi_multilinea
        if self.mosi_multipoint:
            d["GNA_MOSI_multipoint"] = self.mosi_multipoint
        return d


    @property
    def n_mosi(self):
        return (len(self.mosi_multipolygon) + len(self.mosi_multilinea)
                + len(self.mosi_multipoint))


class Ctx:
    """Diario di bordo di un singolo record: note, qualità, segnalazioni."""

    def __init__(self, system, layer, rid, tabella, report):
        self.system = system
        self.layer = layer
        self.rid = rid
        self.tabella = tabella
        self.report = report
        self.quality = "complete"
        self.notes = []
        self.original = ""
        #: regola che ha escluso il record, quando è stato escluso
        self.regola = ""

    def note(self, s):
        if s and str(s) not in self.notes:
            self.notes.append(str(s))

    def demote(self):
        self.quality = "partial"

    def errore(self, codice, messaggio, campo=""):
        self.note(messaggio)
        self.quality = "error"
        self.report.error(codice, messaggio, tabella=self.tabella, campo=campo, record=str(self.rid))

    def avviso(self, codice, messaggio, campo=""):
        self.note(messaggio)
        self.demote()
        self.report.warning(codice, messaggio, tabella=self.tabella, campo=campo, record=str(self.rid))

    def differito(self, codice, messaggio, campo=""):
        self.report.deferred(codice, messaggio, tabella=self.tabella, campo=campo, record=str(self.rid))

    def inammissibile(self, codice, messaggio, campo="", regola=""):
        """Il record è valido ma non ha i requisiti per essere trasferito."""
        self.note(messaggio)
        self.quality = "inammissibile"
        self.regola = regola
        self.report.inammissibile(codice, messaggio, tabella=self.tabella, campo=campo,
                                  record=str(self.rid), regola=regola)


# ---------------------------------------------------------------- convertitore

class MagohToGnaConverter:
    """Trasforma le schede MAGOH nelle strutture DSC e MOSI del Template GNA 1.6."""

    #: Valori di riserva, usati solo se `gna_vocabularies_1_6.json` manca o è
    #: illeggibile: in condizioni normali gli elenchi arrivano da lì.
    #: DSCH e CPR_MOPR non compaiono fra gli obbligatori: sono differiti.
    REQUIRED_DSC = ["dscex", "dscv", "dscm", "dsct", "pvcr", "pvcp", "pvcc",
                    "gpt", "gpm", "gpbb", "cmpd", "fur"]
    #: VRRP, ACCC, AMA e NVCT non ci sono: sono differiti. CDGG non è più obbligatorio.
    #: |year| at or above this value is MAGOH's placeholder for «non determinato»
    ANNO_SENTINELLA = 1000000
    #: upper bound of MAGOH «Non determinato» (-3000000/5000), never a real year
    ANNO_FINE_NON_DETERMINATO = 5000
    #: I livello values that describe events, not sites: no MOSI
    I_LIVELLO_SENZA_MOSI = ("non luogo",)

    REQUIRED_MOSI = ["id", "ogd", "ogn", "ogb", "lcr", "lcp", "lcc", "gel",
                     "gpt", "gpm", "gpbb", "fntp", "ogm", "ogm_new", "des", "dtsi", "dtsf",
                     "fur", "cma", "adp"]

    #: campi che si compilano nel Template GNA, non qui
    DIFFERITI_DSC = {"cpr_mopr": "codice progetto assegnato dal MOPR",
                     "dsch": "identificativo univoco generato dal sistema GNA"}
    DIFFERITI_MOSI = {"vrrp": "codice progetto di riferimento, si autocompila dal MOPR",
                      "accc": "codice identificativo, si autocompila da codice progetto + ID",
                      "ama": "ambito di applicazione, dipende dall'ambito del progetto",
                      "nvct": "riferimento normativo, da scegliere fra i lemmi ammessi"}

    def __init__(self, mapping_path):
        base = Path(mapping_path).parent
        self.mapping = json.loads(Path(mapping_path).read_text(encoding="utf-8-sig"))
        req = ["DSCM_magoh_to_gna", "GPT_magoh_to_gna", "crono_to_DTR", "comuni_sigla", "gna_schema"]
        miss = [x for x in req if x not in self.mapping]
        if miss:
            raise ValueError("mapping.json incompleto: " + ", ".join(miss))

        self.voc = self._carica(base / "gna_vocabularies_1_6.json")
        self.map16 = self._carica(base / "mapping_magoh_gna_1_6.json")
        cfg = self.voc.get("_impostazioni") or {}
        self.campo_anno = (cfg.get("dsc_campo_anno") or "DSCY").lower()
        self.dsct_attivo = cfg.get("dsct_vocabolario_attivo") or "struttura_ufficiale"
        self.id_max = int(cfg.get("id_mosi_lunghezza_massima") or 10)
        self.cdgg_obbligatorio = bool(cfg.get("cdgg_obbligatorio"))
        self.ammissibilita = dict(cfg.get("dsc_ammissibilita") or {})
        self._carica_regole()
        if self.cdgg_obbligatorio and "cdgg" not in self.REQUIRED_MOSI:
            self.REQUIRED_MOSI = self.REQUIRED_MOSI + ["cdgg"]
        self._id_usati = {}
        self._adegua_schema()

    def _carica_regole(self):
        """Obbligatori e campi differiti dal file dei vocabolari, se c'è.

        Gli elenchi nella classe restano come riserva: se il file manca, il
        convertitore lavora comunque invece di fermarsi.
        """
        dif = self.voc.get("_campi_differiti") or {}
        for scheda, attributo in (("DSC", "DIFFERITI_DSC"), ("MOSI", "DIFFERITI_MOSI")):
            blocco = dif.get(scheda)
            if isinstance(blocco, dict) and blocco:
                setattr(self, attributo,
                        {k.lower(): v for k, v in blocco.items() if not k.startswith("_")})

        obb = self.voc.get("_obbligatori") or {}
        for scheda, attributo in (("DSC", "REQUIRED_DSC"), ("MOSI", "REQUIRED_MOSI")):
            elenco = obb.get(scheda)
            if not isinstance(elenco, list) or not elenco:
                continue
            differiti = set(getattr(self, "DIFFERITI_" + scheda.replace("MOSI", "MOSI")))
            # un campo differito non è mai un obbligatorio mancante: sarebbe
            # segnalato due volte, come ERROR e come DEFERRED_GNA
            setattr(self, attributo,
                    [x.lower() for x in elenco if x.lower() not in differiti])

    # -- struttura 1.6 ----------------------------------------------------

    def _adegua_schema(self):
        """Porta lo schema di uscita dalla struttura 1.5 a quella del Template 1.6.

        Si lavora sulla copia in memoria: `mapping.json` resta com'è, così la
        struttura precedente rimane consultabile e il confronto fra le due
        versioni è sempre possibile. Le operazioni sono descritte nel blocco
        `_schema_1_6` del file dei vocabolari, non qui.
        """
        delta = self.voc.get("_schema_1_6") or {}
        schema = (self.mapping.get("gna_schema") or {})
        for scheda, regole in delta.items():
            if scheda.startswith("_") or scheda not in schema:
                continue
            campi = list(schema[scheda])

            def nome(x):
                return x["campo"] if isinstance(x, dict) else str(x)

            rinomina = regole.get("rinomina") or {}
            # la rinomina DSCD→DSCY vale solo se è DSCY il campo anno in uso
            if scheda == "DSC" and self.campo_anno == "dscd":
                rinomina = {}
            for i, x in enumerate(campi):
                nuovo = rinomina.get(nome(x))
                if not nuovo:
                    continue
                if isinstance(x, dict):
                    y = dict(x)
                    y["campo"] = nuovo
                    campi[i] = y
                else:
                    campi[i] = nuovo

            presenti = {nome(x).lower() for x in campi}
            for agg in regole.get("aggiungi") or []:
                campo = agg.get("campo")
                if not campo or campo.lower() in presenti:
                    continue
                dopo = (agg.get("dopo") or "").lower()
                pos = 0
                if dopo:
                    for i, x in enumerate(campi):
                        if nome(x).lower() == dopo:
                            pos = i + 1
                            break
                    else:
                        pos = len(campi)
                elif campi and nome(campi[0]).lower() == "gid":
                    pos = 1  # gid resta sempre la prima colonna
                campi.insert(pos, campo)
                presenti.add(campo.lower())

            schema[scheda] = campi
        self.mapping["gna_schema"] = schema

    @staticmethod
    def _carica(path):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception:
            return {}

    # -- vocabolari -------------------------------------------------------

    def _lemmi(self, sigla):
        """Lemmi ammessi per un campo, tenendo conto del vocabolario attivo."""
        v = self.voc.get(sigla)
        if isinstance(v, dict):
            v = v.get(self.dsct_attivo) or []
        if v:
            return v
        return (self.mapping.get("gna_vocabolari") or {}).get(sigla) or []

    def _in_vocabolario(self, sigla, valore):
        lemmi = self._lemmi(sigla)
        if not lemmi or blank(valore):
            return True
        return nv(valore) in {nv(x) for x in lemmi}

    def _controlla_vocabolari(self, o, c, coppie):
        for campo, sigla in coppie:
            v = o.get(campo)
            if blank(v):
                continue
            if not self._in_vocabolario(sigla, v):
                c.errore("valore_fuori_vocabolario",
                         '%s: "%s" non è un lemma del vocabolario GNA %s' % (campo.upper(), v, sigla),
                         campo=campo.upper())

    # -- FNTP -------------------------------------------------------------

    def _fntp(self, r, parent, ogm, c):
        """Fonte: dalla fonte dell'informazione, dal tipo di documento o dalla modalità.

        Nell'ordine, perché è l'ordine di affidabilità: chi ha prodotto il dato,
        che tipo di documento è, e solo in ultimo come il ritrovamento è stato
        individuato. Se nessuna delle tre dà una risposta certa, il campo resta
        vuoto: sceglierlo a caso vorrebbe dire dichiarare una provenienza falsa.
        """
        t = self.map16.get("FNTP") or {}
        P = parent or {}
        for chiave, sorgenti in (("da_fonte_informazione", ("fonte_informazione",)),
                                 ("da_tipo_documento", ("tipo_documento",)),
                                 ("da_ogm", ())):
            tabella = t.get(chiave) or {}
            valore = ogm if chiave == "da_ogm" else F(P, *sorgenti)
            esito = lookup(tabella, valore)
            if esito and not str(esito).startswith("_"):
                return esito
        c.avviso("fntp_non_determinabile",
                 "FNTP non determinabile dal dato MAGOH: fonte, tipo di documento e modalità "
                 "di individuazione non danno una corrispondenza certa", campo="FNTP")
        return ""

    # -- ID MOSI ----------------------------------------------------------

    def _id_mosi(self, rid, c):
        """ID del MOSI: al massimo dieci caratteri alfanumerici, univoco nel batch.

        La trasformazione è deterministica e la corrispondenza con l'id MAGOH
        finisce nel log, così il record resta sempre rintracciabile.
        """
        originale = "" if blank(rid) else str(rid).strip()
        if not originale:
            c.errore("id_mancante", "ritrovamento senza id: il MOSI non avrebbe un identificativo",
                     campo="ID")
            return ""
        pulito = re.sub(r"[^A-Za-z0-9]", "", originale)
        if not pulito:
            pulito = "R"
        if len(pulito) > self.id_max:
            pulito = pulito[-self.id_max:]
            c.avviso("id_troncato",
                     'ID "%s" ridotto a "%s": il Template GNA ammette al massimo %d caratteri '
                     "alfanumerici" % (originale, pulito, self.id_max), campo="ID")
        if pulito in self._id_usati:
            # Due record distinti non possono uscire con lo stesso ID: il GNA li
            # tratterebbe come lo stesso MOSI e uno dei due andrebbe perso. Si
            # assegna un ID libero e si segnala l'errore, che però sta a monte:
            # va risolto in MAGOH, dove i due record convivono con lo stesso id.
            precedente = self._id_usati[pulito]
            base = pulito
            n = 2
            while pulito in self._id_usati:
                suffisso = str(n)
                pulito = base[:max(1, self.id_max - len(suffisso))] + suffisso
                n += 1
            c.errore("id_duplicato",
                     'id MAGOH "%s" già usato dal record "%s": a questo record è stato assegnato '
                     '"%s" per non sovrascrivere il precedente, ma il duplicato va risolto in '
                     "MAGOH" % (base, precedente, pulito), campo="ID")
        self._id_usati[pulito] = originale
        return pulito

    # -- helper comuni ----------------------------------------------------

    def seal(self, c, o, g):
        o.update({"_source_system": c.system, "_source_layer": c.layer, "_source_id": c.rid,
                  "_rule": getattr(c, "regola", ""),
                  "_conversion_direction": "MAGOH_TO_GNA", "_conversion_quality": c.quality,
                  "_conversion_notes": " | ".join(c.notes), "_original_geometry_type": c.original,
                  "_unmapped_fields": "", "_unmapped_values": "", "_geometry": g})
        return o

    def geo_defaults(self, o, c, opts):
        for k in ("gpt", "gpm", "gpbb"):
            d = (opts.geo_default or {}).get(k)
            if blank(o.get(k)) and not blank(d):
                o[k] = d
                c.note('%s assente nel dato MAGOH: impostato al valore predefinito "%s"' % (k.upper(), d))
                c.demote()

    def validate(self, o, c, req):
        miss = validate_required(o, req)
        if miss:
            for k in miss:
                c.errore("obbligatorio_vuoto",
                         "campo GNA obbligatorio %s vuoto" % k.upper(), campo=k.upper())
        for k, n, lim in validate_lengths(o, self.mapping.get("gna_lunghezze") or {}):
            c.avviso("lunghezza_eccessiva",
                     "%s: %d caratteri, massimo GNA %d" % (k.upper(), n, lim), campo=k.upper())

    #: Lemmi OGM che il Template non accetta in OGM_new: sono fonti, registrate in FNTP.
    OGM_FONTI = ("dati bibliografici", "dati di archivio")

    def _ogm_new(self, ogm, tipologia, c):
        """Modalità di individuazione per OGM_new.

        Una modalità vera passa così com'è; una fonte (archivio, bibliografia)
        è sostituita dalla modalità che corrisponde al tipo di intervento.
        """
        if not blank(ogm) and ogm not in self.OGM_FONTI:
            return ogm
        esito = lookup(self.map16.get("OGM_new_da_tipologia") or {}, tipologia)
        if esito and not str(esito).startswith("_"):
            return esito
        c.avviso("ogm_new_non_determinabile",
                 "OGM_new non determinabile: la modalità MAGOH \"%s\" è una fonte e il tipo di "
                 "intervento \"%s\" non indica una modalità di individuazione"
                 % (ogm or "", tipologia or ""), campo="OGM_NEW")
        return ""

    def _periodi_dtr(self):
        if getattr(self, "_dtr", None) is None:
            from ..gna_template_resources import vocabulary_catalog
            spec = vocabulary_catalog()["vocabularies"].get("DTR") or {}
            self._dtr = [(p["DTR_key"], p["DTR_value"], num(p.get("DTSI")), num(p.get("DTSF")))
                         for p in (f.get("properties") or {} for f in spec.get("data", {}).get("features", []))]
        return self._dtr

    def _cronologia_gna(self, o, c):
        """DTR_new e anni coerenti con il vincolo del Template.

        Il Template chiede DTSI non anteriore all'inizio del periodo DTR e DTSF
        non successivo alla sua fine. Senza anni MAGOH si usano gli estremi del
        periodo; con anni fuori dal periodo si sceglie il periodo più breve che
        li comprende, e se nessun periodo li comprende DTR resta da scegliere.
        """
        periodi = self._periodi_dtr()
        scelto = next((p for p in periodi if p[1] == o.get("dtr")), None)
        o["dtr_new"] = None
        if scelto and scelto[2] is None:
            o["dtr_new"] = scelto[0]          # non determinabile / non rilevabile
            return
        di, df = num(o["dtsi"]), num(o["dtsf"])
        if di is None or df is None:
            if not scelto:
                c.avviso("cronologia_incompleta",
                         "DTSI o DTSF assenti e periodo DTR non disponibile: la cronologia "
                         "specifica va completata nel Template", campo="DTSI")
                return
            if di is None:
                di = scelto[2]
                o["dtsi"] = int(di)
            if df is None:
                df = scelto[3]
                o["dtsf"] = int(df)
            c.avviso("cronologia_dal_periodo",
                     "anni assenti nel dato MAGOH: DTSI e DTSF presi dagli estremi del periodo "
                     "\"%s\" (%g / %g)" % (scelto[1], di, df), campo="DTSI")
        if di > df:
            return                            # già segnalata come cronologia invertita

        def comprende(p):
            return p[2] is not None and p[2] <= di and df <= p[3]

        if scelto and comprende(scelto):
            o["dtr_new"] = scelto[0]
            return
        candidati = sorted((p for p in periodi if comprende(p)), key=lambda p: p[3] - p[2])
        if candidati:
            nuovo = candidati[0]
            c.note("anni %g–%g fuori dal periodo \"%s\": DTR impostato a \"%s\""
                   % (di, df, o.get("dtr") or "", nuovo[1]))
            o["dtr"], o["dtr_new"] = nuovo[1], nuovo[0]
        else:
            c.avviso("cronologia_multi_periodo",
                     "gli anni %g–%g non rientrano in un solo periodo DTR del GNA: DTR va scelto "
                     "nel Template" % (di, df), campo="DTR")
            o["dtr"] = ""

    def _entro_limite(self, o, campo, c, rimando=''):
        """Porta o[campo] entro il limite GNA, conservando sempre i riferimenti URL."""
        limite = int((self.mapping.get("gna_lunghezze") or {}).get(campo) or 0)
        testo = o.get(campo)
        if not limite or not testo or len(str(testo)) <= limite:
            return
        ridotto = fit_with_references(testo, limite, rimando)
        c.avviso("testo_ridotto",
                 "%s di %d caratteri ridotto a %d per il limite GNA di %d: i riferimenti URL "
                 "sono conservati e il testo integrale resta nella scheda MAGOH"
                 % (campo.upper(), len(str(testo)), len(ridotto), limite), campo=campo.upper())
        o[campo] = ridotto

    def _nsc_e_oss(self, testo, c, appendice=""):
        """Distribuisce una descrizione lunga fra NSC e OSS.

        Il GNA limita NSC a 5000 caratteri. Le descrizioni MAGOH più ricche
        arrivano a superarlo di parecchio, e lasciarle intere significherebbe
        far decidere al Geoportale che cosa buttare via, in silenzio. Qui la
        parte eccedente passa in OSS — campo libero, stesso limite, altrimenti
        vuoto — con una riga che dice che cos'è, così il testo resta tutto e la
        continuità è dichiarata invece che dedotta.

        Se anche i due campi insieme non bastano, il resto si tronca. Il rimando
        alla scheda MAGOH viene messo in fondo all'ultimo campo occupato **dopo**
        il taglio, non prima: è la strada verso il testo completo, e sarebbe la
        prima cosa a sparire proprio nei record in cui serve.
        """
        lung = self.mapping.get("gna_lunghezze") or {}
        lim_nsc = int(lung.get("nsc") or 0)
        lim_oss = int(lung.get("oss") or 0)
        if not testo:
            return (appendice if len(appendice) <= (lim_nsc or len(appendice)) else ""), ""
        if not lim_nsc or len(testo) + len(appendice) <= lim_nsc:
            return testo + appendice, ""

        coda = " " + CODA_NSC
        nsc, resto = taglia_su_parola(testo, lim_nsc - len(coda))
        nsc += coda
        intestazione = (TESTA_OSS % lim_nsc) + "\n"
        oss = intestazione + resto
        troncato = 0
        if lim_oss and len(oss) + len(appendice) > lim_oss:
            oss, avanzo = taglia_su_parola(oss, lim_oss - len(appendice) - 2)
            oss += " …"
            troncato = len(avanzo)
        oss += appendice

        if troncato:
            c.avviso("nsc_troncato",
                     "descrizione di %d caratteri: i primi %d in NSC, la continuazione in OSS, "
                     "e %d caratteri non entrano in nessuno dei due (il testo completo resta "
                     "nella scheda MAGOH collegata)" % (len(testo), lim_nsc, troncato),
                     campo="NSC")
        else:
            c.note("descrizione di %d caratteri: NSC ne contiene %d, la continuazione è in OSS "
                   "(il GNA limita NSC a %d)" % (len(testo), lim_nsc, lim_nsc))
        return nsc, oss

    def _gpm(self, precisione):
        """Metodo di posizionamento, dalla precisione di georeferenziazione.

        È lo stesso dato che decide l'ammissibilità della DSC, e va letto con
        gli stessi nomi di campo: leggerne uno solo significava non trovarlo sul
        layer di lavoro, lasciare GPM vuoto e far scrivere al valore predefinito
        della finestra «posizionamento esatto» anche su un intervento che non è
        stato possibile georeferenziare. Dichiarare una precisione che non si ha
        è peggio che lasciare il campo vuoto.
        """
        return (lookup(self.map16.get("GPM") or {}, precisione)
                or lookup(self.mapping.get("GPM_magoh_to_gna", {}), precisione))

    # -- ammissibilità della DSC -----------------------------------------

    def valore_ammissibilita(self, r):
        """Il valore su cui si decide, letto dal primo campo valorizzato.

        I nomi dei campi stanno nel JSON e non qui: il layer di lavoro QGIS e la
        tabella del GeoPackage chiamano questa informazione in due modi diversi,
        e indovinare quale sia «il» nome giusto sarebbe stato sbagliato in un
        caso su due.
        """
        campi = self.ammissibilita.get("campi") or []
        return F(r, *campi) if campi else ""

    def dsc_ammissibile(self, r):
        """(ammessa, valore letto, regola). Fuori dal codice la regola, qui solo il confronto."""
        regola = self.ammissibilita.get("regola") or "ammissibilita_dsc"
        ammessi = self.ammissibilita.get("lemmi_ammessi") or []
        if not self.ammissibilita.get("attiva") or not ammessi:
            return True, self.valore_ammissibilita(r), regola
        valore = self.valore_ammissibilita(r)
        if blank(valore):
            return False, "", regola
        # confronto esatto sul lemma: niente sottostringhe, altrimenti
        # "Poco preciso" passerebbe perché contiene "preciso".
        atteso = {str(x).strip().casefold() for x in ammessi}
        return str(valore).strip().casefold() in atteso, valore, regola

    def _differisci(self, o, c, campi):
        """Segna i campi che il Template GNA compilerà da sé, lasciandoli vuoti."""
        for campo, motivo in campi.items():
            o[campo] = ""
            c.differito("campo_differito",
                        "%s da completare nel Template GNA: %s" % (campo.upper(), motivo),
                        campo=campo.upper())

    # -- DSC --------------------------------------------------------------

    def intervento_to_dsc(self, r, opts, report):
        mid = F(r, "idscheda", "id")
        c = Ctx("MAGOH_INTERVENTO", opts.source_interventi, mid, "GNA_DSC", report)
        o = {}
        o["_source_codice_locale"] = F(r, "codice_locale")
        o["_source_idscheda"] = "" if blank(mid) else str(mid)
        o["_precisione"] = str(self.valore_ammissibilita(r) or "")

        ogd = lookup(self.mapping.get("thesaurus_ogd") or {}, F(r, "ogd")) or F(r, "ogd")
        o["dscex"] = lookup(self.mapping.get("DSCEX_ogd_to_esito", {}), ogd)
        if not o["dscex"]:
            c.avviso("dscex_non_determinato",
                     'DSCEX non determinato: ogd "%s" non è nella tabella di mappatura' % ogd
                     if ogd else "DSCEX non determinato: campo ogd assente", campo="DSCEX")

        # DSCH e CPR_MOPR: li genera il GNA, non MAGOH
        self._differisci(o, c, self.DIFFERITI_DSC)

        o["dscv"] = F(r, "ubicazione")

        # anno dell'intervento: solo un anno certo a quattro cifre
        di, anno = F(r, "data_intervento"), F(r, "anno")
        candidati = anni_in(anno) or anni_in(di)
        if len(set(candidati)) == 1:
            o[self.campo_anno] = candidati[0]
        elif len(set(candidati)) > 1:
            o[self.campo_anno] = ""
            c.avviso("anno_ambiguo",
                     '%s non compilato: "%s" contiene più anni (%s) e non è riducibile con '
                     "certezza a uno solo" % (self.campo_anno.upper(), di or anno,
                                              ", ".join(sorted(set(candidati)))),
                     campo=self.campo_anno.upper())
        else:
            o[self.campo_anno] = ""
            c.avviso("anno_assente",
                     "%s non compilato: nel dato MAGOH non c'è un anno a quattro cifre"
                     % self.campo_anno.upper(), campo=self.campo_anno.upper())

        tip = F(r, "tipologia_intervento")
        o["dscm"] = lookup((self.map16.get("DSCM") or {}), tip) \
            or lookup(self.mapping.get("DSCM_magoh_to_gna", {}), tip)
        tab_dsct = (self.map16.get("DSCT") or {}).get(self.dsct_attivo) or {}
        o["dsct"] = lookup(tab_dsct, tip)
        if tip and not o["dsct"]:
            c.avviso("dsct_ambiguo",
                     'DSCT non assegnato: la tipologia MAGOH "%s" non ha una corrispondenza certa '
                     "nel vocabolario %s del Template 1.6" % (tip, self.dsct_attivo), campo="DSCT")
        if tip and not o["dscm"]:
            c.avviso("dscm_non_mappato",
                     'DSCM non assegnato: tipologia_intervento "%s" non è nel vocabolario' % tip,
                     campo="DSCM")

        desc = scientific_description(F(r, "descrizione", "descrizione_sintetica", "note"))
        o["nsc"], o["oss"] = self._nsc_e_oss(desc, c, '')
        notes=scientific_description(F(r,'note'))
        parts=[p for p in (o['oss'],notes if notes!=desc else '') if p]
        o['oss']=with_provenance('\n\n'.join(parts),mid,o["_source_codice_locale"])
        # note e rimando MAGOH si aggiungono dopo la ripartizione NSC/OSS:
        # il limite del campo si ricontrolla qui, conservando sempre il rimando.
        self._entro_limite(o, 'oss', c)
        o["pvcr"] = F(r, "regione") or opts.regione

        com = F(r, "id_comune", "comune")
        com = municipality_names().get(str(com).strip(),com)
        if is_codice(com):
            if opts.comune_fisso:
                c.note('id_comune "%s" è un codice: sostituito con "%s"' % (com, opts.comune_fisso))
                com = opts.comune_fisso
            else:
                c.avviso("comune_codice",
                         'id_comune "%s" è un codice di lookup MAGOH, non un nome: PVCC vuoto' % com,
                         campo="PVCC")
                com = ""
        elif blank(com) and opts.comune_fisso:
            com = opts.comune_fisso
            c.note('usato il comune predefinito "%s"' % com)
            c.demote()
        o["pvcc"] = com
        sg = F(r, "sigla")
        o["pvcp"] = str(sg).upper() if sigla_valida(sg) else lookup(self.mapping.get("comuni_sigla", {}), com)
        top = F(r, "toponimo")
        top = toponym_names().get(str(top).strip(), top)
        o["pvcl"] = "" if is_codice(top) else readable_label(top)
        ind, civ = F(r, "indirizzo"), F(r, "numero_civico")
        ind = readable_label(street_names().get(str(ind).strip(), '')) if is_codice(ind) else ind
        o["pvci"] = ", ".join(str(x) for x in (ind, civ) if not blank(x))

        o["gpt"] = lookup(self.mapping.get("GPT_magoh_to_gna", {}), F(r, "sistema_di_rilevazione"))
        o["gpm"] = self._gpm(self.valore_ammissibilita(r))
        o["gpbb"] = lookup(self.mapping.get("GPBB_magoh_to_gna", {}), F(r, "tipo_cartografico_di_riferimento"))
        self.geo_defaults(o, c, opts)

        pm = num(F(r, "profondita_massima"))
        o["mtar"] = "" if pm is None else (-pm if opts.profondita == "inverti" and pm > 0
                                           else F(r, "profondita_massima"))
        if pm is None:
            c.avviso("quota_assente",
                     "profondità non indicata: nel Template 1.6 l'informazione altimetrica è "
                     "attesa per i dati recenti", campo="MTAR")

        o["bibm"] = compose_bibm(r)
        o["cmpd"] = F(r, "data_ultimo_aggiornamento") if opts.full_timestamp \
            else year_of(F(r, "data_ultimo_aggiornamento"))
        o["cmpn"] = F(r, "compilatore_scheda")
        o["rsr"] = F(r, "esecutore")
        o["fur"] = F(r, "funzionario_competente")

        self.validate(o, c, self.REQUIRED_DSC)
        self._controlla_vocabolari(o, c, [("dscex", "DSCEX"), ("dscm", "DSCM"), ("dsct", "DSCT")])

        g = r.get("__geom", r.get("_geometry"))
        c.original = gtype(g)
        mp = to_multipolygon(g)
        geom = mp or g or None
        if g and not mp:
            c.avviso("geometria_non_convertibile",
                     "GNA richiede un MultiPolygon per la DSC: conservata %s" % gtype(g))
        if not g:
            c.errore("geometria_assente", "record privo di geometria: non esportabile")
        o["_geometry_output_type"] = gtype(geom)
        return self.seal(c, o, geom)

    # -- MOSI -------------------------------------------------------------

    def iii_to_mosi(self, r, parent, evidenze, opts, report):
        rid = F(r, "id")
        c = Ctx("MAGOH_III_LIVELLI", opts.source_ritrovamenti, rid, "GNA_MOSI", report)
        o = {}
        o["_source_codice_locale"] = F(r, "codice_locale")
        o["_source_ritrovamento_id"] = rid or ""
        o["_source_intervento_id"] = F(parent or {}, "idscheda", "id") if parent else F(r, "idscheda")
        o["_source_codici_evidenze"] = "|".join(str(F(ev,"codice_locale")) for ev in evidenze
                                                if F(ev,"codice_locale")) if opts.usa_evidenze else ""
        o["_source_evidenza_ids"] = ""
        # la precisione è un dato dell'intervento: il MOSI la eredita solo
        # perché il report di valutazione la mostri accanto al record
        o["_precisione"] = str(self.valore_ammissibilita(parent or {}) or "")
        o["_geometry_source"] = ""
        o["_geometry_output_type"] = ""

        # VRRP, ACCC e AMA li compila il Template a partire dal MOPR
        self._differisci(o, c, self.DIFFERITI_MOSI)
        o["id"] = self._id_mosi(rid, c)

        ii = F(r, "ii_livello")
        alias = lookup(self.mapping.get("II_livello_alias", {}), ii)
        if alias:
            c.note('II livello "%s" ricondotto a "%s"' % (ii, alias))
            ii = alias
        o["ogd"] = lookup(self.mapping.get("II_livello_to_OGD", {}), ii)
        if ii and not o["ogd"]:
            c.avviso("ogd_non_mappato",
                     'II livello "%s" non presente nella tabella di mappatura: OGD vuoto' % ii,
                     campo="OGD")
        o["ogt"] = F(r, "iii_livello", "tipologia")

        tab_ogb = self.map16.get("OGB") or {}
        o["ogb"] = lookup(tab_ogb, F(r, "situazione_attuale", "stato_conservazione")) \
            or tab_ogb.get("_predefinito") or "dato non disponibile"
        o["gel"] = "localizzazione fisica"

        P = parent or {}

        def pf(*n):
            return F(P, *n)

        # MOD-02: only the Intervento narrative; the Note field is never
        # used as a substitute when the narrative is missing.
        o["des"] = scientific_description(pf("descrizione", "descrizione_sintetica"))
        # A find created in QGIS is not on MAGOH, even when its Intervento is.
        local = o["_source_codice_locale"] or not is_magoh_id(rid)
        o['doz']=with_provenance(F(r,'doz'),rid if local else o['_source_intervento_id'],
                                 o["_source_codice_locale"])
        ogm_grezzo = pf("ogm")
        ogm_risolto = lookup(self.mapping.get("thesaurus_ogm") or {}, ogm_grezzo) or ogm_grezzo
        o["ogm"] = lookup(self.map16.get("OGM") or {}, ogm_risolto) or ""
        if ogm_risolto and not o["ogm"]:
            c.avviso("ogm_non_mappato",
                     'OGM: "%s" non corrisponde a un lemma del vocabolario 1.6' % ogm_risolto,
                     campo="OGM")

        o["fntp"] = self._fntp(r, parent, o["ogm"], c)
        o["ogm_new"] = self._ogm_new(o["ogm"], pf("tipologia_intervento"), c)

        o["lcr"] = pf("regione") or opts.regione
        lc = pf("id_comune", "comune")
        lc = municipality_names().get(str(lc).strip(),lc)
        if is_codice(lc):
            if opts.comune_fisso:
                lc = opts.comune_fisso
                c.note('lookup comune sostituito con "%s"' % lc)
            else:
                lc = ""
                c.avviso("comune_codice", "id_comune è un codice di lookup MAGOH: LCC vuoto",
                         campo="LCC")
        elif blank(lc) and opts.comune_fisso and parent:
            lc = opts.comune_fisso
        o["lcc"] = lc
        sg = pf("sigla")
        o["lcp"] = str(sg).upper() if sigla_valida(sg) else lookup(self.mapping.get("comuni_sigla", {}), lc)
        tp = pf("toponimo")
        tp = toponym_names().get(str(tp).strip(), tp)
        if not is_codice(tp) and not blank(tp):
            o["pvl"] = readable_label(tp)

        o["gpt"] = lookup(self.mapping.get("GPT_magoh_to_gna", {}), pf("sistema_di_rilevazione"))
        o["gpm"] = self._gpm(self.valore_ammissibilita(parent or {}))
        o["gpbb"] = lookup(self.mapping.get("GPBB_magoh_to_gna", {}), pf("tipo_cartografico_di_riferimento"))
        self.geo_defaults(o, c, opts)
        o["fur"] = pf("funzionario_competente")
        o["cma"] = pf("data_ultimo_aggiornamento") if opts.full_timestamp \
            else year_of(pf("data_ultimo_aggiornamento"))
        o["cmc"] = pf("compilatore_scheda")
        o["cmr"] = pf("esecutore")
        o["bibm"] = compose_bibm(parent) if parent else ""
        # Il Template chiede BIBM quando la fonte è bibliografica e DOZ quando
        # è un dato di archivio.
        if o.get("fntp") == "dati bibliografici" and blank(o["bibm"]):
            c.avviso("bibm_mancante", "fonte bibliografica senza riferimento bibliografico (BIBM)",
                     campo="BIBM")
        if o.get("fntp") == "dato di archivio" and blank(o.get("doz")):
            c.avviso("doz_mancante", "dato di archivio senza riferimento al documento (DOZ)",
                     campo="DOZ")
        if not parent:
            c.avviso("intervento_non_collegato",
                     "scheda Intervento collegata non trovata: i campi che la mappatura eredita "
                     "dall'intervento restano vuoti")

        # quote
        qs = [num(F(r, k)) for k in ("q_p", "q_pr", "q_et", "q_rm", "q_ta",
                                     "q_am", "q_bm", "q_mo", "q_c", "q_nd")]
        qs = [x for x in qs if x is not None]
        o["mtam"] = max(qs) if qs else ""
        prof = num(F(r, "profondit"))
        if prof is None:
            o["mtar"] = ""
        elif prof < 0:
            o["mtar"] = F(r, "profondit")
        elif opts.profondita == "inverti":
            o["mtar"] = -prof
            c.note("profondit %g invertita" % prof)
        elif opts.profondita == "auto":
            if qs:
                o["mtar"] = ""
                c.note("profondit %g esclusa da MTAR: presenti quote di periodo" % prof)
            elif prof < 5:
                o["mtar"] = -prof
                c.note("profondit %g interpretata come profondità senza segno" % prof)
            else:
                o["mtar"] = ""
                o["mtam"] = prof if o["mtam"] == "" else o["mtam"]
                c.note("profondit %g trattata come quota assoluta" % prof)
                c.demote()
        else:
            o["mtar"] = ""

        # cronologia
        ci = F(r, "crono_iniziale_testo", "crono_iniziale", "cronologia_iniziale")
        cv = crono_by_value(self.mapping, ci)
        if cv and cv.get("name_full"):
            ci = cv["name_full"]
        e = lookup(self.mapping.get("crono_to_DTR", {}), ci)
        o["dtr"] = e.get("dtr", "") if isinstance(e, dict) else (e or "")
        if ci and not o["dtr"]:
            c.avviso("dtr_non_mappato", 'cronologia "%s" non mappata su DTR' % ci, campo="DTR")
        o["dtsi"] = F(r, "datazione")
        o["dtsf"] = F(r, "datazione_finale")
        # MAGOH stores «non determinato» as an extreme year
        # (-3000000 … 5000): never export either bound as a real date (also 5000).
        for campo_data in ("dtsi", "dtsf"):
            valore = num(o[campo_data])
            if valore is not None and (abs(valore) >= self.ANNO_SENTINELLA
                                       or valore == self.ANNO_FINE_NON_DETERMINATO):
                c.note("%s %g è il valore MAGOH per «non determinato»: lasciato vuoto"
                       % (campo_data.upper(), valore))
                o[campo_data] = ""
        di, df = num(o["dtsi"]), num(o["dtsf"])
        if di is not None and df is not None and di > df:
            c.errore("cronologia_invertita",
                     "cronologia invertita: DTSI %g è successivo a DTSF %g" % (di, df), campo="DTSI")
        self._cronologia_gna(o, c)

        from .entity_summary import build_summary, compose_des, safe_ogt
        # OGN = III livello + Ubicazione dell'Intervento, nessun altro campo.
        o['ogn'], summary, o['dtt'] = build_summary(r, parent, F)
        # DES = sintesi I–III livello + narrativa integrale (MOD-02); DTT vuoto (MOD-03).
        o['des'] = compose_des(summary, o.get('des'), o['_source_intervento_id'])
        o['dtt'] = None
        o['ogt_key'] = safe_ogt(o.get('ogt'), o.get('ogd'))
        o['ogz'] = F(r, 'specifiche_situazione_attuale', 'note_stato_conservazione')
        if o.get('ogt') and not o['ogt_key']:
            c.avviso('ogt_non_univoco', 'III livello conservato in OGN/DES; nessuna corrispondenza OGT attiva, esatta e compatibile con OGD.', campo='OGT')
        # il GNA rifiuta una DES oltre il limite del campo, e con essa
        # l'intero layer. Il testo si riduce e rimanda alla scheda Intervento
        # MAGOH, dove resta integrale.
        intervento = o.get('_source_intervento_id')
        self._entro_limite(o, 'des', c,
                           magoh_provenance(intervento) if is_magoh_id(intervento) else '')

        # CDGG non è più obbligatorio: si compila solo se l'informazione esiste
        o["cdgg"] = ""
        o["adp"] = 1
        o["ssk"] = "Non validata"

        self.validate(o, c, self.REQUIRED_MOSI)
        self._controlla_vocabolari(o, c, [("ogd", "OGD"), ("ogm", "OGM"), ("ogb", "OGB"),
                                          ("fntp", "FNTP"), ("nvct", "NVCT"), ("gel", "GEL")])

        # geometria: la regola del progetto, invariata
        geom = None
        if opts.usa_evidenze and evidenze:
            parts, lines, ids = [], [], []
            for ev in evidenze:
                eg = ev.get("__geom", ev.get("_geometry"))
                if not eg:
                    continue
                if F(ev, "id"):
                    ids.append(str(F(ev, "id")))
                if ev.get("__relationNote"):
                    c.note(ev["__relationNote"])
                t = gtype(eg)
                if t == "MultiPolygon":
                    parts.extend(copy.deepcopy(eg.get("coordinates", [])))
                elif t == "Polygon":
                    parts.append(copy.deepcopy(eg.get("coordinates", [])))
                elif t == "MultiLineString":
                    lines.extend(copy.deepcopy(eg.get("coordinates", [])))
                elif t == "LineString":
                    lines.append(copy.deepcopy(eg.get("coordinates", [])))
            if parts:
                geom = {"type": "MultiPolygon", "coordinates": parts}
                o["_geometry_source"] = "scheda_evidenza_archeologica"
                o["_source_evidenza_ids"] = "|".join(ids)
                c.original = "MultiPolygon da %d evidenze (%d poligoni)" % (len(evidenze), len(parts))
                c.note("geometria presa dalle evidenze archeologiche collegate (%d), senza dissolvenza"
                       % len(evidenze))
            elif lines:
                geom = {"type": "MultiLineString", "coordinates": lines}
                o["_geometry_source"] = "scheda_evidenza_archeologica"
                o["_source_evidenza_ids"] = "|".join(ids)
                c.original = "MultiLineString da %d evidenze (%d linee)" % (len(evidenze), len(lines))
        if not geom:
            g = r.get("__geom", r.get("_geometry"))
            geom = to_multipoint(g)
            o["_geometry_source"] = "scheda_di_ritrovamento"
            c.original = gtype(g)
        gt = gtype(geom)
        o["_layer_gna"] = ("MOSI_multipolygon" if gt == "MultiPolygon"
                           else "MOSI_multilinea" if gt == "MultiLineString" else "MOSI_multipoint")
        # MOD-01: nota fissa al posizionamento solo per i MOSI puntuali/multipunto.
        from .entity_summary import GEN_MOSI_PUNTUALE
        o["gen"] = GEN_MOSI_PUNTUALE if o["_layer_gna"] == "MOSI_multipoint" else ""
        o["_geometry_output_type"] = gt
        if not geom:
            c.errore("geometria_assente", "record privo di geometria: non esportabile")
        return self.seal(c, o, geom)

    # -- orchestrazione ---------------------------------------------------

    def convert(self, interventi, ritrovamenti, evidenze, opts=None):
        opts = opts or ConversionOptions()
        res = ConversionResult()
        self._id_usati = {}
        report = res.report

        parents = {}
        visti = {}
        for r in interventi:
            k = str(F(r, "idscheda", "id"))
            if k:
                visti[k] = visti.get(k, 0) + 1
                if k not in parents:
                    parents[k] = r
        for k, n in visti.items():
            if n > 1:
                report.error("idscheda_duplicato",
                             "idscheda %s presente in %d schede Intervento" % (k, n),
                             tabella="GNA_DSC", campo="idscheda", record=k)
        for r in interventi:
            ammessa, valore, regola = self.dsc_ammissibile(r)
            scheda = self.intervento_to_dsc(r, opts, report)
            if ammessa or opts.working_template:
                res.dsc.append(scheda)
                continue
            # Il record è valido: non viene scartato, viene messo da parte.
            # I suoi ritrovamenti continuano a diventare MOSI: la regola
            # riguarda la scheda di scavo, non i siti che ha individuato.
            ammessi = ", ".join(self.ammissibilita.get("lemmi_ammessi") or [])
            modello = (self.ammissibilita.get("messaggio_vuoto") if blank(valore)
                       else self.ammissibilita.get("messaggio_escluso")) or ""
            messaggio = (modello % {"valore": valore, "ammessi": ammessi}) if modello else regola
            mid = F(r, "idscheda", "id")
            report.inammissibile("dsc_non_ammissibile", messaggio, tabella="GNA_DSC",
                                 campo=(self.ammissibilita.get("campi") or [""])[0],
                                 record=str(mid), regola=regola)
            scheda["_conversion_quality"] = "inammissibile"
            scheda["_rule"] = regola
            scheda["_precisione"] = "" if blank(valore) else str(valore)
            scheda["_conversion_notes"] = " | ".join(
                x for x in [scheda.get("_conversion_notes"), messaggio] if x)
            res.dsc_inammissibili.append(scheda)

        eb = {}
        if opts.usa_evidenze:
            for e in evidenze:
                k = str(F(e, "idiii_livello"))
                if k:
                    eb.setdefault(k, []).append(e)

        for r in ritrovamenti:
            if nv(F(r, "i_livello")) in {nv(x) for x in self.I_LIVELLO_SENZA_MOSI}:
                report.warning("mosi_non_luogo_escluso",
                               'ritrovamento %s ("%s / %s"): I livello «Non luogo», nessun MOSI generato'
                               % (F(r, "id"), F(r, "ii_livello") or "", F(r, "iii_livello") or ""),
                               tabella="GNA_MOSI", record=str(F(r, "id") or ""))
                res.mosi_esclusi_non_luogo.append(str(F(r, "id") or ""))
                continue
            p = parents.get(str(F(r, "idscheda")))
            es = eb.get(str(F(r, "id")), []) if opts.usa_evidenze else None
            for e in es or []:
                ei = F(e, "idintervento")
                expected = F(r, "idscheda")
                if not blank(ei) and expected and str(ei) != str(expected):
                    e["__relationNote"] = (
                        'idintervento evidenza "%s" diverso da ritrovamento.idscheda "%s": '
                        "geometria mantenuta perché idiii_livello è esplicito" % (ei, expected))
            m = self.iii_to_mosi(r, p, es, opts, report)
            if m["_layer_gna"] == "MOSI_multipolygon":
                res.mosi_multipolygon.append(m)
            elif m["_layer_gna"] == "MOSI_multilinea":
                res.mosi_multilinea.append(m)
            else:
                res.mosi_multipoint.append(m)
        self._segnala_punti_coincidenti(res, report)
        return res

    def _segnala_punti_coincidenti(self, res, report):
        """MOSI points sharing exactly the same position.

        In MAGOH these are findings without a position of their own. The
        coordinates are kept (nothing is invented); the positioning method is
        declared as approximate and the reason is written in DOZ.
        """
        gruppi = {}
        for m in res.mosi_multipoint:
            g = m.get("_geometry") or {}
            coords = g.get("coordinates") if isinstance(g, dict) else None
            if not coords:
                continue
            chiave = json.dumps([[round(float(x), 9) for x in pt[:2]] for pt in coords]) \
                if isinstance(coords[0], (list, tuple)) else json.dumps([round(float(x), 9) for x in coords[:2]])
            gruppi.setdefault(chiave, []).append(m)
        for membri in gruppi.values():
            if len(membri) < 2:
                continue
            ids = ", ".join(str(x.get("_source_ritrovamento_id") or x.get("_source_id") or "?") for x in membri)
            for m in membri:
                m["gpm"] = "posizionamento approssimato"
                nota = ("Posizione non determinata in MAGOH: coincide con quella di altri ritrovamenti "
                        "dello stesso intervento (%s)." % ids)
                m["doz"] = (str(m.get("doz") or "").rstrip() + "\n\n" + nota).strip()
                m["_conversion_notes"] = " | ".join(x for x in (m.get("_conversion_notes"), nota) if x)
            report.warning("mosi_punti_coincidenti",
                           "MOSI puntuali con la stessa posizione (%s): GPM impostato a "
                           "«posizionamento approssimato» e nota in DOZ" % ids, tabella="GNA_MOSI")

    # -- esportazione -----------------------------------------------------

    def export(self, res, outdir, opzioni=None):
        """Scrive il pacchetto di staging nella struttura prevista dal Template 1.6.

        Restituisce il dizionario prodotto da ``scrivi_pacchetto``: percorsi
        scritti, sommario strutturato e sommario in forma leggibile.
        """
        from .serializers import scrivi_pacchetto
        return scrivi_pacchetto(self, res, outdir, opzioni)
