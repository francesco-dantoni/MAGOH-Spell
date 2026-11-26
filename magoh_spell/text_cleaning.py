"""Plain, readable archaeological text, without HTML or repeated source footers."""
import html
import re
from html.parser import HTMLParser

class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
        if not self.hidden and tag in ('p','div','br','li','tr','h1','h2','h3'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style') and self.hidden:self.hidden-=1
        if not self.hidden and tag in ('p','div','li','tr'):self.parts.append('\n')
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)

def plain_text(value):
    if value is None:return ''
    value=str(value)
    # Decode HTML-escaped markup too, but avoid repeatedly unescaping plain prose.
    if re.search(r'&lt;/?(?:p|br|div|a|span)\b',value,re.I):value=html.unescape(value)
    parser=_PlainText();parser.feed(value);parser.close()
    lines=[re.sub(r'[\t \xa0]+',' ',s).strip() for s in ''.join(parser.parts).splitlines()]
    return re.sub(r'\n{3,}','\n\n','\n'.join(lines)).strip()

_LOCAL_ORIGIN='Dato creato con il plug-in MAGOH Spell'
_FOOTER=re.compile(r'\s*(?:Dati estratti dalla piattaforma MAGOH\s*;?\s*(?:per maggiori dettagli\s*:?)?\s*'
 r'(?:https://magoh\.cfs\.unipi\.it/c/magoh/view\?id=\d+\s*)+'
 r'|'+_LOCAL_ORIGIN+r'(?: \(codice [A-Z]+-\d+\))?; non presente nella piattaforma MAGOH\.\s*)',re.I)

def scientific_description(value):
    """Move only recognizable generated source footers, never arbitrary prose."""
    return _FOOTER.sub('',plain_text(value)).strip()

def is_magoh_id(identifier):
    identifier=str(identifier or '').strip()
    return bool(re.fullmatch(r'\d+',identifier)) and int(identifier)>0

def magoh_provenance(identifier,local_code=''):
    """Link to the MAGOH record; a record created in QGIS has none to link."""
    code=str(local_code or '').strip()
    if not code and is_magoh_id(identifier):
        return ('Dati estratti dalla piattaforma MAGOH; per maggiori dettagli: '
                'https://magoh.cfs.unipi.it/c/magoh/view?id='+str(identifier).strip())
    if not code and not str(identifier or '').strip():
        return ''
    return _LOCAL_ORIGIN+(' (codice %s)'%code if code else '')+'; non presente nella piattaforma MAGOH.'

def with_provenance(notes,identifier,local_code=''):
    body=scientific_description(notes)
    source=magoh_provenance(identifier,local_code)
    return '\n\n'.join(p for p in (body,source) if p)


_URL=re.compile(r'https?://\S+',re.I)
_REDUCED='[…] Testo ridotto per il limite di %d caratteri del GNA'
_REDUCED_REF=_REDUCED+': il testo integrale è nella scheda indicata sotto.'


def _cut_on_word(text,limit):
    if len(text)<=limit:
        return text
    if limit<=0:
        return ''
    cut=text.rfind(' ',0,limit)
    if cut<limit*0.6:
        cut=limit
    return text[:cut].rstrip()


def fit_with_references(text,limit,reference=''):
    """Bring ``text`` within ``limit`` characters, always keeping its URL references.

    Paragraphs that contain a URL (for instance the link to the MAGOH record)
    are set aside, the rest is shortened on a word boundary and marked as
    reduced, then the URL paragraphs are appended again. ``reference`` is an
    extra paragraph (such as the MAGOH provenance line) added only when the
    text has to be shortened and is not already present. A text within the
    limit is returned unchanged.
    """
    text=str(text or '')
    limit=int(limit or 0)
    if not limit or len(text)<=limit:
        return text
    paragraphs=[p for p in re.split(r'\n\s*\n',text)]
    refs=[p.strip() for p in paragraphs if _URL.search(p)]
    body='\n\n'.join(p for p in paragraphs if not _URL.search(p)).strip()
    reference=str(reference or '').strip()
    if reference and reference not in refs:
        refs.append(reference)
    tail='\n\n'.join(refs)
    marker=(_REDUCED_REF if refs else _REDUCED+'.')%limit
    if len(tail)+len(marker)+4>limit:
        # Even the references do not fit with the note: keep the URLs alone.
        urls=[]
        for p in refs:
            for u in _URL.findall(p):
                if u not in urls:
                    urls.append(u)
        tail=' '.join(urls)
        marker=(_REDUCED%limit)+':'
        if len(tail)+len(marker)+4>limit:
            return _cut_on_word(text,limit)
    budget=limit-len(tail)-len(marker)-(4 if tail else 1)
    kept=_cut_on_word(body,budget)
    result=(kept+' '+marker).strip()
    if tail:
        result+='\n\n'+tail
    return result if len(result)<=limit else _cut_on_word(result,limit)
