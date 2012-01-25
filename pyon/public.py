#!/usr/bin/env python

"""Entry point for importing common Pyon/ION packages. Most files should only need to import from here."""

__author__ = 'Adam R. Smith, Michael Meisinger'
__license__ = 'Apache 2.0'

__all__ = []


# Tell the magic import log setup to pass through this file
from pyon.util.log import import_paths
import_paths.append(__name__)

from pyon.util.log import log
__all__ += ['log']

from pyon.core.bootstrap import CFG, obj_registry, IonObject, sys_name
__all__ += ['CFG', 'obj_registry', 'IonObject', 'sys_name']

from pyon.util.async import spawn, switch
__all__ += ['spawn', 'switch']

from pyon.core.process import PyonProcessError, GreenProcess, GreenProcessSupervisor, PythonProcess
__all__ += ['PyonProcessError', 'GreenProcess', 'GreenProcessSupervisor', 'PythonProcess']

from pyon.core import exception as iex
__all__ += ['iex']

from pyon.net import messaging, channel, endpoint
__all__ += ['messaging', 'channel', 'endpoint']

from pyon.ion.process import IonProcessSupervisor
__all__ += ['IonProcessSupervisor']

from pyon.container.cc import Container
__all__ += ['Container']

from pyon.service.service import BaseService
__all__ += ['BaseService']

from pyon.ion.endpoint import ProcessRPCClient, ProcessRPCServer, StreamPublisher, StreamSubscriber, \
                                ProcessSubscriber, ProcessPublisher
__all__ += ['ProcessRPCClient', 'ProcessRPCServer', 'StreamPublisher', 'StreamSubscriber',
            'ProcessSubscriber', 'ProcessPublisher']

from pyon.ion.resource import ResourceTypes, RT, PredicateType, PRED, AT, LifeCycleStates, LCS, LCE
__all__ += ['RT', 'PRED', 'AT', 'LCS', 'LCE']
__all__ += ['ResourceTypes', 'PredicateType', 'LifeCycleStates']

from pyon.ion.streamproc import StreamProcess
__all__ += ['StreamProcess']
