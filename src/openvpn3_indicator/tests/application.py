#!/usr/bin/env python3

import unittest
from unittest import mock

import dbus
import openvpn3

from openvpn3_indicator.application import Application
from openvpn3_indicator.multi_indicator import MENU_UNSET, MultiIndicator


class StatusNotifierWatcherTest(unittest.TestCase):
    def test_get_status_notifier_watcher_owner_requires_watcher_protocol(self):
        properties = mock.Mock()
        watcher = mock.Mock()
        bus = mock.Mock()
        bus.get_name_owner.return_value = ':1.42'
        bus.get_object.return_value = watcher
        application = mock.Mock(session_bus=bus)

        with mock.patch('openvpn3_indicator.application.dbus.Interface', return_value=properties):
            owner = Application.get_status_notifier_watcher_owner(application)

        self.assertEqual(':1.42', owner)
        bus.get_object.assert_called_once_with('org.kde.StatusNotifierWatcher', '/StatusNotifierWatcher')
        self.assertEqual(properties.Get.call_args_list, [
            mock.call('org.kde.StatusNotifierWatcher', 'ProtocolVersion'),
            mock.call('org.kde.StatusNotifierWatcher', 'IsStatusNotifierHostRegistered'),
        ])

    def test_get_status_notifier_watcher_owner_rejects_owner_change(self):
        properties = mock.Mock()
        bus = mock.Mock()
        bus.get_name_owner.side_effect = [':1.42', ':1.43']
        application = mock.Mock(session_bus=bus)

        with mock.patch('openvpn3_indicator.application.dbus.Interface', return_value=properties):
            with self.assertRaises(dbus.exceptions.DBusException):
                Application.get_status_notifier_watcher_owner(application)

    def test_watcher_without_host_is_not_ready(self):
        properties = mock.Mock()
        properties.Get.side_effect = [0, False]
        application = mock.Mock()
        with mock.patch('openvpn3_indicator.application.dbus.Interface', return_value=properties):
            with self.assertRaises(dbus.exceptions.DBusException):
                Application.get_status_notifier_watcher_owner(application)

    def test_unavailable_watcher_is_retried_with_backoff(self):
        application = mock.Mock(status_notifier_watcher_refresh_generation=1, shutting_down=False)
        application.get_status_notifier_watcher_owner.side_effect = dbus.exceptions.DBusException('not ready')
        application.schedule_status_notifier_watcher_refresh.side_effect = lambda *args: Application.schedule_status_notifier_watcher_refresh(application, *args)

        with mock.patch('openvpn3_indicator.application.GLib.timeout_add') as timeout_add:
            result = Application.refresh_status_notifier_watcher(application, 1, 20)

        self.assertFalse(result)
        application.warning.assert_called_once()
        timeout_add.assert_called_once_with(
            5000,
            application.refresh_status_notifier_watcher,
            1,
            21,
        )

    def test_ready_watcher_republishes_after_passive_status(self):
        application = mock.Mock(status_notifier_watcher_refresh_generation=1, shutting_down=False)
        application.get_status_notifier_watcher_owner.return_value = ':1.42'

        with mock.patch('openvpn3_indicator.application.GLib.timeout_add') as timeout_add:
            result = Application.refresh_status_notifier_watcher(application, 1, 0)

        self.assertFalse(result)
        application.multi_indicator.reset.assert_called_once_with()
        application.multi_indicator.update.assert_not_called()
        timeout_add.assert_called_once_with(
            100,
            application.republish_status_notifier_watcher,
            1,
        )

    def test_late_callback_cannot_republish_after_another_lock(self):
        application = mock.Mock(status_notifier_watcher_refresh_generation=2, shutting_down=False)
        Application.republish_status_notifier_watcher(application, 1)
        application.multi_indicator.update.assert_not_called()

    def test_host_can_restart_without_changing_bus_owner(self):
        application = mock.Mock(shutting_down=False)
        Application.on_status_notifier_host_registered(application)
        application.begin_status_notifier_watcher_refresh.assert_called_once_with()

    def test_no_publication_while_host_is_unavailable(self):
        indicators = MultiIndicator('test')
        indicators.new_indicator(active=True)
        indicators.set_available(False)
        with mock.patch('openvpn3_indicator.multi_indicator.AppIndicator3.Indicator.new') as create:
            indicators.update()
        create.assert_not_called()
        self.assertTrue(indicators.invalid)

    def test_registration_paths_are_unique_across_generations_and_slots(self):
        indicators = MultiIndicator('test')
        identifiers = set()
        for generation in range(3):
            for slot in range(3):
                identifiers.add(indicators.sub_identifier(slot))
            indicators.reset()
        self.assertEqual(9, len(identifiers))

    def test_same_menu_labels_cannot_reuse_another_sessions_item(self):
        indicators = MultiIndicator('test')
        first = indicators.new_indicator(active=True)
        first.set_menu(('custom', 'pause'), object())
        with mock.patch('openvpn3_indicator.multi_indicator.AppIndicator3.Indicator.new') as create:
            indicators.update()
            old_path = create.call_args.args[0]
            first.close()
            second = indicators.new_indicator(active=True)
            second.set_menu(('custom', 'pause'), object())
            indicators.update()
            self.assertNotEqual(old_path, create.call_args.args[0])
            self.assertEqual(2, create.call_count)

    def test_indicator_replaces_menu_only_when_its_key_changes(self):
        parent = mock.Mock()
        parent.default_icon = 'icon'
        parent.default_description = 'description'
        parent.default_title = 'title'
        indicator = MultiIndicator.Indicator(parent, 'test')
        first_menu = object()

        self.assertTrue(indicator.set_menu(('idle',), first_menu))
        self.assertFalse(indicator.set_menu(('idle',), object()))
        self.assertIs(first_menu, indicator.menu)
        self.assertTrue(indicator.set_menu(('connected',), object()))

    def test_committing_an_icon_change_does_not_reset_the_menu(self):
        multi_indicator = MultiIndicator.__new__(MultiIndicator)
        multi_indicator._sub_menu_keys = [MENU_UNSET]
        target = mock.Mock()
        multi_indicator.sub_indicator = lambda num: target
        indicator = mock.Mock(
            icon='icon',
            description='description',
            title='title',
            menu=object(),
            menu_key=('idle',),
        )

        MultiIndicator.commit_indicator(multi_indicator, indicator, 0)
        MultiIndicator.commit_indicator(multi_indicator, indicator, 0)

        target.set_menu.assert_called_once_with(indicator.menu)
        indicator.menu_key = ('connected',)
        MultiIndicator.commit_indicator(multi_indicator, indicator, 0)
        self.assertEqual(2, target.set_menu.call_count)

    def test_first_commit_configures_an_empty_menu(self):
        multi_indicator = MultiIndicator.__new__(MultiIndicator)
        multi_indicator._sub_menu_keys = [MENU_UNSET]
        target = mock.Mock()
        multi_indicator.sub_indicator = lambda num: target
        indicator = mock.Mock(
            icon='icon',
            description='description',
            title='title',
            menu=None,
            menu_key=None,
        )

        with mock.patch('openvpn3_indicator.multi_indicator.Gtk.Menu') as menu:
            MultiIndicator.commit_indicator(multi_indicator, indicator, 0)

        target.set_menu.assert_called_once_with(menu.return_value)


