"""Optional administrative query hints, not a proof of spatial completeness.

Only verified API province/municipality spellings are queried. Missing coverage
or names widen the query; exact feature intersection remains in api_spatial.
No network or project mutations occur here.
"""
import gzip
import json
import math
from functools import lru_cache
from pathlib import Path

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform


@lru_cache(maxsize=1)
def load_index():
    with gzip.open(Path(__file__).parent / 'data/admin_index.json.gz', 'rt', encoding='utf-8') as stream:
        data = json.load(stream)
    if data.get('schema') != 1 or data.get('crs') != 'EPSG:3003' or not data.get('items'):
        raise ValueError('Invalid administrative index')
    def valid_box(box):
        return box is None or (len(box) == 4 and all(math.isfinite(v) for v in box)
                               and box[0] <= box[2] and box[1] <= box[3])
    for province in data['provinces'].values():
        if province['region'] not in data['regions'] or not valid_box(province['bbox']):
            raise ValueError('Invalid province index')
    for region in data['regions'].values():
        if not valid_box(region['bbox']):
            raise ValueError('Invalid region index')
    for item in data['items']:
        if item['province'] not in data['provinces'] or not valid_box(item['bbox']):
            raise ValueError('Invalid municipality index')
    return data


def _overlaps(a, b):
    return b is None or not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def plan_queries(area, context, filters, api_provinces, api_municipalities, data=None):
    """Return (queries, explanation). Always retain explicit administrative AND.

    The municipality facet can be truncated: an absent name triggers a province
    query, never exclusion. Missing municipal bounds widen the entire province.
    At most eight municipalities per province, at most sixteen total queries.
    An explicit municipality cannot usefully be narrowed further.
    """
    base = dict(filters)
    if base.get('comune'):
        return [base], 'explicit_municipality'
    data = data if data is not None else load_index()
    transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4326'),
                                       QgsCoordinateReferenceSystem(data['crs']), context)
    box = transform.transformBoundingBox(area.boundingBox())
    # Conservative extra margin for coordinate-operation/edge uncertainty.
    box.grow(100.0)
    bounds = [box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()]
    if not all(math.isfinite(v) for v in bounds):
        return [base], 'invalid_extent'
    names = {str(name).strip().casefold():str(name).strip() for name in api_provinces if str(name).strip()}
    municipalities = {str(name).strip().casefold():str(name).strip() for name in api_municipalities if str(name).strip()}
    explicit = base.get('provincia', '').casefold()
    if explicit:
        names[explicit] = base['provincia']
        if not any(p['name'].casefold() == explicit for p in data['provinces'].values()):
            return [base], 'unknown_explicit_province'
    queries, widened = [], 0
    for pid, province in data['provinces'].items():
        pname = province['name'].casefold()
        if explicit and pname != explicit:
            continue
        children = [item for item in data['items'] if item['province'] == pid]
        candidates = [item for item in children if item['bbox'] is not None and _overlaps(bounds, item['bbox'])]
        parent_box = province['bbox'] or data['regions'][province['region']]['bbox']
        if not candidates and not _overlaps(bounds, parent_box):
            continue
        if pname not in names:
            # API facet absence cannot establish there are no records here.
            return [base], 'unknown_api_province'
        province_query = dict(base, provincia=names[pname])
        if (not candidates or any(item['bbox'] is None for item in children)
                or len(candidates) > 8
                or any(item['name'].casefold() not in municipalities for item in candidates)):
            queries.append(province_query)
            widened += 1
        else:
            for item in candidates:
                queries.append(dict(province_query, comune=municipalities[item['name'].casefold()]))
    unique = {tuple(sorted(query.items())):query for query in queries}
    if not unique or len(unique) > 16:
        return [base], 'outside_coverage_or_large_area'
    return list(unique.values()), 'widened_provinces=' + str(widened)
