"""Readable source identity, without inferring archaeological classifications."""
import re
import unicodedata
from functools import lru_cache
from ..gna_template_resources import vocabulary_catalog


def normalize(value):
    text = unicodedata.normalize('NFKD', str(value or ''))
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in text.lower() if not unicodedata.combining(c))).strip()


#: MOD-01: testo fisso del campo GEN per ogni MOSI puntuale o multipunto.
#: Va scritto esattamente così, senza identificativi o altre frasi.
GEN_MOSI_PUNTUALE = (
    "La geometria puntuale costituisce una rappresentazione convenzionale dell'evidenza "
    'archeologica, adottata nel modello dati MAGOH quando le informazioni disponibili non '
    "consentono di definirne l'estensione planimetrica. Il punto riprende la localizzazione "
    'associata alla scheda di ritrovamento MAGOH e non rappresenta necessariamente la posizione'
    " esatta o l'intera estensione dell'evidenza. La medesima localizzazione può essere "
    'condivisa da più evidenze archeologiche distinte, determinando la sovrapposizione di più '
    'geometrie puntuali.'
)


@lru_cache(maxsize=1)
def dictionaries():
    catalog = vocabulary_catalog()['vocabularies']
    return {name: [f['properties'] for f in catalog[name]['data']['features']] for name in ('OGD', 'OGT')}


def safe_ogt(term, ogd):
    """Only one active, exact term inside the already established OGD."""
    rows = dictionaries()
    parents = {str(r['OGD_key']) for r in rows['OGD']
               if str(r['OGD_key']) == str(ogd) or normalize(r['OGD_value']) == normalize(ogd)}
    found = {str(r['OGT_key']) for r in rows['OGT']
             if r.get('ATT') is True and str(r['OGD_key']) in parents and
             normalize(r['OGT_value']) == normalize(term)}
    return '{' + next(iter(found)) + '}' if len(found) == 1 else None


def compose_ogn(iii_livello, ubicazione):
    """OGN del MOSI: III livello del Ritrovamento e Ubicazione dell'Intervento.

    Nessun altro campo: niente tipi di evidenza, cronologia o denominazione.
    Il III livello prende l'iniziale maiuscola; un valore mancante non lascia
    separatori vuoti. Oltre 250 caratteri il testo viene accorciato con «…».
    """
    definizione = str(iii_livello or '').strip()
    definizione = definizione[:1].upper() + definizione[1:]
    luogo = str(ubicazione or '').strip()
    ogn = ' – '.join(x for x in (definizione, luogo) if x)
    return ogn if len(ogn) <= 250 else ogn[:247].rstrip() + '…'


def build_summary(record, parent, get):
    """Return OGN, the Ritrovamento summary that opens DES, and DTT.

    OGN = III livello + Ubicazione (compose_ogn). The DES summary holds
    only the Ritrovamento ID and its I, II and III level labels (MOD-02): no
    IV level block and no «Cronologia del ritrovamento» line. DTT is never
    filled (MOD-03).
    """
    levels = [(label, str(get(record, name) or '').strip()) for label, name in
              (('I livello', 'i_livello'), ('II livello', 'ii_livello'), ('III livello', 'iii_livello'))]
    heading = compose_ogn(get(record, 'iii_livello'), get(parent or {}, 'ubicazione'))
    identifier = str(get(record, 'id') or '').strip()
    blocks = ['Sintesi dei dati MAGOH — Ritrovamento' + (' ' + identifier if identifier else '')]
    # i livelli seguono subito l'intestazione, senza riga vuota.
    blocks.extend('%s: %s' % pair for pair in levels if pair[1])
    return heading, '\n'.join(blocks), None


def compose_des(summary, narrative, intervention_id):
    """DES = Ritrovamento summary + blank line + full Intervento narrative (MOD-02).

    The narrative is used as it is: never summarised, rewritten, truncated or
    filtered by keywords. Without a usable narrative, DES is the summary alone.
    """
    narrative = str(narrative or '').strip()
    if not narrative:
        return summary
    identifier = str(intervention_id or '').strip()
    header = 'Descrizione del contesto — scheda Intervento MAGOH' + (' ' + identifier if identifier else '') + ':'
    return summary + '\n\n' + header + '\n' + narrative
