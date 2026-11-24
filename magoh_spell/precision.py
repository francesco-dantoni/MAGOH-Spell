from functools import lru_cache
from pathlib import Path
import json

VALUES = ('Preciso', 'Non Preciso', 'Non localizzabile')
_BY_TEXT = {value.casefold(): value for value in VALUES}

def canonical(value):
    if isinstance(value,str):
        return _BY_TEXT.get(value.strip().casefold(), value)
    return value

@lru_cache(maxsize=1)
def import_maps():
    tables = json.loads((Path(__file__).parent/'data/form_value_maps.json').read_bytes())
    result = {}
    for table, fields in tables.items():
        for field, options in fields.items():
            choices = {}
            for label, code in options.items():
                for text in (label,code):
                    choices.setdefault(text.strip().casefold(),set()).add(code)
            result[table,field] = {k:next(iter(v)) for k,v in choices.items() if len(v)==1}
    return result

def normalize_import(table, attrs):
    maps = import_maps()
    for field, value in attrs.items():
        if isinstance(value,str):
            attrs[field] = maps.get((table,field),{}).get(value.strip().casefold(),value)
