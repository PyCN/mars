# Copyright 1999-2018 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import logging
import os
import sys

from .actors import create_actor_pool
from .config import options
from .errors import StartArgumentError
from .lib.tblib import pickling_support
from .utils import get_next_port

pickling_support.install()
logger = logging.getLogger(__name__)

try:
    from pytest_cov.embed import cleanup_on_sigterm
    cleanup_on_sigterm()
except ImportError:  # pragma: no cover
    pass


class BaseApplication(object):
    """
    :type pool mars.actors.pool.gevent_pool.ActorContext
    """
    service_description = ''
    service_logger = logger

    def __init__(self):
        self.args = None
        self.endpoint = None
        self.pool = None
        self.n_process = None

        self._running = False

    def __call__(self, argv=None):
        import json

        if argv is None:
            argv = sys.argv[1:]
        new_argv = []
        for a in argv:
            if not a.startswith('-D'):
                new_argv.append(a)
                continue
            conf, val = a[2:].split('=', 1)
            conf_parts = conf.split('.')
            conf_obj = options
            for g in conf_parts[:-1]:
                conf_obj = getattr(conf_obj, g)
            try:
                setattr(conf_obj, conf_parts[-1], json.loads(val))
            except:
                setattr(conf_obj, conf_parts[-1], val)

        return self._main(new_argv)

    def _main(self, argv=None):
        parser = argparse.ArgumentParser(description=self.service_description)
        parser.add_argument('-a', '--advertise', help='advertise ip')
        parser.add_argument('-k', '--kv-store', help='address of kv store service, '
                                                     'for instance, etcd://localhost:4001')
        parser.add_argument('-e', '--endpoint', help='endpoint of the service')
        parser.add_argument('-s', '--schedulers', help='endpoint of scheduler, when single scheduler '
                                                       'and etcd is not available')
        parser.add_argument('-H', '--host', help='host of the scheduler service, only available '
                                                 'when `endpoint` is absent')
        parser.add_argument('-p', '--port', help='port of the scheduler service, only available '
                                                 'when `endpoint` is absent')
        parser.add_argument('--level', help='log level')
        parser.add_argument('--format', help='log format')
        parser.add_argument('--log_conf', help='log config file')
        parser.add_argument('--inspect', help='inspection endpoint')
        parser.add_argument('--load-modules', nargs='*', help='modules to import')
        self.config_args(parser)
        args = parser.parse_args(argv)
        self.args = args

        endpoint = args.endpoint
        host = args.host
        port = args.port
        options.kv_store = args.kv_store if args.kv_store else options.kv_store

        load_modules = []
        for mod in args.load_modules or ():
            load_modules.extend(mod.split(','))
        if not args.load_modules:
            load_module_str = os.environ.get('MARS_LOAD_MODULES')
            if load_module_str:
                load_modules = load_module_str.split(',')
        load_modules.append('mars.tensor')
        for m in load_modules:
            __import__(m, globals(), locals(), [])
        self.service_logger.info('Modules %s loaded', ','.join(load_modules))

        self.n_process = 1

        self.config_service()
        self.config_logging()

        if not host:
            host = args.advertise or '0.0.0.0'
        if not endpoint and port:
            endpoint = host + ':' + port

        try:
            self.validate_arguments()
        except StartArgumentError as ex:
            parser.error('Failed to start application: %s' % ex)

        if getattr(self, 'require_pool', True):
            self.endpoint, self.pool = self._try_create_pool(endpoint=endpoint, host=host, port=port)
        self.service_logger.info('%s started at %s.', self.service_description, self.endpoint)
        self.main_loop()

    def config_logging(self):
        import logging.config
        log_conf = self.args.log_conf or 'logging.conf'

        conf_file_paths = [
            '', os.path.abspath('.'),
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ]
        log_configured = False
        for path in conf_file_paths:
            conf_path = log_conf
            if path:
                conf_path = os.path.join(conf_path)
            if os.path.exists(conf_path):
                logging.config.fileConfig(conf_path, disable_existing_loggers=False)
                log_configured = True

        if not log_configured:
            if not self.args.level:
                level = logging.INFO
            else:
                level = getattr(logging, self.args.level.upper())
            logging.getLogger('mars').setLevel(level)
            logging.basicConfig(format=self.args.format)

    def validate_arguments(self):
        pass

    def _try_create_pool(self, endpoint=None, host=None, port=None):
        pool = None
        if endpoint:
            pool = self.create_pool(address=endpoint)
        else:
            use_port = None
            retrial = 5
            while use_port is None:
                use_port = port or get_next_port()
                try:
                    endpoint = '{0}:{1}'.format(host, use_port)
                    pool = self.create_pool(address=endpoint)
                    break
                except:
                    retrial -= 1
                    if retrial == 0:
                        raise

                    if port is None:
                        use_port = None
                    else:
                        raise
        return endpoint, pool

    def create_pool(self, *args, **kwargs):
        kwargs.update(dict(n_process=self.n_process, backend='gevent'))
        return create_actor_pool(*args, **kwargs)

    def main_loop(self):
        try:
            with self.pool:
                try:
                    self.start()
                    self._running = True
                    while True:
                        self.pool.join(1)
                        stopped = []
                        for idx, proc in enumerate(self.pool.processes):
                            if not proc.is_alive():
                                stopped.append(idx)
                        if stopped:
                            self.handle_process_down(stopped)
                finally:
                    self.stop()
        finally:
            self._running = False

    def handle_process_down(self, proc_indices):
        """
        Handle process down event, the default action is to quit
        the whole application. Applications can inherit this method
        to do customized process-level failover.

        :param proc_indices: indices of processes (not pids)
        """
        for idx in proc_indices:
            proc = self.pool.processes[idx]
            self.service_logger.fatal(
                'Process %d exited unpredictably. exitcode=%d', proc.pid, proc.exitcode)
        raise KeyboardInterrupt

    def config_service(self):
        pass

    def config_args(self, parser):
        raise NotImplementedError

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError
