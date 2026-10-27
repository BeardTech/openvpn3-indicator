#!/usr/bin/env python3
# vim:ts=4:sts=4:sw=4:expandtab

#
# openvpn3-indicator - Simple indicator application for OpenVPN3.
# Copyright (C) 2024 Grzegorz Gutowski <grzegorz.gutowski@uj.edu.pl>
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License,
# or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.
# If not, see <https://www.gnu.org/licenses/>.
#

import gettext
import logging
import pathlib
import re
import signal
import time
import traceback
import webbrowser

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, GObject, Gtk, Gio

import dbus
from dbus.mainloop.glib import DBusGMainLoop
try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except:
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3

import openvpn3

from openvpn3_indicator.about import APPLICATION_ID, APPLICATION_VERSION, APPLICATION_NAME, APPLICATION_TITLE, APPLICATION_SYSTEM_TAG
from openvpn3_indicator.about import MANAGER_VERSION_MINIMUM, MANAGER_VERSION_RECOMMENDED
from openvpn3_indicator.multi_indicator import MultiIndicator
from openvpn3_indicator.multi_notifier import MultiNotifier
from openvpn3_indicator.credential_store import CredentialStore
from openvpn3_indicator.dialogs.about import construct_about_dialog
from openvpn3_indicator.dialogs.credentials import CredentialsUserInput, construct_credentials_dialog
from openvpn3_indicator.dialogs.configuration import construct_configuration_select_dialog, construct_configuration_import_dialog, construct_configuration_remove_dialog
from openvpn3_indicator.dialogs.notification import show_error_dialog, show_warning_notification, show_info_notification
from openvpn3_indicator.status import get_status_icon, get_status_description


#TODO: Which input slots should not be stored ? (OTPs, etc.)
#TODO: Understand better the possible session state changes
#TODO: Collect and present session logs and stats
#TODO: Implement other than AppIndicator ways to have system tray icon
#TODO: /usr/share/metainfo ?
#TODO: Understand mimetype icons inheritance
#TODO: Prepare localization

DEFAULT_CONFIG_NAME = gettext.gettext('UNKNOWN')
DEFAULT_SESSION_NAME = gettext.gettext('UNKNOWN')
STATUS_NOTIFIER_WATCHER_BUS_NAME = 'org.kde.StatusNotifierWatcher'
STATUS_NOTIFIER_WATCHER_OBJECT_PATH = '/StatusNotifierWatcher'
STATUS_NOTIFIER_WATCHER_INTERFACE = 'org.kde.StatusNotifierWatcher'
TERMINAL_SESSION_STATUSES = {'SESS_REMOVED', 'SESSION_EXPIRED', 'SESS_EXPIRED', 'PROC_STOPPED', 'PROC_KILLED'}

###
#
# Application
#
###

