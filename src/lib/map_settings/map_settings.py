from default_settings import CFG as DEF_CFG
from lib.map_settings.cfg import CFG_DSC, INTERFACES


def make_map_settings(wiki: dict, cfg: dict) -> dict:
    return {key: make_interface(sections, wiki, cfg) for key, sections in INTERFACES.items()}


def make_interface(sections: tuple, wiki: dict, cfg: dict) -> dict:
    result = {}
    for key in sections:
        result[key] = make_section(DEF_CFG.get(key, {}), cfg.get(key, {}), CFG_DSC.get(key, {}), wiki.get(key, {}))
    return result


def make_section(def_cfg: dict, cfg: dict, dsc: dict, wiki: dict) -> dict:
    result = {}
    for key, default in def_cfg.items():
        result[key] = make_param(key, default, cfg.get(key, default), wiki.get(key), dsc.get(key, {}))
    if 'null' in wiki:
        result['description'] = wiki['null']
    return result


def make_param(key: str, default, value, desc: str, dsc: dict) -> dict:
    # name: {'name': h_name, 'desc': description, 'type': type_, 'default': value}
    # options - optional
    desc = desc or 'description'

    options = dsc.get('options')
    if callable(options):
        options = options()
    if isinstance(options, (set, frozenset)):
        options = list(options)
    elif isinstance(options, dict):
        options = list(options.keys())
    elif not isinstance(options, (list, tuple)):
        options = None

    if options:
        type_ = 'select'
    elif 'type' in dsc:
        type_ = str(dsc['type'])
    elif isinstance(default, bool):
        type_ = 'checkbox'
    else:
        type_ = 'text'

    name = dsc.get('name') or key
    result = {'name': name, 'desc': desc, 'type': type_, 'default': default, 'value': value}
    if options:
        result['options'] = options
    return result
