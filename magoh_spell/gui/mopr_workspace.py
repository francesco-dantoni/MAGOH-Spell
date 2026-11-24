"""Persistent official MOPR schema and closed, offline vocabularies."""
from pathlib import Path
import json
import os
import uuid
import zipfile

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsGeometry, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsFeature, QgsWkbTypes, QgsEditorWidgetSetup,
    QgsEditFormConfig, QgsAttributeEditorField, QgsProviderRegistry,
)
from ..conversion.gna_project import GnaMoprData
from ..i18n import _tf as t

WORKSPACE_PROP = 'magoh/gna_mopr_workspace'
LABELS = {
    'EMC': 'Ente MiC coinvolto (sigla)', 'CPR': 'Codice progetto',
    'AMA': 'Ambito di attività', 'CTG': 'Categoria', 'OGT': 'Tipo di progetto',
    'OGN': 'Denominazione del progetto', 'DES': 'Descrizione del progetto',
    'LCR': 'Regione', 'LCP': 'Provincia', 'LCC': 'Comune',
    'GEL': 'Tipo di localizzazione', 'GET': 'Tipo di georeferenziazione',
    'GPT': 'Tecnica di georeferenziazione', 'GPM': 'Metodo di posizionamento',
    'GPBB': 'Base cartografica', 'ENTE': 'Ente di riferimento',
    'FUR': 'Funzionario responsabile', 'CMA': 'Anno di redazione',
    'CMC': 'Compilatore — Cognome, Nome', 'CMR': 'Responsabile — Cognome, Nome',
    'ADP': 'Accessibilità dei dati',
}
REQUIRED = tuple(LABELS)
PROFILE = {'AMA': '9', 'CTG': '3', 'OGT': '26', 'GET': '3'}
PARENTS = {'LCP': ('LCR', 'cod_reg'), 'LCC': ('LCP', 'cod_prov'),
           'ENTE': ('LCR', 'cod_reg'), 'FUR': ('ENTE', 'ENTE_key')}


def value(v):
    return '' if v is None or str(v) in ('NULL', 'None') else str(v).strip()


class MoprVocabulary:
    def __init__(self, plugin_dir):
        self.document = json.loads((Path(plugin_dir) / 'conversion/mopr_vocab_official.json').read_text(encoding='utf-8'))
        self.fields = self.document['fields']
        self.index = {f: {str(r[d['config']['key']]): r for r in d['rows']} for f, d in self.fields.items()}

    def pairs(self, field, parent=None):
        d = self.fields[field]
        rows = d['rows']
        if field in PARENTS:
            column = PARENTS[field][1]
            rows = [r for r in rows if value(r.get(column)) == value(parent)]
        return sorted([(str(r[d['config']['key']]), str(r[d['config']['value']])) for r in rows], key=lambda p: p[1].casefold())

    def label(self, field, key):
        r = self.index[field].get(value(key), {})
        return value(r.get(self.fields[field]['config']['value']))

    def validate(self, attrs, draft=False):
        errors = []
        for f in REQUIRED:
            if not draft and not value(attrs.get(f)):
                errors.append(LABELS[f] + ': valore obbligatorio.')
        for f in self.fields:
            key = value(attrs.get(f))
            if draft and not key: continue
            if f == 'FUR': key = key.strip('{}')
            row = self.index[f].get(key)
            if row is None:
                errors.append(LABELS[f] + ': scegliere un valore del vocabolario ufficiale.')
            elif f in PARENTS:
                parent, column = PARENTS[f]
                if value(row.get(column)) != value(attrs.get(parent)):
                    errors.append(LABELS[f] + ': valore non coerente con ' + LABELS[parent] + '.')
        for f, expected in PROFILE.items():
            if value(attrs.get(f)) and value(attrs.get(f)) != expected:
                errors.append('Questo percorso supporta il profilo studio/ricerca (AMA 9, CTG 3, OGT 26), con perimetro areale.')
                break
        return errors


def checked_geometry(geometry, crs):
    g = QgsGeometry(geometry)
    if g.isEmpty() or QgsWkbTypes.geometryType(g.wkbType()) != QgsWkbTypes.PolygonGeometry:
        raise ValueError(t('Occorre un perimetro poligonale non vuoto.', 'A non-empty polygon perimeter is required.'))
    if not g.isGeosValid():
        raise ValueError(t('Il perimetro non è valido: correggere incroci o anelli prima di continuare.', 'The perimeter is not valid: fix self-intersections or rings before continuing.'))
    target = QgsCoordinateReferenceSystem('EPSG:4326')
    if not crs.isValid(): raise ValueError(t('Il layer non ha un sistema di riferimento valido.', 'The layer has no valid coordinate reference system.'))
    if crs != target:
        g.transform(QgsCoordinateTransform(crs, target, QgsProject.instance()))
    g.convertToMultiType()
    return g


