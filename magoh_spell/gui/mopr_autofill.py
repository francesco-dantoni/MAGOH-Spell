"""Conservative MOPR proposals from detached sources and bundled vocabularies."""
import re
import unicodedata
from qgis.core import (QgsGeometry, QgsFeatureRequest, QgsCoordinateTransform,
                       QgsCoordinateReferenceSystem)
from ..api_admin_index import load_index
from ..geographic_lookups import municipality_names

# Verified 15 October 2026. Province PT moved to Lucca in August 2025.
# The keys must also exist in the included official ENTE vocabulary.
TUSCAN_AUTHORITIES = {'FI':'SABAP-FI','PO':'SABAP-FI','LU':'SABAP-LU','MS':'SABAP-LU',
                     'PT':'SABAP-LU','PI':'SABAP-PI','LI':'SABAP-PI','SI':'SABAP-SI',
                     'GR':'SABAP-SI','AR':'SABAP-SI'}
# Fonti della tabella precedente (competenze territoriali delle SABAP):
# https://soprintendenzafirenze.cultura.gov.it/
# https://cultura.gov.it/ente/soprintendenza-archeologia-belle-arti-e-paesaggio-per-le-province-di-lucca-massa-carrara-e-pistoia
# https://sabappisalivorno.cultura.gov.it/
# https://cultura.gov.it/ente/soprintendenza-archeologia-belle-arti-e-paesaggio-per-le-province-di-siena-grosseto-e-arezzo


def norm(text):
    value = ''.join(c for c in unicodedata.normalize('NFKD', str(text or '')) if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', value.replace('’', "'")).strip().casefold()


def geography_fields(layer):
    fields = {norm(f.name()):f.name() for f in layer.fields()}
    result = {}
    for role, names in {
        'comune':('id_comune','comune','nome_comune'),
        'provincia':('provincia','nome_provincia','sigla_provincia','sigla_prov'),
        'regione':('regione','nome_regione'), 'istat':('pro_com','cod_istat','istat','lcc')}.items():
        for name in names:
            if name in fields:
                result[role]=fields[name]; break
    title = norm(layer.name())
    for stem, role in (('comun','comune'),('provinc','provincia'),('region','regione')):
        if stem in title and 'nome' in fields and role not in result:
            result[role]=fields['nome']
    return result


def scan_geography(task, sources, area_wkb, context):
    """No access to live layers/project/widgets from the QGIS worker."""
    area = QgsGeometry(); area.fromWkb(area_wkb)
    observations = set()
    scanned = 0
    for source, crs, fields, selected in sources:
        if task.isCanceled(): return None
        local = QgsGeometry(area)
        if crs != QgsCoordinateReferenceSystem('EPSG:4326'):
            local.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4326'),crs,context))
        request = QgsFeatureRequest().setFilterRect(local.boundingBox())
        request.setSubsetOfAttributes(list(fields.values()),source.fields())
        if selected is not None: request.setFilterFids(selected)
        for feature in source.getFeatures(request):
            if task.isCanceled(): return None
            scanned += 1
            if scanned > 50000:
                return {'observations':[], 'limited':True}
            geometry = feature.geometry()
            if geometry.isEmpty() or not geometry.isGeosValid() or not geometry.intersects(local): continue
            row = tuple(str(feature[fields[role]]) if role in fields and feature[fields[role]] is not None else ''
                        for role in ('comune','provincia','regione','istat'))
            observations.add(row)
    return {'observations':list(observations), 'limited':False}


def resolve_geography(vocab, observations):
    regions = vocab.index['LCR']
    provinces = vocab.index['LCP']
    communes = vocab.index['LCC']
    region_names = {norm(vocab.label('LCR',key)):key for key in regions}
    province_names = {norm(vocab.label('LCP',key)):key for key in provinces}
    # Index contributes name/code correspondence, never a spatial conclusion.
    data = load_index()
    for item in data['items']:
        code = str(int(str(item['istat'])[:3])) if str(item.get('istat','')).isdigit() else ''
        if code in provinces:
            province_names[norm(data['provinces'][item['province']]['name'])]=code
    by_name = {}
    for key,row in communes.items(): by_name.setdefault(norm(row['nome']),[]).append((key,row))
    values = {'LCR':set(),'LCP':set(),'LCC':set()}
    unknown_commune = False
    for name, province, region, istat in observations:
        name=municipality_names().get(str(name),name)
        p = province_names.get(norm(province)); r = region_names.get(norm(region))
        if p: values['LCP'].add(p); values['LCR'].add(str(provinces[p]['cod_reg']))
        if r: values['LCR'].add(r)
        hits = by_name.get(norm(name),[]) if name and not str(name).isdigit() else []
        if istat and str(istat).isdigit():
            row = communes.get(str(int(istat)))
            hits = [(str(int(istat)),row)] if row else []
        if p: hits = [(key,row) for key,row in hits if str(row['cod_prov'])==p]
        if r: hits = [(key,row) for key,row in hits if str(row['cod_reg'])==r]
        if name or istat:
            if not hits: unknown_commune=True
            for key,row in hits:
                values['LCC'].add(key); values['LCP'].add(str(row['cod_prov'])); values['LCR'].add(str(row['cod_reg']))
    proposals = {key:next(iter(items)) for key,items in values.items() if len(items)==1}
    if unknown_commune: proposals.pop('LCC',None)
    if 'LCP' in proposals and proposals.get('LCR') != str(provinces[proposals['LCP']]['cod_reg']):
        proposals.pop('LCP',None); proposals.pop('LCC',None)
    if 'LCC' in proposals and proposals.get('LCP') != str(communes[proposals['LCC']]['cod_prov']): proposals.pop('LCC',None)
    if proposals.get('LCR')=='9' and proposals.get('LCP'):
        sigla = vocab.label('LCP',proposals['LCP'])
        authority = TUSCAN_AUTHORITIES.get(sigla)
        for key,row in vocab.index['ENTE'].items():
            if authority and vocab.label('ENTE',key)==authority and str(row['cod_reg'])=='9' and row.get('ATT'):
                proposals.update(ENTE=key, EMC=authority); break
    return proposals
