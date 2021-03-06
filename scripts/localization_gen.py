import argparse
import ast
import importlib.util
import os
import sys
import time
from collections import OrderedDict

import googletrans

SRC_DIR = os.path.join(os.path.split(os.path.abspath(sys.path[0]))[0], 'src')
LNG_DIR = os.path.join(SRC_DIR, 'languages')
DST_FILE = os.path.join(LNG_DIR, 'ru.py')
EXT = '.py'
WALK_SUBDIR = ('lib',)
TOP_IGNORE = ('test.py',)
LF = '\n'

HEADER_1 = """
def _config_pretty_models(_, count):
    ot = 'о'
    if count == 1:
        et = 'ь'
        ot = 'а'
    elif count in [2, 3, 4]:
        et = 'и'
    else:
        et = 'ей'
    pretty = ['ноль', 'одна', 'две', 'три', 'четыре', 'пять', 'шесть']
    count = pretty[count] if count < 7 else count
    return 'Загружен{} {} модел{}'.format(ot, count, et)""".split(LF) + [''] * 2

# === dicts header ===
LANG_CODE = {
    'IETF': 'ru-RU',
    'ISO': 'ru',
    'aws': 'ru-RU',
}

YANDEX_EMOTION = {
    'good'    : 'добрая',
    'neutral' : 'нейтральная',
    'evil'    : 'злая',
}

YANDEX_SPEAKER = {
    'jane'  : 'Джейн',
    'oksana': 'Оксана',
    'alyss' : 'Алиса',
    'omazh' : 'Омар',  # я это не выговорю
    'zahar' : 'Захар',
    'ermil' : 'Саня'  # и это
}

RHVOICE_SPEAKER = {
    'anna'     : 'Аня',
    'aleksandr': 'Александр',
    'elena'    : 'Елена',
    'irina'    : 'Ирина'
}

AWS_SPEAKER = {
    'Tatyana': 'Татьяна',
    'Maxim': 'Максим',
}
HEADER_DICTS = {
    'LANG_CODE': LANG_CODE,
    'YANDEX_EMOTION': YANDEX_EMOTION,
    'YANDEX_SPEAKER': YANDEX_SPEAKER,
    'RHVOICE_SPEAKER': RHVOICE_SPEAKER,
    'AWS_SPEAKER': AWS_SPEAKER,
}
# ======


class RawRepr(str):
    def __new__(cls, text):
        # noinspection PyArgumentList
        return str.__new__(cls, text)

    def __repr__(self):
        return self


ASSIGNS = {
    'Загружено {} моделей': RawRepr('_config_pretty_models'),
}


class LIFOFixDict(OrderedDict):
    def __init__(self, *args, maxlen=20, **kwargs):
        self._max = maxlen
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        if key in self:
            del self[key]
        super().__setitem__(key, value)
        if 0 < self._max < len(self):
            self.popitem(False)


