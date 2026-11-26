"""Reversible, transactional parent/child filters shared by both tools panels.

Propagation follows relation keys, never spatial coincidence. Ancestors and
descendants are traversed separately: returning through a parent must not add
unmatched siblings. Each relevant layer is read once, without geometries.
"""
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
import os

from qgis.core import (QgsProject, QgsVectorLayer, QgsFeatureRequest,
                       QgsExpression, QgsProviderRegistry)


class RelatedFilterError(RuntimeError):
    """The requested view could not be applied safely."""


@dataclass(frozen=True)
class Edge:
    parent: str
    child: str
    parent_fields: tuple
    child_fields: tuple


def _key(values):
    result = []
    for value in values:
        if value is None or (hasattr(value, 'isNull') and value.isNull()):
            return None
        # Numeric fields may be represented as int on one side and float on
        # the other. Text identifiers (including leading zeroes) stay exact.
        part = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
        if not part.strip():
            return None
        result.append(part)
    return tuple(result)


def _table_identity(layer):
    if layer.providerType() != 'ogr':
        return None
    uri = QgsProviderRegistry.instance().decodeUri('ogr', layer.source())
    path, table = uri.get('path'), uri.get('layerName')
    if not path or not table or not str(path).lower().endswith('.gpkg'):
        return None
    return os.path.normcase(os.path.realpath(path)), table


def relation_graph(project, canonical_relations):
    from .gna_identity import is_mopr, gna_role
    layers = {l.id(): l for l in project.mapLayers().values()
              if isinstance(l, QgsVectorLayer) and l.isValid()}
    protected = {lid for lid, layer in layers.items() if is_mopr(layer)}
    edges = set()
    for relation in project.relationManager().relations().values():
        if not relation.isValid():
            continue
        parent, child = relation.referencedLayerId(), relation.referencingLayerId()
        if parent not in layers or child not in layers:
            continue
        if parent in protected or child in protected:
            continue  # A DSC/MOSI view must never restrict the MOPR perimeter.
        pairs = sorted(relation.fieldPairs().items())
        if pairs:
            edges.add(Edge(parent, child, tuple(p for _, p in pairs), tuple(c for c, _ in pairs)))

    # Fallback for MAGOH layers loaded manually, or with renamed display labels.
    # Never join two projects merely because their numeric IDs happen to match.
    tables = defaultdict(list)
    for layer in layers.values():
        identity = _table_identity(layer)
        if identity:
            tables[identity].append(layer)
    for path in {identity[0] for identity in tables}:
        gna = [l for (p, _), ls in tables.items() if p == path for l in ls if gna_role(l)]
        for parent in (l for l in gna if gna_role(l) == 'dsc'):
            for child in (l for l in gna if gna_role(l) == 'mosi'):
                # Same archive, same project and excavation; never spatial coincidence.
                pf = {f.name().upper(): f.name() for f in parent.fields()}
                cf = {f.name().upper(): f.name() for f in child.fields()}
                keys = ('DSCH', 'CPR_MOPR')
                edges.add(Edge(parent.id(), child.id(), tuple(pf[k] for k in keys), tuple(cf[k] for k in keys)))
        for _, _, parent_name, parent_field, child_name, child_field in canonical_relations:
            for parent in tables.get((path, parent_name), []):
                for child in tables.get((path, child_name), []):
                    if parent_field in parent.fields().names() and child_field in child.fields().names():
                        edges.add(Edge(parent.id(), child.id(), (parent_field,), (child_field,)))
    # Duplicate views of the same physical table must agree too.
    for siblings in tables.values():
        siblings = [layer for layer in siblings if layer.id() not in protected]
        if len(siblings) < 2:
            continue
        for left in siblings:
            pk = tuple(left.fields()[i].name() for i in left.dataProvider().pkAttributeIndexes())
            if not pk:
                continue
            for right in siblings:
                if right.id() != left.id() and all(f in right.fields().names() for f in pk):
                    edges.add(Edge(left.id(), right.id(), pk, pk))
    return layers, sorted(edges, key=lambda e: (e.parent, e.child, e.parent_fields, e.child_fields))


def component(source_id, edges):
    neighbours = defaultdict(set)
    for edge in edges:
        neighbours[edge.parent].add(edge.child)
        neighbours[edge.child].add(edge.parent)
    found, pending = {source_id}, [source_id]
    while pending:
        for target in neighbours[pending.pop()] - found:
            found.add(target)
            pending.append(target)
    return found


