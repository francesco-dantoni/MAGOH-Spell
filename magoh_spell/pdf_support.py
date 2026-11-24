"""PDF resource lifetime shared by Qt5 and Qt6, including exceptional exits."""
from contextlib import contextmanager

from qgis.PyQt import sip
from qgis.PyQt.QtGui import QPageSize, QPainter, QPdfWriter


@contextmanager
def pdf_canvas(path, dpi=150):
    """Close the writer even when an exception traceback retains its frame.

    Explicit C++ destruction releases Windows file locks before the caller
    removes its temporary file. Neither yielded object may be used afterwards.
    """
    writer = QPdfWriter(path)
    painter = None
    try:
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setResolution(int(dpi) if dpi else 150)
        painter = QPainter(writer)
        if not painter.isActive():
            raise RuntimeError("Impossibile iniziare la scrittura del PDF: verifica percorso e permessi.")
        yield writer, painter
    finally:
        try:
            if painter is not None and painter.isActive():
                painter.end()
        finally:
            if painter is not None:
                sip.delete(painter)
            sip.delete(writer)
