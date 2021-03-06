#!/usr/bin/env python

"""Script to store configuration and interfaces into the directory and resource registry"""

__author__ = 'Seman Said, Michael Meisinger'

from copy import deepcopy
import os
from collections import OrderedDict

from pyon.core.path import list_files_recursive
from pyon.datastore.datastore_common import DatastoreFactory
from pyon.ion.directory_standalone import DirectoryStandalone
from pyon.ion.resregistry_standalone import ResourceRegistryStandalone
from pyon.util.containers import get_safe
from pyon.util.log import log


class InterfaceAdmin:
    """
    Administrates work with interfaces in the datastore
    """

    DIR_RESFILES_PATH = "/Config"
    DIR_CONFIG_PATH = "/Config"

    def __init__(self, sysname, config=None):
        self._closed = False
        self.sysname = sysname
        self.config = config
        self.dir = DirectoryStandalone(sysname=self.sysname, config=self.config)
        self.rr = ResourceRegistryStandalone(sysname=self.sysname, config=self.config)

    def close(self):
        self.dir.close()
        self.dir = None
        self.rr.close()
        self.rr = None
        self._closed = True

    def __del__(self):
        if not self._closed:
            log.warn("WARNING: Call close() on InterfaceAdmin (and datastores)")

    def set_config(self, config):
        self.config = config

    def system_data_exists(self):
        """ Returns True if there is persistent system data in the database - this may be a restart. """
        actors = self.rr.find_by_type("ActorIdentity", id_only=True, limit=1)
        return bool(actors)

    def create_core_datastores(self):
        """ Create core datastores, so that they exist when containers start concurrently. """
        ds = DatastoreFactory.get_datastore(config=self.config, scope=self.sysname, variant=DatastoreFactory.DS_BASE)
        datastores = [
            ('resources', 'RESOURCES'),
            ('events', 'EVENTS'),
            ('state', 'STATE'),
            ('objects', 'OBJECTS'),
        ]
        count = 0
        ds_created = []
        for local_dsn, profile in datastores:
            if not ds.datastore_exists(local_dsn):
                ds.create_datastore(datastore_name=local_dsn, profile=profile)
                count += 1
                ds_created.append(local_dsn)
        if count:
            log.info("store_interfaces: Created %s datastores: %s" % (count, ds_created))


    def store_config(self, system_cfg):
        """
        Store system config in directory and setup basic directory structure, so that everything is
        prepared when containers start concurrently.
        """
        # Register some default keys
        self.dir.register_safe("/", "DIR", description="Directory root", sys_name=self.sysname)
        self.dir.register_safe("/", "Agents", description="Running agents", create_only=True)
        self.dir.register_safe("/", "Config", description="System configuration", create_only=True)
        self.dir.register_safe("/", "System", description="System management information", create_only=True)
        self.dir.register_safe("/System", "Locks", description="System exclusive locks", create_only=True)

        if get_safe(system_cfg, "bootstrap.store_config") is True:
            de = self.dir.lookup(self.DIR_CONFIG_PATH + "/CFG")
            if de:
                log.debug("store_interfaces: Updating system config in directory...")
            else:
                log.info("store_interfaces: Storing system config in directory...")
            self.dir.register(self.DIR_CONFIG_PATH, "CFG", **deepcopy(system_cfg))

    def store_interfaces(self, object_definition_file=None, service_definition_file=None, idempotent=True):
        """
        Store system interfaces (services, objects, config files), so that everything is
        prepared when containers start concurrently.
        """
        self.idempotent = idempotent
        self.bulk_entries = {}
        self.bulk_resources = []
        self.serial_num = 1

        if object_definition_file:
            # Update single object definition
            self.store_object_interfaces(file=object_definition_file)
        elif service_definition_file:
            # Update single service definition
            self.store_service_interfaces(file=service_definition_file)
        else:
            # Update all interfaces
            if self.idempotent:
                de = self.rr.find_by_type("ServiceDefinition", id_only=True)
                if de:
                    log.debug("store_interfaces: Interfaces already stored. Not updating.")
                    return
            # load all files
            self.store_object_interfaces()
            self.store_service_interfaces()
            #self.store_config_files()

        self._register_bulk()

    def store_object_interfaces(self, file=None):
        # log.info("Storing object interfaces in datastore...")
        if file and os.path.exists(file):
            self._load_object_files([file])
        elif file:
            log.warn("store_interfaces: Error couldn't find the file path")
        else:
            data_yaml_filenames = list_files_recursive('obj/data', '*.yml', ['ion.yml', 'resource.yml', 'shared.yml'])
            self._load_object_files(data_yaml_filenames)

    def _load_object_files(self, filenames):
        serial_num = 1
        for file_path in filenames:
            objs = OrderedDict()
            class_content = ''

            # Open and read data model definition file
            with open(file_path, 'r') as f:
                file_content_str = f.read()

            first_time = True
            for line in file_content_str.split('\n'):
                if len(line) > 0 and (line[0].isalpha()):
                    if not first_time:
                        objs[classname] = class_content
                        class_content = ""
                    else:
                        first_time = False
                    classname = line.split(':')[0]
                    class_content += line + "\n"
                else:
                    class_content += line + "\n"
            objs[classname] = class_content

            for key in objs.keys():
                obj_type = self._create_object_type(key, objs[key], serial_num)
                #self.bulk_resources.append(obj_type)
                serial_num += 1

    def store_service_interfaces(self, file=None):
        # log.info("Storing service interfaces in datastore...")
        if file and os.path.exists(file):
            self._load_service_files([file])
        elif file:
            log.warn("store_interfaces: Error couldn't find the file path")
        else:
            service_yaml_filenames = list_files_recursive('obj/services', '*.yml')
            self._load_service_files(service_yaml_filenames)

    def _load_service_files(self, filenames):
        for file_path in filenames:
            objs = {}
            with open(file_path, 'r') as f:
                file_content_str = f.read()
            for line in file_content_str.split('\n'):
                if line[:5] == "name:":
                    key = line.split()[1]
                    objs[key] = file_content_str

            if not objs:
                log.error("=====ERROR===== Can't find object name for: %s", file_path)

            for key in objs.keys():
                svc_def = self._create_service_definition(key, objs[key], file_path)
                self.bulk_resources.append(svc_def)

    def store_config_files(self):
        # log.info("Storing system res files in datastore...")
        resource_filenames = list_files_recursive('res/config', '*.yml')
        self._load_config_files(resource_filenames)

    def _load_config_files(self, filenames):
        for file_path in filenames:
            with open(file_path, 'r') as f:
                file_content_str = f.read()
            objs = {}
            objs[os.path.basename(file_path)] = file_content_str

            for key in objs.keys():
                self.bulk_entries[(self.DIR_RESFILES_PATH, key)] = dict(file_path=file_path, definition=objs[key])

    def _create_object_type(self, name, definition, definition_order):
        return dict(type_="ObjectType", name=name, definition=definition, definition_order=definition_order)

    def _create_service_definition(self, name, definition, namespace):
        return dict(type_="ServiceDefinition", name=name, definition=definition, namespace=namespace)

    def _register_bulk(self):
        if self.bulk_entries:
            log.info("store_interfaces: Storing %s entries in directory..." % len(self.bulk_entries))
            entries = [(path, key, attrs) for ((path, key), attrs) in self.bulk_entries.iteritems()]
            res = self.dir.register_mult(entries)
            self.bulk_entries = {}

        if self.bulk_resources:
            log.info("store_interfaces: Storing %s resources in registry..." % len(self.bulk_resources))
            res = self.rr.create_mult(self.bulk_resources)
            self.bulk_resources = []

        log.debug("store_interfaces: Storing interfaces successful")

    def declare_core_exchange_resources(self):
        """
        Create important messaging resources (queues, bindings, etc), so that they are ready before
        the first containers start and processes get spawned concurrently.
        """
        from putil.rabbitmq.rabbit_util import RabbitManagementUtil
        rabbit_util = RabbitManagementUtil(self.config, sysname=self.sysname)

        # Event persister queue
        rabbit_util.declare_exchange("events")
        rabbit_util.declare_queue("events", "event_persister")
        rabbit_util.bind_queue("events", "event_persister", "#")
