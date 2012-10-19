#!/usr/bin/env python

__author__ = 'Adam R. Smith, Michael Meisinger, Dave Foster <dfoster@asascience.com>'
__license__ = 'Apache 2.0'

from pyon.util.log import log
from pyon.core.thread import PyonThreadManager, PyonThread, ThreadManager, PyonThreadTraceback, PyonHeartbeatError
from pyon.service.service import BaseService
from gevent.event import Event, waitall, AsyncResult
from gevent.queue import Queue
from gevent import greenlet
from pyon.util.async import spawn
from pyon.core.exception import IonException, ContainerError
from pyon.util.containers import get_ion_ts
from pyon.core.bootstrap import CFG
import threading
import traceback

class OperationInterruptedException(BaseException):
    """
    Interrupted exception. Used by external items timing out execution in the
    IonProcessThread's control thread.

    Derived from BaseException to specifically avoid try/except Exception blocks,
    such as in Publisher's publish_event.
    """
    pass

class IonProcessThread(PyonThread):
    """
    Form the base of an ION process.
    """

    def __init__(self, target=None, listeners=None, name=None, service=None, cleanup_method=None, heartbeat_secs=10, **kwargs):
        """
        Constructs an ION process.

        You don't create one of these directly, the IonProcessThreadManager run by a container does this for
        you via the ProcManager interface. Call the container's spawn_process method and this method will run.

        @param  target          A callable to run in the PyonThread. If None (typical), will use the target method
                                defined in this class.
        @param  listeners       A list of listening endpoints attached to this thread.
        @param  name            The name of this ION process.
        @param  service         An instance of the BaseService derived class which contains the business logic for
                                an ION process.
        @param  cleanup_method  An optional callable to run when the process is stopping. Runs after all other
                                notify_stop calls have run. Should take one param, this instance.
        @param  heartbeat_secs  Number of seconds to wait in between heartbeats.
        """
        self._startup_listeners = listeners or []
        self.listeners          = []
        self._listener_map      = {}
        self.name               = name
        self.service            = service
        self._cleanup_method    = cleanup_method

        self.thread_manager     = ThreadManager(failure_notify_callback=self._child_failed) # bubbles up to main thread manager
        self._ctrl_thread       = None
        self._ctrl_queue        = Queue()
        self._ready_control     = Event()
        self._errors            = []
        self._ctrl_current      = None      # set to the AR generated by _routing_call when in the context of a call

        # processing vs idle time (ms)
        self._start_time        = None
        self._proc_time         = 0

        # for heartbeats, used to detect stuck processes
        self._heartbeat_secs    = heartbeat_secs    # amount of time to wait between heartbeats
        self._heartbeat_stack   = None              # stacktrace of last heartbeat
        self._heartbeat_time    = None              # timestamp of heart beat last matching the current op
        self._heartbeat_op      = None              # last operation (by AR)
        self._heartbeat_count   = 0                 # number of times this operation has been seen consecutively

        PyonThread.__init__(self, target=target, **kwargs)

    def heartbeat(self):
        """
        Returns a tuple indicating everything is ok.

        Should only be called after the process has been started.
        Checks the following:
            - All attached endpoints are alive + listening
            - The control flow greenlet is alive + listening or processing

        @return 3-tuple indicating (listeners ok, ctrl thread ok, heartbeat status). Use all on it for a
                boolean indication of success.
        """
        listeners_ok = True
        for l in self.listeners:
            if not (l in self._listener_map and not self._listener_map[l].proc.dead):
                listeners_ok = False

        ctrl_thread_ok = self._ctrl_thread.running

        # are we currently processing something?
        heartbeat_ok = True
        if self._ctrl_current is not None:
            st = traceback.extract_stack(self._ctrl_thread.proc.gr_frame)

            if self._ctrl_current == self._heartbeat_op:

                if st == self._heartbeat_stack:
                    self._heartbeat_count += 1  # we've seen this before! increment count

                    # we've been in this for the last X ticks, or it's been X seconds, fail this part of the heartbeat
                    if self._heartbeat_count > CFG.get_safe('cc.timeout.heartbeat_proc_count_threshold', 30) or \
                       int(get_ion_ts()) - int(self._heartbeat_time) >= CFG.get_safe('cc.timeout.heartbeat_proc_time_threshold', 30) * 1000:
                        heartbeat_ok = False
                else:
                    # it's made some progress
                    self._heartbeat_count = 1
                    self._heartbeat_stack = st
                    self._heartbeat_time  = get_ion_ts()
            else:
                self._heartbeat_op      = self._ctrl_current
                self._heartbeat_count   = 1
                self._heartbeat_time    = get_ion_ts()
                self._heartbeat_stack   = st

        else:
            self._heartbeat_op      = None
            self._heartbeat_count   = 0

        return (listeners_ok, ctrl_thread_ok, heartbeat_ok)

    @property
    def time_stats(self):
        """
        Returns a 3-tuple of (total time, idle time, processing time), all in ms.
        """
        now = int(get_ion_ts())
        running_time = now - self._start_time

        idle_time = running_time - self._proc_time

        return (running_time, idle_time, self._proc_time)

    def _child_failed(self, child):
        """
        Occurs when any child greenlet fails.

        Propogates the error up to the process supervisor.
        """
        self.proc.kill(child.exception)

    def add_endpoint(self, listener):
        """
        Adds a listening endpoint to this ION process.

        Spawns the listen loop and sets the routing call to synchronize incoming messages
        here. If this process hasn't been started yet, adds it to the list of listeners
        to start on startup.
        """
        if self.proc:
            listener.routing_call           = self._routing_call
            self._listener_map[listener]    = self.thread_manager.spawn(listener.listen)
            self.listeners.append(listener)
        else:
            self._startup_listeners.append(listener)

    def target(self, *args, **kwargs):
        """
        Control entrypoint. Setup the base properties for this process (mainly a listener).
        """
        if self.name:
            threading.current_thread().name = self.name

        # start time
        self._start_time = int(get_ion_ts())

        # spawn control flow loop
        self._ctrl_thread = self.thread_manager.spawn(self._control_flow)

        # wait on control flow loop, heartbeating as appropriate
        while not self._ctrl_thread.ev_exit.wait(timeout=self._heartbeat_secs):
            hbst = self.heartbeat()

            if not all(hbst):
                log.warn("Heartbeat status for process %s returned %s", self, hbst)
                if self._heartbeat_stack is not None:
                    stack_out = "".join(traceback.format_list(self._heartbeat_stack))
                else:
                    stack_out = "N/A"

                #raise PyonHeartbeatError("Heartbeat failed: %s, stacktrace:\n%s" % (hbst, stack_out))
                log.warn("Heartbeat failed: %s, stacktrace:\n%s", hbst, stack_out)

        # this is almost a no-op as we don't fall out of the above loop without
        # exiting the ctrl_thread, but having this line here makes testing much
        # easier.
        self._ctrl_thread.join()

    def _routing_call(self, call, context, *callargs, **callkwargs):
        """
        Endpoints call into here to synchronize across the entire IonProcess.

        Returns immediately with an AsyncResult that can be waited on. Calls
        are made by the loop in _control_flow. We pass in the calling greenlet so
        exceptions are raised in the correct context.

        @param  call        The call to be made within this ION processes' calling greenlet.
        @param  callargs    The keyword args to pass to the call.
        @param  context     Optional process-context (usually the headers of the incoming call) to be
                            set. Process-context is greenlet-local, and since we're crossing greenlet
                            boundaries, we must set it again in the ION process' calling greenlet.
        """
        ar = AsyncResult()

        if len(callargs) == 0 and len(callkwargs) == 0:
            log.trace("_routing_call got no arguments for the call %s, check your call's parameters", call)

        self._ctrl_queue.put((greenlet.getcurrent(), ar, call, callargs, callkwargs, context))
        return ar

    def has_pending_call(self, ar):
        """
        Returns true if the call (keyed by the AsyncResult returned by _routing_call) is still pending.
        """
        for _, qar, _, _, _, _ in self._ctrl_queue.queue:
            if qar == ar:
                return True

        return False

    def _cancel_pending_call(self, ar):
        """
        Cancels a pending call (keyed by the AsyncResult returend by _routing_call).

        @return True if the call was truly pending.
        """
        if self.has_pending_call(ar):
            ar.set(False)
            return True

        return False

    def _interrupt_control_thread(self):
        """
        Signal the control flow thread that it needs to abort processing, likely due to a timeout.
        """
        self._ctrl_thread.proc.kill(exception=OperationInterruptedException, block=False)

    def cancel_or_abort_call(self, ar):
        """
        Either cancels a future pending call, or aborts the current processing if the given AR is unset.

        The pending call is keyed by the AsyncResult returned by _routing_call.
        """
        if not self._cancel_pending_call(ar) and not ar.ready():
            self._interrupt_control_thread()

    def _control_flow(self):
        """
        Main process thread of execution method.

        This method is run inside a greenlet and exists for each ION process. Listeners
        attached to the process, either RPC Servers or Subscribers, synchronize their calls
        by placing future calls into the queue by calling _routing_call.  This is all done
        automatically for you by the Container's Process Manager.

        This method blocks until there are calls to be made in the synchronized queue, and
        then calls from within this greenlet.  Any exception raised is caught and re-raised
        in the greenlet that originally scheduled the call.  If successful, the AsyncResult
        created at scheduling time is set with the result of the call.
        """
        self._ready_control.set()

        for calltuple in self._ctrl_queue:
            calling_gl, ar, call, callargs, callkwargs, context = calltuple
            log.debug("control_flow making call: %s %s %s (has context: %s)", call, callargs, callkwargs, context is not None)

            res = None
            start_proc_time = int(get_ion_ts())

            # check context for expiration
            if context is not None and 'reply-by' in context:
                if start_proc_time >= int(context['reply-by']):
                    log.info("control_flow: attempting to process message already exceeding reply-by, ignore")
                    continue

            # also check ar if it is set, if it is, that means it is cancelled
            if ar.ready():
                log.info("control_flow: attempting to process message that has been cancelled, ignore")
                continue

            try:
                with self.service.push_context(context):
                    self._ctrl_current = ar
                    res = call(*callargs, **callkwargs)
            except OperationInterruptedException:
                # endpoint layer takes care of response as it's the one that caused this
                log.debug("Operation interrupted")
                pass
            except Exception as e:
                # raise the exception in the calling greenlet, and don't
                # wait for it to die - it's likely not going to do so.

                # try decorating the args of the exception with the true traceback
                # this should be reported by ThreadManager._child_failed
                exc = PyonThreadTraceback("IonProcessThread _control_flow caught an exception (call: %s, *args %s, **kwargs %s, context %s)\nTrue traceback captured by IonProcessThread' _control_flow:\n\n%s" % (call, callargs, callkwargs, context, traceback.format_exc()))
                e.args = e.args + (exc,)

                # HACK HACK HACK
                # we know that we only handle TypeError and IonException derived things, so only forward those if appropriate
                if isinstance(e, (TypeError, IonException)):
                    calling_gl.kill(exception=e, block=False)
                else:
                    # otherwise, swallow/record/report and hopefully we can continue on our way
                    self._errors.append((call, callargs, callkwargs, context, e, exc))

                    log.warn(exc)
                    log.warn("Attempting to continue...")

                    # have to raise something friendlier on the client side
                    calling_gl.kill(exception=ContainerError(str(exc)), block=False)
            finally:
                proc_time = int(get_ion_ts()) - start_proc_time
                self._proc_time += proc_time

                self._ctrl_current = None

            ar.set(res)

    def start_listeners(self):
        """
        Starts all listeners in managed greenlets.

        This must be called after starting this IonProcess. Currently, the Container's ProcManager
        will handle this for you, but if using an IonProcess manually, you must remember to call
        this method or no attached listeners will run.
        """

        # spawn all listeners in startup listeners (from initializer, or added later)
        for listener in self._startup_listeners:
            self.add_endpoint(listener)

        ev = Event()

        def allready(ev):
            waitall([x.get_ready_event() for x in self.listeners])
            ev.set()

        spawn(allready, ev)

        ev.wait(timeout=10)

    def _notify_stop(self):
        """
        Called when the process is about to be shut down.

        Instructs all listeners to close, puts a StopIteration into the synchronized queue,
        and waits for the listeners to close and for the control queue to exit.
        """
        map(lambda x: x.close(), self.listeners)
        self._ctrl_queue.put(StopIteration)

        # wait_children will join them and then get() them, which may raise an exception if any of them
        # died with an exception.
        self.thread_manager.wait_children(30)

        PyonThread._notify_stop(self)

        # run the cleanup method if we have one
        if self._cleanup_method is not None:
            self._cleanup_method(self)

    def get_ready_event(self):
        """
        Returns an Event that is set when the control greenlet is up and running.
        """
        return self._ready_control


class IonProcessThreadManager(PyonThreadManager):

    def _create_thread(self, target=None, **kwargs):
        return IonProcessThread(target=target, heartbeat_secs=self.heartbeat_secs, **kwargs)

# ---------------------------------------------------------------------------------------------------


class StandaloneProcess(BaseService):
    """
    A process is an ION process of type "standalone" that has an incoming messaging
    attachment for the process and operations as defined in a service YML.
    """
    process_type = "standalone"


class SimpleProcess(BaseService):
    """
    A simple process is an ION process of type "simple" that has no incoming messaging
    attachment.
    """
    process_type = "simple"


class ImmediateProcess(BaseService):
    """
    An immediate process is an ION process of type "immediate" that does its action in
    the on_init and on_start hooks, and that it terminated immediately after completion.
    Has no messaging attachment.
    """
    process_type = "immediate"