class Parser:
    def __init__(self, target='F'):
        self.result = OrderedDict()
        self.target = target
        self.line, self.class_, self.def_, self.filename = [None] * 4
        self.calls = 0
        self.phrases = set()
        self._store = LIFOFixDict()

    def parse(self, file_path, filename):
        self.line, self.class_, self.def_ = [None] * 3
        self.calls = 0
        self.phrases = set()
        self._store = LIFOFixDict()
        self.filename = filename
        w_time = time.time()
        try:
            with open(file_path, encoding='utf8') as fd:
                data = fd.read()
        except IOError as e:
            print('Error reading {}: {}'.format(file_path, e))
            return
        try:
            body = ast.parse(data).body
        except Exception as e:
            print('AST {}:{}'.format(file_path, e))
            return
        list(map(self._finder, body))
        w_time = time.time() - w_time
        if self.calls:
            print('Parse {} in {} sec. Founds {} calls, {} phrases'.format(
                filename, w_time, self.calls, len(self.phrases)))

    def _pw(self, msg):
        msg = '{}#L{}: {}; class={}, def={}'.format(self.filename, self.line, msg, self.class_, self.def_)
        print('\033[93m{}\033[0m'.format(msg))  # YELLOW = 93

    def _store_set(self, val: ast.Assign):
        def _store(key_, val_):
            if isinstance(key_, ast.Name) and isinstance(key_.ctx, ast.Store) and isinstance(val_, ast.Str) and val_.s:
                self._store[key_.id] = val_.s

        for key in val.targets:
            if isinstance(key, (ast.Tuple, ast.List)) and isinstance(val.value, (ast.Tuple, ast.List)) and \
                    isinstance(key.ctx, ast.Store):
                list(map(_store, key.elts, val.value.elts))
                return
            _store(key, val.value)

    def _save(self, value: str, level):
        if value not in self.result:
            self.result[value] = []
        self.calls += 1
        self.phrases.add(value)
        self.result[value].append(
            {'class': self.class_, 'def': self.def_, 'line': self.line, 'file': self.filename, 'lvl': level}
        )

    def _call_probe(self, node: ast.Call, level):
        value = None
        if not node.args:
            self._pw('Call without args?')
        elif not isinstance(node.args[0], (ast.Name, ast.Str)):
            self._pw('First arg must be Name or Str, not {}'.format(type(node.args[0])))
        elif isinstance(node.args[0], ast.Name):
            if not isinstance(node.args[0].ctx, ast.Load):
                self._pw('Arg type={} not Load, WTF'.format(type(node.args[0].ctx)))
            elif node.args[0].id not in self._store:
                self._pw('Wrong arg type or missing. Name={}, must be str'.format(node.args[0].id))
            else:
                value = self._store[node.args[0].id]
        else:
            value = node.args[0].s or None
            if not value:
                self._pw('Empty text?')
        if value:
            self._save(value, level)

    def _finder(self, node, level=0):
        self.line = getattr(node, 'lineno', self.line)
        if not level:
            self.class_, self.def_ = None, None
        if isinstance(node, ast.ClassDef) and not level:
            self.class_ = node.name
            self.def_ = None
        elif isinstance(node, ast.FunctionDef):
            if not level:
                self.def_ = node.name
                self.class_ = None
            elif level == 2 or not self.def_:
                self.def_ = node.name

        if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == self.target:
            self._call_probe(node, level)
        elif isinstance(node, ast.Assign):
            self._store_set(node)

        if isinstance(node, ast.AST):
            for _, b in ast.iter_fields(node):
                self._finder(b, level+1)
        elif isinstance(node, list):
            for x in node:
                self._finder(x, level+1)


def _read_lng_comments(file: str) -> list:
    with open(file, encoding='utf8') as fd:
        line = 'True'
        while not line.startswith('_LNG'):
            line = fd.readline()
            if not line:
                return []
            line = line.strip()

        result = []
        comment = ''
        while True:
            line = fd.readline()
            if not line:
                break
            line = line.strip()
            if line == '}':
                break
            if line.startswith('#'):
                comment = line[1:].lstrip()
            elif comment:
                result.append(comment)
        return result


def read_lng_comments(data: dict, file=DST_FILE) -> {}:
    try:
        comments = _read_lng_comments(file)
    except IOError as e:
        print('Error reading {}: {}'.format(file, e))
        comments = None
    if not comments:
        return {}
    keys = [x for x in data.keys()]
    if len(comments) != len(keys):
        print('Comments count={}, data keys={}. Mismatch.'.format(len(comments), len(keys)))
        return {}
    return dict(zip(keys, comments))