class SessionRefreshTest(unittest.TestCase):
    def make_application(self):
        application = mock.Mock(
            invalid_sessions=True, invalid_ui=False, backend_proxies_invalid=False,
            sessions={}, configs={}, config_names={}, name_configs={},
            config_sessions={}, session_configs={}, session_names={},
            session_statuses={}, session_dialogs={}, sessions_connected=set(),
            session_subscriptions=set(), shutting_down=False,
        )
        application.config_manager.FetchAvailableConfigs.return_value = []
        return application

    def make_session(self, path, config_path='/config/custom'):
        session = mock.Mock()
        session.GetPath.return_value = path
        session.GetStatus.return_value = {
            'major': openvpn3.StatusMajor.CONNECTION,
            'minor': openvpn3.StatusMinor.CONN_CONNECTED,
            'message': '',
        }
        session.GetProperty.side_effect = lambda name: {'config_path': config_path, 'config_name': 'custom'}[name]
        return session

    def test_each_status_callback_targets_its_own_session(self):
        application = self.make_application()
        sessions = [self.make_session('/session/one'), self.make_session('/session/two')]
        application.session_manager.FetchAvailableSessions.return_value = sessions
        Application.refresh_sessions(application)
        application.on_session_event.reset_mock()
        for session in sessions:
            callback = session.StatusChangeCallback.call_args.args[0]
            callback(2, 7, 'connected')
        self.assertEqual(application.on_session_event.call_args_list, [
            mock.call('/session/one', 2, 7, 'connected'),
            mock.call('/session/two', 2, 7, 'connected'),
        ])

    def test_list_failure_preserves_last_successful_snapshot(self):
        application = self.make_application()
        application.sessions = {'/session/one': self.make_session('/session/one')}
        snapshot = application.sessions
        application.session_manager.FetchAvailableSessions.side_effect = dbus.exceptions.DBusException('unavailable')
        Application.refresh_sessions(application)
        self.assertIs(snapshot, application.sessions)
        self.assertTrue(application.invalid_sessions)

    def test_disappearing_session_does_not_hide_other_sessions(self):
        application = self.make_application()
        gone = self.make_session('/session/gone')
        gone.GetStatus.side_effect = dbus.exceptions.DBusException(
            'gone', name='org.freedesktop.DBus.Error.UnknownObject',
        )
        live = self.make_session('/session/live')
        application.session_manager.FetchAvailableSessions.return_value = [gone, live]
        Application.refresh_sessions(application)
        self.assertEqual({'/session/live'}, set(application.sessions))
        self.assertTrue(application.invalid_sessions)

    def test_configuration_failure_does_not_hide_live_sessions(self):
        application = self.make_application()
        application.session_manager.FetchAvailableSessions.return_value = [self.make_session('/session/live')]
        application.config_manager.FetchAvailableConfigs.side_effect = dbus.exceptions.DBusException('unavailable')
        Application.refresh_sessions(application)
        self.assertIn('/session/live', application.sessions)
        self.assertEqual('custom', application.session_names['/session/live'])

    def test_equal_config_names_are_matched_by_path(self):
        application = self.make_application()
        configs = []
        for path in ('/config/one', '/config/two'):
            config = mock.Mock()
            config.GetPath.return_value = path
            config.GetConfigName.return_value = 'custom'
            configs.append(config)
        application.config_manager.FetchAvailableConfigs.return_value = configs
        application.session_manager.FetchAvailableSessions.return_value = [self.make_session('/session/live', '/config/two')]
        Application.refresh_sessions(application)
        self.assertEqual([], application.config_sessions['/config/one'])
        self.assertEqual(['/session/live'], application.config_sessions['/config/two'])
        self.assertIsNone(application.name_configs['custom'])
        self.assertEqual((('custom', '/config/one'), ('custom', '/config/two')),
                         Application.menu_configuration_key(application))
        application.session_manager.LookupConfigName.assert_not_called()

    def test_empty_authoritative_list_removes_stale_sessions(self):
        application = self.make_application()
        old = self.make_session('/session/old')
        application.sessions = {'/session/old': old}
        application.sessions_connected = {'/session/old'}
        application.session_manager.FetchAvailableSessions.return_value = []
        Application.refresh_sessions(application)
        self.assertEqual({}, application.sessions)
        self.assertEqual(set(), application.sessions_connected)
        old.StatusChangeCallback.assert_called_once_with(None)

    def test_scheduled_refresh_continues_after_an_exception(self):
        application = self.make_application()
        application.refresh_scheduled_state.side_effect = RuntimeError('temporary failure')
        self.assertTrue(Application.on_schedule(application))
        self.assertTrue(application.invalid_sessions)
        self.assertTrue(application.invalid_ui)

    def test_credentials_callback_ignores_a_session_removed_with_dialog_open(self):
        application = self.make_application()
        Application.on_session_credentials(application, '/session/removed', {})
        application.action_session_disconnect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
