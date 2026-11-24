"""Temporary canvas AOI tools embedded in the existing API import dialog."""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QVBoxLayout,
)
from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest,
    QgsGeometry, QgsMapLayerProxyModel, QgsPointXY, QgsProject, QgsRectangle, QgsWkbTypes,
    QgsTask, QgsVectorLayerFeatureSource,
)
from qgis.gui import QgsMapLayerComboBox, QgsMapTool, QgsRubberBand

from ..api_spatial import transformed_area


def _layer_area(task, source, selected_ids):
    """Read a detached feature source, never a live layer, in the worker."""
    request = QgsFeatureRequest().setNoAttributes()
    if selected_ids is not None:
        request.setFilterFids(selected_ids)
    parts, chunks, count = [], [], 0
    for feature in source.getFeatures(request):
        if task.isCanceled():
            return None
        geom = feature.geometry()
        if geom.isNull() or geom.isEmpty() or not geom.isGeosValid():
            raise ValueError('Geometrie vuote o non valide nella selezione / Empty or invalid geometries in selection')
        parts.append(geom)
        count += 1
        if len(parts) >= 200:
            chunks.append(QgsGeometry.unaryUnion(parts))
            parts = []
    if task.isCanceled():
        return None
    if parts:
        chunks.append(QgsGeometry.unaryUnion(parts))
    return QgsGeometry.unaryUnion(chunks) if chunks else QgsGeometry(), count


