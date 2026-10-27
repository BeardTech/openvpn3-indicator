# Indicator lifecycle review

This review covers the startup, lock/unlock, stale-menu and session-state
problems reported during development. The changes below have been reviewed
in source; their automated regression tests have **not been run**.

## Corrections

| Trigger | Previous failure | Current handling |
| --- | --- | --- |
| GNOME starts slowly or its extension is temporarily disabled | Startup stopped after a fixed readiness timeout | Wait asynchronously and publish when a watcher and host are available |
| Watcher owner changes during lock/unlock | Lost or duplicated tray items | Suspend publishing, retire old items, recreate them with fresh D-Bus paths |
| Host registers again under the same owner | Owner-change handler did not run | Listen for host-registration/property signals and periodically check readiness |
| Host disappears during recovery | Regular UI refresh could publish prematurely | Suppress publication until readiness is checked again |
| Menu changes from idle to a live session | Existing item could keep its previous exported menu | Recreate the item when its menu token changes |
| Two sessions have the same labels/actions | Menu cache could target the previous session | Include logical indicator identity, session ID and menu object in caching |
| Multiple slots across several watcher generations | A generated ID could collide with a previous slot ID | Explicit generation and slot components in every native indicator ID |
| Status changes on multiple VPNs | Loop closure attributed callbacks to the last session | Bind each callback's session ID when subscribing |
| A session vanishes while being read | Entire state was cleared, including other live VPNs | Skip the missing object; retain other sessions and retry |
| Session/configuration listing fails temporarily | Live VPNs were presented as absent | Retain the last successful state; retry and log the failure with its traceback |
| Session removal is confirmed | Stale Restart/Disconnect actions persisted | Remove the particular session on destruction/terminal events or an authoritative list |
| A status signal is missed | Polling updated menus but not existing icons | Refresh icon, title and menu from the session snapshot |
| OpenVPN session/configuration/log daemon changes owner | Old proxies or log subscriptions stayed bound to the old daemon | Rebuild proxies/subscriptions and refresh the authoritative state |
| Configurations have equal names or a profile is removed while a VPN runs | Name lookup associated sessions incorrectly or lost their display name | Resolve configurations by their paths and retain the session's own name |
| A periodic callback raises | No subsequent refresh was scheduled | Keep the recurring timer alive and retry |
| Startup configuration is removed or state is temporarily incomplete | KeyError or premature startup connection | Wait for a complete snapshot and check configuration existence |
| A credential dialog responds after its session vanished | Callback indexed a removed session or stored credentials without a config | Ignore vanished sessions and only store credentials for a known config |
| Quit, SIGTERM or SIGINT | Cleanup could be bypassed; notifications remained | Quit through the application lifecycle, unregister signals/items and withdraw notifications |
| Existing build executable belongs to root | Rebuilding attempted to truncate an unwritable file | Build a temporary executable and replace the artifact atomically; fail on zip errors |

## Remaining validation

These are manual scenarios, not checks already performed for this revision:

1. Reboot with and without an existing VPN; inspect the first menu.
2. Lock/unlock repeatedly, both idle and connected. Verify one icon per live
   session and correct actions after each unlock.
3. Connect, pause, resume, restart and disconnect; check icon/menu agreement.
4. Open two sessions and change only one; ensure its own menu/actions update.
5. Remove a session externally; ensure its actions disappear while other
   sessions remain visible.
6. Interrupt/restart OpenVPN services in a controlled environment; check
   eventual resynchronization and useful logs.
7. Disable/re-enable the AppIndicator extension; ensure automatic recovery.
8. Quit and restart the user service; check no active duplicate items or
   notifications remain.

The OpenVPN Python SDK still performs synchronous D-Bus calls, including
internal retries. An unavailable daemon can therefore delay the main loop
during a refresh. This revision keeps retries alive but does not move SDK
calls to a background worker. During an unreadable session list, the UI
retains the last known state, which can be temporarily out of date.

Atomic executable replacement requires a writable parent directory. It
does not repair root-owned preparation directories left by earlier sudo
builds. Prefer building with `make all` as the normal user, then installing
with `sudo make install` once directory ownership is correct.