def propagate(source_id, matches, records, edges):
    """Return feature IDs on each layer, in linear indexed graph traversals."""
    combined = {lid: set() for lid in records}
    combined[source_id] = set(matches)
    index_cache = {}
    for upwards in (False, True):
        links = defaultdict(list)
        for edge in edges:
            a, b, fa, fb = (edge.child, edge.parent, edge.child_fields, edge.parent_fields) if upwards else (
                edge.parent, edge.child, edge.parent_fields, edge.child_fields)
            links[a].append((b, fa, fb))
        seen = defaultdict(set)
        seen[source_id].update(matches)
        queue = deque([(source_id, set(matches))])
        while queue:
            current, new_fids = queue.popleft()
            for target, from_fields, to_fields in links[current]:
                if target == source_id:
                    continue  # The root predicate is authoritative, including cycles.
                cache_key = target, to_fields
                if cache_key not in index_cache:
                    idx = defaultdict(set)
                    for fid, attrs in records[target].items():
                        key = _key(attrs[f] for f in to_fields)
                        if key is not None:
                            idx[key].add(fid)
                    index_cache[cache_key] = idx
                keys = {_key(records[current][fid][f] for f in from_fields) for fid in new_fids}
                discovered = set()
                for key in keys - {None}:
                    discovered.update(index_cache[cache_key].get(key, ()))
                fresh = discovered - seen[target]
                if fresh:
                    seen[target].update(fresh)
                    combined[target].update(fresh)
                    queue.append((target, fresh))
    return combined


def _balanced_or(parts):
    while len(parts) > 1:
        parts = ['(' + parts[i] + ' OR ' + parts[i + 1] + ')' if i + 1 < len(parts) else parts[i]
                 for i in range(0, len(parts), 2)]
    return parts[0] if parts else '1=0'


def _sql_literal(value):
    # QGIS expression strings additionally escape backslashes; SQL provider
    # subsets (notably SQLite/OGR) must keep those characters unchanged.
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return QgsExpression.quotedValue(value)


def subset_for_ids(layer, fids, records):
    if not records:
        return ''  # An already empty base view needs no provider rewrite.
    if not fids:
        return '1=0'
    if fids == set(records):
        return ''  # No restriction beyond the user's base subset.
    primary = tuple(layer.fields()[i].name() for i in layer.dataProvider().pkAttributeIndexes())
    if not primary and layer.providerType() == 'memory':
        return '$id IN (' + ','.join(str(fid) for fid in sorted(fids)) + ')'
    if not primary:
        raise RelatedFilterError('Il layer «%s» non espone una chiave primaria filtrabile. Nessun filtro parziale è stato applicato.' % layer.name())
    quote = QgsExpression.quotedColumnRef
    literal = _sql_literal
    if len(primary) == 1:
        field = primary[0]
        return quote(field) + ' IN (' + ','.join(literal(records[fid][field]) for fid in sorted(fids)) + ')'
    clauses = ['(' + ' AND '.join(quote(f) + ' = ' + literal(records[fid][f]) for f in primary) + ')'
               for fid in sorted(fids)]
    return _balanced_or(clauses)


def _read_records(layer, fields):
    request = QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry)
    request.setSubsetOfAttributes(sorted(fields), layer.fields())
    iterator = layer.getFeatures(request)
    try:
        return {f.id(): {name: f[name] for name in fields} for f in iterator}
    finally:
        iterator.close()  # release OGR readers before changing provider subsets