def mopr_data(attrs, geometry, crs, vocab):
    errors = vocab.validate(attrs, draft=True)
    if errors: raise ValueError('\n'.join(errors))
    g = checked_geometry(geometry, crs)
    mopr_id=value(attrs.get('MOPR_ID')) or str(uuid.uuid4())
    cpr=value(attrs.get('CPR')) or 'MAGOH_BOZZA_' + mopr_id.replace('-','')
    data = GnaMoprData(
        cpr=cpr, emc=value(attrs['EMC']), ogn=value(attrs['OGN']), des=value(attrs['DES']),
        ente=value(attrs['ENTE']), fur=value(attrs['FUR']).strip('{}'),
        cma=value(attrs['CMA']), cmc=value(attrs['CMC']), cmr=value(attrs['CMR']),
        regione=vocab.label('LCR', attrs['LCR']), provincia=vocab.label('LCP', attrs['LCP']),
        comune=vocab.label('LCC', attrs['LCC']), geometry=json.loads(g.asJson()),
        gpt=value(attrs['GPT']), gpm=value(attrs['GPM']), gpbb=value(attrs['GPBB']),
        gel=value(attrs['GEL']), adp=value(attrs['ADP']),
        lcr=value(attrs['LCR']), lcp=value(attrs['LCP']), lcc=value(attrs['LCC']),
        presa_visione=attrs.get('presa_visione') in (True, 1, '1', 'true'),
        mopr_id=mopr_id,
    )
    errors = data.validate(draft=True)
    if errors: raise ValueError('\n'.join(errors))
    return data


def make_removable(layer):
    """V1.23: the layer can be removed from the legend (flag only, no data change)."""
    try:
        from qgis.core import QgsMapLayer
        removable = getattr(QgsMapLayer, 'Removable', None) or QgsMapLayer.LayerFlag.Removable
        if not (layer.flags() & removable):
            layer.setFlags(layer.flags() | removable)
    except Exception:
        pass


def unlock_workspace_layer(layer):
    """V1.15: the working MOPR loaded by the plugin is never locked.

    Both the working and exported plugin-owned MOPR must remain editable and
    removable in the layer tree. No provider permissions are escalated.
    V1.23: the same applies to the MOSI and DSC layers the plugin adds to the
    MAGOH working project.
    """
    try:
        if layer.readOnly():
            layer.setReadOnly(False)
    except Exception:
        pass
    make_removable(layer)


