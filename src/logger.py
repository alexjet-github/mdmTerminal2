#!/usr/bin/env python3

import logging
import os
import queue
import threading
import time
import zlib
from logging.handlers import RotatingFileHandler
from functools import lru_cache
import json

from languages import F
from owner import Owner
from utils import write_permission_check

DEBUG = logging.DEBUG
INFO = logging.INFO
WARN = logging.WARN
ERROR = logging.ERROR
CRIT = logging.CRITICAL
# Спецсообщения для удаленного логгера
_REMOTE = 999

LOG_LEVEL = {
    'debug'   : DEBUG,
    'info'    : INFO,
    'warning' : WARN,
    'warn'    : WARN,
    'error'   : ERROR,
    'critical': CRIT,
    'crit'    : CRIT,
}

LVL_NAME = {
    DEBUG: 'DEBUG',
    INFO: 'INFO ',
    WARN: 'WARN ',
    ERROR: 'ERROR',
    CRIT: 'CRIT ',
    _REMOTE: 'REMOTE',
}

COLORS = {
    DEBUG: 90,
    INFO: 92,
    WARN: 93,
    ERROR: 91,
    CRIT: 95,
}
COLOR_END = '\033[0m'
NAME_COLOR = '1;36'
MODULE_COLOR = 36

REMOTE_LOG_MODE = {'raw', 'json', 'colored'}
REMOTE_LOG_DEFAULT = 'colored'


def colored(msg, color):
    return '\033[{}m{}{}'.format(color, msg, COLOR_END)


def get_loglvl(str_lvl) -> int:
    return LOG_LEVEL.get(str_lvl, 100500)


def _namer(name):
    return name + '.gz'


def _rotator(source, dest):
    with open(source, 'rb') as sf:
        data = sf.read()
        compressed = zlib.compress(data, 9)
        with open(dest, 'wb') as df:
            df.write(compressed)
    os.remove(source)


@lru_cache(maxsize=512)
def _name_builder(names: tuple, colored_=False):
    result = []
    for num, name in enumerate(names):
        result.append(colored(name, NAME_COLOR if not num else MODULE_COLOR) if colored_ else name)
    return '->'.join(result)


class _LogWrapper:
    def __init__(self, name: str or list, print_):
        if isinstance(name, str):
            name = [name]
        self.name = name
        self._print = print_

    def __call__(self, msg: str, lvl=DEBUG):
        self._print(self.name, msg, lvl)

    def module(self, module_name: str, msg: str, lvl=DEBUG):
        self._print(self.name + [module_name], msg, lvl)

    def add(self, name: str):
        return _LogWrapper(self.name + [name], self._print)


