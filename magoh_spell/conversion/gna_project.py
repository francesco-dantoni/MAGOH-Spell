# -*- coding: utf-8 -*-
"""Generazione di un progetto ufficiale GNA 1.6.0 a partire da MAGOH.

Questa unità NON sostituisce il Plugin GNA ministeriale. Il suo compito è:
- copiare senza alterare il pacchetto Template GNA 1.6.0 ufficiale;
- inserire un solo MOPR generico (profilo studio/ricerca);
- riversare DSC e MOSI convertiti nei layer reali del GeoPackage ufficiale;
- valorizzare i campi ereditati dal MOPR e i default che il progetto QGIS
  ufficiale genererebbe normalmente durante il data-entry;
- tradurre le etichette dei vocabolari nelle chiavi attese dal Template.

Il conferimento resta affidato al Plugin GNA ufficiale, che effettua anche il
controllo di avvenuto download dei MOSI già presenti nel MOPR.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path


CPR_RE = re.compile(r"^[A-Z-]+_(19[0-9]{2}|20[0-9]{2})_[0-9]{5}-[A-Z]{2,4}_[0-9]{6,9}(_[0-9]{2})?$")
PERSONAL_CODE_RE = re.compile(r"^[0-9]{5}-[A-Z]{2,4}$")


class GnaProjectError(RuntimeError):
    pass


def _norm(v):
    return re.sub(r"\s+", " ", str(v or "").strip()).casefold()


def _blank(v):
    return v is None or str(v).strip() in ("", "NULL")


def build_cpr(emc: str, year: str, personal_code: str, progressive: str) -> str:
    emc = str(emc or "").strip().upper().replace(" ", "-")
    personal_code = str(personal_code or "").strip().upper()
    progressive = re.sub(r"\D", "", str(progressive or ""))
    year = re.sub(r"\D", "", str(year or ""))
    if len(progressive) < 6:
        progressive = progressive.zfill(6)
    return f"{emc}_{year}_{personal_code}_{progressive}"


def validate_cpr(cpr: str) -> bool:
    return bool(CPR_RE.fullmatch(str(cpr or "").strip()))


@dataclass
class GnaMoprData:
    # campi obbligatori / essenziali del MOPR generico per studio/ricerca
    cpr: str
    emc: str
    ogn: str
    des: str
    ente: object
    fur: object
    cma: str
    cmc: str
    cmr: str
    regione: str
    provincia: str
    comune: str
    geometry: dict

    # georeferenziazione coerente con la conversione MAGOH
    gpt: str = ""
    gpm: str = ""
    gpbb: str = ""

    # Profilo GNA per ricerca/tesi: AMA 9, CTG 3, OGT 26.
    ama: object = "9"
    ctg: object = "3"
    ogt: object = "26"
    adp: object = "1"
    presa_visione: bool = False
    note: str = ""
    gel: str = "1"
    lcr: str = ""
    lcp: str = ""
    lcc: str = ""

    # valori tecnici creati una sola volta per il pacchetto
    mopr_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def validate(self, draft=False):
        if draft:
            return ([] if isinstance(self.geometry,dict) and self.geometry.get('type') in ('Polygon','MultiPolygon')
                    else ['La geometria MOPR deve essere poligonale.'])
        errors = []
        if not validate_cpr(self.cpr):
            errors.append(
                "CPR non conforme. Formato atteso: ENTE_ANNO_00000-XX_000000 "
                "(es. SABAP-FI_2026_00020-MS_000043)."
            )
        if not self.emc.strip():
            errors.append("EMC / Ente MiC coinvolto mancante.")
        if not self.ogn.strip():
            errors.append("OGN / Denominazione del progetto mancante.")
        if not self.des.strip():
            errors.append("DES / Descrizione del progetto mancante.")
        if _blank(self.ente):
            errors.append("ENTE / SABAP di riferimento mancante.")
        if _blank(self.fur):
            errors.append("FUR / Funzionario responsabile mancante.")
        if not re.fullmatch(r"\d{4}", str(self.cma or "")):
            errors.append("CMA / Anno di redazione non valido.")
        for field_name, value in (("CMC", self.cmc), ("CMR", self.cmr)):
            if not str(value or "").strip() or "," not in str(value):
                errors.append(f"{field_name} deve essere nella forma 'Cognome, Nome'.")
        if not self.regione.strip() or not self.provincia.strip() or not self.comune.strip():
            errors.append("Regione, provincia e comune del MOPR devono essere indicati.")
        if not isinstance(self.geometry, dict) or self.geometry.get("type") not in ("Polygon", "MultiPolygon"):
            errors.append("La geometria MOPR deve essere poligonale.")
        if not self.presa_visione:
            errors.append("È necessaria la presa visione dell'obbligo di scaricare i MOSI già presenti in GNA.")
        return errors


class GnaProjectBuilder:
    """Costruisce il pacchetto GNA ufficiale.

    Conserva l'interfaccia usata dalla finestra di esportazione; il lavoro vero
    lo fa `gna_package.PacchettoGNA`, che scrive il GeoPackage del Template
    direttamente in SQLite e quindi si può collaudare fuori da QGIS.
    """

    def __init__(self, plugin_dir):
        self.plugin_dir = Path(plugin_dir)
        self.warnings = []

    # -- contenimento -----------------------------------------------------

    @staticmethod
    def _coordinate(x):
        if isinstance(x, (list, tuple)):
            if len(x) >= 2 and all(isinstance(v, (int, float)) for v in x[:2]):
                yield x
            else:
                for y in x:
                    for z in GnaProjectBuilder._coordinate(y):
                        yield z


    # -- costruzione ------------------------------------------------------

    def build(self, result, mopr, staging_dir, spatial_filter=None, project_name=None):
        errori = mopr.validate(draft=True)
        if errori:
            raise GnaProjectError("MOPR incompleto:\n- " + "\n- ".join(errori))

        from .mopr_filter import filter_to_mopr
        result, checked_filter = filter_to_mopr(result, mopr.geometry)
        spatial_filter = spatial_filter or checked_filter

        from .gna_package import PacchettoGNA

        dati = {
            "draft": True,
            "spatial_filter": spatial_filter,
            "cpr": mopr.cpr, "emc": mopr.emc, "ogn": mopr.ogn, "des": mopr.des,
            "ente": mopr.ente, "fur": mopr.fur, "fur_key": mopr.fur,
            "cma": mopr.cma, "cmc": mopr.cmc, "cmr": mopr.cmr,
            "regione": mopr.regione, "sigla": mopr.provincia, "comune": mopr.comune,
            "gpt": mopr.gpt, "gpm": mopr.gpm, "gpbb": mopr.gpbb,
            "gel": mopr.gel, "adp": mopr.adp,
            "lcr": mopr.lcr, "lcp": mopr.lcp, "lcc": mopr.lcc,
            "ama": mopr.ama, "ctg": mopr.ctg, "ogt": mopr.ogt,
            "ogm": "dati di archivio",
            "note": mopr.note, "presa_visione": mopr.presa_visione,
            "mopr_id": mopr.mopr_id, "geometria": mopr.geometry,
            # il pacchetto prende il nome del progetto MAGOH Spell.
            "nome_progetto": project_name or "",
        }
        pacchetto = PacchettoGNA(self.plugin_dir)
        esito = pacchetto.costruisci(result, dati, Path(staging_dir))
        self.warnings = list(esito.get("avvisi") or [])
        self.warnings.append('Template di lavoro: i campi richiesti dal conferimento ufficiale possono essere completati successivamente.')

        c = esito["conteggi"]
        mosi = sum(v for k, v in c.items() if k.startswith("MOSI"))
        return {
            "project_dir": esito["cartella"],
            "project_zip": esito["zip"],
            "gpkg": esito["gpkg"],
            "qgz": esito["qgz"],
            "manifest": esito["manifesto"],
            "warnings": self.warnings,
            "da_completare": esito.get("da_completare") or {},
            "errors": esito.get("errori") or [],
            "counts": {"MOPR": c.get("MOPR", 0), "DSC": c.get("DSC_multipolygon", 0),
                       "MOSI": mosi},
        }