class Writter:
    def __init__(self, file):
        self._file = file
        self._fd = None
        self._size, self._lines = 0, 0

    def _wl(self, line: str or list):
        if not isinstance(line, (list, tuple, set)):
            self._lines += 1
            self._size += self._fd.write(line + LF)
        else:
            self._lines += len(line)
            self._size += self._fd.write(LF.join(line) + LF)

    def _w_dict(self, data: dict, name: str, dict_comment='', keys_comments=None):
        keys_comments = keys_comments or {}
        dict_comment = '  # {}'.format(dict_comment) if dict_comment else ''
        if data:
            self._wl('{} = {{{}'.format(name, dict_comment))
        else:
            self._wl(['{} = {{}}{}'.format(name, dict_comment), ''])
            return
        old_comment = ''
        for key, val in data.items():
            new_comment = keys_comments.get(key, '')
            if new_comment and new_comment != old_comment:
                old_comment = new_comment
                self._wl('    # {}'.format(new_comment))
            self._wl('    {}: {},'.format(repr(key), repr(val) if isinstance(val, str) else val))
        self._wl('}')

    def write_new(self, data: dict, lng_comment: str, comments=None):
        dict_comments = {}
        dicts = get_old(self._file, HEADER_DICTS.keys())
        for old in [x for x in dicts.keys()]:
            if not dicts[old]:
                dicts[old] = HEADER_DICTS[old]
                dict_comments[old] = 'missing, received from ru.py'
        dict_comments['_LNG'] = lng_comment
        self._writter(data, dicts, False, dict_comments, comments)

    def write_gen(self, data: dict, comment_mode: str):
        comments = {key: make_txt_comment(val, comment_mode) for key, val in data.items()}
        data = {key: ASSIGNS.get(key, None) for key in data}
        self._writter(data, HEADER_DICTS, True, {}, comments)

    def _writter(self, data: dict, dicts: dict, gen: bool, dict_comments: dict, comments=None):
        self._size, self._lines = 0, 0
        with open(self._file, encoding='utf8', mode='w') as self._fd:
            self._wl('# Generated by {}'.format(sys.argv[0]))
            self._wl('')
            if gen:
                self._wl(HEADER_1)
            [self._w_dict(val, key, dict_comments.get(key, '')) or self._wl('') for key, val in dicts.items()]
            self._w_dict(data, '_LNG', dict_comments.get('_LNG', ''), comments)
        print('Saved {} lines ({} bytes) to {}'.format(self._lines, self._size, self._file))


def border(count=16):
    print('=' * count)


def walking():
    def _walk(top_path, top_name='', subdir=(), no_files=()):
        dirs = []
        for k in os.listdir(top_path):
            if k.startswith(('__', '.')):
                continue
            path = os.path.join(top_path, k)
            name = '/'.join((top_name, k)) if top_name else k
            if os.path.isfile(path):
                if os.path.splitext(path)[1] == EXT and not (no_files and k in no_files):
                    yield path, name
            elif os.path.isdir and (not subdir or k in subdir):
                dirs.append((path, name))
        for path, name in dirs:
            yield from _walk(path, name)
    yield from _walk(SRC_DIR, subdir=WALK_SUBDIR, no_files=TOP_IGNORE)


def make_txt_comment(val: list, mode: str) -> str:
    if mode == 'calls':
        return _make_txt_comment_def(val)
    elif mode in ('lines', 'files'):
        return _make_txt_comment_line(val, mode == 'files')
    return ''


def _make_txt_comment_line(val: list, only_files: bool) -> str:
    calls = OrderedDict()
    for v in val:
        if v['file'] not in calls:
            calls[v['file']] = OrderedDict()
        if not only_files:
            calls[v['file']][str(v['line'])] = None
    return ' '.join('{}{}{}'.format(k, '' if only_files else '#L', ';#L'.join(v.keys())) for k, v in calls.items())


def _make_txt_comment_def(val: list) -> str:
    calls = OrderedDict()
    for v in val:
        if v['file'] not in calls:
            calls[v['file']] = OrderedDict()
        if v['class'] and v['class'] not in calls[v['file']]:
            calls[v['file']][v['class']] = OrderedDict()
        if v['def'] and v['class']:
            calls[v['file']][v['class']][v['def']] = None
        elif v['class']:
            calls[v['file']][v['class']][v['class']] = None
        elif v['def']:
            calls[v['file']]['#' + v['def']] = str(v['def'])
        else:
            line = '#L{}'.format(v['line'])
            calls[v['file']][line] = line
    result = []
    for k, v in calls.items():
        f_result = []
        for k2, v2 in v.items():
            if isinstance(v2, str):
                f_result.append('.{}'.format(v2))
            else:
                def_ = ', '.join(v2) or '!error'
                def_ = def_ if len(v2) < 2 else '({})'.format(def_)
                f_result.append('{}.{}'.format(k2, def_))
        result.append('{}#{}'.format(k, ';'.join(f_result)))
    return ' '.join(result)