class Logger(threading.Thread):
    REMOTE_LOG = 'remote_log'
    CHANNEL = 'net_block'

    def __init__(self):
        super().__init__(name='Logger')
        self.cfg, self.own = None, None
        self.file_lvl = None
        self.print_lvl = None
        self.in_print = None
        self._handler = None
        self._app_log = None
        self._conn = None
        self._remote_log_mode = REMOTE_LOG_DEFAULT
        self._queue = queue.Queue()
        self.log = self.add('Logger')
        self.log('start', INFO)

    def init(self, cfg, owner: Owner):
        self.cfg = cfg['log']
        self.own = owner
        self._init()
        self.start()

    def reload(self):
        self._queue.put_nowait('reload')

    def join(self, timeout=30):
        self.log('stop.', INFO)
        self._queue.put_nowait(None)
        super().join(timeout=timeout)

    def run(self):
        while True:
            data = self._queue.get()
            if isinstance(data, tuple):
                self._best_print(*data)
            elif data is None:
                break
            elif data == 'reload':
                self._init()
            elif isinstance(data, list) and len(data) == 3 and data[0] == 'remote_log':
                self._add_connect(data[1], data[2])
            else:
                self.log('Wrong data: {}'.format(repr(data)), ERROR)
        self._close_connect()

    def permission_check(self):
        if not write_permission_check(self.cfg.get('file')):
            msg = 'Логгирование в {} невозможно - отсутствуют права на запись. Исправьте это'
            self.log(F(msg, self.cfg.get('file')), CRIT)
            return False
        return True

    def _init(self):
        self.file_lvl = get_loglvl(self.cfg.get('file_lvl', 'info'))
        self.print_lvl = get_loglvl(self.cfg.get('print_lvl', 'info'))
        self.in_print = self.cfg.get('method', 3) in [2, 3] and self.print_lvl <= CRIT
        in_file = self.cfg.get('method', 3) in [1, 3] and self.file_lvl <= CRIT

        if self.cfg['remote_log']:
            # Подписка
            self.own.subscribe(self.REMOTE_LOG, self._add_remote_log, self.CHANNEL)
        else:
            # Отписка
            self.own.unsubscribe(self.REMOTE_LOG, self._add_remote_log, self.CHANNEL)
            self._close_connect()

        if self._app_log:
            self._app_log.removeHandler(self._handler)
            self._app_log = None

        if self._handler:
            self._handler.close()
            self._handler = None

        if self.cfg.get('file') and in_file and self.permission_check():
            self._handler = RotatingFileHandler(filename=self.cfg.get('file'), maxBytes=1024 * 1024,
                                                backupCount=2, encoding='utf8',
                                                )
            self._handler.rotator = _rotator
            self._handler.namer = _namer
            self._handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            self._handler.setLevel(logging.DEBUG)

            self._app_log = logging.getLogger('logger')
            # Отключаем печать в консольку
            self._app_log.propagate = False
            self._app_log.setLevel(logging.DEBUG)
            self._app_log.addHandler(self._handler)

    def _add_remote_log(self, _, data, lock, conn):
        try:
            # Забираем сокет у сервера
            conn_ = conn.extract()
            if conn_:
                conn_.settimeout(None)
                self._queue.put_nowait(['remote_log', conn_, data])
        finally:
            lock()

    def _add_connect(self, conn, mode):
        self._close_connect()
        self._conn = conn
        self._remote_log_mode = mode if mode in REMOTE_LOG_MODE else REMOTE_LOG_DEFAULT
        self._conn.start_remote_log()
        self.log('OPEN REMOTE LOG FOR {}:{}'.format(self._conn.ip, self._conn.port), WARN)

    def _close_connect(self):
        if self._conn:
            try:
                msg = 'CLOSE REMOTE LOG, BYE.'
                if self._remote_log_mode == 'raw':
                    pass
                elif self._remote_log_mode == 'json':
                    msg = self._to_print_json(('Logger',), msg, _REMOTE, time.time())
                else:
                    msg = colored(msg, COLORS[INFO])
                self._conn.write(msg)
            except RuntimeError:
                pass
            try:
                self._conn.close()
            except RuntimeError:
                pass
            self.log('CLOSE REMOTE LOG FOR {}:{}'.format(self._conn.ip, self._conn.port), WARN)
            self._conn = None

    def add(self, name) -> _LogWrapper:
        return _LogWrapper(name, self._print)

    def _print(self, *args):
        self._queue.put_nowait((time.time(), *args))

    def _best_print(self, l_time: float, names: list, msg: str, lvl: int):
        names = tuple(names)
        if lvl not in COLORS:
            raise RuntimeError('Incorrect log level:{}'.format(lvl))
        print_line = None
        if self.in_print and lvl >= self.print_lvl:
            print_line = self._to_print(names, msg, lvl, l_time)
            print(print_line)
        if self._conn:
            if self._remote_log_mode == 'raw':
                print_line = self._to_print_raw(names, msg, lvl, l_time)
            elif self._remote_log_mode == 'json':
                print_line = self._to_print_json(names, msg, lvl, l_time)
            else:
                print_line = print_line or self._to_print(names, msg, lvl, l_time)
            self._to_remote_log(print_line)
        if self._app_log and lvl >= self.file_lvl:
            self._to_file(_name_builder(names), msg, lvl)

    def _to_file(self, name, msg, lvl):
        self._app_log.log(lvl, '{}: {}'.format(name, msg))

    def _str_time(self, l_time: float) -> str:
        time_str = time.strftime('%Y.%m.%d %H:%M:%S', time.localtime(l_time))
        if self.cfg['print_ms']:
            time_str += '.{:03d}'.format(int(l_time * 1000 % 1000))
        return time_str

    def _to_print(self, names: tuple, msg: str, lvl: int, l_time: float) -> str:
        str_time = self._str_time(l_time)
        return '{} {}: {}'.format(str_time, _name_builder(names, True), colored(msg, COLORS[lvl]))

    def _to_print_raw(self, names: tuple, msg: str, lvl: int, l_time: float) -> str:
        return '{} {} {}: {}'.format(self._str_time(l_time), LVL_NAME[lvl], _name_builder(names), msg)

    @staticmethod
    def _to_print_json(names: tuple, msg: str, lvl: int, l_time: float) -> str:
        return json.dumps({'lvl': LVL_NAME[lvl], 'time': l_time, 'callers': names, 'msg': msg}, ensure_ascii=False)

    def _to_remote_log(self, line: str):
        if self._conn:
            try:
                self._conn.write(line)
            except RuntimeError:
                self._close_connect()