class RelatedFilterSession:
    def __init__(self, plugin):
        self.plugin = plugin
        self.bases = {}
        self.applied = {}
        self.busy = False
        self.last_counts = {}
        self.warnings = []
        self._record_cache = {}

    def _records(self, layer, fields):
        from .search_index import revision
        token = revision(layer)
        signature = (token, frozenset(fields))
        old = self._record_cache.get(layer.id())
        if token is not None and old and old[0] == signature:
            return old[1]
        records = _read_records(layer, fields)
        if token is not None and len(records) <= 50000:
            if sum(len(value[1]) for key, value in self._record_cache.items() if key != layer.id()) + len(records) > 100000:
                self._record_cache.clear()
            self._record_cache[layer.id()] = (signature, records)
        else:
            self._record_cache.pop(layer.id(), None)
        live = QgsProject.instance().mapLayers()
        for lid in list(self._record_cache):
            if lid not in live:
                del self._record_cache[lid]
        return records

    def base_view(self, layer):
        """Read a plugin-filtered persistent layer without resetting the canvas view."""
        base = self.bases.get(layer.id(), layer.subsetString()) if layer.subsetString() == self.applied.get(layer.id()) else layer.subsetString()
        if base == layer.subsetString():
            return layer
        if layer.providerType() not in ('ogr', 'postgres') or layer.isEditable():
            raise RelatedFilterError('Ripristinare prima i filtri su questo layer in memoria o in modifica.')
        options = QgsVectorLayer.LayerOptions()
        options.loadDefaultStyle = False
        view = QgsVectorLayer(layer.source(), 'MAGOH read-only base view', layer.providerType(), options)
        if not view.isValid() or not view.setSubsetString(base or ""):
            raise RelatedFilterError('Impossibile leggere il filtro originale: nessun filtro modificato.')
        return view

    @contextmanager
    def _transaction(self):
        if self.busy:
            raise RelatedFilterError('Un aggiornamento dei filtri è già in corso.')
        self.busy = True
        canvas = self.plugin.iface.mapCanvas()
        frozen = canvas.isFrozen()
        canvas.freeze(True)
        try:
            yield
        finally:
            self.busy = False
            canvas.freeze(frozen)
            if not frozen:
                canvas.refresh()

    @staticmethod
    def _set_many(layers, subsets):
        before = {lid: layers[lid].subsetString() for lid in subsets if lid in layers}
        attempted = []
        try:
            for lid, expression in subsets.items():
                layer = layers.get(lid)
                if layer is None or layer.subsetString() == expression:
                    continue
                if layer.isEditable():
                    raise RelatedFilterError('Salvare o annullare prima le modifiche del layer «%s».' % layer.name())
                attempted.append(lid)
                if not layer.setSubsetString(expression):
                    raise RelatedFilterError('Il provider ha rifiutato il filtro per «%s».' % layer.name())
                layer.triggerRepaint()
        except Exception as exc:
            failures = []
            for lid in reversed(attempted):
                if not layers[lid].setSubsetString(before[lid]):
                    failures.append(layers[lid].name())
            if failures:
                raise RelatedFilterError(str(exc) + '\nRipristino non riuscito: ' + ', '.join(failures)) from exc
            raise

    def _restore_owned(self, layers):
        restore = {}
        self.warnings = []
        for lid, base in self.bases.items():
            layer = layers.get(lid)
            if layer is None:
                continue
            if layer.subsetString() == self.applied.get(lid):
                restore[lid] = base
            else:
                self.warnings.append('Filtro cambiato fuori dal gestore, conservato: ' + layer.name())
        self._set_many(layers, restore)
        self.bases.clear()
        self.applied.clear()

    def clear(self):
        with self._transaction():
            layers = {l.id(): l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer)}
            self._restore_owned(layers)
            self.last_counts = {}
            self._notify_warnings()

    def _notify_warnings(self):
        if self.warnings:
            self.plugin.iface.messageBar().pushWarning('MAGOH', '\n'.join(self.warnings))

    def apply(self, source, matches=None, expression=None):
        """Apply one predicate to its ancestors/descendants, or roll back all.

        `matches` contains QGIS feature IDs, not logical IDs or record counts.
        `expression` is a provider subset, used by chronology/category filters.
        """
        project = QgsProject.instance()
        layers, edges = relation_graph(project, self.plugin.MAGOH_RELATIONS)
        if source.id() not in layers:
            raise RelatedFilterError('Il layer sorgente non è più disponibile.')
        scope = component(source.id(), edges)
        edges = [e for e in edges if e.parent in scope and e.child in scope]
        previous = {lid: layers[lid].subsetString() for lid in scope | set(self.bases) if lid in layers}
        old_bases, old_applied = dict(self.bases), dict(self.applied)
        old_counts = dict(self.last_counts)
        with self._transaction():
            try:
                self._restore_owned(layers)
                bases = {lid: layers[lid].subsetString() or '' for lid in scope}
                root_expr = expression or ''
                if expression is not None:
                    combined = ('(' + bases[source.id()] + ') AND (' + root_expr + ')') if bases[source.id()] and root_expr else (root_expr or bases[source.id()])
                    self._set_many(layers, {source.id(): combined})
                fields = {lid: {layers[lid].fields()[i].name() for i in layers[lid].dataProvider().pkAttributeIndexes()} for lid in scope}
                for edge in edges:
                    fields[edge.parent].update(edge.parent_fields)
                    fields[edge.child].update(edge.child_fields)
                records = {}
                for lid in sorted(scope):
                    layer = layers[lid]
                    records[lid] = self._records(layer, fields[lid])
                root_ids = set(records[source.id()]) if expression is not None else (set(matches or ()) & set(records[source.id()]))
                retained = propagate(source.id(), root_ids, records, edges)
                subsets = {}
                for lid in sorted(scope):
                    predicate = root_expr if lid == source.id() and expression is not None else subset_for_ids(layers[lid], retained[lid], records[lid])
                    subsets[lid] = ('(' + bases[lid] + ') AND (' + predicate + ')') if bases[lid] and predicate else (predicate or bases[lid])
                self._set_many(layers, subsets)
                self.bases, self.applied = bases, subsets
                self.last_counts = {lid: len(ids) for lid, ids in retained.items()}
                self._notify_warnings()
                return dict(self.last_counts)
            except Exception as exc:
                self._set_many(layers, previous)
                self.bases, self.applied, self.last_counts = old_bases, old_applied, old_counts
                raise RelatedFilterError(str(exc)) from exc


