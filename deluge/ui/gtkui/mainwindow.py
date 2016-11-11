# -*- coding: utf-8 -*-
#
# Copyright (C) 2007-2009 Andrew Resch <andrewresch@gmail.com>
#
# This file is part of Deluge and is licensed under GNU General Public License 3.0, or later, with
# the additional special exception to link portions of this program with the OpenSSL library.
# See LICENSE for more details.
#

from __future__ import unicode_literals

import logging
import os.path
from hashlib import sha1 as sha

from gi.repository import Gdk, Gtk
from gi.repository.Gdk import DragAction.COPY
from gi.repostiory.Gdk.WindowState import ICONIFIED, MAXIMIZED, WITHDRAWN
from twisted.internet import reactor
from twisted.internet.error import ReactorNotRunning

import deluge.component as component
import deluge.ui.gtkui.common
from deluge.common import fspeed, resource_filename
from deluge.configmanager import ConfigManager
from deluge.ui.client import client
from deluge.ui.gtkui.dialogs import PasswordDialog
from deluge.ui.gtkui.ipcinterface import process_args

try:
    from gi.repository import Wnck
except ImportError:
    Wnck = None


log = logging.getLogger(__name__)


class _GtkBuilderSignalsHolder(object):
    def connect_signals(self, mapping_or_class):

        if isinstance(mapping_or_class, dict):
            for name, handler in mapping_or_class.items():
                if hasattr(self, name):
                    raise RuntimeError(
                        'A handler for signal %r has already been registered: %s' %
                        (name, getattr(self, name))
                    )
                setattr(self, name, handler)
        else:
            for name in dir(mapping_or_class):
                if not name.startswith('on_'):
                    continue
                if hasattr(self, name):
                    raise RuntimeError('A handler for signal %r has already been registered: %s' %
                                       (name, getattr(self, name)))
                setattr(self, name, getattr(mapping_or_class, name))


