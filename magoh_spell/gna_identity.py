"""Recognize GNA data by physical schema, not translated display labels.

Vocabulary tables (DSCEX, DSCM...), WMS services and relation auxiliaries
are deliberately not offered as DSC/MOSI records in the manager.
"""
import re
from pathlib import Path
from qgis.core import QgsVectorLayer, QgsProviderRegistry, QgsDataSourceUri


def source_table(layer):
    if layer.providerType() == 'ogr':
        uri = QgsProviderRegistry.instance().decodeUri('ogr', layer.source())
        name = str(uri.get('layerName') or '')
        if not name and uri.get('path'):
            # A file that holds a single table (GeoJSON, Shapefile): its name is the table.
            name = Path(str(uri['path'])).stem
        return name
    if layer.providerType() == 'postgres':
        return QgsDataSourceUri(layer.source()).table()
    return ''


#: Schema of the national GNA database that the official GNA plugin reads
#: while downloading («edit»). Its layers hold every record in Italy: they are
#: a source for the download, never the user's DSC/MOSI.
NATIONAL_SCHEMAS = ('edit',)


def national_service(layer):
    return (isinstance(layer, QgsVectorLayer) and layer.providerType() == 'postgres'
            and QgsDataSourceUri(layer.source()).schema() in NATIONAL_SCHEMAS)


def gna_role(layer):
    """'dsc' / 'mosi' only for Template GNA tables (GeoPackage, shared postgres template).

    V1.23.2: service layers (WFS, memory) and the national GNA database added
    by the official plugin are no longer taken for the user's records.
    """
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid() or not layer.isSpatial():
        return ''
    if national_service(layer):
        return ''
    names = {f.name().upper() for f in layer.fields()}
    table = source_table(layer).rsplit('.', 1)[-1].strip('"').upper()
    # Use schema confirmation even when a custom property is present: a
    # stale/copied property must not turn an unrelated user layer into GNA.
    if {'DSCH', 'DSCEX', 'CPR_MOPR'}.issubset(names) and re.fullmatch(r'DSC_(MULTIPOLYGON|MULTIPOINT|MULTILINEA)', table):
        return 'dsc'
    if {'ACCC', 'OGD', 'CPR_MOPR', 'DOZ'}.issubset(names) and re.fullmatch(r'MOSI_(MULTIPOLYGON|MULTIPOINT|MULTILINEA|MULTILINE)', table):
        return 'mosi'
    return ''


def is_mopr(layer):
    """Internal propagation guard only; MOPR is not a searchable record type."""
    return (isinstance(layer, QgsVectorLayer) and layer.isValid() and
            source_table(layer).upper() == 'MOPR' and
            {'CPR', 'MOPR_ID'}.issubset({f.name().upper() for f in layer.fields()}))
