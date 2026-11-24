"""Registrazione degli editor di campo MAGOH, sicura dopo un aggiornamento.

QGIS conserva per tutta la sessione le factory degli editor di campo e non
permette di sostituirne una con lo stesso ID. Dopo «Installa da ZIP» senza
riavvio, la factory della versione precedente continuerebbe a creare i widget
della versione precedente. Qui la factory superstite viene «adottata»: il suo
modulo d'origine riceve le classi Wrapper correnti. I formattatori, che QGIS
permette di rimuovere, vengono invece sostituiti.
"""
from qgis.core import QgsApplication
from qgis.gui import QgsGui


def register_editor(widget_id, factory_cls, wrappers, owners):
    """Registra la factory oppure aggiorna quella lasciata da una versione precedente.

    ``wrappers`` è un dizionario {nome globale: classe} che la factory usa in
    ``create``. ``owners`` è la lista del modulo chiamante che tiene vivi gli
    oggetti Python registrati.
    """
    registry = QgsGui.editorWidgetRegistry()
    if widget_id not in registry.factories():
        factory = factory_cls()
        owners.append(factory)
        registry.registerWidget(widget_id, factory)
        return True
    existing = registry.factory(widget_id)
    if existing is None or isinstance(existing, factory_cls):
        return False
    namespace = getattr(getattr(type(existing), 'create', None), '__globals__', None)
    current = getattr(factory_cls.create, '__globals__', None)
    if not namespace or namespace is current or namespace.get('ID') != widget_id:
        return False
    namespace.update(wrappers)
    owners.append(existing)
    return True


def register_formatter(formatter_id, formatter_cls, owners):
    """Aggiunge il formattatore, sostituendo quello di una versione precedente."""
    formatters = QgsApplication.fieldFormatterRegistry()
    existing = formatters.fieldFormatter(formatter_id)
    if existing is not None and existing.id() == formatter_id:
        if isinstance(existing, formatter_cls):
            return False
        try:
            formatters.removeFieldFormatter(formatter_id)
        except TypeError:
            formatters.removeFieldFormatter(existing)
    formatter = formatter_cls()
    owners.append(formatter)
    formatters.addFieldFormatter(formatter)
    return True
