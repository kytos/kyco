# -*- coding: utf-8 -*-
"""Kyco - Kytos Contoller

This module contains the main class of Kyco, which is
:class:`~.controller.Controller`.

Basic usage:

.. code-block:: python3

    controller = Controller()
    controller.start()
"""

import os
import re

from importlib.machinery import SourceFileLoader
from threading import Thread

from kyco.core.buffers import KycoBuffers
from kyco.core.events import KycoShutdownEvent
from kyco.core.events import KycoSwitchUp
from kyco.core.events import KycoSwitchDown
from kyco.core.event_handlers import raw_event_handler
from kyco.core.event_handlers import msg_in_event_handler
from kyco.core.event_handlers import msg_out_event_handler
from kyco.core.event_handlers import app_event_handler
from kyco.core.switch import KycoSwitch
from kyco.core.tcp_server import KycoOpenFlowRequestHandler
from kyco.core.tcp_server import KycoServer
from kyco.utils import start_logger
from kyco.utils import KycoCoreNApp

log = start_logger()


class Controller(object):
    """This is the main class of Kyco.

    The main responsabilities of this class is:
        - start a thread with :class:`~.core.tcp_server.KycoServer`;
        - manage KycoNApps (install, load and unload);
        - keep the buffers (instance of :class:`~core.buffers.KycoBuffers`);
        - manage which event should be sent to NApps methods;
        - manage the buffers handlers, considering one thread per handler.
    """
    def __init__(self, options):
        self._threads = {}
        self.buffers = KycoBuffers()
        self.connection_pool = {}
        self.events_listeners = {'KycoNewConnection': [self.new_connection_handler],
                                 'KycoConnectionLost': [self.connection_lost_handler]}
        self.switches = {}
        self.napps = {}
        self.server = None
        self.options = options

    def start(self):
        """Start the controller.

        Starts a thread with the KycoServer (TCP Server).
        Starts a thread for each buffer handler.
        Load the installed apps."""
        log.info("Starting Kyco - Kytos Controller")
        self.server = KycoServer((self.options.listen, int(self.options.port)),
                                 KycoOpenFlowRequestHandler,
                                 self.buffers.raw_events.put)

        #TODO: Refact to be more pythonic
        thrds = {'tcp_server': Thread(name='TCP server',
                                      target=self.server.serve_forever),
                 'raw_event_handler': Thread(name='RawEvent Handler',
                                             target=raw_event_handler,
                                             args=[self.events_listeners,
                                                   self.connection_pool,
                                                   self.buffers.raw_events,
                                                   self.buffers.msg_in_events,
                                                   self.buffers.app_events]),
                 'msg_in_event_handler': Thread(name='MsgInEvent Handler',
                                                target=msg_in_event_handler,
                                                args=[self.events_listeners,
                                                      self.buffers.msg_in_events]),
                 'msg_out_event_handler': Thread(name='MsgOutEvent Handler',
                                                 target=msg_out_event_handler,
                                                 args=[self.events_listeners,
                                                       self.connection_pool,
                                                       self.buffers.msg_out_events]),
                 'app_event_handler': Thread(name='AppEvent Handler',
                                             target=app_event_handler,
                                             args=[self.events_listeners,
                                                   self.buffers.app_events])}

        self._threads = thrds
        for thread in self._threads.values():
            thread.start()

        log.info("Loading kyco apps...")
        self.load_napps()

    def stop(self):
        """Stops the controller.

        This method should:
            - announce on the network that the controller will shutdown;
            - stop receiving incoming packages;
            - call the 'shutdown' method of each KycoNApp that is running;
            - finish reading the events on all buffers;
            - stop each running handler;
            - stop all running threads;
            - stop the KycoServer;
        """
        log.info("Stopping Kyco")
        self.server.socket.close()
        self.server.shutdown()
        self.buffers.send_stop_signal()

        self.unload_napps()

        for thread in self._threads.values():
            log.info("Stopping thread: %s", thread.name)
            thread.join()

        for thread in self._threads.values():
            while thread.is_alive():
                pass

    def notify_listeners(self, event):
        for key in self.listeners:
            if re.match(key, type(event).__name__):
                for listener in self.listeners[key]:
                    listener(event)

    def raw_event_handler(self):
        """Handle raw events.

        This handler listen to the raw_buffer, get every event added to this
        buffer and sends it to the listeners listening to this event.
        """

        log.info("Raw Event Handler started")
        while True:
            event = self.buffers.raw_buffer.get()

            if isinstance(event, KycoShutdownEvent):
                log.debug("RawEvent handler stopped")
                break

            log.debug("RawEvent handler called")
            # Sending the event to the listeners
            self.notify_listeners(event)

    def msg_in_event_handler(self):
        """Handle msg_in events.

        This handler listen to the msg_in_buffer, get every event added to this
        buffer and sends it to the listeners listening to this event.
        """

        log.info("Message In Event Handler started")
        while True:
            event = self.buffers.msg_in_buffer.get()

            if isinstance(event, KycoShutdownEvent):
                log.debug("MsgInEvent handler stopped")
                break

            log.debug("MsgInEvent handler called")
            # Sending the event to the listeners
            self.notify_listeners(event)

    def msg_out_event_handler(self):
        """Handle msg_out events.

        This handler listen to the msg_out_buffer, get every event added to
        this buffer and sends it to the listeners listening to this event.
        """

        log.info("Message Out Event Handler started")
        while True:
            event = self.buffers.msg_out_buffer.get()

            if isinstance(event, KycoShutdownEvent):
                log.debug("MsgOutEvent handler stopped")
                break

            log.debug("MsgOutEvent handler called")
            dpid = event.connection
            message = event.content['message']

            # Sending the OpenFlow message to the switch
            self.send_to_switch(dpid, message.pack())

            # Sending the event to the listeners
            self.notify_listeners(event)

    def app_event_handler(self):
        """Handle app events.

        This handler listen to the app_buffer, get every event added to this
        buffer and sends it to the listeners listening to this event.
        """

        log.info("App Event Handler started")
        while True:
            event = self.app_buffer.get()

            if isinstance(event, KycoShutdownEvent):
                log.debug("AppEvent handler stopped")
                break

            log.debug("AppEvent handler called")
            # Sending the event to the listeners
            self.notify_listeners(event)

    def add_new_switch(self, switch):
        """Adds a new switch on the controller.

        If the switch already exists (dpid), then an exception is raised.
        Args:
            switch (KycoSwitch): A KycoSwitch object
        """

        if switch.dpid in self.switches:
            if self.switches[switch.dpid].is_connected():
                error_message = ("Kyco already have a connected switch with "
                                 "dpid {}")
                raise Exception(error_message.format(switch.dpid))
            else:
                self.switches[switch.dpid].save_connection(switch.socket)
        else:
            self.switches[switch.dpid] = switch

    def disconnect_switch(self, dpid):
        """End the connection with a switch.

        If no switch with the specified dpid is passed, an exception is raised.
        Args:
            dpid: the dpid of the switch
        """

        if dpid not in self.switches:
            raise Exception("Switch {} not found on Kyco".format(dpid))
        self.switches[dpid].disconnect()

    def new_connection_handler(self, event):
        """Handle a KycoNewConnection event.

        This method will read the event and store the connection (socket) data
        into the correct switch object on the controller.

        At last, it will create and send a SwitchUp event to the app buffer.

        Args:
            event (KycoNewConnection): The received event with the needed infos
        """

        log.info("Handling KycoNewConnection event")

        socket = event.content['request']
        dpid = event.connection
        switch = KycoSwitch(dpid, socket)

        try:
            self.add_new_switch(switch)
        except Exception as e:
            log.error('Error while handling a new connection')
            raise e

        new_event = KycoSwitchUp(content={}, connection=dpid,
                                 timestamp=event.timestamp)

        self.buffers.app_events.put(new_event)

    def connection_lost_handler(self, event):
        """Handle a ConnectionLost event.

        This method will read the event and change the switch that has been
        disconnected.

        At last, it will create and send a SwitchDown event to the app buffer.

        Args:
            event (KycoConnectionLost): Received event with the needed infos
        """

        log.info("Handling KycoConnectionLost event")

        self.disconnect_switch(event.connection)
        new_event = KycoSwitchDown(content={}, connection=event.connection,
                                   timestamp=event.timestamp)

        self.buffers.app_events.put(new_event)

    def send_to_switch(connection, message):
        """ Send a message to through the given connection

        Args:
            connection (socket/request): socket connection to switch
            message (binary OpenFlowMessage)

        """
        # TODO: Create a Switch class and a method send()
        connection.send(message)

    def load_napp(self, napp_name):
        """Load a single app.

        Load a single NAPP based on its name.
        Args:
            napp_name (str): Name of the NApp to be loaded.
        """
        path = os.path.join(self.options.napps, napp_name, 'main.py')
        module = SourceFileLoader(napp_name, path)

        # TODO: Think a better way to export this
        buffers = {'add_to_msg_out_buffer': self.buffers.msg_out_events.put,
                   'add_to_msg_in_buffer': self.buffers.msg_in_events.put,
                   'add_to_app_buffer': self.buffers.app_events.put}

        napp = module.load_module().Main(**buffers)
        self.napps[napp_name] = napp

        for event_type, listeners in napp._listeners.items():
            if event_type not in self.events_listeners:
                self.events_listeners[event_type] = []
            self.events_listeners[event_type].extend(listeners)

    def install_napp(self, napp_name):
        """Install the requested NApp by its name.

        Downloads the NApps from the NApp network and install it.
        TODO: Download or git-clone?

        Args:
            napp_name (str): Name of the NApp to be installed.
        """
        pass

    def load_napps(self):
        """Load all NApps installed on the NApps dir"""
        napps_dir = self.options.napps
        for napp_name in os.listdir(napps_dir):
            if os.path.isdir(os.path.join(napps_dir, napp_name)):
                log.info("Loading app %s", napp_name)
                self.load_napp(napp_name)

    def unload_napp(self, napp_name):
        """Unload a specific NApp based on its name.

        Args:
            napp_name (str): Name of the NApp to be unloaded.
        """
        napp = self.napps.pop(napp_name)
        napp.shutdown()

    def unload_napps(self):
        """Unload all loaded NApps that are not core NApps."""
        # list() is used here to avoid the error:
        # 'RuntimeError: dictionary changed size during iteration'
        # This is caused by looping over an dictionary while removing
        # items from it.
        for napp_name in list(self.napps):
            if not isinstance(self.napps[napp_name], KycoCoreNApp):
                self.unload_napp(napp_name)
