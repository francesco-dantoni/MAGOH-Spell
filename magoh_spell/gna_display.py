"""Small offline preview labels; no geometry or vocabulary network requests."""
from functools import lru_cache
import csv
import re
from .gna_template_resources import vocabulary_catalog
from .i18n import _tf


@lru_cache(maxsize=8)
def labels(name):
    spec = vocabulary_catalog()['vocabularies'].get(name, {})
    return {str(f['properties'].get(name+'_key')): str(f['properties'].get(name+'_value') or '')
            for f in spec.get('data', {}).get('features', [])}


def describe(role, feature):
    fields = feature.fields()
    def value(name):
        index = fields.lookupField(name)
        if index < 0:
            return ''
        v = feature[index]
        if v is None or (hasattr(v, 'isNull') and v.isNull()):
            return ''
        return str(v).strip()
    def vocabulary(name):
        raw = value(name)
        if raw.startswith('{') and raw.endswith('}'):
            tokens = next(csv.reader([raw[1:-1]], skipinitialspace=True), [])
        else:
            tokens = [raw] if raw else []
        lookup = labels(name)
        return '; '.join(lookup.get(token.strip(), token.strip()) for token in tokens)
    if role == 'dsc':
        # GNA 1.6 calls the field DSCV; accept DSVC in external schemas too.
        parts = (value('DSCY'), value('DSVC') or value('DSCV'))
    else:
        parts = (vocabulary('OGT'), vocabulary('OGD'), vocabulary('DTR'))
    # Missing values add neither placeholders nor dangling separators.
    return ' — '.join(part for part in parts if part)


# ---------------------------------------------------------------------------
# due colonne «ID scheda» e «Ubicazione» nei risultati di Gestione.
# Solo lettura: nessun campo viene scritto per alimentare l'anteprima.
# L'ID di provenienza MAGOH si mostra così com'è; un codice GNA o il fid
# tecnico di QGIS portano sempre il nome del campo, così non si confondono.
# ---------------------------------------------------------------------------
MAGOH_VIEW = re.compile(r'https://magoh\.cfs\.unipi\.it/c/magoh/view\?id=(\d+)')
MAGOH_FINDING = re.compile(r'Sintesi dei dati MAGOH — Ritrovamento (\d+)')
LOCAL_CODE = re.compile(r'Dato creato con il plug-in MAGOH Spell \(codice ([A-Z]+-\d+)\)')
OGN_SEPARATOR = ' – '


def _text(feature, name):
    index = feature.fields().lookupField(name)
    if index < 0:
        return ''
    v = feature[index]
    if v is None or (hasattr(v, 'isNull') and v.isNull()):
        return ''
    text = str(v).strip()
    return '' if text.upper() == 'NULL' else text


def _short(text, limit=14):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def preview_identity(role, feature):
    """(testo, descrizione dell'origine) dell'identificativo da mostrare."""
    if role == 'intervento':
        local = _text(feature, 'codice_locale')
        if local:
            return local, _tf('Codice scheda locale', 'Local record code')
        ident = _text(feature, 'idscheda')
        if ident:
            return ident, _tf('ID scheda MAGOH (idscheda)', 'MAGOH record ID (idscheda)')
    elif role == 'dsc':
        found = MAGOH_VIEW.findall(_text(feature, 'OSS'))
        if found:
            return found[-1], _tf('ID scheda Intervento MAGOH di provenienza (da OSS)', 'Source MAGOH Intervento ID (from OSS)')
        local = LOCAL_CODE.findall(_text(feature, 'OSS'))
        if local:
            return local[-1], _tf('Codice scheda locale (da OSS)', 'Local record code (from OSS)')
        dsch = _text(feature, 'DSCH')
        if dsch:
            return 'DSCH ' + _short(dsch, 9), _tf('Codice GNA DSCH: {v}', 'GNA code DSCH: {v}', v=dsch)
    elif role == 'mosi':
        found = MAGOH_FINDING.search(_text(feature, 'DES'))
        if found:
            return found.group(1), _tf('ID Ritrovamento MAGOH di provenienza (da DES)', 'Source MAGOH Ritrovamento ID (from DES)')
        accc = _text(feature, 'ACCC')
        if _text(feature, 'GNA') == '1' and accc:
            # record downloaded from the GNA by the official plugin:
            # its id_viarch is only a progressive number, ACCC is its code.
            return 'ACCC ' + _short(accc), _tf('Scheda già presente nel GNA, codice ACCC: {v}', 'Record already in the GNA, ACCC code: {v}', v=accc)
        idv = _text(feature, 'id_viarch')
        if idv:
            return 'id_viarch ' + idv, _tf('Identificativo id_viarch del MOSI', 'MOSI id_viarch identifier')
        accc = _text(feature, 'ACCC')
        if accc:
            return 'ACCC ' + _short(accc), _tf('Codice GNA ACCC: {v}', 'GNA code ACCC: {v}', v=accc)
    fid = feature.id()
    return 'fid %s' % fid, _tf('Solo identificativo tecnico QGIS (fid)', 'QGIS technical fid only')


def preview_location(role, feature, dsc_locations=None):
    """Ubicazione da mostrare; per i MOSI quella dell'Intervento collegato."""
    if role == 'intervento':
        return _text(feature, 'ubicazione')
    if role == 'dsc':
        return _text(feature, 'DSCV') or _text(feature, 'DSVC')
    if role == 'mosi':
        linked = (dsc_locations or {}).get(_text(feature, 'DSCH'), '')
        if linked:
            return linked
        ogn = _text(feature, 'OGN')
        head, sep, tail = ogn.partition(OGN_SEPARATOR)
        if sep and head.strip() and tail.strip():
            return tail.strip()
        return _text(feature, 'PVL')
    return ''