class Application(Gtk.Application):
    def __init__(self):
        Gtk.Application.__init__(self,
            application_id=APPLICATION_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
            )
        self.settings = Gio.Settings.new(APPLICATION_ID)
        self.add_main_option('version', ord('V'), GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Show version and exit", None)
        self.add_main_option('verbose', ord('v'), GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Show more info", None)
        self.add_main_option('debug', ord('d'), GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Show debug info", None)
        self.add_main_option('silent', ord('s'), GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Show less info", None)
        self.clear_secret_storage = False
        self.shutting_down = False
        self.signal_matches = list()
        self.add_main_option('clear-secret-storage', ord('c'), GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "Remove all data stored in secret storage", None)
        self.connect('handle-local-options', self.on_handle_local_options)
        self.connect('startup', self.on_startup)
        self.connect('activate', self.on_activate)
        self.connect('open', self.on_open)
        self.connect('shutdown', self.on_shutdown)

    def on_handle_local_options(self, application, options):
        options = options.end().unpack()
        level=logging.WARNING
        if options.get('version', False):
            print(f'{APPLICATION_NAME} {APPLICATION_VERSION}')
            return 0
        if options.get('clear-secret-storage', False):
            self.clear_secret_storage = True
        if options.get('debug', False):
            level = logging.DEBUG
        elif options.get('silent', False):
            level = logging.INFO
        elif options.get('verbose', False):
            level = logging.ERROR
        logging.basicConfig(level = level)
        return -1

    def on_activate(self, data):
        self.info(f'Activate')

    def on_open(self, application, files, n_files, hint):
        self.info(f'Open {n_files} {hint}')
        for file in files:
            config_path = file.get_path()
            self.action_config_open(config_path)

    def on_shutdown(self, application):
        self.shutting_down = True
        for match in self.signal_matches:
            match.remove()
        self.signal_matches.clear()
        for session in getattr(self, 'sessions', {}).values():
            try:
                session.StatusChangeCallback(None)
            except Exception:
                self.debug(traceback.format_exc())
        # Explicitly unregister the tray items.  GNOME can otherwise retain
        # stale items when the application exits during a shell transition.
        if hasattr(self, 'multi_indicator'):
            self.multi_indicator.close()
        if hasattr(self, 'multi_notifier'):
            self.multi_notifier.close()

    def on_startup(self, data):
        self.info(f'Startup')
        for signum in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self.on_termination_signal)
        DBusGMainLoop(set_as_default=True)

        bus = dbus.Bus()
        self.session_bus = bus
        self.status_notifier_watcher_refresh_generation = 0
        self.status_notifier_watcher_owner = None
        self.status_notifier_watcher_refresh_pending = False
        # Subscribe before the initial availability check so that a watcher
        # restart during startup cannot be missed.
        self.signal_matches.append(self.session_bus.add_signal_receiver(
            self.on_status_notifier_watcher_owner_changed,
            signal_name='NameOwnerChanged',
            dbus_interface='org.freedesktop.DBus',
            bus_name='org.freedesktop.DBus',
            arg0=STATUS_NOTIFIER_WATCHER_BUS_NAME,
        ))
        self.signal_matches.append(self.session_bus.add_signal_receiver(
            self.on_status_notifier_host_registered,
            signal_name='StatusNotifierHostRegistered',
            dbus_interface=STATUS_NOTIFIER_WATCHER_INTERFACE,
            bus_name=STATUS_NOTIFIER_WATCHER_BUS_NAME,
            path=STATUS_NOTIFIER_WATCHER_OBJECT_PATH,
        ))
        self.signal_matches.append(self.session_bus.add_signal_receiver(
            self.on_status_notifier_properties_changed,
            signal_name='PropertiesChanged',
            dbus_interface='org.freedesktop.DBus.Properties',
            bus_name=STATUS_NOTIFIER_WATCHER_BUS_NAME,
            path=STATUS_NOTIFIER_WATCHER_OBJECT_PATH,
            arg0=STATUS_NOTIFIER_WATCHER_INTERFACE,
        ))

        self.multi_notifier = MultiNotifier(self, f'{APPLICATION_NAME}')
        self.notifiers = dict()
        # GNOME Shell can restart the StatusNotifierWatcher after suspend/resume.
        # Wait until the new watcher is ready, then republish the indicators.

        self.dbus = dbus.SystemBus()
        self.backend_proxies_invalid = True
        try:
            self.config_manager = openvpn3.ConfigurationManager(self.dbus)
            self.session_manager = openvpn3.SessionManager(self.dbus)
            self.backend_proxies_invalid = False
        except dbus.exceptions.DBusException:
            self.warning('OpenVPN managers unavailable at startup; retrying', exc_info=True)
        # Subscribe by well-known name, independent of a particular daemon
        # owner. The SDK's proxy-bound subscription can die with that owner.
        self.signal_matches.append(self.dbus.add_signal_receiver(
            self.on_session_manager_signal,
            signal_name='SessionManagerEvent',
            dbus_interface='net.openvpn.v3.sessions',
            bus_name='net.openvpn.v3.sessions',
            path='/net/openvpn/v3/sessions',
        ))
        for service_name in ('net.openvpn.v3.sessions', 'net.openvpn.v3.configuration', 'net.openvpn.v3.log'):
            self.signal_matches.append(self.dbus.add_signal_receiver(
                self.on_openvpn_service_owner_changed,
                signal_name='NameOwnerChanged',
                dbus_interface='org.freedesktop.DBus',
                bus_name='org.freedesktop.DBus',
                arg0=service_name,
            ))

        # Detect if the config manager version is v21 or newer
        # TODO: This can be simplified once the openvpn3 module provides
        #       a version query API
        self.manager_version = 0
        self.manager_version = 9999
        try:
            cmgr_obj = self.dbus.get_object('net.openvpn.v3.configuration','/net/openvpn/v3/configuration')
            cmgr_prop = dbus.Interface(cmgr_obj, dbus_interface='org.freedesktop.DBus.Properties')
            cmgr_peer = dbus.Interface(cmgr_obj, dbus_interface='org.freedesktop.DBus.Peer')
            cmgr_peer.Ping()
            try:
                cmgr_version = str(cmgr_prop.Get('net.openvpn.v3.configuration','version'))
            except dbus.exceptions.DBusException:
                self.debug(f'Waiting for backend to start')
                time.sleep(0.5)
                cmgr_version = str(cmgr_prop.Get('net.openvpn.v3.configuration','version'))
            if cmgr_version.startswith('git:'):
                # development version: presume all features are available
                # and use a high version number
                pass
            elif cmgr_version.startswith('v'):
                # Version identifiers may cary a "release label",
                # like v19_beta, v22_dev
                self.manager_version = int(re.split(r'[^0-9]', cmgr_version[1:], 1)[0])
        except:
            self.debug(traceback.format_exc())
            self.warning(f'Backend version check failed')
        if self.manager_version < MANAGER_VERSION_MINIMUM:
            self.error(f'You are using version {self.manager_version} of OpenVPN3 software which is not supported. Consider an upgrade to a newer version. We recommend version {MANAGER_VERSION_RECOMMENDED}.', notify=True)
        elif self.manager_version < MANAGER_VERSION_RECOMMENDED:
            self.warning(f'You are using version {self.manager_version} of OpenVPN3 software. Consider an upgrade to a newer version. We recommend version {MANAGER_VERSION_RECOMMENDED}.', notify=True)
        self.debug(f'Running with manager version {self.manager_version}')

        self.credential_store = CredentialStore()
        if self.clear_secret_storage:
            for config in self.credential_store.keys():
                credentials = self.credential_store[config]
                for key in list(credentials.keys()):
                    self.info(f'Removing entry {key} from secret storage')
                    del credentials[key]

        self.configs = dict()
        self.sessions = dict()
        self.sessions_connected = set()
        self.session_subscriptions = set()
        self.config_names = dict()
        self.name_configs = dict()
        self.config_sessions = dict()
        self.session_configs = dict()
        self.session_names = dict()
        self.failed_authentications = set()
        self.session_dialogs = dict()
        self.session_statuses = dict()

        self.multi_indicator = MultiIndicator(f'{APPLICATION_NAME}')
        self.multi_indicator.set_available(False)
        self.initial_indicators_published = False
        self.default_indicator = self.multi_indicator.new_indicator()
        self.default_indicator.icon=f'{APPLICATION_NAME}-idle'
        self.default_indicator.description=f'{APPLICATION_TITLE}'
        self.default_indicator.title=f'{APPLICATION_TITLE}'
        self.default_indicator.order_key='0'
        self.default_indicator.active=True
        self.indicators = dict()

        self.last_invalid = time.monotonic()
        self.invalid_sessions = True
        self.invalid_ui = True

        self.startup_config_id = None
        self.startup_config_name = None
        try:
            startup_action = self.settings.get_string('startup-action')
            self.debug(f'Startup action: {startup_action}')
            if startup_action == 'RESTART':
                self.startup_config_id = self.settings.get_string('most-recent-configuration-id')
            start_id = re.match(r'STARTID:(?P<id>.*)', startup_action)
            if start_id:
                self.startup_config_id = start_id.group('id')
            start_name = re.match(r'STARTNAME:(?P<name>.*)', startup_action)
            if start_name:
                self.startup_config_name = start_name.group('name')
        except:
            pass
        if self.startup_config_id or self.startup_config_name:
            self.info(f'Startup configuration set to {self.startup_config_id or self.startup_config_name}')

        GLib.timeout_add(1000, self.on_schedule)
        self.hold()
        self.begin_status_notifier_watcher_refresh()
        GLib.timeout_add(5000, self.check_status_notifier_watcher)

    def on_status_notifier_watcher_owner_changed(self, name, old_owner, new_owner):
        old_owner = str(old_owner)
        new_owner = str(new_owner)
        self.info(f'StatusNotifierWatcher owner changed from {old_owner or "<none>"} to {new_owner or "<none>"}')
        self.begin_status_notifier_watcher_refresh()

    def on_status_notifier_host_registered(self):
        self.begin_status_notifier_watcher_refresh()

    def on_status_notifier_properties_changed(self, interface, changed, invalidated):
        if 'IsStatusNotifierHostRegistered' in changed or 'IsStatusNotifierHostRegistered' in invalidated:
            self.begin_status_notifier_watcher_refresh()

    def begin_status_notifier_watcher_refresh(self):
        if self.shutting_down or not hasattr(self, 'multi_indicator'):
            return
        self.status_notifier_watcher_refresh_generation += 1
        self.status_notifier_watcher_refresh_pending = True
        self.multi_indicator.set_available(False)
        self.schedule_status_notifier_watcher_refresh(
            self.status_notifier_watcher_refresh_generation,
        )

    def check_status_notifier_watcher(self):
        if self.shutting_down:
            return False
        if not self.status_notifier_watcher_refresh_pending:
            try:
                owner = self.get_status_notifier_watcher_owner()
            except dbus.exceptions.DBusException:
                owner = None
            if owner is None or owner != self.status_notifier_watcher_owner:
                self.begin_status_notifier_watcher_refresh()
        return True

    def get_status_notifier_watcher_owner(self):
        owner = str(self.session_bus.get_name_owner(STATUS_NOTIFIER_WATCHER_BUS_NAME))
        watcher = self.session_bus.get_object(STATUS_NOTIFIER_WATCHER_BUS_NAME, STATUS_NOTIFIER_WATCHER_OBJECT_PATH)
        properties = dbus.Interface(watcher, dbus_interface='org.freedesktop.DBus.Properties')
        properties.Get(STATUS_NOTIFIER_WATCHER_INTERFACE, 'ProtocolVersion')
        # The watcher can be exported before GNOME's AppIndicator extension
        # has registered itself as a host.  Registering an indicator at that
        # point creates an icon with an empty D-Bus menu.
        if not bool(properties.Get(STATUS_NOTIFIER_WATCHER_INTERFACE, 'IsStatusNotifierHostRegistered')):
            raise dbus.exceptions.DBusException('StatusNotifierWatcher host is not registered yet')
        if owner != str(self.session_bus.get_name_owner(STATUS_NOTIFIER_WATCHER_BUS_NAME)):
            raise dbus.exceptions.DBusException('StatusNotifierWatcher owner changed during availability check')
        return owner

    def schedule_status_notifier_watcher_refresh(self, expected_generation, attempt=0):
        delay = min(5000, 250 * (2 ** min(attempt, 5)))
        self.debug(f'Scheduling StatusNotifierWatcher refresh attempt {attempt + 1} in {delay} ms')
        GLib.timeout_add(
            delay,
            self.refresh_status_notifier_watcher,
            expected_generation,
            attempt,
        )

    def refresh_status_notifier_watcher(self, expected_generation, attempt):
        if self.shutting_down or self.status_notifier_watcher_refresh_generation != expected_generation:
            return False
        try:
            current_owner = self.get_status_notifier_watcher_owner()
        except dbus.exceptions.DBusException:
            self.debug(f'StatusNotifierWatcher refresh attempt {attempt + 1} failed')
            if attempt == 20:
                self.warning('StatusNotifierWatcher is still unavailable; retrying indicator registration')
            self.schedule_status_notifier_watcher_refresh(expected_generation, attempt + 1)
            return False

        self.debug(f'StatusNotifierWatcher is ready after {attempt + 1} refresh attempts')
        self.status_notifier_watcher_owner = current_owner
        # The watcher can own its bus name before exporting its protocol interface.
        # Recreate the AppIndicator objects to avoid a stale registration.
        self.multi_indicator.reset()
        GLib.timeout_add(100, self.republish_status_notifier_watcher, expected_generation)
        return False

    def republish_status_notifier_watcher(self, expected_generation):
        if not self.shutting_down and self.status_notifier_watcher_refresh_generation == expected_generation:
            # The host can disappear again between readiness and publication.
            try:
                if self.get_status_notifier_watcher_owner() != self.status_notifier_watcher_owner:
                    self.begin_status_notifier_watcher_refresh()
                    return False
            except dbus.exceptions.DBusException:
                self.begin_status_notifier_watcher_refresh()
                return False
            self.debug('Republishing indicators to StatusNotifierWatcher')
            try:
                # Prepare logical menus while publication is suspended,
                # including startup before the first periodic refresh.
                if self.invalid_ui:
                    self.refresh_ui()
                self.multi_indicator.set_available(True)
                self.multi_indicator.update()
            except Exception:
                self.warning('Indicator publication failed; retrying', exc_info=True)
                self.begin_status_notifier_watcher_refresh()
                return False
            self.status_notifier_watcher_refresh_pending = False
            self.invalid_sessions = True
        return False

    def refresh_ui(self):
        if self.invalid_ui:
            new_indicators = dict()
            for session_id in self.sessions:
                indicator = self.indicators.get(session_id, None)
                session_name = self.get_session_name(session_id)
                if indicator is None:
                    indicator = self.multi_indicator.new_indicator()
                    indicator.active = True
                # Polling must update existing icons as well as menus when a
                # status signal was missed during a daemon/shell restart.
                indicator.icon = self.session_icon(session_id)
                indicator.description = f'{APPLICATION_TITLE}: {session_name}'
                indicator.title = f'{APPLICATION_TITLE}: {session_name}'
                indicator.order_key = f'1-{session_name}-{session_id}'
                new_indicators[session_id] = indicator
            new_notifiers = dict()
            for session_id in self.sessions:
                notifier = self.notifiers.get(session_id, None)
                if notifier is None:
                    session_name = self.get_session_name(session_id)
                    notifier = self.multi_notifier.new_notifier(f'session-{session_id}-status', mute_repetitions=True)
                    notifier.icon = self.session_icon(session_id)
                    notifier.title = f'{APPLICATION_TITLE}: {session_name}'
                    notifier.body = self.session_description(session_id)
                    notifier.active = False
                new_notifiers[session_id] = notifier
            for session_id, indicator in self.indicators.items():
                if session_id not in new_indicators:
                    indicator.close()
            for session_id, notifier in self.notifiers.items():
                if session_id not in new_notifiers:
                    notifier.close()
            if len(new_indicators) == 0:
                self.default_indicator.active = True
                #TODO: Change icon, description, etc. Based on what?
                idle_menu_key = self.idle_menu_key()
                if self.default_indicator.menu_key != idle_menu_key:
                    self.default_indicator.set_menu(idle_menu_key, self.construct_idle_menu())
            else:
                self.default_indicator.active = False
            self.indicators = new_indicators
            for session_id, indicator in self.indicators.items():
                if session_id is not None:
                    #TODO: Change icon, description, etc. based on status
                    session_menu_key = self.session_menu_key(session_id)
                    if indicator.menu_key != session_menu_key:
                        indicator.set_menu(session_menu_key, self.construct_session_menu(session_id))
            self.multi_indicator.update()
            if not self.initial_indicators_published:
                self.initial_indicators_published = True
                GLib.idle_add(self.republish_initial_indicators)
            self.notifiers = new_notifiers
            self.invalid_ui = False

    def republish_initial_indicators(self):
        if self.shutting_down or self.status_notifier_watcher_refresh_pending:
            return False
        # GNOME can accept the indicator registration before it consumes the
        # D-Bus menu.  Publish it once more on the next main-loop iteration
        # so the initial menu is registered as well.
        self.debug('Republishing initial indicators')
        self.multi_indicator.reset()
        self.multi_indicator.update()
        return False

    def refresh_sessions(self):
        if self.invalid_sessions:
            new_session_ids = set()
            retry = False
            rebuild = self.backend_proxies_invalid
            try:
                if rebuild:
                    self.config_manager = openvpn3.ConfigurationManager(self.dbus)
                    self.session_manager = openvpn3.SessionManager(self.dbus)
                available_sessions = self.session_manager.FetchAvailableSessions()
            except Exception:
                # An unavailable list is not an empty list. Keep the last
                # snapshot and retry; only confirmed removals clear sessions.
                self.warning('Session list refresh failed', exc_info=True)
                return

            try:
                new_sessions = dict()
                new_session_statuses = dict()
                new_session_names = dict()
                new_session_configs = dict()
                for session in available_sessions:
                    session_id = str(session.GetPath())
                    if not rebuild and session_id in self.sessions:
                        session = self.sessions[session_id]
                    try:
                        status = session.GetStatus()
                    except dbus.exceptions.DBusException as exc:
                        if exc.get_dbus_name() in ('org.freedesktop.DBus.Error.UnknownObject', 'org.freedesktop.DBus.Error.UnknownMethod'):
                            # This particular session vanished during refresh.
                            retry = True
                            continue
                        retry = True
                        self.warning(f'Status refresh failed for {session_id}', exc_info=True)
                        status = self.session_statuses.get(session_id, {
                            'major': openvpn3.StatusMajor.UNSET,
                            'minor': openvpn3.StatusMinor.UNSET,
                            'message': '',
                        })
                    new_sessions[session_id] = session
                    new_session_statuses[session_id] = {
                        'major' : openvpn3.StatusMajor(status['major']),
                        'minor' : openvpn3.StatusMinor(status['minor']),
                        'message' : str(status['message']),
                    }
                    if new_session_statuses[session_id]['minor'].name in TERMINAL_SESSION_STATUSES:
                        new_sessions.pop(session_id)
                        new_session_statuses.pop(session_id)
                        continue
                    try:
                        # Names are not unique; bind sessions by config path.
                        new_session_configs[session_id] = str(session.GetProperty('config_path'))
                        new_session_names[session_id] = str(session.GetProperty('config_name'))
                    except dbus.exceptions.DBusException:
                        retry = True
                        if session_id in self.session_configs:
                            new_session_configs[session_id] = self.session_configs[session_id]
                        new_session_names[session_id] = self.session_names.get(session_id, DEFAULT_SESSION_NAME)

                try:
                    new_configs = dict()
                    new_config_names = dict()
                    for config in self.config_manager.FetchAvailableConfigs():
                        config_id = str(config.GetPath())
                        try:
                            config_name = str(config.GetConfigName())
                        except dbus.exceptions.DBusException:
                            retry = True
                            config_name = self.config_names.get(config_id, DEFAULT_CONFIG_NAME)
                        new_configs[config_id] = config
                        new_config_names[config_id] = config_name
                except Exception:
                    # Configuration metadata failure must not hide live VPNs.
                    self.warning('Configuration list refresh failed', exc_info=True)
                    retry = True
                    new_configs = self.configs.copy()
                    new_config_names = self.config_names.copy()

                new_config_sessions = {config_id: [] for config_id in new_configs}
                for session_id, config_id in new_session_configs.items():
                    if config_id in new_config_sessions:
                        new_config_sessions[config_id].append(session_id)

                for session_id, session in self.sessions.items():
                    if session_id not in new_sessions or session is not new_sessions[session_id]:
                        try:
                            session.StatusChangeCallback(None)
                        except Exception:
                            self.debug(traceback.format_exc())
                for session_id, session in new_sessions.items():
                    if session_id not in self.session_subscriptions or session is not self.sessions.get(session_id):
                        try:
                            session.StatusChangeCallback(
                                lambda major, minor, message, session_id=session_id: self.on_session_event(session_id, major, minor, message)
                            )
                            self.session_subscriptions.add(session_id)
                        except Exception:
                            # Status polling remains available if subscribing
                            # fails while the log daemon is restarting.
                            self.warning(f'Status subscription failed for {session_id}', exc_info=True)
                            self.session_subscriptions.discard(session_id)
                            try:
                                session.StatusChangeCallback(None)
                            except Exception:
                                self.debug(traceback.format_exc())
                            retry = True
                        if session_id not in self.sessions:
                            new_session_ids.add(session_id)
                self.sessions = new_sessions
                self.configs = new_configs
                self.config_names = new_config_names
                # Legacy STARTNAME settings are only safe for unique names.
                name_configs = dict()
                for config_id, config_name in new_config_names.items():
                    if config_name in name_configs:
                        name_configs[config_name] = None
                    else:
                        name_configs[config_name] = config_id
                self.name_configs = name_configs
                self.config_sessions = new_config_sessions
                self.session_configs = new_session_configs
                self.session_names = new_session_names
                self.session_statuses = new_session_statuses
                self.sessions_connected.intersection_update(new_sessions)
                self.session_subscriptions.intersection_update(new_sessions)
                self.backend_proxies_invalid = False

                self.debug(f'Configs: {sorted(self.configs.keys())}')
                self.debug(f'Sessions: {sorted(self.sessions.keys())}')
                self.debug(f'Config names: {self.config_names}')
                self.debug(f'Config sessions: {self.config_sessions}')
                self.debug(f'Session configs: {self.session_configs}')
                self.debug(f'Session statuses: {self.session_statuses}')
                self.invalid_sessions = retry
                self.invalid_ui = True
            except Exception:
                self.warning('Session state refresh failed', exc_info=True)
                return
            for session_id in new_session_ids:
                session_status = self.session_statuses[session_id]
                self.on_session_event(session_id, session_status['major'], session_status['minor'], session_status['message'])
            for session_id, dialog in list(self.session_dialogs.items()):
                if session_id not in self.sessions:
                    dialog.destroy()
                    self.session_dialogs.pop(session_id, None)

    def get_config_name(self, config_id):
        return self.config_names.get(config_id, DEFAULT_CONFIG_NAME)

    def get_session_name(self, session_id):
        config_id = self.session_configs.get(session_id, '')
        return self.config_names.get(config_id, self.session_names.get(session_id, DEFAULT_SESSION_NAME))

    def action_settings_startup(self, _object, value):
        self.settings.set_string('startup-action', value)
        self.invalid_ui = True
        self.refresh_ui()

    def construct_menu_settings_startup(self):
        startup_action = self.settings.get_string('startup-action') or ''
        menu = Gtk.Menu()
        menu_action = ''
        menu_title = gettext.gettext('No Connection')
        if startup_action == menu_action:
            menu_title += ' ✓'
        menu_item = Gtk.MenuItem.new_with_label(menu_title)
        menu_item.connect('activate', self.action_settings_startup, menu_action)
        menu.append(menu_item)
        menu_action = 'RESTART'
        menu_title = gettext.gettext('Restart Connection')
        if startup_action == menu_action:
            menu_title += ' ✓'
        menu_item = Gtk.MenuItem.new_with_label(menu_title)
        menu_item.connect('activate', self.action_settings_startup, menu_action)
        menu.append(menu_item)
        for config_name, config_id in self.menu_configuration_key():
            menu_action = f'STARTID:{config_id}'
            menu_title = gettext.gettext('Start {name}').format(name=config_name)
            if startup_action == menu_action or (
                startup_action == f'STARTNAME:{config_name}' and self.name_configs.get(config_name) == config_id
            ):
                menu_title += ' ✓'
            menu_item = Gtk.MenuItem.new_with_label(menu_title)
            menu_item.connect('activate', self.action_settings_startup, menu_action)
            menu.append(menu_item)
        return menu

    def construct_menu_config(self, config_id):
        menu = Gtk.Menu()
        menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Connect'))
        menu_item.connect('activate', self.action_config_connect, config_id)
        menu.append(menu_item)
        menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Remove'))
        menu_item.connect('activate', self.action_config_remove, config_id)
        menu.append(menu_item)
        return menu

    def session_menu_action_key(self, session_id):
        status = self.session_statuses[session_id]
        if openvpn3.StatusMajor.CONNECTION == status['major']:
            if openvpn3.StatusMinor.CONN_CONNECTED == status['minor']:
                return 'pause'
            if openvpn3.StatusMinor.CONN_PAUSED == status['minor']:
                return 'resume'
        return None

    def menu_configuration_key(self):
        return tuple(sorted((name, config_id) for config_id, name in self.config_names.items()))

    def session_menu_key(self, session_id):
        available_configurations = tuple(
            (config_name, config_id)
            for config_name, config_id in self.menu_configuration_key()
            if len(self.config_sessions[config_id]) == 0
        )
        return (
            session_id,
            self.get_session_name(session_id),
            self.session_menu_action_key(session_id),
            available_configurations,
            self.settings.get_string('startup-action'),
            self.menu_configuration_key(),
        )

    def idle_menu_key(self):
        configurations = tuple(
            (
                config_name,
                config_id,
                tuple(
                    (session_id, self.session_menu_action_key(session_id))
                    for session_id in sorted(self.config_sessions[config_id])
                ),
            )
            for config_name, config_id in self.menu_configuration_key()
        )
        return (
            configurations,
            self.settings.get_string('startup-action'),
            self.menu_configuration_key(),
        )

    def construct_menu_session(self, session_id):
        menu = Gtk.Menu()
        status = self.session_statuses[session_id]
        major = status['major']
        minor = status['minor']
        menu_item = Gtk.MenuItem.new_with_label(self.get_session_name(session_id))
        menu.append(menu_item)

        if False: #TODO: When does it make sense to allow explicit Connect?
            menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Connect'))
            menu_item.connect('activate', self.action_session_connect, session_id)
            menu.append(menu_item)
        if openvpn3.StatusMajor.CONNECTION == major and openvpn3.StatusMinor.CONN_CONNECTED == minor:
            menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Pause'))
            menu_item.connect('activate', self.action_session_pause, session_id)
            menu.append(menu_item)
        if openvpn3.StatusMajor.CONNECTION == major and openvpn3.StatusMinor.CONN_PAUSED == minor:
            menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Resume'))
            menu_item.connect('activate', self.action_session_resume, session_id)
            menu.append(menu_item)
        if True:
            menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Restart'))
            menu_item.connect('activate', self.action_session_restart, session_id)
            menu.append(menu_item)
        if True:
            menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Disconnect'))
            menu_item.connect('activate', self.action_session_disconnect, session_id)
            menu.append(menu_item)
        return menu

    def construct_session_menu(self, session_id):
        menu = self.construct_menu_session(session_id)
        menu.append(Gtk.SeparatorMenuItem())
        add_separator = False
        for config_name, config_id in self.menu_configuration_key():
            if len(self.config_sessions[config_id]) == 0:
                config_menu = self.construct_menu_config(config_id)
                menu_item = Gtk.MenuItem.new_with_label(config_name)
                menu_item.set_submenu(config_menu)
                menu.append(menu_item)
                add_separator = True
        if add_separator:
            menu.append(Gtk.SeparatorMenuItem())
        menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Import Config'))
        menu_item.connect('activate', self.action_config_import)
        menu.append(menu_item)
        menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Startup Settings'))
        menu_item.set_submenu(self.construct_menu_settings_startup())
        menu.append(menu_item)
        menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('About'))
        menu_item.connect('activate', self.action_about)
        menu.append(menu_item)
        menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Quit'))
        menu_item.connect('activate', self.action_quit)
        menu.append(menu_item)
        menu.show_all()
        return menu

    def construct_idle_menu(self):
        menu = Gtk.Menu()
        for config_name, config_id in self.menu_configuration_key():
            session_ids = self.config_sessions[config_id]
            if len(session_ids) > 0:
                for session_id in session_ids:
                    #TODO: Add some information on session status to menu items (perhaps in the title?)
                    session = self.sessions[session_id]
                    session_menu = self.construct_menu_session(session_id)
                    menu_item = Gtk.MenuItem.new_with_label(config_name)
                    menu_item.set_submenu(session_menu)
                    menu.append(menu_item)
            else:
                config_menu = self.construct_menu_config(config_id)
                menu_item = Gtk.MenuItem.new_with_label(config_name)
                menu_item.set_submenu(config_menu)
                menu.append(menu_item)
        if len(self.config_names) > 0:
            menu.append(Gtk.SeparatorMenuItem())
        menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Import Config'))
        menu_item.connect('activate', self.action_config_import)
        menu.append(menu_item)
        menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Startup Settings'))
        menu_item.set_submenu(self.construct_menu_settings_startup())
        menu.append(menu_item)
        menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('About'))
        menu_item.connect('activate', self.action_about)
        menu.append(menu_item)
        menu_item = Gtk.MenuItem.new_with_label(gettext.gettext('Quit'))
        menu_item.connect('activate', self.action_quit)
        menu.append(menu_item)
        menu.show_all()
        return menu

    def session_icon(self, session_id):
        status = self.session_statuses[session_id]
        major = status['major']
        minor = status['minor']
        return get_status_icon(major, minor)

    def session_description(self, session_id):
        status = self.session_statuses[session_id]
        major = status['major']
        minor = status['minor']
        return get_status_description(major, minor)

    def notify_session_change(self, session_id):
        indicator = self.indicators.get(session_id, None)
        if indicator:
            indicator.icon = self.session_icon(session_id)
        notifier = self.notifiers.get(session_id, None)
        if notifier:
            notifier.active = False
            notifier.icon = self.session_icon(session_id)
            notifier.body = self.session_description(session_id)
            notifier.timespan = 3
            notifier.active = True

    def on_session_manager_event(self, event):
        self.info(f'Session Manager Event {event}')
        event_type = event.GetType()
        if openvpn3.SessionManagerEventType.SESS_CREATED == event_type:
            self.invalid_sessions = True
        elif openvpn3.SessionManagerEventType.SESS_DESTROYED == event_type:
            self.remove_session(str(event.GetPath()))
            self.invalid_sessions = True

    def on_session_manager_signal(self, path, event_type, owner):
        self.on_session_manager_event(openvpn3.SessionManagerEvent(path, event_type, owner))

    def on_openvpn_service_owner_changed(self, name, old_owner, new_owner):
        if self.shutting_down or not hasattr(self, 'sessions'):
            return
        self.info(f'OpenVPN service {name} owner changed')
        # Session/configuration proxies and log subscriptions are bound to
        # the old unique owner. Rebuild them on the next successful refresh.
        self.backend_proxies_invalid = True
        self.invalid_sessions = True

    def remove_session(self, session_id):
        session = self.sessions.pop(session_id, None)
        if session is not None:
            try:
                session.StatusChangeCallback(None)
            except Exception:
                self.debug(traceback.format_exc())
        self.session_statuses.pop(session_id, None)
        self.session_configs.pop(session_id, None)
        self.session_names.pop(session_id, None)
        self.sessions_connected.discard(session_id)
        self.session_subscriptions.discard(session_id)
        for session_ids in self.config_sessions.values():
            if session_id in session_ids:
                session_ids.remove(session_id)
        dialog = self.session_dialogs.pop(session_id, None)
        if dialog is not None:
            dialog.destroy()
        self.invalid_sessions = True
        self.invalid_ui = True

    def on_network_manager_event(self, event):
        self.info(f'Network Manager Event {event}')

    def can_store_input_slot(self, input_slot):
        type, group = input_slot.GetTypeGroup()
        result = type == openvpn3.ClientAttentionType.CREDENTIALS and group in [
                openvpn3.ClientAttentionGroup.UNSET,
                openvpn3.ClientAttentionGroup.USER_PASSWORD,
                openvpn3.ClientAttentionGroup.HTTP_PROXY_CREDS,
                openvpn3.ClientAttentionGroup.PK_PASSPHRASE,
                #openvpn3.ClientAttentionGroup.CHALLENGE_STATIC,
                #openvpn3.ClientAttentionGroup.CHALLENGE_DYNAMIC,
                #openvpn3.ClientAttentionGroup.CHALLENGE_AUTH_PENDING,
            ]
        self.debug(f'Input slot {input_slot.GetLabel()} of type {type}, group {group} is decided {"not " if not result else ""}safe for storage')
        return result
        print(type,group)

    def on_session_event(self, session_id, major, minor, message):
        if session_id not in self.sessions:
            return
        session = self.sessions[session_id]
        major = openvpn3.StatusMajor(major)
        minor = openvpn3.StatusMinor(minor)
        message = str(message)
        self.info(f'Session Event {major} {minor} {message}')
        self.session_statuses[session_id] = {
            'major' : major,
            'minor' : minor,
            'message' : message,
        }
        self.invalid_ui = True

        if minor.name in TERMINAL_SESSION_STATUSES:
            self.remove_session(session_id)
            return

        if openvpn3.StatusMajor.CONNECTION == major and openvpn3.StatusMinor.CFG_OK == minor:
            try:
                if session_id not in self.sessions_connected:
                    session.Ready()
                    session.Connect()
                    self.sessions_connected.add(session_id)
            except: #TODO: Catch only expected exceptions
                self.debug(traceback.format_exc())
        if openvpn3.StatusMajor.SESSION == major and openvpn3.StatusMinor.SESS_AUTH_URL == minor:
            self.action_auth_url(None, session_id, message)
        if openvpn3.StatusMajor.CONNECTION == major and openvpn3.StatusMinor.CONN_AUTH_FAILED == minor:
            #TODO: Notify authentication failure
            #TODO: Record authentication failure

            self.action_session_disconnect(None, session_id)
            config_id = self.session_configs.get(session_id, None)
            if config_id is not None:
                self.failed_authentications.add(config_id)
                self.action_config_connect(None, config_id)
        if openvpn3.StatusMajor.CONNECTION == major and openvpn3.StatusMinor.CONN_CONNECTED == minor:
            config_id = self.session_configs.get(session_id, None)
            if config_id is not None and config_id in self.failed_authentications:
                self.failed_authentications.remove(config_id)

        if openvpn3.StatusMajor.CONNECTION == major and openvpn3.StatusMinor.CONN_FAILED == minor:
            #TODO: Notify connection failure
            #TODO: Record connection failure
            self.action_session_disconnect(None, session_id)
        if openvpn3.StatusMajor.CONNECTION == major and openvpn3.StatusMinor.CONN_DISCONNECTED == minor:
            pass
        if openvpn3.StatusMajor.CONNECTION == major and openvpn3.StatusMinor.CONN_DONE == minor:
            pass
        if openvpn3.StatusMajor.CONNECTION == major and openvpn3.StatusMinor.CFG_REQUIRE_USER == minor:
            try:
                required_credentials = list()
                for input_slot in session.FetchUserInputSlots():
                    if input_slot.GetTypeGroup()[0] != openvpn3.ClientAttentionType.CREDENTIALS:
                        continue
                    description = str(input_slot.GetLabel())
                    mask = bool(input_slot.GetInputMask())
                    can_store = self.can_store_input_slot(input_slot)
                    required_credentials.append((description, mask, can_store))
                force_ui = False
                config_id = self.session_configs.get(session_id, None)
                if config_id is not None:
                    if config_id in self.failed_authentications:
                        force_ui = True
                self.action_get_credentials(None, session_id, required_credentials, force_ui=force_ui)
            except: #TODO: Catch only expected exceptions
                self.debug(traceback.format_exc())
                #TODO: Catch only expected exceptions
                #TODO: Notify authentication failure
                #TODO: Record authentication failure
                self.action_session_disconnect(None, session_id)
        self.notify_session_change(session_id)

    def action_auth_url(self, _object, session_id, url):
        webbrowser.open_new(url)

    def store_set_credentials(self, config_id, credentials):
        store = self.credential_store[config_id]
        for key, value in credentials.items():
            store[key] = value

    def store_clear_credentials(self, config_id, credentials_keys):
        store = self.credential_store[config_id]
        for key in store.keys():
            if key in credentials_keys:
                del store[key]

    def store_get_credentials(self, config_id):
        credentials = dict()
        store = self.credential_store[config_id]
        for key in store.keys():
            credentials[key] = store[key]
        return credentials

    def action_get_credentials(self, _object, session_id, required_credentials, force_ui=False):
        credentials = dict()
        required_keys = set([ description for description, mask, can_store in required_credentials ])
        config_id = self.session_configs.get(session_id, None)
        if config_id is not None:
            for key, value in self.store_get_credentials(config_id).items():
                if key in required_keys:
                    credentials[key] = value

        require_ui = False
        for key in required_keys:
            if key not in credentials:
                require_ui = True
                break

        if require_ui or force_ui:
            user_inputs = [ CredentialsUserInput(
                    name=description,
                    mask=mask,
                    value=credentials.get(description, None) if can_store else None,
                    can_store=can_store)
                for description, mask, can_store in required_credentials ]


            def on_cancel():
                status = self.session_statuses.get(session_id, None)
                if status is None:
                    return
                major = status['major']
                minor = status['minor']
                if openvpn3.StatusMajor.CONNECTION == major and openvpn3.StatusMinor.CFG_REQUIRE_USER == minor:
                    self.action_session_disconnect(None, session_id)
                if session_id in self.session_dialogs:
                    del self.session_dialogs[session_id]

            def on_connect(user_inputs, store):
                if self.shutting_down or session_id not in self.sessions:
                    return
                credentials = dict([ (ui.name, ui.value) for ui in user_inputs ])
                if config_id is not None:
                    if store:
                        store_credentials = dict([ (ui.name, ui.value) for ui in user_inputs if ui.can_store and ui.value ])
                        self.store_set_credentials(config_id, store_credentials)
                    else:
                        self.store_clear_credentials(config_id, credentials.keys())
                if session_id in self.session_dialogs:
                    del self.session_dialogs[session_id]
                self.on_session_credentials(session_id, credentials)

            session_name = self.get_session_name(session_id)
            def remain_active():
                self.debug('Remain active')
                if session_id in self.session_statuses:
                    major = self.session_statuses[session_id]['major']
                    minor = self.session_statuses[session_id]['minor']
                    if openvpn3.StatusMajor.CONNECTION == major and openvpn3.StatusMinor.CFG_REQUIRE_USER == minor:
                        return True
                if session_id in self.session_dialogs:
                    del self.session_dialogs[session_id]
                return False
            dialog = construct_credentials_dialog(session_name, user_inputs, on_connect=on_connect, on_cancel=on_cancel, remain_active=remain_active)
            dialog.set_visible(True)
            if session_id in self.session_dialogs:
                self.session_dialogs[session_id].destroy()
            self.session_dialogs[session_id] = dialog
        else:
            self.on_session_credentials(session_id, credentials)

    def on_session_credentials(self, session_id, credentials):
        session = self.sessions.get(session_id)
        if self.shutting_down or session is None:
            return
        try:
            for input_slot in session.FetchUserInputSlots():
                if input_slot.GetTypeGroup()[0] != openvpn3.ClientAttentionType.CREDENTIALS:
                    continue
                input_slot.ProvideInput(credentials.get(input_slot.GetLabel(), ''))
            session.Ready()
            session.Connect()
            self.sessions_connected.add(session_id)
        except: #TODO: Catch only expected exceptions
            self.debug(traceback.format_exc())
            self.action_session_disconnect(None, session_id)

    def on_schedule(self):
        if self.shutting_down:
            return False
        try:
            self.refresh_scheduled_state()
        except Exception:
            # GLib removes a timeout whose callback raises. Keep this timer
            # alive so a transient error cannot freeze the menu permanently.
            self.warning('Scheduled refresh failed; retrying', exc_info=True)
            self.invalid_sessions = True
            self.invalid_ui = True
        return True

    def refresh_scheduled_state(self):
        self.debug(f'Schedule')
        if self.last_invalid + 30 < time.monotonic():
            self.debug('Forced refresh of sessions')
            self.invalid_sessions = True
        if self.invalid_sessions:
            self.last_invalid = time.monotonic()
            self.refresh_sessions()
        if self.invalid_ui:
            self.refresh_ui()
        self.multi_notifier.update()
        if not self.invalid_sessions and (self.startup_config_id or self.startup_config_name):
            config_id = self.startup_config_id or self.name_configs.get(self.startup_config_name, None)
            if config_id in self.configs and len(self.config_sessions.get(config_id, [])) == 0:
                self.debug(f'Starting config {config_id} as requested in startup settings.')
                self.action_config_connect(None, config_id)
            self.startup_config_id = None
            self.startup_config_name = None

    def action_config_connect(self, _object, config_id):
        self.info(f'Connect Config {config_id}')
        if config_id not in self.configs:
            return
        try:
            session = self.session_manager.NewTunnel(self.configs[config_id])
            self.settings.set_string('most-recent-configuration-id', config_id)
        except: #TODO: Catch only expected exceptions
            self.debug(traceback.format_exc())
            pass

    def action_config_remove(self, _object, config_id):
        self.info(f'Remove Config {config_id}')
        if config_id not in self.configs:
            return
        try:
            def on_remove():
                if config_id not in self.configs:
                    return
                self.configs[config_id].Remove()
                self.invalid_sessions = True
            dialog = construct_configuration_remove_dialog(name=self.get_config_name(config_id), on_remove=on_remove)
            dialog.set_visible(True)
        except: #TODO: Catch only expected exceptions
            self.debug(traceback.format_exc())
            pass

    def action_session_connect(self, _object, session_id):
        self.info(f'Connect Session {session_id}')
        if session_id not in self.sessions:
            return
        try:
            self.sessions[session_id].Connect()
        except: #TODO: Catch only expected exceptions
            self.debug(traceback.format_exc())
            pass

    def action_session_pause(self, _object, session_id):
        self.info(f'Pause Session {session_id}')
        if session_id not in self.sessions:
            return
        try:
            self.sessions[session_id].Pause()
        except: #TODO: Catch only expected exceptions
            self.debug(traceback.format_exc())
            pass

    def action_session_resume(self, _object, session_id):
        self.info(f'Resume Session {session_id}')
        if session_id not in self.sessions:
            return
        try:
            self.sessions[session_id].Resume()
        except: #TODO: Catch only expected exceptions
            self.debug(traceback.format_exc())
            pass

    def action_session_restart(self, _object, session_id):
        self.info(f'Restart Session {session_id}')
        if session_id not in self.sessions:
            return
        try:
            self.sessions[session_id].Restart()
        except: #TODO: Catch only expected exceptions
            self.debug(traceback.format_exc())
            pass

    def action_session_disconnect(self, _object, session_id):
        self.info(f'Disconnect Session {session_id}')
        if session_id not in self.sessions:
            return
        try:
            self.sessions[session_id].Disconnect()
        except: #TODO: Catch only expected exceptions
            self.debug(traceback.format_exc())
            pass

    def on_config_import(self, name, path):
        self.info(f'Import Config {name} {path}')
        try:
            try:
                config_description = pathlib.Path(path).read_text()
            except FileNotFoundError:
                self.error(
                    msg=f"Configuration file not found: {path}",
                    notify=False,
                    dialog=True,
                    title="Configuration Import Failed"
                )
                return
            except PermissionError:
                self.error(
                    msg=f"Permission denied accessing file: {path}",
                    notify=False,
                    dialog=True,
                    title="Configuration Import Failed"
                )
                return
            except UnicodeDecodeError:
                self.error(
                    msg=f"File encoding error: {path}\nUnable to read file as text. Please check if this is a valid OpenVPN configuration file.",
                    notify=False,
                    dialog=True,
                    title="Configuration Import Failed"
                )
                return
            except OSError as e:
                self.error(
                    msg=f"Error reading file: {path}\n{str(e)}",
                    notify=False,
                    dialog=True,
                    title="Configuration Import Failed"
                )
                return
            try:
                import_args = dict()
                if self.manager_version > 20:
                    # system_tag arrived in openvpn3-linux v21
                    import_args['system_tag'] = APPLICATION_SYSTEM_TAG
                config_obj = self.config_manager.Import(name, config_description, single_use=False, persistent=True, **import_args)
            except dbus.exceptions.DBusException as excp:
                msg = excp.get_dbus_message()
                msg = re.sub(r'^.*GDBus.Error:[^\s]*', '', msg).strip()
                self.error(
                    msg=f"Failed to import configuration {name}:\n{msg}",
                    notify=False,
                    dialog=True,
                    title="Configuration Import Failed"
                )
                return
            except Exception as e:
                self.error(
                    msg=f"Unexpected error during configuration import:\n{str(e)}",
                    notify=False,
                    dialog=True,
                    title="Configuration Import Failed"
                )
                return
            if self.manager_version >= 22:
                try:
                    v = config_obj.Validate()
                except dbus.exceptions.DBusException as excp:
                    msg = excp.get_dbus_message()
                    msg = re.sub(r'^.*GDBus.Error:[^\s]*', '', msg).strip()
                    self.error(
                        msg=f"OpenVPN Config {name} imported from {path} failed validation:\n{msg}",
                        notify=False,
                        dialog=True,
                        title="Configuration Import Failed"
                    )
                    self.info(f'Removing Config {name}')
                    config_obj.Remove()
                    return

            self.invalid_sessions = True
            self.info(msg=f'Successfully imported config {name} from {path}', notify=True)
        except:
            self.debug(traceback.format_exc())
            self.error(
                msg=f"Unexpected error importing configuration {name} from {path}",
                notify=False,
                dialog=True,
                title="Configuration Import Failed"
            )

    def action_config_import(self, _object):
        self.info(f'Import Config')
        dialog = construct_configuration_select_dialog(on_import=self.on_config_import)
        dialog.set_visible(True)

    def action_config_open(self, path):
        self.info(f'Import Config {path}')
        dialog = construct_configuration_import_dialog(path=path, on_import=self.on_config_import)
        dialog.set_visible(True)

    def action_about(self, _object):
        self.info(f'About')
        dialog = construct_about_dialog()
        dialog.set_visible(True)

    def action_quit(self, _object):
        self.info(f'Quit')
        self.quit()

    def on_termination_signal(self):
        self.quit()
        return False

    def logging_notify(self, msg, title=f'{APPLICATION_NAME}', icon='active'):
        icon = f'{APPLICATION_NAME}-{icon}'
        self.multi_notifier.new_notifier(
            identifier = 'logging',
            title = title,
            body = msg,
            icon = icon,
            active = True,
            timespan = 2,
            mute_repetitions = False,
        )
        self.multi_notifier.update()

    def debug(self, msg, notify=False, *args, **kwargs):
        logging.debug(msg, *args, **kwargs)
        if notify:
            self.logging_notify(msg)

    def info(self, msg, notify=False, dialog=False, title=None, *args, **kwargs):
        logging.info(msg, *args, **kwargs)
        if notify:
            self.logging_notify(msg)

        if dialog:
            show_info_notification(title=title, message=msg)

    def warning(self, msg, notify=False, dialog=False, title=None, *args, **kwargs):
        logging.warning(msg, *args, **kwargs)
        if notify:
            self.logging_notify(msg, icon='active-error')

        if dialog:
            show_warning_notification(title=title, message=msg)

    def error(self, msg, notify=False, dialog=False, title=None, *args, **kwargs):
        logging.error(msg, *args, **kwargs)
        if notify:
            self.logging_notify(msg, icon="active-error")

        if dialog:
            show_error_dialog(title=title, message=msg)
