"""Bounded text index for GNA. Never reads geometries or binary attachments."""
from collections import OrderedDict
from pathlib import Path
from qgis.PyQt import sip
from qgis.core import QgsFeatureRequest, QgsProviderRegistry, QgsFeature, QgsFields

PREVIEW = {'codice_locale','idscheda','fid','id','ID','id_viarch','ACCC','DSCH','DSCY','DSCV','DSVC','OGN','OGD','OGT','DTR'}


def revision(layer):
    if layer is None or sip.isdeleted(layer):
        return None
    if layer.isEditable() or layer.providerType() != 'ogr':
        return None
    uri = QgsProviderRegistry.instance().decodeUri('ogr', layer.source())
    path = uri.get('path')
    if not path:
        return None
    try:
        stats = []
        for suffix in ('', '-wal'):
            p = Path(str(path)+suffix)
            stats.append((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None)
        return layer.source(), tuple(stats), layer.subsetString(), tuple((f.name(), f.typeName()) for f in layer.fields())
    except OSError:
        return None


class TextIndex:
    MAX_BYTES = 12 * 1024 * 1024
    def __init__(self):
        self.entries = OrderedDict()
        self.bytes = 0

    def find(self, layer, text, field='', cache_id=None, label=None):
        """label(index, value): etichetta normalizzata dei campi a elenco (V1.23.1), oppure None."""
        from .conversion.entity_summary import normalize
        needle = str(text).strip().casefold()
        if not needle or layer is None or sip.isdeleted(layer):
            return []
        norm_needle = normalize(text)
        signature = revision(layer)
        key = cache_id or layer.id(), str(field or ''), label is not None
        cached = self.entries.pop(key, None)
        if cached:
            self.bytes -= cached[2]
        if cached and signature is not None and cached[0] == signature:
            rows, size = cached[1:]
        else:
            indices = [i for i, f in enumerate(layer.fields())
                       if (not field or f.name() == field) and
                       not any(s in f.typeName().lower() for s in ('blob','binary','bytearray','bytea')) and
                       f.name().lower() not in ('geom','geometry','geojson')]
            preview_indices = [i for i, f in enumerate(layer.fields()) if f.name() in PREVIEW]
            preview_fields = QgsFields()
            for i in preview_indices:
                preview_fields.append(layer.fields()[i])
            request = QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry).setSubsetOfAttributes(sorted(set(indices+preview_indices)))
            rows, size, streamed = [], 0, []
            iterator = layer.getFeatures(request)
            try:
                for feature in iterator:
                    values = []
                    for index in indices:
                        value = feature[index]
                        if value is not None and not (hasattr(value, 'isNull') and value.isNull()):
                            values.append(str(value).strip().casefold())
                            shown = label(index, value) if label is not None else ''
                            if shown:
                                values.append(shown)
                    joined = '\x00'.join(values)
                    preview = QgsFeature(preview_fields,feature.id())
                    preview.setAttributes([feature[i] for i in preview_indices])
                    if needle in joined or (norm_needle and norm_needle in joined):
                        streamed.append(preview)
                    size += len(joined)*4 + 1024
                    if size <= self.MAX_BYTES:
                        rows.append((feature.id(), joined, preview))
                    else:
                        rows.clear()  # Continue streaming; never build an unbounded index.
            finally:
                iterator.close()
        if signature is not None and size <= self.MAX_BYTES:
            while self.entries and (self.bytes + size > self.MAX_BYTES or len(self.entries) >= 6):
                _, old = self.entries.popitem(last=False)
                self.bytes -= old[2]
            self.entries[key] = (signature, rows, size)
            self.bytes += size
        if size > self.MAX_BYTES:
            return streamed
        return [preview for _, content, preview in rows if needle in content or (norm_needle and norm_needle in content)]