class AreaMapTool(QgsMapTool):
    def __init__(self, panel, rectangle):
        super().__init__(panel.canvas)
        self.panel = panel
        self.source_crs = panel.canvas.mapSettings().destinationCrs()
        self.rectangle = rectangle
        self.points = []
        self.start = None
        self.finished = False
        self.band = QgsRubberBand(panel.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.band.setColor(QColor(0, 135, 115, 85))
        self.band.setWidth(2)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _polygon(self, points):
        return QgsGeometry.fromPolygonXY([points + [points[0]]]) if len(points) >= 3 else QgsGeometry()

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rectangle:
            self.start = QgsPointXY(event.mapPoint())

    def canvasMoveEvent(self, event):
        point = QgsPointXY(event.mapPoint())
        if self.rectangle and self.start is not None:
            geom = QgsGeometry.fromRect(QgsRectangle(self.start, point))
        else:
            geom = self._polygon(self.points + [point])
        self.band.setToGeometry(geom, None)

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            if not self.rectangle and len(self.points) >= 3:
                self.complete(self._polygon(self.points))
            else:
                self.complete(None)
        elif event.button() == Qt.MouseButton.LeftButton:
            point = QgsPointXY(event.mapPoint())
            if self.rectangle:
                if self.start is not None:
                    self.complete(QgsGeometry.fromRect(QgsRectangle(self.start, point)))
            elif not self.points or point != self.points[-1]:
                self.points.append(point)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.complete(None)
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and len(self.points) >= 3:
            self.complete(self._polygon(self.points))
        elif event.key() == Qt.Key.Key_Backspace and self.points:
            self.points.pop()
            self.band.setToGeometry(self._polygon(self.points), None)

    def complete(self, geometry):
        if self.finished:
            return
        self.finished = True
        self.panel.finish_drawing(geometry)

    def deactivate(self):
        super().deactivate()
        if not self.finished:
            self.finished = True
            # QGIS is already replacing the tool. Avoid a reentrant setMapTool.
            self.panel.finish_drawing(None, restore=False)

    def dispose(self):
        self.finished = True
        if self.band is not None:
            self.canvas().scene().removeItem(self.band)
            self.band = None


class ApiAreaPanel(QGroupBox):
    def __init__(self, dialog, tr):
        super().__init__(tr('Area geografica', 'Geographic area'), dialog)
        self.dialog, self.tr = dialog, tr
        self.canvas = dialog.iface.mapCanvas()
        self.area = None
        self.tool = None
        self.previous_tool = None
        self.drawing = False
        self.area_task = None
        self.task_generation = 0
        self.preview = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.preview.setColor(QColor(0, 135, 115, 55))
        self.preview.setStrokeColor(QColor(0, 135, 115))
        self.preview.setWidth(2)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        for it, en, callback in (
            ('Rettangolo', 'Rectangle', lambda: self.draw(True)),
            ('Poligono libero', 'Free polygon', lambda: self.draw(False)),
            ('Estensione mappa', 'Map extent', self.use_extent),
        ):
            button = QPushButton(tr(it, en))
            button.setAutoDefault(False)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        self.layer_combo.setAllowEmptyLayer(True)
        self.layer_combo.setLayer(None)
        form.addRow(tr('Layer poligonale', 'Polygon layer'), self.layer_combo)
        self.feature_scope = QComboBox()
        self.feature_scope.setProperty('magoh_translate_choices', True)
        self.feature_scope.addItem(tr('Solo elementi selezionati', 'Selected features only'), 'selected')
        self.feature_scope.addItem(tr('Tutto il layer (esplicito)', 'Whole layer (explicit)'), 'all')
        form.addRow(tr('Geometrie da usare', 'Geometries to use'), self.feature_scope)
        layout.addLayout(form)
        row = QHBoxLayout()
        use = QPushButton(tr('Usa geometrie del layer', 'Use layer geometries'))
        use.setAutoDefault(False)
        use.clicked.connect(self.use_layer)
        clear = QPushButton(tr('Rimuovi area', 'Clear area'))
        clear.setAutoDefault(False)
        clear.clicked.connect(self.clear)
        row.addWidget(use)
        row.addWidget(clear)
        layout.addLayout(row)
        self.status = QLabel(tr('Nessuna area selezionata.', 'No area selected.'))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.include_parents = QCheckBox(tr(
            'Includi i padri necessari, anche fuori area', 'Include required parents, even outside the area'))
        self.include_parents.setChecked(True)
        layout.addWidget(self.include_parents)
        self.fast_admin = QCheckBox(tr(
            'Ricerca rapida per Comuni (opzionale)', 'Fast search by Municipality (optional)'))
        self.fast_admin.setChecked(False)
        layout.addWidget(self.fast_admin)
        fast_hint = QLabel(tr(
            'Riduce le richieste usando un indice locale leggero. Può non trovare schede attribuite a un Comune diverso '
            'dalla loro geometria. Lascia disattivato per la ricerca completa. Confini o nomi mancanti allargano la ricerca.',
            'Reduces requests using a lightweight local index. May miss records assigned to a Municipality different '
            'from their geometry. Leave unchecked for a complete search. Missing boundaries or names widen the search.'))
        fast_hint.setWordWrap(True)
        fast_hint.setObjectName('MagohHint')
        layout.addWidget(fast_hint)
        hint = QLabel(tr(
            'Intersezione esatta, non centroide. Le schede senza geometria usano quella del padre. '
            'I padri aggiunti non trascinano fratelli esterni all’area. Senza questa opzione, i padri devono già esistere nel template. '
            'Le richieste sono paginate e sempre limitate alla Provincia scelta, con Comune facoltativo.',
            'Exact intersection, not centroid. Records without geometry use their parent’s geometry. '
            'Added parents do not pull in siblings outside the area. Without this option, parents must already exist in the template. '
            'Requests are paginated and always restricted to the selected Province, with an optional Municipality.'))
        hint.setWordWrap(True)
        hint.setObjectName('MagohHint')
        layout.addWidget(hint)
        self.canvas.destinationCrsChanged.connect(self.refresh_preview)

    def set_area(self, geometry, crs, description):
        try:
            area = transformed_area(geometry, crs, QgsProject.instance().transformContext())
        except (ValueError, RuntimeError) as exc:
            QMessageBox.warning(self.dialog, 'MAGOH API', str(exc))
            return False
        self.area = area
        self.status.setText(self.tr('Filtro spaziale pronto: {source}', 'Spatial filter ready: {source}', source=description))
        self.refresh_preview()
        return True

    def refresh_preview(self):
        if self.tool is not None and self.tool.source_crs != self.canvas.mapSettings().destinationCrs():
            self.tool.complete(None)
        if self.preview is None:
            return
        self.preview.hide()
        if self.area is None or not self.isVisible():
            return
        try:
            geom = QgsGeometry(self.area)
            geom.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4326'),
                                                 self.canvas.mapSettings().destinationCrs(), QgsProject.instance()))
            self.preview.setToGeometry(geom, None)
            self.preview.show()
        except (ValueError, RuntimeError):
            self.status.setText(self.tr('Anteprima non disponibile nel CRS corrente.', 'Preview unavailable in the current CRS.'))

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_preview()

    def hideEvent(self, event):
        if self.preview is not None:
            self.preview.hide()
        super().hideEvent(event)

    def clear(self):
        self.area = None
        self.status.setText(self.tr('Nessuna area selezionata.', 'No area selected.'))
        self.refresh_preview()

    def use_extent(self):
        # visiblePolygon accounts for canvas rotation, unlike the outer bbox.
        points = [QgsPointXY(p) for p in self.canvas.mapSettings().visiblePolygon()]
        if points and points[0] != points[-1]:
            points.append(points[0])
        geom = QgsGeometry.fromPolygonXY([points])
        self.set_area(geom, self.canvas.mapSettings().destinationCrs(), self.tr('estensione mappa', 'map extent'))

    def use_layer(self):
        layer = self.layer_combo.currentLayer()
        if layer is None or not layer.isValid():
            QMessageBox.warning(self.dialog, 'MAGOH API', self.tr('Scegli un layer poligonale.', 'Choose a polygon layer.'))
            return
        ids = None
        if self.feature_scope.currentData() == 'selected':
            ids = layer.selectedFeatureIds()
            if not ids:
                QMessageBox.warning(self.dialog, 'MAGOH API', self.tr('Seleziona almeno un elemento nel layer.', 'Select at least one feature in the layer.'))
                return
        crs, name = layer.crs(), layer.name()
        source = QgsVectorLayerFeatureSource(layer)
        self.task_generation += 1
        generation = self.task_generation
        self.dialog._set_busy(True)
        self.dialog.download_progress.setRange(0, 0)
        self.status.setText(self.tr('Preparazione area…', 'Preparing area…'))
        def complete(exception, result):
            if generation != self.task_generation:
                return
            self.area_task = None
            self.dialog._set_busy(False)
            if exception is not None:
                self.status.setText(self.tr('Area non aggiornata; correggi la selezione.', 'Area not updated; correct the selection.'))
                QMessageBox.warning(self.dialog, 'MAGOH API', str(exception))
            elif result is not None:
                geom, count = result
                self.set_area(geom, crs, self.tr('{n} elementi da {layer}', '{n} features from {layer}', n=count, layer=name))
        self.area_task = QgsTask.fromFunction('MAGOH: prepare area', _layer_area, source, ids, on_finished=complete)
        QgsApplication.taskManager().addTask(self.area_task)

    def cancel_task(self):
        self.task_generation += 1
        if self.area_task is not None:
            self.area_task.cancel()
            self.area_task = None
            self.status.setText(self.tr('Preparazione interrotta; area precedente mantenuta.', 'Preparation cancelled; previous area retained.'))

    def draw(self, rectangle):
        self.previous_tool = self.canvas.mapTool()
        self.tool = AreaMapTool(self, rectangle)
        self.drawing = True
        self.dialog.hide()
        self.canvas.setMapTool(self.tool)
        self.canvas.setFocus()
        self.dialog.iface.messageBar().pushInfo('MAGOH API', self.tr(
            'Trascina per il rettangolo. Poligono: clic per vertice, tasto destro/Invio per terminare. Esc annulla.',
            'Drag for a rectangle. Polygon: click vertices, right-click/Enter to finish. Esc cancels.'))

    def finish_drawing(self, geometry, restore=True):
        tool = self.tool
        self.tool = None
        if tool is not None:
            tool.finished = True
            if restore and self.canvas.mapTool() is tool:
                if self.previous_tool is not None:
                    self.canvas.setMapTool(self.previous_tool)
                else:
                    self.canvas.unsetMapTool(tool)
            tool.dispose()
            tool.deleteLater()
        self.drawing = False
        self.dialog.show()
        self.dialog.raise_()
        if geometry is not None:
            self.set_area(geometry, self.canvas.mapSettings().destinationCrs(), self.tr('area disegnata', 'drawn area'))

    def dispose(self):
        self.cancel_task()
        if self.tool is not None:
            self.tool.finished = True
            if self.canvas.mapTool() is self.tool:
                if self.previous_tool is not None:
                    self.canvas.setMapTool(self.previous_tool)
                else:
                    self.canvas.unsetMapTool(self.tool)
            self.tool.dispose()
            self.tool.deleteLater()
            self.tool = None
        self.drawing = False
        if self.preview is not None:
            self.canvas.destinationCrsChanged.disconnect(self.refresh_preview)
            self.canvas.scene().removeItem(self.preview)
            self.preview = None
