#!/usr/bin/env python3

import configparser
import os
import platform
import shutil
import time

import languages
import logger
import utils
from languages import F, LANG_CODE
from lib import volume, detectors
from lib.audio_utils import APMSettings
from lib.ip_storage import make_interface_storage
from lib.keys_utils import Keystore
from lib.map_settings.wiki_parser import WikiParser
from lib.models_storage import ModelsStorage
from lib.proxy import proxies
from lib.state_helper import state_helper
from lib.tools.config_updater import ConfigUpdater
from owner import Owner

DATA_FORMATS = {'json': '.json', 'yaml': '.yml'}


class DummyOwner:
    def __init__(self):
        self._info = []
        self._say = []

    def say(self, msg: str, lvl: int = 0, alarm=None, wait=0, is_file: bool = False, blocking: int = 0):
        self._say.append((msg, lvl, alarm, wait, is_file, blocking))

    def say_info(self, msg: str, lvl: int = 0, alarm=None, wait=0, is_file: bool = False):
        self._info.append((msg, lvl, alarm, wait, is_file))

    def replacement(self, own: Owner):
        # Произносим накопленные фразы
        for info in self._info:
            own.say_info(*info)
        for say in self._say:
            own.say(*say)


class ConfigHandler(utils.HashableDict):
    def __init__(self, cfg: dict, state: dict, path: dict, log, owner: Owner):
        super().__init__()
        self._start_time = time.time()
        self._plugins_api = state['system'].pop('PLUGINS_API', 0)
        self._version_info = state['system'].pop('VERSION', (0, 0, 0))
        self.state, save_ini, save_state = state_helper(state, path, log.add('SH'))
        self.platform = platform.system().capitalize()
        self.detector = None
        self.models = ModelsStorage()
        self._save_me_later = False
        self._allow_addresses = []
        self.update(cfg)
        self.path = path
        self.__owner = owner
        self.own = DummyOwner()  # Пока player нет храним фразы тут.
        self.log = log
        languages.set_lang.set_logger(self.log.add('Localization'))
        self._config_init(save_ini)
        if save_state:
            self.save_state()

    @property
    def uptime(self) -> int:
        return int(time.time() - self._start_time)

    @property
    def API(self):
        return self._plugins_api

    @property
    def version_info(self) -> tuple:
        return self._version_info

    @property
    def ini_version(self) -> int:
        return self.state['system']['ini_version']

    @property
    def version_str(self) -> str:
        return '.'.join(map(str, self._version_info))

    @property
    def wiki_desc(self) -> dict:
        return WikiParser(self, self.log.add('Wiki')).get()

    def key(self, prov, api_key):
        if prov == 'aws':
            return self._aws_credentials()
        key_ = self.gt(prov, api_key)
        if prov == 'azure':
            return Keystore().azure(key_, self.gt('azure', 'region'))
        if prov == 'yandex' and not key_ and self.yandex_api(prov) == 1:
            # Будем брать ключ у транслита для старой версии
            try:
                key_ = Keystore().yandex_v1_free()
            except RuntimeError as e:
                raise RuntimeError(F('Ошибка получения ключа для Yandex: {}', e))
        return key_

    def _aws_credentials(self):
        return (
            (self.gt('aws', 'access_key_id'), self.gt('aws', 'secret_access_key'), self.gt('aws', 'region')),
            self.gt('aws', 'boto3')
        )

    @staticmethod
    def language_name() -> str:
        return languages.set_lang.lang

    @staticmethod
    def tts_lang(provider: str) -> str:
        name = '{}_tts'.format(provider)
        if name in LANG_CODE:
            return LANG_CODE[name]
        elif provider == 'google':
            return LANG_CODE['ISO']
        elif provider == 'aws':
            return LANG_CODE['aws']
        else:
            return LANG_CODE['IETF']

    @staticmethod
    def stt_lang(provider: str) -> str:
        name = '{}_stt'.format(provider)
        if name in LANG_CODE:
            return LANG_CODE[name]
        else:
            return LANG_CODE['IETF']

    def yandex_api(self, prov):
        return self.gt(prov, 'api', 1) if prov == 'yandex' else 1

    def gt(self, sec, key, default=None):
        # .get для саб-словаря
        return self.get(sec, {}).get(key, default)

    def gts(self, key, default=None):
        # .get из 'settings'
        return self['settings'].get(key, default)

    def get_uint(self, key: str, default=0) -> int:
        try:
            result = int(self.gts(key, default))
        except ValueError:
            result = 0
        else:
            if result < 0:
                result = 0
        return result

    def _path_check(self):
        for dir_ in ('resources', 'data', 'plugins', 'models', 'samples', 'backups'):
            self._make_dir(self.path[dir_])
        for file in ('ding', 'dong', 'bimp'):
            self._lost_file(self.path[file])

    def select_hw_detector(self):
        if not self['listener']['detector']:
            name = 'snowboy' if self.platform == 'Linux' else None
            name = 'porcupine' if self._porcupine_allow() else name
        else:
            name = self['listener']['detector']
        self.detector = detectors.detector(name, self.path['home'])
        is_broken = self.detector.NAME not in detectors.DETECTORS
        if is_broken:
            msg = 'Unrecognized hotword detector \'{}\', terminal won\'t work correctly!'.format(name)
            self.log(msg, logger.WARN)
        else:
            self.log('Hotword detection: {}'.format(self.detector), logger.INFO)
        self.models_load()

    def _porcupine_allow(self) -> bool:
        try:
            return detectors.porcupine_select_auto(self.path['home'])
        except RuntimeError as e:
            self.log('Porcupine broken: {}'.format(e), logger.WARN)
        return False

    def allow_connect(self, ip: str) -> bool:
        if ip not in self._allow_addresses:
            return False
        if ip == '127.0.0.1':
            return True
        if not self['smarthome'].get('ip') and self.gts('first_love'):
            self['smarthome']['ip'] = ip
            self.config_save()
        if self.gts('last_love') and ip != self['smarthome'].get('ip'):
            return False
        return True

    def is_testfile_name(self, filename: str) -> bool:
        return utils.is_valid_base_filename(filename) and \
               os.path.splitext(filename)[1].lower() == self.path['test_ext']

    def path_to_sample(self, model_id: str, sample_num) -> str:
        return os.path.join(self.path['samples'], model_id, '{}.wav'.format(sample_num))

    def remove_samples(self, model_id: str):
        if not model_id:
            raise RuntimeError('model_id empty')
        target = os.path.join(self.path['samples'], model_id)
        if not os.path.isdir(target):
            raise RuntimeError('{} not a directory'.format(target))
        try:
            shutil.rmtree(target)
        except Exception as e:
            raise RuntimeError(e)

    def _config_init(self, save_ini):
        self._cfg_check(self.config_load() or save_ini)
        self._path_check()
        self.tts_cache_check()

        self.proxies_init()
        self.apm_configure()
        self.select_hw_detector()

        self.allow_addresses_init()
        self._say_ip()

    def allow_addresses_init(self):
        ips = self['smarthome']['allow_addresses']
        try:
            self._allow_addresses = make_interface_storage(ips)
            msg = str(self._allow_addresses)
        except RuntimeError as e:
            self._allow_addresses = []
            msg = 'NONE'
            wrong = '[smarthome] allow_addresse = {}'.format(ips)
            self.log('Wrong value {}: {}'.format(repr(wrong), e), logger.WARN)
        self.log('Allow IP addresses: {}'.format(msg), logger.INFO)

    def proxies_init(self):
        proxies.configure(self.get('proxy', {}))

    def apm_configure(self):
        APMSettings().cfg(**self['noise_suppression'])

    def _cfg_check(self, to_save=False):
        for key in ['providerstt', 'providerstt']:
            to_save |= self._cfg_dict_checker(self.gts(key))
        to_save |= self._log_file_init()
        to_save |= self._tts_cache_path_check()
        to_save |= self._init_volume()
        to_save |= self._first()
        if to_save:
            self.config_save()

    def _init_volume(self):
        if self.gt('volume', 'line_out'):
            return False
        self['volume']['card'], self['volume']['line_out'] = volume.extract_volume_control()
        return len(self['volume']['line_out']) > 0

    def _tts_cache_path_check(self):
        to_save = False
        if not self['cache']['path']:
            # ~/tts_cache/
            self['cache']['path'] = os.path.join(self.path['home'], 'tts_cache')
            to_save = True
        self._make_dir(self['cache']['path'])
        return to_save

    def _log_file_init(self):  # Выбираем доступную для записи директорию для логов
        if self['log']['file']:
            return False

        file = 'mdmterminal.log'
        for path in ('/var/log', self.path['home'], self.path['tmp']):
            target = os.path.join(path, file)
            if utils.write_permission_check(target):
                break
        self['log']['file'] = target
        return True

    def _cfg_dict_checker(self, key: str):
        if key and (key not in self or type(self[key]) != dict):
            self[key] = {}
            return True
        return False

    def get_allow_models(self) -> list:
        return utils.str_to_list(self.gt('models', 'allow'))

    def get_all_models(self) -> list:
        files = self['models'].keys() if self.detector.FAKE_MODELS else os.listdir(self.path['models'])
        return [file for file in files if self.detector.is_model_name(file)]

    def get_all_testfile(self) -> list:
        if not os.path.isdir(self.path['test']):
            return []
        return [file for file in os.listdir(self.path['test']) if self.is_testfile_name(file)]

    def save_dict(self, name: str, data: dict, pretty=False, format_='json') -> bool:
        file_path = os.path.join(self.path['data'], name + DATA_FORMATS.get(format_, '.json'))
        try:
            utils.dict_to_file(file_path, data, pretty)
        except RuntimeError as e:
            self.log(F('Ошибка сохранения {}: {}', file_path, str(e)), logger.ERROR)
            return False
        return True

    def load_dict(self, name: str, format_='json') -> dict or None:
        file_path = os.path.join(self.path['data'], name + DATA_FORMATS.get(format_, '.json'))
        if not os.path.isfile(file_path):
            self.log(F('Файл не найден (это нормально): {}', file_path))
            return None
        try:
            return utils.dict_from_file(file_path)
        except RuntimeError as e:
            self.log(F('Ошибка загрузки {}: {}', file_path, str(e)), logger.ERROR)
            return None

    def save_state(self):
        wtime = time.time()
        utils.dict_to_file(self.path['state'], self.state, True)
        self.log('Save state in {}'.format(utils.pretty_time(time.time() - wtime)))

    def start(self):
        self.own, dummy = self.__owner, self.own
        del self.__owner
        dummy.replacement(self.own)

    def config_save(self, final=False, forced=False):
        if final:
            if self._save_me_later:
                self._save_me_later = False
                self._config_save()
        elif self.gts('lazy_record') and not forced:
            self._save_me_later = True
        else:
            self._save_me_later = False
            self._config_save()

    def _config_save(self):
        wtime = time.time()

        config = ConfigParserOnOff()
        for key, val in self.items():
            if isinstance(val, dict):
                config[key] = val

        with open(self.path['settings'], 'w', encoding='utf8') as configfile:
            config.write(configfile)
        self.log(F('Конфигурация сохранена за {}', utils.pretty_time(time.time() - wtime)), logger.INFO)
        self.own.say_info(F('Конфигурация сохранена!'))

    def models_load(self):
        def lower_warning():
            if self.detector.FAKE_MODELS:
                return
            l_name_ = file.lower()
            if l_name_ != file:
                msg_ = 'Please, rename {} to {} in {} for stability!'.format(
                    repr(file), repr(l_name_), self.path['models']
                )
                self.log(msg_, logger.WARN)

        models, paths, msg = [], [], None
        if self.detector.NO_MODELS:
            pass
        elif not (os.path.isdir(self.path['models']) or self.detector.FAKE_MODELS):
            msg = F('Директория с моделями не найдена {}', self.path['models'])
        else:
            allow = self.get_allow_models()
            for file in self.get_all_models():
                full_path = file if self.detector.FAKE_MODELS else os.path.join(self.path['models'], file)
                if self.detector.FAKE_MODELS or os.path.isfile(full_path):
                    lower_warning()
                    if not allow or file in allow:
                        paths.append(full_path)
                        models.append(file)

        self.models = ModelsStorage(paths, self['models'], models, no_models=self.detector.NO_MODELS)
        msg = msg or F('Загружено {} моделей', len(self.models))
        self.log(msg, logger.INFO)
        self.own.say_info(msg)

    def config_load(self):
        wtime = time.time()
        if not os.path.isfile(self.path['settings']):
            msg = 'Файл настроек не найден по пути {}. Для первого запуска это нормально'
            self.log(F(msg, self.path['settings']), logger.INFO)
            return True
        updater = ConfigUpdater(self, self.log)
        count = updater.from_ini(self.path['settings'])
        wtime = time.time() - wtime
        self.lang_init()
        self.log(F('Загружено {} опций за {}', count, utils.pretty_time(wtime)), logger.INFO)
        self.own.say_info(F('Конфигурация загружена!'))
        return updater.save_ini

    def lang_init(self):
        lang = self.gts('lang')
        err = languages.set_lang(lang, self.gts('lang_check'))
        if err:
            self.log(F('Ошибка инициализации языка {}: {}', repr(lang), err), logger.ERROR)
        msg = F('Локализация {} загружена за {}', lang, utils.pretty_time(languages.set_lang.load_time))
        self.log(msg, logger.INFO)

    def update_from_external(self, data: str or dict) -> dict or None:
        cu = ConfigUpdater(self, self.log)
        if isinstance(data, str):
            result = cu.from_json(data)
        elif isinstance(data, dict):
            result = cu.from_external_dict(data)
        else:
            self.log('Unknown settings type: {}'.format(type(data)), logger.ERROR)
            return None
        if result:
            return cu.diff
        else:
            return None

    def print_cfg_change(self):
        self.log(F('Конфигурация изменилась'))

    def print_cfg_no_change(self):
        self.log(F('Конфигурация не изменилась'))

    def update_from_dict(self, data: dict) -> bool:
        return self._cfg_update(ConfigUpdater(self, self.log).from_dict(data))

    def _cfg_update(self, result: int):
        if result:
            self.config_save()
            return True
        return False

    def tts_cache_check(self):
        min_file_size = 1024
        max_size = self['cache'].get('tts_size', 50) * 1024 * 1024
        cache_path = self.gt('cache', 'path')
        if not os.path.isdir(cache_path):
            msg = F('Директория c tts кэшем не найдена {}', cache_path)
            self.log(msg)
            self.own.say_info(msg)
            return
        current_size = 0
        files, wrong_files = [], []
        # Формируем список из пути и размера файлов, заодно считаем общий размер.
        # Файлы по 1 KiB считаем поврежденными и удалим в любом случае
        for file in os.listdir(cache_path):
            pfile = os.path.join(cache_path, file)
            if os.path.isfile(pfile):
                fsize = os.path.getsize(pfile)
                if fsize > min_file_size:
                    current_size += fsize
                    files.append([pfile, fsize])
                else:
                    wrong_files.append(pfile)

        # Удаляем поврежденные файлы
        if wrong_files:
            for file in wrong_files:
                os.remove(file)
            wrong_files = [os.path.split(file)[1] for file in wrong_files]
            self.log(F('Удалены поврежденные файлы: {}', ', '.join(wrong_files)), logger.WARN)

        normal_size = not files or current_size < max_size or max_size < 0
        say = F('Размер tts кэша {}: {}', utils.pretty_size(current_size), F('Ок.') if normal_size else F('Удаляем...'))
        self.log(say, logger.INFO)

        if normal_size:
            return
        self.own.say_info(say)

        new_size = int(max_size * 0.7)
        deleted_files = 0
        # Сортируем файлы по дате последнего доступа
        files.sort(key=lambda x: os.path.getatime(x[0]))
        deleted = []
        for file in files:
            if current_size <= new_size:
                break
            current_size -= file[1]
            os.remove(file[0])
            deleted_files += 1
            deleted.append(os.path.split(file[0])[1])
        self.log(F('Удалено: {}', ', '.join(deleted)))
        msg = F('Удалено {} файлов. Новый размер TTS кэша {}', deleted_files, utils.pretty_size(current_size))
        self.log(msg, logger.INFO)
        self.own.say_info(msg)

    def _make_dir(self, path: str):
        if not os.path.isdir(path):
            self.log(F('Директория {} не найдена. Создаю...', path), logger.INFO)
            os.makedirs(path)

    def _lost_file(self, path: str):
        if not os.path.isfile(path):
            msg = '{} {}'.format(F('Файл {} не найден.', path), F('Это надо исправить!'))
            self.log(msg, logger.CRIT)
            self.own.say(msg)

    def _first(self):
        if not self.gts('ip'):
            self['settings']['ip'] = utils.get_ip_address()
            return True
        return False

    def _say_ip(self):
        if not (self['smarthome']['outgoing_socket'] or self['smarthome']['ip']):
            msg = F('Терминал еще не настроен, мой IP адрес: {}', self.gts('ip'))
            self.log(msg, logger.WARN)
            self.own.say(msg)


class ConfigParserOnOff(configparser.ConfigParser):
    """bool (True/False) -> (on/off)"""
    def read_dict(self, dictionary, source='<dict>'):
        """Read configuration from a dictionary.

        Keys are section names, values are dictionaries with keys and values
        that should be present in the section. If the used dictionary type
        preserves order, sections and their keys will be added in order.

        All types held in the dictionary are converted to strings during
        reading, including section names, option names and keys.

        Optional second argument is the `source' specifying the name of the
        dictionary being read.
        """
        elements_added = set()
        for section, keys in dictionary.items():
            section = str(section)
            try:
                self.add_section(section)
            except (configparser.DuplicateSectionError, ValueError):
                if self._strict and section in elements_added:
                    raise
            elements_added.add(section)
            for key, value in keys.items():
                key = self.optionxform(str(key))
                if value is not None:
                    if isinstance(value, bool):
                        value = 'on' if value else 'off'
                    else:
                        value = str(value)
                if self._strict and (section, key) in elements_added:
                    raise configparser.DuplicateOptionError(section, key, source)
                elements_added.add((section, key))
                self.set(section, key, value)