def get_diff(new: dict, old: dict) -> tuple:
    def _diff(x, y):
        return [k for k in x if k not in y]
    return _diff(new, old), _diff(old, new)


def print_diff(add: list, del_: list, file=DST_FILE):
    def _print(data: list):
        print('  ' + ('{}  '.format(LF) if len(data) < 25 else '; ').join([repr(k) for k in data]))
    if not (add or del_):
        print('No change in {}'.format(file))
    else:
        print('File {} change:'.format(file))
        if add:
            print(' New phrases ({}):'.format(len(add)))
            _print(add)
        if del_:
            print(' Deleted phrases ({}):'.format(len(del_)))
            _print(del_)


def get_old(file=DST_FILE, keys=('_LNG',)) -> dict:
    if os.path.isfile(file):
        try:
            spec = importlib.util.spec_from_file_location("module.name", file)
            foo = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(foo)
            # noinspection PyProtectedMember
            return getattr(foo, keys[0]) if len(keys) < 2 else {key: getattr(foo, key, {}) for key in keys}
        except Exception as e:
            print('Error loading {}: {}'.format(file, e))
    return {} if len(keys) < 2 else {key: {} for key in keys}


def cli():
    c_mode = {
        'lines': 'Save file names and line numbers in comments',
        'calls': 'Save file names and class\\method names in comments',
        'files': 'Save only file names in comments',
        'nope': 'Don\'t add comments',
    }
    c_default = 'files'
    c_help = 'Add comments for _LNG in generated file (default: {}):'.format(c_default) + LF
    c_help = '{}{}'.format(c_help, LF.join('{:5} -  {}'.format(k, v) for k, v in c_mode.items()))

    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--comments', default=c_default, choices=c_mode, metavar='[mode]', help=c_help)
    parser.add_argument('--only-changes', action='store_true', help='Don\'t save generated file if _LANG unchanged')

    parser.add_argument('-up', type=str, default='', metavar='[LNG]', help='Make/update language file')

    parser.add_argument('-gt-list', action='store_true', help='Print supported languages')
    parser.add_argument('-gt', type=str, default='', metavar='[LNG]',
                        help='Use google translate to translate ru.py to another language')
    parser.add_argument('--no-gt', action='store_true', help='Don\'t translate, save direct')
    parser.add_argument('--full', action='store_true', help='Full translate, otherwise only new phrases')
    parser.add_argument('--delay', type=float, default=3, metavar='[sec]',
                        help='Requests delay. Google may banned your client IP address')
    parser.add_argument('--proxy', type=str, default='', metavar='[ip:port]', help='http proxy, for gt')
    return parser.parse_args()


def version_warning():
    if sys.version_info[:2] < (3, 6):
        print(
            'WARNING! This program may incorrect work in python < 3.6. You use {}.{}.{}'.format(*sys.version_info[:3])
        )


def _fix_gt(origin: str, text: str) -> str:
    # gt теряет пробелы
    start, end = 0, 0
    for el in origin:
        if el != ' ':
            break
        start += 1
    for el in origin[::-1]:
        if el != ' ':
            break
        end += 1
    return ' ' * start + text.strip(' ') + ' ' * end


def google_translator(lang, delay, proxies, data: dict, chunk=30) -> dict:
    def progress(percent, eta):
        print('[{}%] ETA: {} sec, Elapse: {} sec.{}'.format(
            round(percent, 1), int(eta), int(time.time() - start_time), ' ' * 20), end='', flush=True
        )
    print('Translate {} phrases from {} to {}'.format(
        len(data), googletrans.LANGUAGES['ru'], googletrans.LANGUAGES[lang]
    ))
    print()

    chunks = [x for x in data.keys()]
    chunks = [chunks[i:i + chunk] for i in range(0, len(chunks), chunk)]
    full = len(chunks)
    count, drift = 0, 0
    start_time = time.time()
    translator = googletrans.Translator(proxies=proxies)
    for part in chunks:
        print(end='\r', flush=True)
        progress((100 / full) * count, (full - count) * (delay + drift))
        drift = time.time()
        for trans in translator.translate(text=part, src='ru', dest=lang):
            data[trans.origin] = _fix_gt(trans.origin, trans.text)
        drift = time.time() - drift
        time.sleep(delay)
        count += 1
    print(end='\r', flush=True)
    progress(100, 0)
    print()
    return data


