"""QGIS integration for identifying new local archaeological records."""
from qgis.core import (Qgis, QgsAttributeEditorContainer, QgsAttributeEditorElement,
                       QgsAttributeEditorField, QgsEditorWidgetSetup, QgsExpression,
                       QgsExpressionFunction, QgsReadWriteContext)
from qgis.PyQt.QtXml import QDomDocument
from . import record_id_editor
from ..api_spatial import apply_intervention_labels
from ..local_codes import TABLES, reserve_technical_ids, shown_id

class TechnicalId(QgsExpressionFunction):
    def __init__(self):
        super().__init__('magoh_next_technical_id', 1, 'MAGOH')
        self.sources = {}

    def func(self, values, context, parent, node):
        source = self.sources.get(str(values[0]))
        if not source:
            parent.setEvalErrorString('Riaprire il progetto con MAGOH Spell attivo.')
            return None
        path, table, pool = source
        value = next(pool, None)
        if value is None:
            try:
                pool = reserve_technical_ids(path, table)
            except Exception as exc:
                parent.setEvalErrorString(str(exc))
                return None
            self.sources[str(values[0])] = path, table, pool
            value = next(pool)
        return value

    def isStatic(self, node, parent, context):
        return False

_function = None

def unregister():
    global _function
    if _function is not None:
        QgsExpression.unregisterFunction('magoh_next_technical_id')
        _function = None


def technical_default(layer, path, table):
    global _function
    if _function is None:
        _function = TechnicalId()
        if not QgsExpression.registerFunction(_function):
            raise RuntimeError('Riavviare QGIS per registrare il generatore degli ID MAGOH.')
    key = layer.id()
    source = _function.sources.get(key)
    if not source or source[:2] != (path,table):
        _function.sources[key] = path, table, reserve_technical_ids(path, table)
    return 'magoh_next_technical_id(@layer_id)'


#: Template aliases of the technical keys: the only identifier label in the forms.
LABELS = {
    'magoh_scheda_intervento': 'ID Scheda di Intervento',
    'magoh_ritrovamenti': 'ID Scheda di Ritrovamento',
    'magoh_evidenza_archeologica': 'ID Scheda di Evidenza archeologica (IV Livello)',
    'magoh_evidenza_archeologica_no_geom': 'ID Scheda di Evidenza archeologica (IV Livello)',
}
#: Only the Scheda di Intervento is labelled on the map.
LABELLED = 'magoh_scheda_intervento'


def _kind(element):
    if isinstance(element, QgsAttributeEditorContainer):
        return 'container'
    if isinstance(element, QgsAttributeEditorField):
        return 'field'
    # A text element read back from a project may arrive as the base class.
    return 'text' if element.type() == Qgis.AttributeEditorType.TextElement else ''


def _template_field(layer, key, parent):
    """The key field as the Template style draws it (label font and colour)."""
    count, ids = layer.listStylesInDatabase()[:2]
    for style_id in ids[:count]:
        document = QDomDocument()
        document.setContent(layer.getStyleFromDatabase(style_id)[0])
        nodes = document.elementsByTagName('attributeEditorField')
        for i in range(nodes.count()):
            element = nodes.at(i).toElement()
            if element.attribute('name') == key:
                return QgsAttributeEditorElement.create(element, layer.id(), layer.fields(),
                                                        QgsReadWriteContext(), parent)
    return QgsAttributeEditorField(key, layer.fields().indexOf(key), parent)


def _restore_layout(layer, container, key, label):
    """Keep the Template layout: drop codice_locale and put the key field back
    where the first 1.0 build had replaced it with a text element."""
    rebuilt, changed = [], False
    for element in list(container.children()):
        kind = _kind(element)
        if kind == 'field' and element.name() == 'codice_locale':
            changed = True
            continue
        if kind == 'text' and element.name() == label:
            rebuilt.append(_template_field(layer, key, container))
            changed = True
            continue
        if kind == 'container':
            _restore_layout(layer, element, key, label)
        rebuilt.append(element.clone(container))
    if changed:
        container.clear()
        for element in rebuilt:
            container.addChildElement(element)


def _label_sources(labeling):
    if labeling is None:
        return set()
    if labeling.type() != 'rule-based':
        return {labeling.settings().fieldName}
    sources, rules = set(), [labeling.rootRule()]
    while rules:
        rule = rules.pop()
        if rule.settings():
            sources.add(rule.settings().fieldName)
        rules.extend(rule.children())
    return sources


def _drop_generated_labels(layer, key):
    """Remove only labels written by MAGOH Spell; a user's own labels stay."""
    generated = {f'coalesce("codice_locale",to_string("{key}"))', shown_id(key, 'nuova')}
    sources = _label_sources(layer.labeling())
    if sources and sources <= generated:
        layer.setLabelsEnabled(False)
        layer.setLabeling(None)


def install_form(layer, table):
    idx = layer.fields().indexOf('codice_locale')
    if idx < 0:
        return
    key = TABLES[table]
    technical = layer.fields().indexOf(key)
    layer.setFieldAlias(idx, 'Codice scheda')
    layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup('TextEdit', {}))
    if technical >= 0:
        record_id_editor.register()
        layer.setFieldAlias(technical, LABELS[table])
        layer.setEditorWidgetSetup(technical, QgsEditorWidgetSetup(record_id_editor.ID, {}))
    cfg = layer.editFormConfig()
    cfg.setReadOnly(idx, True)
    if technical >= 0:
        cfg.setReadOnly(technical, True)
        _restore_layout(layer, cfg.invisibleRootContainer(), key, LABELS[table])
    layer.setEditFormConfig(cfg)
    if table == LABELLED:
        apply_intervention_labels(layer)
    else:
        _drop_generated_labels(layer, key)
    layer.setDisplayExpression(shown_id(key))
