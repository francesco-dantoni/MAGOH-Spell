# -*- coding: utf-8 -*-
"""Costruisce il pacchetto GNA 1.6.0 conferibile a partire dai dati MAGOH.

Il risultato è una copia del Template ufficiale — progetto QGIS, GeoPackage,
stili e plugin GNA — con dentro il MOPR, le schede DSC e le schede MOSI. Da lì
il conferimento si fa con il Plugin GNA ufficiale, come per un Template
compilato a mano: MAGOH non parla mai col server del Geoportale.

Tutto quello che sta qui gira **senza QGIS**, e questo è il punto: il pacchetto
si può generare e verificare in un collaudo automatico invece che soltanto a
mano dentro QGIS.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from ..security import safe_extract
from .entity_summary import GEN_MOSI_PUNTUALE

from .gna_template_writer import (ErroreTemplate, ScrittoreTemplate, Vocabolari,
                                  _nv, _vuoto)

# ---------------------------------------------------------------- MOPR

CPR_REGEX = re.compile(r"^[A-Z-]+_(19[0-9]{2}|20[0-9]{2})_[0-9]{5}-[A-Z]{2,4}_[0-9]{6,9}$")


def nome_file_progetto(nome):
    """Nome di file sicuro (Windows/macOS/Linux) per il pacchetto GNA (V1.15)."""
    testo = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', str(nome or '')).strip().strip('.')
    testo = re.sub(r'\s+', ' ', testo)
    testo = testo[:120].rstrip(' .') or 'Progetto_GNA'
    if testo.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM%d' % n for n in range(1, 10)), *('LPT%d' % n for n in range(1, 10))}:
        testo = 'Progetto_' + testo
    return testo


def cpr_valido(cpr):
    return bool(CPR_REGEX.match(str(cpr or "").strip()))


# ---------------------------------------------------------------- pacchetto

GNA_TEMPLATE_SOURCE = "https://gna.cultura.gov.it/download.html"


class PacchettoGNA:
    """Riempie una copia del Template GNA 1.6.0 con i dati convertiti."""

    NOME_CARTELLA = "PROGETTO_GNA_1_6_0"

    def __init__(self, dir_plugin):
        self.dir_plugin = Path(dir_plugin)
        self.template_zip = self.dir_plugin / "resources" / "Template_GNA_1.6.0.zip"
        conv = self.dir_plugin / "conversion"
        self.voc = Vocabolari(conv / "gna_vocab_snapshot_1_6.json",
                              conv / "gna_vocab_cache.json")
        self.mappe = json.loads(
            (conv / "mapping_template_1_6.json").read_text(encoding="utf-8-sig"))
        self.avvisi = []
        self.da_completare = {}

    # -- preparazione -----------------------------------------------------

    def _estrai_template(self, destinazione: Path):
        from ..gna_template_resources import verified_template_bytes
        from io import BytesIO
        if not self.template_zip.exists():
            raise ErroreTemplate(
                "Il Template GNA 1.6.0 non è incluso nel plugin: manca %s"
                % self.template_zip.name)
        if destinazione.exists():
            raise ErroreTemplate('La cartella del pacchetto esiste già: scegliere una nuova destinazione. Nessun file è stato sostituito.')
        destinazione.mkdir(parents=True)
        with zipfile.ZipFile(BytesIO(verified_template_bytes())) as z:
            safe_extract(z, destinazione)
        # il Template può stare in una sottocartella dello zip
        gpkg = next(iter(sorted(destinazione.rglob("Template_GNA.gpkg"))), None)
        qgz = next(iter(sorted(destinazione.rglob("Template_GNA_1.6.qgz"))), None)
        if gpkg is None or qgz is None:
            raise ErroreTemplate(
                "La copia del Template non contiene Template_GNA.gpkg e Template_GNA_1.6.qgz.")
        if gpkg.parent != destinazione:
            # porta il contenuto al primo livello: il .qgz cerca ./Template_GNA.gpkg
            for x in list(gpkg.parent.iterdir()):
                shutil.move(str(x), str(destinazione / x.name))
            gpkg = destinazione / gpkg.name
            qgz = destinazione / qgz.name
        # V1.15: the project to open is the one next to the GeoPackage, not the
        # reference copy kept in STILI/.
        accanto = gpkg.parent / "Template_GNA_1.6.qgz"
        if accanto.is_file():
            qgz = accanto
        return gpkg, qgz

    def _rinomina_pacchetto(self, gpkg: Path, qgz: Path, nome: str):
        """V1.15: GeoPackage e progetto prendono il nome del progetto MAGOH Spell.

        Il progetto ufficiale punta a ./Template_GNA.gpkg: i riferimenti nel .qgs
        vengono riscritti verso il nuovo nome. Stili e plugin GNA restano invariati.
        """
        nuovo_gpkg = gpkg.with_name(nome + '.gpkg')
        nuovo_qgz = qgz.with_name(nome + '.qgz')
        if nuovo_gpkg != gpkg:
            gpkg.rename(nuovo_gpkg)
        vecchio = gpkg.name
        nuovo = nuovo_gpkg.name
        temporaneo = qgz.with_name(qgz.name + '.tmp')
        with zipfile.ZipFile(qgz) as sorgente, zipfile.ZipFile(temporaneo, 'w', zipfile.ZIP_DEFLATED) as destinazione:
            for info in sorgente.infolist():
                dati = sorgente.read(info.filename)
                nome_membro = info.filename
                if nome_membro.lower().endswith('.qgs'):
                    document = ET.fromstring(dati)
                    for elem in document.iter():
                        if elem.text:
                            elem.text = elem.text.replace(vecchio, nuovo)
                        for key, value in list(elem.attrib.items()):
                            elem.set(key, value.replace(vecchio, nuovo))
                    dati = ET.tostring(document, encoding='utf-8', xml_declaration=True)
                    nome_membro = nome + '.qgs'
                destinazione.writestr(nome_membro, dati)
        qgz.unlink()
        temporaneo.rename(nuovo_qgz)
        return nuovo_gpkg, nuovo_qgz

    # -- traduzioni comuni ------------------------------------------------

    def _segnala(self, campo, motivo):
        self.da_completare.setdefault(campo, motivo)

    def _codici_luogo(self, comune, sigla, regione):
        """I codici ISTAT che il Template si aspetta al posto dei nomi."""
        cod_reg = self.voc.chiave("Regioni_tb", regione)
        prov = self.voc.etichette("Province_tb")
        cod_prov = None
        if not _vuoto(sigla):
            for k, v in prov.items():
                if _nv(v) == _nv(sigla):
                    cod_prov = k
                    break
        pro_com = None
        # V1.15.1: one index for all records instead of scanning ~8,000
        # municipalities (with text normalisation) for every DSC and MOSI.
        if getattr(self, '_indice_comuni', None) is None:
            self._indice_comuni = {}
            for k, v in (self.voc.dati.get("Comuni_tb") or {}).get("voci", {}).items():
                self._indice_comuni.setdefault(_nv(v.get("nome")), []).append((k, v))
        for k, v in self._indice_comuni.get(_nv(comune), ()) if not _vuoto(comune) else ():
            if not cod_prov or str(v.get('cod_prov')) == str(cod_prov):
                pro_com = k
                cod_prov = cod_prov or str(v.get("cod_prov") or "")
                cod_reg = cod_reg or str(v.get("cod_reg") or "")
                break
        if pro_com is None and not _vuoto(comune):
            self._segnala("PVCC / LCC",
                          'il codice ISTAT del comune "%s" non è nella copia locale dei '
                          "vocabolari: va scelto nel Template (elenco Comuni_tb)" % comune)
        return cod_reg, cod_prov, pro_com

    def _anni_da_dtr(self, dtr):
        a = self.voc.anni_dtr(dtr) if dtr else [None, None]
        return (a or [None, None])[0], (a or [None, None])[1]

    # -- costruzione ------------------------------------------------------

    def costruisci(self, risultato, mopr, cartella_uscita):
        """Scrive il pacchetto. `mopr` è un dizionario con i dati del progetto."""
        nome = nome_file_progetto(mopr.get('nome_progetto')) if mopr.get('nome_progetto') else ''
        nome_cartella = f"{nome}_GNA" if nome else self.NOME_CARTELLA
        cartella = Path(cartella_uscita) / nome_cartella
        self.spatial_filter=mopr.get('spatial_filter') or {}
        self.draft=bool(mopr.get('draft'))
        # record di staging -> (campo, valore) della chiave scritta nel GeoPackage
        self._chiavi_gna = {}
        gpkg, qgz = self._estrai_template(cartella)
        if nome:
            gpkg, qgz = self._rinomina_pacchetto(gpkg, qgz, nome)

        scrittore = ScrittoreTemplate(gpkg, self.voc)
        conteggi = {}
        try:
            cpr = str(mopr.get("cpr") or "").strip()
            if not mopr.get('draft') and not cpr_valido(cpr):
                raise ErroreTemplate(
                    "Il codice progetto (CPR) non ha la forma richiesta dal Template 1.6.0: "
                    "ENTE_ANNO_00000-XX_000000. Valore ricevuto: %r" % cpr)
            mopr_id = str(mopr.get("mopr_id") or uuid.uuid4())

            conteggi["MOPR"] = self._scrivi_mopr(scrittore, mopr, cpr, mopr_id)
            dsch_per_intervento, n_dsc = self._scrivi_dsc(scrittore, risultato, mopr, cpr)
            conteggi["DSC_multipolygon"] = n_dsc
            conteggi.update(self._scrivi_mosi(scrittore, risultato, mopr, cpr, mopr_id,
                                              dsch_per_intervento))
            for t in list(conteggi):
                if conteggi[t]:
                    scrittore.aggiorna_estensione(t)
            self.avvisi.extend(scrittore.avvisi)
            errori = list(scrittore.errori)
        finally:
            scrittore.chiudi()

        manifesto = self._manifesto(cartella, gpkg, qgz, cpr, conteggi, errori)
        self._scrivi_log(risultato, cartella)
        from ..gna_template_resources import prepare_offline_project
        prepare_offline_project(qgz)
        zip_path = self._comprimi(cartella)
        return {"cartella": str(cartella), "zip": str(zip_path), "gpkg": str(gpkg),
                "qgz": str(qgz), "conteggi": conteggi, "avvisi": list(self.avvisi),
                "errori": errori, "da_completare": dict(self.da_completare),
                "manifesto": manifesto}

    # -- MOPR -------------------------------------------------------------

    def _chiave_multipla(self, scrittore, sigla, valore):
        """V1.20: valore per una ValueRelation a scelta multipla, nella forma «{chiave}».

        Un'etichetta scritta così com'è verrebbe mostrata da QGIS tra parentesi.
        """
        if _vuoto(valore):
            return None
        testo = str(valore).strip()
        if testo.startswith("{") and testo.endswith("}"):
            return testo
        chiave = scrittore.voc.chiave(sigla, testo)
        if chiave is None:
            self._segnala(sigla, '"%s" non è nel vocabolario %s del Template 1.6: da scegliere nel Template'
                          % (testo, sigla))
            return None
        return "{%s}" % chiave

    def _scrivi_mopr(self, scrittore, mopr, cpr, mopr_id):
        geom = mopr.get("geometria")
        if not geom:
            raise ErroreTemplate("Il MOPR non ha una geometria: disegnare l'area di progetto.")
        cod_reg, cod_prov, pro_com = self._codici_luogo(
            mopr.get("comune"), mopr.get("sigla"), mopr.get("regione"))
        attrs = {
            "EMC": mopr.get("emc") or None,
            "CPR": cpr,
            "AMB": "archeologico",
            "AMA": mopr.get("ama") or "studio/ricerca",
            "CTG": mopr.get("ctg") or "INDAGINE TERRITORIALE",
            "OGT": mopr.get("ogt"),
            "OGN": mopr.get("ogn"),
            "DES": mopr.get("des"),
            "OGM": self._chiave_multipla(scrittore, "OGM", mopr.get("ogm")),
            "LCS": "Italia",
            "LCR": mopr.get('lcr') or cod_reg, "LCP": mopr.get('lcp') or cod_prov, "LCC": mopr.get('lcc') or pro_com,
            "GEL": mopr.get('gel') or "localizzazione fisica",
            "GET": "georeferenziazione areale",
            "GEP": "WGS84",
            "GPT": mopr.get("gpt"), "GPM": mopr.get("gpm"), "GPBB": mopr.get("gpbb"),
            "MISZ": "area", "MISU": "mq",
            "DCMY": 3,
            "FUR": '{' + str(mopr.get("fur") or '').strip('{}') + '}',
            "CMA": mopr.get("cma") or datetime.now().strftime("%Y"),
            "CMC": mopr.get("cmc"), "CMR": mopr.get("cmr"),
            "ADP": str(mopr.get('adp') or '1'), "RCGJ": "GNA",
            "DCMN": str(uuid.uuid4()),
            "MOPR_ID": mopr_id,
            "RCGH": str(uuid.uuid4()),
            "ENTE": mopr.get("ente"),
            "CAN": mopr.get("note"),
            "presa_visione": bool(mopr.get("presa_visione")),
            "upload_ok": False,
        }
        attrs = scrittore._traduci("MOPR", attrs, scrittore.CHIAVI_MOPR)
        if _vuoto(attrs.get("FUR")):
            self._segnala("FUR", "il funzionario responsabile si sceglie nel Template "
                                 "dall'elenco ufficiale FUR del Ministero")
        return scrittore.inserisci("MOPR", [(attrs, geom)])

    # -- DSC --------------------------------------------------------------

    def _scrivi_dsc(self, scrittore, risultato, mopr, cpr):
        righe = []
        dsch_per_intervento = {}
        tab_dsct = self.mappe.get("DSCT_via_AMA") or {}
        for r in risultato.dsc:
            dsch = str(uuid.uuid4())
            src = str(r.get("_source_idscheda") or r.get("_source_id") or "")
            if src:
                dsch_per_intervento[src] = dsch
            self._chiavi_gna[id(r)] = ("DSCH", dsch)
            cod_reg, cod_prov, pro_com = self._codici_luogo(
                r.get("pvcc"), r.get("pvcp"), r.get("pvcr"))
            dsct = r.get("dsct")
            dsct_ama = tab_dsct.get(str(dsct)) if dsct else None
            if dsct and not dsct_ama:
                self._segnala("DSCT", 'la tipologia "%s" non ha una corrispondenza nel '
                                      "vocabolario AMA, a cui il Template collega DSCT" % dsct)
            anno = r.get("dscy") or ""
            attrs = {
                "TSK": "DSC", "LIR": "I", "ESC": "GNA",
                "DSCV": r.get("dscv"),
                "DSCH": dsch,
                "DSCN": str(uuid.uuid4()),
                "DSCD": str(anno) if anno else None,
                "DSCY": anno or None,
                "DSCM": r.get("dscm"),
                "DSCT": dsct_ama,
                "NSC": r.get("nsc"),
                "OSS": r.get("oss"),
                "PVCS": "Italia",
                "PVCR": cod_reg, "PVCP": cod_prov, "PVCC": pro_com,
                "PVCL": r.get("pvcl"), "PVCI": r.get("pvci"),
                "GEL": "localizzazione fisica",
                "GET": "georeferenziazione areale",
                "GEP": "WGS84",
                "GPT": r.get("gpt"), "GPM": r.get("gpm"), "GPBB": r.get("gpbb"),
                "BIBM": r.get("bibm"),
                "MTAR": r.get("mtar"),
                "ADSP": "1",
                "CMPD": r.get("cmpd") or mopr.get("cma"),
                "CMPN": r.get("cmpn") or mopr.get("cmc"),
                "RSR": r.get("rsr") or mopr.get("cmr"),
                "FUR": mopr.get("fur_key"),
                "CPR_MOPR": cpr,
                "DSCEX": r.get("dscex"),
            }
            # DSCT arriva già come chiave AMA: si esclude dalla traduzione
            valore_dsct = attrs.pop("DSCT", None)
            attrs = scrittore._traduci("DSC_multipolygon", attrs,
                                       {k: v for k, v in scrittore.CHIAVI_DSC.items()
                                        if k != "DSCT"})
            attrs["DSCT"] = valore_dsct
            righe.append((attrs, r.get("_geometry")))
        if righe and _vuoto(mopr.get("fur_key")):
            self._segnala("FUR", "il funzionario responsabile si sceglie nel Template "
                                 "dall'elenco ufficiale FUR del Ministero")
        return dsch_per_intervento, scrittore.inserisci("DSC_multipolygon", righe)

    # -- MOSI -------------------------------------------------------------

    def _scrivi_mosi(self, scrittore, risultato, mopr, cpr, mopr_id, dsch_per_intervento):
        gruppi = (("MOSI_multipolygon", risultato.mosi_multipolygon, "georeferenziazione areale"),
                  ("MOSI_multilinea", risultato.mosi_multilinea, "georeferenziazione lineare"),
                  ("MOSI_multipoint", risultato.mosi_multipoint, "georeferenziazione puntuale"))
        conteggi = {}
        for tabella, righe_src, get in gruppi:
            if not righe_src:
                continue
            righe = []
            for r in righe_src:
                idv = str(r.get("id") or "").strip()
                self._chiavi_gna[id(r)] = (("ACCC", "%s_%s" % (cpr, idv)) if idv
                                           else ("id_viarch", ""))
                cod_reg, cod_prov, pro_com = self._codici_luogo(
                    r.get("lcc"), r.get("lcp"), r.get("lcr"))
                dtr = r.get("dtr")
                dtsi, dtsf = r.get("dtsi"), r.get("dtsf")
                if _vuoto(dtsi) or _vuoto(dtsf):
                    a, b = self._anni_da_dtr(dtr)
                    if _vuoto(dtsi) and a is not None:
                        dtsi = a
                    if _vuoto(dtsf) and b is not None:
                        dtsf = b
                    if _vuoto(dtsi) or _vuoto(dtsf):
                        self._segnala("DTSI / DTSF",
                                      "cronologia specifica assente e non deducibile dal periodo: "
                                      "va completata nel Template")
                attrs = {
                    "id_viarch": idv,
                    "ACCC": "%s_%s" % (cpr, idv) if idv else None,
                    "AMB": "archeologico",
                    "AMA": mopr.get("ama") or "studio/ricerca",
                    "OGD": r.get("ogd"),
                    "OGN": r.get("ogn"),
                    "OGB": r.get("ogb") or "dato non disponibile",
                    "OGZ": r.get("ogz"),
                    "OGT": r.get("ogt_key"),
                    "LCS": "Italia",
                    "LCR": cod_reg, "LCP": cod_prov, "LCC": pro_com,
                    "PVL": r.get("pvl"),
                    "DTR": dtr,
                    "DTT": None,  # V1.19 (MOD-03): mai compilato
                    "DTSI": dtsi, "DTSF": dtsf,
                    "DES": r.get("des"),
                    "DOZ": r.get("doz"),
                    "OGM": r.get("ogm"),
                    "FNTP": self.mappe.get("FNTP", {}).get(str(r.get("fntp")), r.get("fntp")),
                    "GEL": "localizzazione fisica",
                    "GET": get,
                    "GEP": "WGS84",
                    "GPT": r.get("gpt"), "GPM": r.get("gpm"), "GPBB": r.get("gpbb"),
                    # V1.19 (MOD-01): la nota dipende dal layer effettivo di esportazione.
                    "GEN": GEN_MOSI_PUNTUALE if tabella == "MOSI_multipoint" else None,
                    "CDGG": r.get("cdgg") or "dato non disponibile",
                    "BPT": "dato non disponibile",
                    "NVCT": r.get("nvct"),
                    "BIBM": r.get("bibm"),
                    "MTAM": r.get("mtam"), "MTAR": r.get("mtar"),
                    "FUR": mopr.get("fur_key"),
                    "CMA": r.get("cma") or mopr.get("cma"),
                    "CMC": r.get("cmc") or mopr.get("cmc"),
                    "CMR": r.get("cmr") or mopr.get("cmr"),
                    "ADP": "1", "DCMP": "4",
                    "DCMN": str(uuid.uuid4()),
                    "RCGJ": "GNA", "DSCJ": "GNA",
                    "SSK": "Non validata",
                    "CPR_MOPR": cpr, "VRRP": cpr, "MOPR_id": mopr_id,
                    "DSCH": dsch_per_intervento.get(str(r.get("_source_intervento_id") or "")),
                }
                if not _vuoto(r.get("ogt")) and not r.get('ogt_key'):
                    self._segnala("OGT",
                                  "la definizione di dettaglio MAGOH (III livello) non ha un "
                                  "corrispondente certo nel vocabolario OGT del GNA: è stata "
                                  "conservata nella sintesi OGN/DES; OGT va scelto nel Template")
                attrs = scrittore._traduci(tabella, attrs, scrittore.CHIAVI_MOSI)
                righe.append((attrs, r.get("_geometry")))
            conteggi[tabella] = scrittore.inserisci(tabella, righe)
        return conteggi

    # -- contorno ---------------------------------------------------------

    def _manifesto(self, cartella, gpkg, qgz, cpr, conteggi, errori):
        m = {
            "template_gna": "1.6.0",
            "fonte_template_gna": GNA_TEMPLATE_SOURCE,
            "generato": datetime.now().isoformat(timespec="seconds"),
            "origine": "MAGOH Spell",
            "CPR": cpr,
            "record_inseriti": conteggi,
            "stato": 'template_di_lavoro_da_completare' if getattr(self,'draft',False) else 'da_verificare',
            "filtro_MOPR": getattr(self,'spatial_filter',{}),
            "avvisi": self.avvisi[:200],
            "errori": errori[:200],
            "da_completare_nel_template": self.da_completare,
            "gpkg": gpkg.name, "qgz": qgz.name,
            "conferimento": ("Aprire " + qgz.name + ", scaricare con il Plugin GNA i MOSI "
                             "già presenti entro il MOPR, controllare e usare 'invia'."),
        }
        (cartella / "MAGOH_GNA_manifest.json").write_text(
            json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        righe = [
            "PACCHETTO GNA 1.6.0 GENERATO DA MAGOH",
            "=" * 46, "",
            "Contiene una copia adattata del Template GNA 1.6.0 con i dati MAGOH convertiti.",
            "Fonte del Template GNA: Geoportale Nazionale per l’Archeologia, Ministero della Cultura.",
            GNA_TEMPLATE_SOURCE,
            "",
            "Record inseriti:",
        ]
        for k, v in conteggi.items():
            righe.append("   %-22s %6d" % (k, v))
        if self.da_completare:
            righe += ["", "Da completare nel Template prima del conferimento:"]
            for campo, motivo in self.da_completare.items():
                righe.append("   %-14s %s" % (campo, motivo))
        righe += [
            "", "Come conferire:",
            "  1. Aprire " + qgz.name + " in QGIS.",
            "  2. Attivare il Plugin GNA 1.6.0 (disponibile separatamente dal Geoportale GNA).",
            "  3. Scaricare i MOSI già presenti nel perimetro del MOPR.",
            "  4. Risolvere eventuali duplicati e completare i campi elencati sopra.",
            "  5. Usare la funzione 'invia' del Plugin GNA.",
            "",
            "MAGOH non invia nulla al server del Geoportale: il conferimento resta",
            "interamente nelle mani del software ministeriale.",
        ]
        (cartella / "LEGGIMI_MAGOH_GNA.txt").write_text("\n".join(righe), encoding="utf-8")
        return str(cartella / "MAGOH_GNA_manifest.json")

    def _scrivi_log(self, risultato, cartella):
        """Log di conversione dentro il pacchetto: lega ogni record GNA alla
        scheda MAGOH di partenza ed elenca anche i record esclusi, con il motivo.
        È l'unico posto in cui restano gli identificativi MAGOH."""
        from .serializers import LOG_COLS_PACCHETTO, log_rows, write_csv
        righe = log_rows(risultato, chiavi=getattr(self, "_chiavi_gna", {}))
        return write_csv(cartella / "conversion_log.csv", righe, LOG_COLS_PACCHETTO)

    def _comprimi(self, cartella: Path):
        zip_path = cartella.parent / (cartella.name + '.zip')
        if zip_path.exists():
            raise ErroreTemplate('Lo ZIP esiste già: nessun file è stato sostituito / ZIP already exists: nothing overwritten')
        with zipfile.ZipFile(zip_path, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for p in sorted(cartella.rglob("*")):
                if p.is_file():
                    z.write(p, str(Path(cartella.name) / p.relative_to(cartella)))
        return zip_path
