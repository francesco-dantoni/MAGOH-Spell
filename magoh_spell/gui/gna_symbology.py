# -*- coding: utf-8 -*-
"""Vestizione dei layer GNA generati dalla conversione.

Le regole sono quelle chieste per la lettura a colpo d'occhio del risultato:

* **DSC** categorizzata su ``DSCEX``: l'esito dell'indagine è l'informazione
  che conta di più in una carta di verifica preventiva, e distinguerlo per
  colore evita di dover aprire la tabella. Poligono retinato (tratteggio
  diagonale) con bordo **verde** per il *positivo*, **rosso** per il *negativo*.
  Il retino, e non il pieno, perché la DSC è quasi sempre sovrapposta ai MOSI:
  un riempimento opaco li nasconderebbe.
* **MOSI poligonali** in **blu**, **MOSI puntuali** in **rosso**.
* MOSI lineari in blu come gli areali: stessa entità, geometria diversa.

Tutto passa da ``applica_simbologia``, che non solleva mai: se una versione di
QGIS non offre una classe la vestizione salta e l'esportazione prosegue.
"""

from __future__ import annotations

VERDE = "27,120,55"
ROSSO = "202,0,32"
BLU = "33,102,172"

#: (valore DSCEX, colore, etichetta)
CATEGORIE_DSCEX = [
    ("positivo", VERDE, "positivo"),
    ("negativo", ROSSO, "negativo"),
]

_RETINO = "b_diagonal"


def _fill(colore, stile=_RETINO, bordo=None, spessore="0.66"):
    from qgis.core import QgsFillSymbol
    return QgsFillSymbol.createSimple({
        "style": stile,
        "color": colore,
        "outline_color": bordo or colore,
        "outline_width": spessore,
        "outline_style": "solid",
        "joinstyle": "round",
    })


def _renderer_dscex(layer):
    """Renderer categorizzato su DSCEX, con una categoria di riserva."""
    from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory

    campo = _nome_campo(layer, "DSCEX")
    if not campo:
        return None

    categorie = []
    for valore, colore, etichetta in CATEGORIE_DSCEX:
        categorie.append(QgsRendererCategory(valore, _fill(colore), etichetta))

    # DSCEX vuoto o non determinato: grigio, così si vede che manca il dato
    # invece di sparire nella categoria sbagliata.
    riserva = QgsRendererCategory("", _fill("120,120,120", stile="dense6"),
                                  "esito non determinato")
    categorie.append(riserva)

    # in the package GeoPackage DSCEX holds the official KEY
    # (1 = negativo, 2 = positivo); the staging GeoJSON holds the label.
    # Classify on an expression that understands both, so a positive DSC is
    # never drawn as «esito non determinato».
    q = '"' + campo.replace('"', '""') + '"'
    espressione = ("CASE WHEN to_string(%s) IN ('2','positivo') THEN 'positivo' "
                   "WHEN to_string(%s) IN ('1','negativo') THEN 'negativo' ELSE '' END") % (q, q)
    renderer = QgsCategorizedSymbolRenderer(espressione, categorie)
    return renderer


def _nome_campo(layer, atteso):
    """Il nome reale del campo, qualunque sia la cassa usata dal driver OGR."""
    try:
        for f in layer.fields():
            if f.name().lower() == atteso.lower():
                return f.name()
    except Exception:
        pass
    return None


def _renderer_semplice(simbolo):
    from qgis.core import QgsSingleSymbolRenderer
    return QgsSingleSymbolRenderer(simbolo)


def _marker(colore, dimensione="2.6"):
    from qgis.core import QgsMarkerSymbol
    return QgsMarkerSymbol.createSimple({
        "name": "circle",
        "color": colore,
        "outline_color": "255,255,255",
        "outline_width": "0.3",
        "size": dimensione,
    })


def _line(colore, spessore="0.8"):
    from qgis.core import QgsLineSymbol
    return QgsLineSymbol.createSimple({"color": colore, "width": spessore,
                                       "capstyle": "round", "joinstyle": "round"})


def applica_simbologia(layer, ruolo):
    """Veste un layer secondo il suo ruolo: ``dsc``, ``mosi_poligono``,
    ``mosi_linea`` o ``mosi_punto``. Restituisce True se ha vestito qualcosa.
    """
    if layer is None:
        return False
    try:
        renderer = None
        if ruolo == "dsc":
            renderer = _renderer_dscex(layer)
            if renderer is None:
                # senza il campo DSCEX resta una vestizione neutra, mai nulla
                renderer = _renderer_semplice(_fill("120,120,120", stile="dense6"))
            layer.setOpacity(0.85)
        elif ruolo == "mosi_poligono":
            renderer = _renderer_semplice(_fill(BLU, stile="solid",
                                                bordo="18,60,110", spessore="0.4"))
            layer.setOpacity(0.55)
        elif ruolo == "mosi_linea":
            renderer = _renderer_semplice(_line(BLU))
        elif ruolo == "mosi_punto":
            renderer = _renderer_semplice(_marker(ROSSO))
        if renderer is None:
            return False
        layer.setRenderer(renderer)
        layer.triggerRepaint()
        return True
    except Exception:
        # la vestizione è un di più: non deve mai far fallire l'esportazione
        return False