class MoprWorkspace:
    def __init__(self, plugin_dir):
        self.plugin_dir = Path(plugin_dir)
        self.vocab = MoprVocabulary(plugin_dir)
        self.layer = None

    def open(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'MOPR.gpkg'
        from .mopr_single import from_origin
        existing = from_origin(path)
        if existing is not None:
            self.layer = existing
            self.configure()
            from .layer_organization import normalize_gna
            normalize_gna()
            return existing
        # Never replace a user's existing file; reuse only a compatible schema.
        if not path.exists():
            with zipfile.ZipFile(self.plugin_dir / 'resources/Template_GNA_1.6.0.zip') as z:
                member = next(n for n in z.namelist() if n.endswith('/Template_GNA.gpkg') or n == 'Template_GNA.gpkg')
                with path.open('xb') as out: out.write(z.read(member))
        source = str(path) + '|layername=MOPR'
        for layer in QgsProject.instance().mapLayers().values():
            uri = QgsProviderRegistry.instance().decodeUri('ogr', layer.source()) if layer.providerType() == 'ogr' else {}
            same_path = bool(uri.get('path')) and os.path.normcase(os.path.abspath(uri['path'])) == os.path.normcase(os.path.abspath(path))
            if same_path and uri.get('layerName') == 'MOPR' and layer.customProperty(WORKSPACE_PROP, False):
                self.layer = layer
                self.configure()
                from .layer_organization import gna_group, normalize_gna
                root = QgsProject.instance().layerTreeRoot()
                node = root.findLayer(layer.id())
                from .layer_organization import move
                if node is not None: move(node, gna_group())
                normalize_gna()
                return layer
        layer = QgsVectorLayer(source, 'MOPR — compilazione', 'ogr')
        if not layer.isValid() or not set(REQUIRED).issubset(layer.fields().names()):
            raise ValueError(t('MOPR.gpkg esiste ma non è un MOPR del template: scegliere un’altra cartella. Nessun file è stato sostituito.', 'MOPR.gpkg exists but is not a template MOPR: choose another folder. No file was replaced.'))
        layer.setCustomProperty(WORKSPACE_PROP, True)
        self.layer = layer
        self.configure()
        QgsProject.instance().addMapLayer(layer, False)
        from .layer_organization import gna_group, normalize_gna
        group = gna_group()
        group.addLayer(layer)
        normalize_gna()
        return layer

    def configure(self):
        layer = self.layer
        unlock_workspace_layer(layer)
        from .gna_presentation import apply_workspace_mopr_style
        apply_workspace_mopr_style(layer)
        config = layer.editFormConfig()
        config.setLayout(QgsEditFormConfig.TabLayout)
        root = config.invisibleRootContainer()
        root.clear()
        # V1.14: guide button inside the native QGIS form of the MOPR layer.
        try:
            from .mopr_guide import ensure_layer_action
            from qgis.core import QgsAttributeEditorAction
            root.addChildElement(QgsAttributeEditorAction(ensure_layer_action(layer), root))
        except Exception:
            pass  # never block MOPR configuration for a help link
        for f in REQUIRED:
            idx = layer.fields().indexFromName(f)
            config.setReadOnly(idx, False)
            layer.setFieldAlias(idx, LABELS[f])
            root.addChildElement(QgsAttributeEditorField(f, idx, root))
            if f in self.vocab.fields:
                d = self.vocab.fields[f]
                pairs = [(str(r[d['config']['key']]), str(r[d['config']['value']])) for r in d['rows']]
                if f in PROFILE: pairs = [p for p in pairs if p[0] == PROFILE[f]]
                mapping = [{label: ('{' + key + '}' if f == 'FUR' else key)} for key, label in pairs]
                layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup('ValueMap', {'map': mapping}))
            elif f == 'presa_visione':
                layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup('CheckBox', {'CheckedState': True, 'UncheckedState': False}))
            elif f == 'DES':
                layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup('TextEdit', {'IsMultiline': True}))
        layer.setEditFormConfig(config)

    def save(self, attrs, geometry, fid=None):
        layer = self.layer
        if layer is None or not layer.isValid(): raise ValueError(t('Aprire prima il MOPR di lavoro.', 'Open the working MOPR first.'))
        if layer.isEditable(): raise ValueError(t('Salvare o annullare prima le modifiche QGIS del layer MOPR.', 'Save or discard the QGIS edits of the MOPR layer first.'))
        data = mopr_data(attrs, geometry, QgsCoordinateReferenceSystem('EPSG:4326'), self.vocab)
        for field in layer.fields():
            text = value(attrs.get(field.name()))
            if field.length() > 0 and len(text) > field.length():
                raise ValueError(t('%s: superata la lunghezza massima di %d caratteri.', '%s: maximum length of %d characters exceeded.') % (field.name(), field.length()))
        for feature in layer.getFeatures():
            if data.cpr and feature.id() != fid and value(feature['CPR']) == data.cpr:
                raise ValueError(t('Questo CPR esiste già nel MOPR: usare un progressivo distinto.', 'This CPR already exists in the MOPR: use a different progressive number.'))
        feature = layer.getFeature(fid) if fid is not None else QgsFeature(layer.fields())
        if fid is not None and not feature.isValid(): raise ValueError(t('La scheda selezionata non esiste più.', 'The selected record no longer exists.'))
        attrs = dict(attrs, MOPR_ID=data.mopr_id, AMB='archeologico', LCS='Italia', GEP='WGS84',
                     MISZ='area', MISU='mq', RCGJ='GNA', upload_ok=False)
        attrs['CPR']=data.cpr
        attrs['FUR'] = '{' + value(attrs.get('FUR')).strip('{}') + '}' if value(attrs.get('FUR')) else None
        if fid is None: attrs.update(DCMN=str(uuid.uuid4()), RCGH=str(uuid.uuid4()))
        for field, val in attrs.items():
            idx = layer.fields().indexFromName(field)
            if idx >= 0:
                if value(val)=='': val=None
                elif field in ('LCR','LCP','LCC','ENTE'): val=int(val)
                feature.setAttribute(idx, val)
        feature.setGeometry(geometry)
        if not layer.startEditing(): raise ValueError(t('MOPR non scrivibile.', 'MOPR is not writable.'))
        try:
            ok = layer.updateFeature(feature) if fid is not None else layer.addFeature(feature)
            if not ok or not layer.commitChanges():
                raise ValueError(t('Salvataggio MOPR fallito: ', 'Saving the MOPR failed: ') + '; '.join(layer.commitErrors()))
        except Exception:
            layer.rollBack()
            raise
        # The technical UUID remains unique even when a draft CPR is blank.
        saved = next(f for f in layer.getFeatures() if value(f['MOPR_ID']) == data.mopr_id)
        layer.selectByIds([saved.id()])
        layer.triggerRepaint()
        return saved.id()

    def read(self, fid):
        if self.layer is None or not self.layer.isValid(): raise ValueError(t('Il layer MOPR non è disponibile.', 'The MOPR layer is not available.'))
        if self.layer.isEditable(): raise ValueError(t('Salvare o annullare prima le modifiche QGIS del MOPR.', 'Save or discard the QGIS edits of the MOPR first.'))
        feature = self.layer.getFeature(fid)
        if not feature.isValid(): raise ValueError(t('Selezionare una scheda MOPR salvata.', 'Select a saved MOPR record.'))
        attrs = {f.name(): feature[f.name()] for f in self.layer.fields()}
        return mopr_data(attrs, feature.geometry(), self.layer.crs(), self.vocab)