def _translate(direct, lang, delay, proxies, data) -> dict:
    if not direct and data:
        proxies = {'http': proxies} if proxies else None
        data = google_translator(lang, delay, proxies, data)
        border()
        br = False
        for key, val in data.items():
            if not (isinstance(val, str) or val is None):
                print('Wrong val type by GT, {}, in key={}, set None'.format(repr(type(val)), repr(key)))
                data[key], br = None, True
        br and border()
    return data


def main_trans(file_path, lang='', delay=3, proxies=None, direct=True, full=False):
    data = {k: None for k in get_old()}
    if not data:
        print('Nope.')
        return
    old = get_old(file=file_path) if not full else {}
    if old:
        border()
        add, del_ = get_diff(data, old)
        new = _translate(direct, lang, delay, proxies, {k: None for k in add})
        data = {key: new[key] if key in new else old[key] for key in data}
        print_diff([new[k] or k for k in add], [old[k] or k for k in del_], file_path)
        border()
    else:
        data = _translate(direct, lang, delay, proxies, data)
    lng_comment = 'google translate - it\'s a good idea!' if lang else ''
    Writter(file_path).write_new(data, lng_comment, read_lng_comments(data))
    print('Check {} before using'.format(file_path))


def main_gen(only_changes, comment_mode):
    print('Comments mode: {}'.format(comment_mode))
    border()
    parser = Parser()
    sum_time = time.time()
    sum_parse = len([parser.parse(path, name) for path, name in walking()])
    sum_time = time.time() - sum_time
    print()
    print('Parse {} files in {} sec'.format(sum_parse, sum_time))
    border()
    pop_count = 1
    call_count, unique_count = 0, 0
    for k, v in parser.result.items():
        unique_count += 1
        call_count += len(v)
        if len(v) > pop_count:
            pop_count = len(v)
    print('Summary: {} calls, {} phrases'.format(call_count, unique_count))
    if pop_count > 1:
        print('The most popular strings({} calls)'.format(pop_count))
        for k, v in parser.result.items():
            if len(v) == pop_count:
                print('  # {}'.format(make_txt_comment(v, comment_mode if comment_mode != 'nope' else 'files')))
                print('  {}'.format(repr(k)))
    border()
    add, del_ = get_diff(parser.result, get_old())
    print_diff(add, del_)
    if not only_changes or add or del_:
        border()
        Writter(DST_FILE).write_gen(parser.result, comment_mode)
    print()


def languages_list():
    print('  CODE        LANGUAGE')
    border(32)
    [print('   {:7}-  {:}'.format(k, v)) for k, v in googletrans.LANGUAGES.items()]
    border(32)


def main():
    version_warning()
    args = cli()
    if args.gt_list:
        languages_list()
        return
    lang = args.up or args.gt
    if not lang:
        main_gen(args.only_changes, args.comments)
        return

    if lang not in googletrans.LANGUAGES:
        print('Wrong lang code {}, use: {}'.format(
            lang, ', '.join([key for key in googletrans.LANGUAGES if key != 'ru'])))
        exit(1)
    if lang == 'ru':
        print('Don\'t use ru.')
        exit(1)
    if args.up:
        file_path = os.path.join(LNG_DIR, '{}.py'.format(lang))
        main_trans(file_path)
    else:
        file_path = os.path.join(LNG_DIR, 'gt_{}.py'.format(lang))
        main_trans(file_path, args.gt, args.delay, args.proxy, args.no_gt, args.full)


if __name__ == '__main__':
    main()
