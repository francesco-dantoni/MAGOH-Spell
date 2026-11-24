"""Piccoli helper GUI condivisi: nessuna logica dati."""
from __future__ import annotations


def fit_dialog_to_parent(dialog, parent=None, preferred_width=None, preferred_height=None):
    """Mantiene un dialog entro l'area utile della finestra QGIS.

    Riduce anche minimumSize ereditate da versioni precedenti, evitando che un
    monitor piccolo o una dashboard con scaling alto renda i pulsanti fuori
    schermo. Non impone dimensioni fisse: l'utente puo' sempre ridimensionare.

    V1.6: il minimo segue il layout reale (niente pulsanti tagliati) e, senza
    preferenze esplicite, la dimensione iniziale segue il contenuto.
    """
    try:
        parent = parent or dialog.parentWidget()
        if parent is not None:
            max_w = max(460, int(parent.width()) - 80)
            max_h = max(380, int(parent.height()) - 110)
        else:
            screen = dialog.screen()
            geo = screen.availableGeometry() if screen else None
            max_w = max(460, (geo.width() - 80) if geo else 900)
            max_h = max(380, (geo.height() - 100) if geo else 700)

        # Il minimo non scende sotto quello richiesto dal layout (testi interi),
        # ma resta entro la finestra QGIS: oltre, il corpo scorre.
        layout_min = dialog.minimumSizeHint()
        min_w = min(max_w, max(420, int(layout_min.width())))
        min_h = min(max_h, max(300, int(layout_min.height())))
        dialog.setMinimumSize(min_w, min_h)
        dialog.setMaximumSize(max_w, max_h)

        # Prima apertura: dimensione dal contenuto. Dopo: quella scelta dall'utente.
        if dialog.property("magohSized"):
            want_w = preferred_width or dialog.width()
            want_h = preferred_height or dialog.height()
        else:
            hint = dialog.sizeHint()
            want_w = preferred_width or max(int(hint.width()), min_w)
            want_h = preferred_height or max(int(hint.height()), min_h)
            dialog.setProperty("magohSized", True)
        dialog.resize(min(max_w, max(min_w, int(want_w))),
                      min(max_h, max(min_h, int(want_h))))
    except Exception:
        pass
    return dialog