class MainWindow(component.Component):
    def __init__(self):
        if Wnck:
            self.screen = Wnck.Screen.get_default()
        component.Component.__init__(self, 'MainWindow', interval=2)
        self.config = ConfigManager('gtkui.conf')
        self.main_builder = Gtk.Builder()

        # Patch this GtkBuilder to avoid connecting signals from elsewhere
        #
        # Think about splitting up  mainwindow gtkbuilder file into the necessary parts
        # to avoid GtkBuilder monkey patch. Those parts would then need adding to mainwindow 'by hand'.
        self.gtk_builder_signals_holder = _GtkBuilderSignalsHolder()
        # FIXME: The deepcopy has been removed: copy.deepcopy(self.main_builder.connect_signals)
        self.main_builder.prev_connect_signals = self.main_builder.connect_signals

        def patched_connect_signals(*a, **k):
            raise RuntimeError('In order to connect signals to this GtkBuilder instance please use '
                               '"component.get(\'MainWindow\').connect_signals()"')
        self.main_builder.connect_signals = patched_connect_signals

        # Get Gtk Builder files Main Window, New release dialog, and Tabs.
        for filename in ('main_window.ui', 'main_window.new_release.ui', 'main_window.tabs.ui',
                         'main_window.tabs.menu_file.ui', 'main_window.tabs.menu_peer.ui'):
            self.main_builder.add_from_file(
                resource_filename('deluge.ui.gtkui', os.path.join('glade', filename)))

        self.window = self.main_builder.get_object('main_window')
        self.window.set_icon(deluge.ui.gtkui.common.get_deluge_icon())
        self.vpaned = self.main_builder.get_object('vpaned')
        self.initial_vpaned_position = self.config['window_pane_position']

        # Keep a list of components to pause and resume when changing window state.
        self.child_components = ['TorrentView', 'StatusBar', 'TorrentDetails']

        # Load the window state
        self.load_window_state()

        # Keep track of window minimization state so we don't update UI when it is minimized.
        self.is_minimized = False
        self.restart = False

        self.window.drag_dest_set(
            Gtk.DestDefaults.ALL, [Gtk.TargetEntry.new('text/uri-list', 0, 80)], DragAction.COPY)

        # Connect events
        self.window.connect('window-state-event', self.on_window_state_event)
        self.window.connect('configure-event', self.on_window_configure_event)
        self.window.connect('delete-event', self.on_window_delete_event)
        self.window.connect('drag-data-received', self.on_drag_data_received_event)
        self.vpaned.connect('notify::position', self.on_vpaned_position_event)
        self.window.connect('expose-event', self.on_expose_event)

        self.config.register_set_function('show_rate_in_title', self._on_set_show_rate_in_title, apply_now=False)

        client.register_event_handler('NewVersionAvailableEvent', self.on_newversionavailable_event)

    def connect_signals(self, mapping_or_class):
        self.gtk_builder_signals_holder.connect_signals(mapping_or_class)

    def first_show(self):
        self.main_builder.prev_connect_signals(self.gtk_builder_signals_holder)
        self.vpaned.set_position(self.initial_vpaned_position)
        if not (
                self.config['start_in_tray'] and self.config['enable_system_tray']
        ) and not self.window.get_property('visible'):
            log.debug('Showing window')
            self.show()

        while Gtk.events_pending():
            Gtk.main_iteration()

    def show(self):
        component.resume(self.child_components)
        self.window.show()

    def hide(self):
        component.get('TorrentView').save_state()
        component.pause(self.child_components)

        # Store the x, y positions for when we restore the window
        self.config['window_x_pos'], self.config['window_y_pos'] = self.window.get_position()
        self.window.hide()

    def present(self):
        def restore():
            # Restore the proper x,y coords for the window prior to showing it
            component.resume(self.child_components)
            self.window.present()
            self.load_window_state()

        if self.config['lock_tray'] and not self.visible():
            dialog = PasswordDialog(_('Enter your password to show Deluge...'))

            def on_dialog_response(response_id):
                if response_id == Gtk.ResponseType.OK:
                    if self.config['tray_password'] == sha(dialog.get_password()).hexdigest():
                        restore()
            dialog.run().addCallback(on_dialog_response)
        else:
            restore()

    def active(self):
        """Returns True if the window is active, False if not."""
        return self.window.is_active()

    def visible(self):
        """Returns True if window is visible, False if not."""
        return self.window.get_property('visible')

    def get_builder(self):
        """Returns a reference to the main window GTK builder object."""
        return self.main_builder

    def quit(self, shutdown=False, restart=False):
        """Quits the GtkUI application.

        Args:
            shutdown (bool): Whether or not to shutdown the daemon as well.
            restart (bool): Whether or not to restart the application after closing.

        """

        def quit_gtkui():
            def stop_gtk_reactor(result=None):
                self.restart = restart
                try:
                    reactor.callLater(0, reactor.fireSystemEvent, 'gtkui_close')
                except ReactorNotRunning:
                    log.debug('Attempted to stop the reactor but it is not running...')

            if shutdown:
                client.daemon.shutdown().addCallback(stop_gtk_reactor)
            elif not client.is_standalone() and client.connected():
                client.disconnect().addCallback(stop_gtk_reactor)
            else:
                stop_gtk_reactor()

        if self.config['lock_tray'] and not self.visible():
            dialog = PasswordDialog(_('Enter your password to Quit Deluge...'))

            def on_dialog_response(response_id):
                if response_id == Gtk.ResponseType.OK:
                    if self.config['tray_password'] == sha(dialog.get_password()).hexdigest():
                        quit_gtkui()
            dialog.run().addCallback(on_dialog_response)
        else:
            quit_gtkui()

    def load_window_state(self):
        if self.config['window_x_pos'] == -32000 or self.config['window_x_pos'] == -32000:
            self.config['window_x_pos'] = self.config['window_y_pos'] = 0

        self.window.move(self.config['window_x_pos'], self.config['window_y_pos'])
        self.window.resize(self.config['window_width'], self.config['window_height'])
        if self.config['window_maximized']:
            self.window.maximize()

    def on_window_configure_event(self, widget, event):
        if not self.config['window_maximized'] and self.visible:
            self.config['window_x_pos'], self.config['window_y_pos'] = self.window.get_position()
            self.config['window_width'] = event.width
            self.config['window_height'] = event.height

    def on_window_state_event(self, widget, event):
        if event.changed_mask & MAXIMIZED:
            if event.new_window_state & MAXIMIZED:
                log.debug('pos: %s', self.window.get_position())
                self.config['window_maximized'] = True
            elif not event.new_window_state & WITHDRAWN:
                self.config['window_maximized'] = False
        if event.changed_mask & ICONIFIED:
            if event.new_window_state & ICONIFIED:
                log.debug('MainWindow is minimized..')
                component.get('TorrentView').save_state()
                component.pause(self.child_components)
                self.is_minimized = True
            else:
                log.debug('MainWindow is not minimized..')
                component.resume(self.child_components)
                self.is_minimized = False
        return False

    def on_window_delete_event(self, widget, event):
        if self.config['close_to_tray'] and self.config['enable_system_tray']:
            self.hide()
        else:
            self.quit()

        return True

    def on_vpaned_position_event(self, obj, param):
        self.config['window_pane_position'] = self.vpaned.get_position()

    def on_drag_data_received_event(self, widget, drag_context, x, y, selection_data, info, timestamp):
        log.debug('Selection(s) dropped on main window %s', selection_data.get_text())
        if selection_data.get_uris():
            process_args(selection_data.get_uris())
        else:
            process_args(selection_data.get_text().split())
        drag_context.finish(True, True, timestamp)

    def on_expose_event(self, widget, event):
        component.get('SystemTray').blink(False)

    def stop(self):
        self.window.set_title('Deluge')

    def update(self):
        # Update the window title
        def _on_get_session_status(status):
            download_rate = fspeed(status['payload_download_rate'], precision=0, shortform=True)
            upload_rate = fspeed(status['payload_upload_rate'], precision=0, shortform=True)
            self.window.set_title(_('D: %s U: %s - Deluge' % (download_rate, upload_rate)))
        if self.config['show_rate_in_title']:
            client.core.get_session_status(
                ['payload_download_rate', 'payload_upload_rate']
                ).addCallback(_on_get_session_status)

    def _on_set_show_rate_in_title(self, key, value):
        if value:
            self.update()
        else:
            self.window.set_title(_('Deluge'))

    def on_newversionavailable_event(self, new_version):
        if self.config['show_new_releases']:
            from deluge.ui.gtkui.new_release_dialog import NewReleaseDialog
            reactor.callLater(5.0, NewReleaseDialog().show, new_version)

    def is_on_active_workspace(self):
        """Determines if MainWindow is on the active workspace.

        Returns:
            bool: True if on active workspace (or wnck module not available), otherwise False.

        """
        if wnck:
            self.screen.force_update()
            win = wnck.window_get(self.window.get_window().xid)
            if win:
                active_wksp = win.get_screen().get_active_workspace()
                if active_wksp:
                    return win.is_on_workspace(active_wksp)
                return False
        return True
