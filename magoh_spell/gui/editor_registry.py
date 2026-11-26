"""Registrazione degli editor di campo MAGOH, sicura dopo un aggiornamento.

QGIS conserva per tutta la sessione le factory degli editor di campo e non
permette di sostituirne una con lo stesso ID. Dopo «Installa da ZIP» senza
riavvio, la factory registrata prima dell'aggiornamento continuerebbe a creare
i propri widget. Qui la factory superstite viene «adottata»: il suo
modulo d'origine riceve le classi Wrapper correnti. I formattatori, che QGIS
permette di rimuovere, vengono invece sostituiti.
"""
from qgis.core import QgsApplication
from qgis.gui import QgsGui


def register_editor(widget_id, factory_cls, wrappers, owners):
    """Registra la factory oppure aggiorna quella registrata prima di un aggiornamento.

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
    """Aggiunge il formattatore, sostituendo quello registrato prima di un aggiornamento."""
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


def field_value(feature, index):
    """Value of field ``index`` of ``feature``, or None when the feature lacks it.

    A form can pass an empty or not yet initialised feature: reading a missing
    index raises KeyError in PyQGIS.
    """
    try:
        if feature is None or index < 0 or index >= len(feature.attributes()):
            return None
        return feature.attribute(index)
    except (KeyError, IndexError, TypeError):
        return None


try:
    from qgis.gui import QgsEditorConfigWidget
    from qgis.PyQt.QtWidgets import QLabel, QVBoxLayout

    class PreservingConfigWidget(QgsEditorConfigWidget):
        """Configuration panel of the MAGOH editors in Layer Properties.

        The editors take no options from the user, but some keep a stored
        configuration (for example the chronology mode): it is returned
        unchanged, so pressing OK never clears it.
        """

        def __init__(self, layer, index, parent):
            super().__init__(layer, index, parent)
            self._config = {}
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel('Editor gestito da MAGOH Spell: nessuna opzione da impostare.\n'
                                    'Editor managed by MAGOH Spell: no options to set.'))
            layout.addStretch(1)

        def config(self):
            return dict(self._config)

        def setConfig(self, config):
            self._config = dict(config or {})

except ImportError:  # pragma: no cover - only outside QGIS
    PreservingConfigWidget = None


def config_widget(layer, index, parent):
    return PreservingConfigWidget(layer, index, parent)