# ---------------------------------------------------------------- scheda Intervento MAGOH

def _voci_intervento(dir_plugin):
    """La vestizione della scheda Intervento, dal JSON ricavato dal .qml."""
    import json
    from pathlib import Path as _P
    try:
        percorso = _P(dir_plugin) / "conversion" / "simbologia_intervento.json"
        return json.loads(percorso.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _campo_presente(layer, nomi):
    """Il primo dei nomi che esiste davvero nel layer, ignorando la cassa."""
    try:
        presenti = {f.name().lower(): f.name() for f in layer.fields()}
    except Exception:
        return None
    for n in nomi:
        if n and str(n).lower() in presenti:
            return presenti[str(n).lower()]
    return None


def _fill_da_voce(v):
    from qgis.core import QgsFillSymbol
    return QgsFillSymbol.createSimple({
        "style": v.get("style") or "solid",
        "color": v.get("color") or "150,150,150,64",
        "outline_color": v.get("outline_color") or "90,90,90,255",
        "outline_width": str(v.get("outline_width") or "0.26"),
        "outline_style": "solid",
        "joinstyle": "bevel",
    })


def simbologia_intervento(layer, dir_plugin, log=None):
    """Veste la scheda Intervento MAGOH categorizzando sulla precisione.

    Restituisce (applicata, messaggi). I valori che il .qml non prevede — il
    vuoto compreso — finiscono in una categoria di ripiego **visibile**: una
    feature senza simbolo non si distingue da una feature che non c'è, e in una
    carta archeologica è una differenza che conta. Ogni valore finito nel
    ripiego viene elencato nei messaggi.
    """
    messaggi = [] if log is None else log
    cfg = _voci_intervento(dir_plugin)
    voci = cfg.get("categorie") or []
    if layer is None or not voci:
        return False, messaggi
    nomi = [cfg.get("campo")] + list(cfg.get("campi_alternativi") or [])
    campo = _campo_presente(layer, nomi)
    if not campo:
        messaggi.append(
            "Vestizione intervento non applicata: il layer «%s» non ha nessuno dei campi "
            "previsti (%s)." % (layer.name(), ", ".join(x for x in nomi if x)))
        return False, messaggi

    try:
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory
    except Exception:
        return False, messaggi

    attesi = {str(v.get("valore")).strip().casefold() for v in voci}
    fuori = {}
    try:
        for f in layer.getFeatures():
            v = f[campo]
            testo = "" if v is None else str(v).strip()
            if testo.casefold() not in attesi:
                fuori[testo] = fuori.get(testo, 0) + 1
    except Exception:
        pass

    categorie = [QgsRendererCategory(v.get("valore"), _fill_da_voce(v),
                                     v.get("etichetta") or v.get("valore"))
                 for v in voci]
    rip = cfg.get("ripiego") or {}
    categorie.append(QgsRendererCategory("", _fill_da_voce(rip),
                                         rip.get("etichetta") or "altro / non indicato"))
    try:
        expression = f'lower(trim("{campo}"))'
        for category in categorie:
            category.setValue(str(category.value() or '').strip().casefold())
        layer.setRenderer(QgsCategorizedSymbolRenderer(expression, categorie))
        layer.triggerRepaint()
    except Exception as exc:
        messaggi.append("Vestizione intervento non applicata: %s" % exc)
        return False, messaggi

    messaggi.append("Vestizione intervento applicata su «%s», campo %s." % (layer.name(), campo))
    if fuori:
        elenco = ", ".join("%s (%d)" % (k or "vuoto", n)
                           for k, n in sorted(fuori.items(), key=lambda x: -x[1])[:8])
        messaggi.append(
            "Valori non previsti dal .qml, mostrati con la categoria di ripiego: " + elenco)
    return True, messaggi


def ha_stile_proprio(layer):
    """Vero se il layer ha già una vestizione che non è quella predefinita.

    Serve a non sovrascrivere in silenzio uno stile che l'utente si è
    costruito: prima di sostituirlo il chiamante chiede conferma.
    """
    try:
        r = layer.renderer()
        if r is None:
            return False
        if bool(layer.customProperty("magoh/simbologia_intervento", False)):
            return False
        return r.type() != "singleSymbol"
    except Exception:
        return False
