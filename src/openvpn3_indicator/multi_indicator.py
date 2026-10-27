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

import logging
import uuid

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3

from openvpn3_indicator.about import *

MENU_UNSET = object()

###
#
# MultiIndicator
#
###

class MultiIndicator():

    @property
    def identifier(self):
        return self._identifier

    def sub_identifier(self, num):
        # Separate generation and slot: generation 1, slot 0 must never
        # reuse the path of generation 0, slot 1.
        return f'{self.identifier}-generation-{self._registration_generation}-slot-{num}'

    def sub_indicator(self, num):
        while len(self._sub_indicators) <= num:
            sub = AppIndicator3.Indicator.new(
                self.sub_identifier(len(self._sub_indicators)),
                self.default_icon,
                self.default_category
                )
            sub.set_ordering_index(num)
            self._sub_indicators.append(sub)
            self._sub_menu_keys.append(MENU_UNSET)
        return self._sub_indicators[num]

    def __init__(self, identifier):
        self._identifier = identifier
        # AppIndicator uses this identifier in its D-Bus object path.  A new
        # value after a watcher restart prevents GNOME Shell from reusing a
        # stale tray item from before suspend/lock.
        self._registration_generation = 0
        self._sub_indicators = list()
        self._sub_menu_keys = list()
        self._indicators = dict()
        self._available = True
        self.default_icon = f'{APPLICATION_NAME}'
        self.default_description = f'{APPLICATION_TITLE}'
        self.default_title = f'{APPLICATION_TITLE}'
        self.default_category = AppIndicator3.IndicatorCategory.SYSTEM_SERVICES
        self.invalid = False

    def invalidate(self):
        self.invalid = True

    def set_available(self, available):
        if self._available != available:
            self._available = available
            if not available:
                self.reset()
            self.invalidate()

    def menu_token(self, indicator):
        # Menus with identical labels can target different sessions. Include
        # both logical identity and the menu object, including unkeyed menus.
        return (indicator.identifier, indicator.menu_key, indicator.menu)

    class Indicator():

        @property
        def parent(self):
            return self._parent

        @property
        def identifier(self):
            return self._identifier

        def __init__(self, parent, identifier, active=False, icon=None, description=None, title=None, order_key=None, menu=None):
            self._parent = parent
            self._identifier = identifier
            self._active = active
            self._icon = icon or self.parent.default_icon
            self._description = description or self.parent.default_description
            self._title = title or self.parent.default_title
            self._order_key = order_key or self.identifier
            self._menu = menu
            self._menu_key = None

        def close(self):
            if self.parent:
                self.parent.del_indicator(self)

        @property
        def active(self):
            return self._active
        @active.setter
        def active(self, active):
            active = bool(active)
            if self._active != active:
                self._active = active
                if self.parent:
                    self.parent.invalidate()

        @property
        def icon(self):
            return self._icon
        @icon.setter
        def icon(self, icon):
            icon = str(icon)
            if self._icon != icon:
                self._icon = icon
                if self.parent and self.active:
                    self.parent.invalidate()

        @property
        def description(self):
            return self._description
        @description.setter
        def description(self, description):
            description = str(description)
            if self._description != description:
                self._description = description
                if self.parent and self.active:
                    self.parent.invalidate()

        @property
        def title(self):
            return self._title
        @title.setter
        def title(self, title):
            title = str(title)
            if self._title != title:
                self._title = title
                if self.parent and self.active:
                    self.parent.invalidate()

        @property
        def order_key(self):
            return self._order_key
        @order_key.setter
        def order_key(self, order_key):
            order_key = str(order_key)
            if self._order_key != order_key:
                self._order_key = order_key
                if self.parent and self.active:
                    self.parent.invalidate()

        @property
        def menu(self):
            return self._menu
        @menu.setter
        def menu(self, menu):
            if self._menu != menu:
                self._menu = menu
                if self.parent and self.active:
                    self.parent.invalidate()

        @property
        def menu_key(self):
            return self._menu_key

        def set_menu(self, menu_key, menu):
            if self._menu_key != menu_key:
                self._menu_key = menu_key
                self.menu = menu
                return True
            return False

    def new_indicator(self, **kwargs):
        identifier = str(uuid.uuid4())
        indicator = self.Indicator(self, identifier, **kwargs)
        self._indicators[identifier] = indicator
        logging.debug(f'Created Indicator {identifier}')
        if indicator.active:
            self.invalidate()
        return indicator

    def del_indicator(self, indicator):
        if indicator.parent == self:
            if indicator.identifier in self._indicators:
                del self._indicators[indicator.identifier]
                if indicator.active:
                    self.invalidate()
                logging.debug(f'Destroyed Indicator {indicator.identifier}')
            indicator._parent = None

    def commit_indicator(self, indicator, num):
        target = self.sub_indicator(num)
        target.set_icon_full(indicator.icon, indicator.description)
        target.set_title(indicator.title)
        if self._sub_menu_keys[num] != self.menu_token(indicator):
            if indicator.menu:
                target.set_menu(indicator.menu)
            else:
                target.set_menu(Gtk.Menu())
            self._sub_menu_keys[num] = self.menu_token(indicator)
        target.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

    def hide_indicator(self, num):
        target = self.sub_indicator(num)
        target.set_status(AppIndicator3.IndicatorStatus.PASSIVE)

    def reset(self):
        for indicator in self._sub_indicators:
            indicator.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
        # AppIndicator can keep an old registration after the
        # StatusNotifierWatcher restarts (for example when the screen is
        # unlocked).  Drop the GObject instances so the next update creates
        # fresh registrations instead of leaving duplicate tray icons.
        self._registration_generation += 1
        self._sub_indicators = list()
        self._sub_menu_keys = list()
        self.invalid = True

    def update(self):
        if not self._available:
            return
        if self.invalid:
            logging.debug('Repairing Indicators')
            indicators = list()
            for indicator in self._indicators.values():
                if indicator.active:
                    indicators.append(indicator)
            indicators = sorted(indicators, key=lambda i : i.order_key)

            # AppIndicator does not reliably replace the D-Bus menu of an
            # existing item after GNOME Shell has rebuilt its watcher.  A
            # fresh item is required, otherwise its icon can represent a
            # connected session while it still exposes the old idle menu.
            menu_changed = any(
                num >= len(self._sub_menu_keys)
                or self._sub_menu_keys[num] != self.menu_token(indicator)
                for num, indicator in enumerate(indicators)
            )
            if menu_changed and self._sub_indicators:
                self.reset()

            num = 0
            for indicator in indicators:
                self.commit_indicator(indicator, num)
                num += 1
            for other in range(num, len(self._sub_indicators)):
                self.hide_indicator(other)
            self.invalid=False

    def close(self):
        self._available = False
        self.reset()
        for indicator in list(self._indicators.values()):
            indicator.close()