def _apply_many(self, selections):
    """one filter over several source layers at once (e.g. every MOSI).

    ``selections`` is a list of (layer, feature ids). Each source keeps its own
    matches; ancestors/descendants are the union of all propagations. All
    previous plugin filters are restored first, as in ``apply``.
    """
    project = QgsProject.instance()
    layers, edges = relation_graph(project, self.plugin.MAGOH_RELATIONS)
    scope = set()
    for source, _ in selections:
        if source.id() not in layers:
            raise RelatedFilterError('Il layer sorgente non è più disponibile.')
        scope |= component(source.id(), edges)
    edges = [e for e in edges if e.parent in scope and e.child in scope]
    previous = {lid: layers[lid].subsetString() for lid in scope | set(self.bases) if lid in layers}
    old_bases, old_applied = dict(self.bases), dict(self.applied)
    old_counts = dict(self.last_counts)
    with self._transaction():
        try:
            # Persistent providers can be read through a separate base view.
            # Do not clear/reapply all visible subsets on every slider tick.
            fast = all(layers[lid].providerType() in ('ogr', 'postgres') and not layers[lid].isEditable() for lid in scope)
            if fast:
                self.warnings = []
                bases = {lid: (self.bases[lid] if lid in self.bases and layers[lid].subsetString() == self.applied.get(lid)
                               else layers[lid].subsetString() or '') for lid in scope}
                outside = {lid: base for lid, base in self.bases.items() if lid not in scope and lid in layers
                           and layers[lid].subsetString() == self.applied.get(lid)}
                self._set_many(layers, outside)
            else:
                self._restore_owned(layers)
                bases = {lid: layers[lid].subsetString() or '' for lid in scope}
            fields = {lid: {layers[lid].fields()[i].name() for i in layers[lid].dataProvider().pkAttributeIndexes()} for lid in scope}
            for edge in edges:
                fields[edge.parent].update(edge.parent_fields)
                fields[edge.child].update(edge.child_fields)
            records = {}
            for lid in sorted(scope):
                layer = layers[lid]
                view = self.base_view(layer) if fast else layer
                records[lid] = _read_records(view, fields[lid]) if view is not layer else self._records(layer, fields[lid])
            retained = {lid: set() for lid in scope}
            for source, matches in selections:
                found = propagate(source.id(), set(matches or ()) & set(records[source.id()]), records, edges)
                for lid, ids in found.items():
                    retained.setdefault(lid, set()).update(ids)
            subsets = {}
            for lid in sorted(scope):
                predicate = subset_for_ids(layers[lid], retained[lid], records[lid])
                subsets[lid] = ('(' + bases[lid] + ') AND (' + predicate + ')') if bases[lid] and predicate else (predicate or bases[lid])
            self._set_many(layers, subsets)
            self.bases, self.applied = bases, subsets
            self.last_counts = {lid: len(ids) for lid, ids in retained.items()}
            self._notify_warnings()
            return dict(self.last_counts)
        except Exception as exc:
            self._set_many(layers, previous)
            self.bases, self.applied, self.last_counts = old_bases, old_applied, old_counts
            raise RelatedFilterError(str(exc)) from exc


RelatedFilterSession.apply_many = _apply_many


def session_for(plugin):
    session = getattr(plugin, '_related_filter_session', None)
    if session is None:
        session = RelatedFilterSession(plugin)
        plugin._related_filter_session = session
    return session
