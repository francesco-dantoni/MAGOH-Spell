def classFactory(iface):
    """MAGOH Spell: punto di ingresso richiesto da QGIS."""
    from .magoh_spell import MagohSpellPlugin
    return MagohSpellPlugin(iface)
