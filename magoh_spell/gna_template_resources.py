"""Trusted GNA 1.6 presentation and full official offline vocabulary rows.

This module has no QGIS dependency. Original layer IDs remain untouched in
the exported official project. Only datasource paths are made portable.
"""
from pathlib import Path
from io import BytesIO
from functools import lru_cache
import gzip
import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
TEMPLATE_SHA256 = 'bf01cf5b4028ecdd8b1c82aeb784998463d43b776e6d105004fcd2e92885f06d'


def verified_template_bytes():
    raw = (ROOT / 'resources/Template_GNA_1.6.0.zip').read_bytes()
    # The bundled XML contains audited form code. Never execute form code
    # from a substituted archive merely because its file has the same name.
    if hashlib.sha256(raw).hexdigest() != TEMPLATE_SHA256:
        raise ValueError('Template GNA incorporato non riconosciuto / Unrecognized bundled GNA template')
    return raw


@lru_cache(maxsize=1)
def official_project_xml():
    raw = verified_template_bytes()
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        with zipfile.ZipFile(BytesIO(archive.read('Template_GNA_1.6.qgz'))) as qgz:
            return qgz.read(next(n for n in qgz.namelist() if n.endswith('.qgs')))


@lru_cache(maxsize=1)
def vocabulary_catalog():
    with gzip.open(ROOT / 'data/gna_vocabularies_1_6.json.gz', 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def materialize_vocabularies(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    sources = {}
    for name, spec in vocabulary_catalog()['vocabularies'].items():
        path = directory / (name + '.json')
        data = json.dumps(spec['data'], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if not path.exists():
            with path.open('xb') as stream:
                stream.write(data)
        elif path.read_bytes() != data:
            raise ValueError('Vocabolario esistente diverso: nessun file sovrascritto / Existing vocabulary differs: ' + name)
        sources['/vsicurl/' + spec['source']] = path
    return sources


def normalize_dictionary_references(doc, sources):
    """Repair stale template widget IDs by the declared vocabulary name.

    Template 1.6 VSU.CTQ still contains an obsolete PostGIS fallback. Bind it
    to the actual official CTQ dictionary, never to that old database host.
    """
    by_name = {l.findtext('layername'): l for l in doc.findall('./projectlayers/maplayer')
               if l.findtext('datasource') in sources}
    for cfg in doc.findall('.//editWidget/config/Option'):
        options = {o.get('name'): o for o in cfg.findall('Option')}
        name = options.get('LayerName')
        target = by_name.get(name.get('value')) if name is not None else None
        if target is not None and 'Layer' in options:
            options['Layer'].set('value', target.findtext('id'))
            if 'LayerSource' in options:
                options['LayerSource'].set('value', str(sources[target.findtext('datasource')]))
            if 'LayerProviderName' in options:
                options['LayerProviderName'].set('value', 'ogr')
        expression = options.get('FilterExpression')
        if expression is not None and expression.get('value'):
            text = expression.get('value')
            # OGR's GeoJSON SQL rejects boolean/string and integer/string
            # comparisons used by the original remote-table definitions.
            text = re.sub(r'''(?i)"ATT"\s*=\s*'true'\s*''', '"ATT" = TRUE ', text).strip()
            match = re.fullmatch(r'''\s*("(?:cod_reg|cod_prov|CTG_key|ENTE_key|OGD_KEY)")\s*=\s*(current_value\s*\([^)]*\))\s*''', text, re.I)
            if match:
                text = 'to_string(%s) = to_string(%s)' % match.groups()
            expression.set('value', text)


def portable_form_code(code):
    for old, new in {'Qt.AlignCenter':'Qt.AlignmentFlag.AlignCenter',
                     'Qt.ArrowCursor':'Qt.CursorShape.ArrowCursor',
                     'Qt.KeepAspectRatio':'Qt.AspectRatioMode.KeepAspectRatio',
                     'Qt.SmoothTransformation':'Qt.TransformationMode.SmoothTransformation'}.items():
        code = code.replace(old, new)
    return code


def native_photo_forms(doc):
    """Replace only the trusted template's formOpen photo preview, not security settings.

    Qt Quick displays an in-memory image; Binary remains the native upload/save
    editor. No project Python, external file/URL or plugin dependency is needed.
    """
    layers = [doc] if doc.tag == 'maplayer' else doc.findall('./projectlayers/maplayer')
    for layer in layers:
        function = layer.find('editforminit')
        if function is None or function.text != 'formOpen':
            continue
        function.text = ''
        for tag, value in (('editforminitcode', ''), ('editforminitcodesource', '0'), ('editforminitfilepath', '')):
            node = layer.find(tag)
            if node is not None:
                node.text = value
        for container in layer.findall('.//attributeEditorContainer'):
            if any(c.tag == 'attributeEditorField' and c.get('name') == 'foto' for c in container):
                if any(c.get('name') == 'MAGOHPhotoPreview' for c in container):
                    continue
                qml = ET.SubElement(container, 'attributeEditorQmlElement', name='MAGOHPhotoPreview', showLabel='0', horizontalStretch='0', verticalStretch='0')
                qml.text = '''import QtQuick 2.0
Item {
 width: 400; height: 260
 Image {
  id: photo; anchors.fill: parent; fillMode: Image.PreserveAspectFit
  source: { var b = expression.evaluate('to_base64("foto")'); return b ? 'data:image/png;base64,' + b : ''; }
 }
 Text {
  anchors.centerIn: parent; visible: photo.status !== Image.Ready
  text: 'Anteprima immagine / Image preview'; color: '#666666'
 }
}'''


def prepare_offline_project(qgz_path):
    """Keep official forms/IDs, point their dictionaries at packaged snapshots."""
    qgz_path = Path(qgz_path)
    sources = materialize_vocabularies(qgz_path.parent / 'vocabolari_MAGOH')
    temporary = qgz_path.with_suffix('.offline.tmp')
    try:
        with zipfile.ZipFile(qgz_path) as source, zipfile.ZipFile(temporary, 'x', zipfile.ZIP_DEFLATED) as dest:
            for info in source.infolist():
                data = source.read(info)
                if info.filename.endswith('.qgs'):
                    doc = ET.fromstring(data)
                    relative_sources = {s: './vocabolari_MAGOH/' + p.name for s, p in sources.items()}
                    normalize_dictionary_references(doc, relative_sources)
                    native_photo_forms(doc)
                    for layer in doc.findall('./projectlayers/maplayer'):
                        if layer.findtext('datasource', '').endswith('|layername=MOPR'):
                            layer.set('readOnly', '0')
                            removable = layer.find('./flags/Removable')
                            if removable is not None:
                                removable.text = '1'
                    for elem in doc.iter():
                        if elem.tag == 'editforminitcode' and elem.text:
                            elem.text = portable_form_code(elem.text)
                        if elem.text in sources:
                            elem.text = './vocabolari_MAGOH/' + sources[elem.text].name
                        for key, value in list(elem.attrib.items()):
                            if value in sources:
                                elem.set(key, './vocabolari_MAGOH/' + sources[value].name)
                    data = ET.tostring(doc, encoding='utf-8', xml_declaration=True)
                dest.writestr(info, data)
        temporary.replace(qgz_path)
    finally:
        if temporary.exists():
            temporary.unlink()
