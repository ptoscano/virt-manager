#
# Copyright (C) 2006, 2013-2014 Red Hat, Inc.
# Copyright (C) 2006 Daniel P. Berrange <berrange@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301 USA.
#

import logging
import re
import queue
import threading
import traceback

from gi.repository import Gio
from gi.repository import GLib
from gi.repository import Gtk

from . import packageutils
from .baseclass import vmmGObject
from .clone import vmmCloneVM
from .connmanager import vmmConnectionManager
from .connect import vmmConnect
from .create import vmmCreate
from .details import vmmDetails
from .error import vmmErrorDialog
from .host import vmmHost
from .inspection import vmmInspection
from .manager import vmmManager
from .migrate import vmmMigrateDialog
from .systray import vmmSystray

DETAILS_PERF = 1
DETAILS_CONFIG = 2
DETAILS_CONSOLE = 3

(PRIO_HIGH,
 PRIO_LOW) = range(1, 3)


class _ConnState(object):
    def __init__(self, uri, probe):
        self.uri = uri

        self.probeConnection = probe

        self.windowClone = None
        self.windowDetails = {}
        self.windowHost = None


class vmmEngine(vmmGObject):
    CLI_SHOW_MANAGER = "manager"
    CLI_SHOW_DOMAIN_CREATOR = "creator"
    CLI_SHOW_DOMAIN_EDITOR = "editor"
    CLI_SHOW_DOMAIN_PERFORMANCE = "performance"
    CLI_SHOW_DOMAIN_CONSOLE = "console"
    CLI_SHOW_HOST_SUMMARY = "summary"

    def __init__(self):
        vmmGObject.__init__(self)

        self.windowConnect = None
        self.windowCreate = None
        self.windowManager = None
        self.windowMigrate = None

        self._connstates = {}
        self.err = vmmErrorDialog()
        self.err.set_find_parent_cb(self._find_error_parent_cb)

        self.timer = None
        self.last_timeout = 0

        self._systray = None

        self._gtkapplication = None
        self._init_gtk_application()

        self._tick_counter = 0
        self._tick_thread_slow = False
        self._tick_thread = threading.Thread(name="Tick thread",
                                            target=self._handle_tick_queue,
                                            args=())
        self._tick_thread.daemon = True
        self._tick_queue = queue.PriorityQueue(100)

        vmmInspection.get_instance()

        # Counter keeping track of how many manager and details windows
        # are open. When it is decremented to 0, close the app or
        # keep running in system tray if enabled
        self.windows = 0

        self.add_gsettings_handle(
            self.config.on_stats_update_interval_changed(self.reschedule_timer))

        self.schedule_timer()
        for uri in self._connobjs:
            self._add_conn(uri, False)

        self._tick_thread.start()
        self.tick()


    @property
    def _connobjs(self):
        return vmmConnectionManager.get_instance().conns


    ############################
    # Gtk Application handling #
    ############################

    def _on_gtk_application_activated(self, ignore):
        """
        Invoked after application.run()
        """
        if not self._application.get_windows():
            logging.debug("Initial gtkapplication activated")
            self._application.add_window(Gtk.Window())

    def _init_gtk_application(self):
        self._application = Gtk.Application(
            application_id="org.virt-manager.virt-manager", flags=0)
        self._application.register(None)
        self._application.connect("activate",
            self._on_gtk_application_activated)

        action = Gio.SimpleAction.new("cli_command",
            GLib.VariantType.new("(sss)"))
        action.connect("activate", self._handle_cli_command)
        self._application.add_action(action)

    def _default_startup(self, skip_autostart, cliuri):
        self._init_systray()

        uris = list(self._connstates.keys())
        if not uris:
            logging.debug("No stored URIs found.")
        else:
            logging.debug("Loading stored URIs:\n%s",
                "  \n".join(sorted(uris)))

        if not skip_autostart:
            self.idle_add(self.autostart_conns)

        if not self.config.get_conn_uris() and not cliuri:
            # Only add default if no connections are currently known
            self.timeout_add(1000, self._add_default_conn)

    def start(self, uri, show_window, domain, skip_autostart):
        # Dispatch dbus CLI command
        if uri and not show_window:
            show_window = self.CLI_SHOW_MANAGER
        data = GLib.Variant("(sss)",
            (uri or "", show_window or "", domain or ""))
        self._application.activate_action("cli_command", data)

        if self._application.get_is_remote():
            logging.debug("Connected to remote app instance.")
            return

        self._default_startup(skip_autostart, uri)
        self._application.run(None)


    def _init_systray(self):
        self._systray = vmmSystray()
        self._systray.connect("action-toggle-manager", self._do_toggle_manager)
        self._systray.connect("action-show-domain", self._do_show_vm)
        self._systray.connect("action-migrate-domain", self._do_show_migrate)
        self._systray.connect("action-clone-domain", self._do_show_clone)
        self._systray.connect("action-exit-app", self.exit_app)

        self.add_gsettings_handle(
            self.config.on_view_system_tray_changed(self._system_tray_changed))

    def _system_tray_changed(self, *ignore):
        systray_enabled = self.config.get_view_system_tray()
        if self.windows == 0 and not systray_enabled:
            # Show the manager so that the user can control the application
            self._show_manager()

    def _add_default_conn(self):
        manager = self.get_manager()

        # Manager fail message
        msg = _("Could not detect a default hypervisor. Make\n"
                "sure the appropriate virtualization packages\n"
                "containing kvm, qemu, libvirt, etc. are\n"
                "installed, and that libvirtd is running.\n\n"
                "A hypervisor connection can be manually\n"
                "added via File->Add Connection")

        logging.debug("Determining default libvirt URI")

        packages_verified = False
        try:
            libvirt_packages = self.config.libvirt_packages
            packages = self.config.hv_packages + libvirt_packages

            packages_verified = packageutils.check_packagekit(
                    manager, manager.err, packages)
        except Exception:
            logging.exception("Error talking to PackageKit")

        tryuri = None
        if packages_verified:
            tryuri = "qemu:///system"
        elif not self.config.test_first_run:
            tryuri = vmmConnect.default_uri()

        if tryuri is None:
            manager.set_startup_error(msg)
            return

        warnmsg = _("The 'libvirtd' service will need to be started.\n\n"
                    "After that, virt-manager will connect to libvirt on\n"
                    "the next application start up.")

        # Do the initial connection in an idle callback, so the
        # packagekit async dialog has a chance to go away
        def idle_connect():
            libvirtd_started = packageutils.start_libvirtd()
            connected = False
            try:
                self.connect_to_uri(tryuri, autoconnect=True)
                connected = True
            except Exception:
                logging.exception("Error connecting to %s", tryuri)

            if not connected and not libvirtd_started:
                manager.err.ok(_("Libvirt service must be started"), warnmsg)

        self.idle_add(idle_connect)

    def autostart_conns(self):
        """
        We serialize conn autostart, so polkit/ssh-askpass doesn't spam
        """
        connections_queue = queue.Queue()
        auto_conns = [conn.get_uri() for conn in self._connobjs.values() if
                      conn.get_autoconnect()]

        def add_next_to_queue():
            if not auto_conns:
                connections_queue.put(None)
            else:
                connections_queue.put(auto_conns.pop(0))

        def state_change_cb(conn):
            if conn.is_active():
                add_next_to_queue()
                conn.disconnect_by_func(state_change_cb)

        def handle_queue():
            while True:
                uri = connections_queue.get()
                if uri is None:
                    return
                if uri not in self._connobjs:
                    add_next_to_queue()
                    continue

                conn = self._connobjs[uri]
                conn.connect("state-changed", state_change_cb)
                self.idle_add(self.connect_to_uri, uri)

        add_next_to_queue()
        self._start_thread(handle_queue, "Conn autostart thread")


    def _do_vm_removed(self, conn, connkey):
        detailsmap = self._connstates[conn.get_uri()].windowDetails
        if connkey not in detailsmap:
            return

        detailsmap[connkey].cleanup()
        detailsmap.pop(connkey)

    def _do_vm_renamed(self, conn, oldconnkey, newconnkey):
        detailsmap = self._connstates[conn.get_uri()].windowDetails
        if oldconnkey not in detailsmap:
            return

        detailsmap[newconnkey] = detailsmap.pop(oldconnkey)

    def _do_conn_changed(self, conn):
        if conn.is_active() or conn.is_connecting():
            return

        uri = conn.get_uri()

        detailsmap = self._connstates[conn.get_uri()].windowDetails
        for connkey in list(detailsmap):
            detailsmap[connkey].cleanup()
            detailsmap.pop(connkey)

        if (self.windowCreate and
            self.windowCreate.conn and
            self.windowCreate.conn.get_uri() == uri):
            self.windowCreate.close()

    def reschedule_timer(self, *args, **kwargs):
        ignore1 = args
        ignore2 = kwargs
        self.schedule_timer()

    def schedule_timer(self):
        interval = self.config.get_stats_update_interval() * 1000

        if self.timer is not None:
            self.remove_gobject_timeout(self.timer)
            self.timer = None

        self.timer = self.timeout_add(interval, self.tick)

    def _add_obj_to_tick_queue(self, obj, isprio, **kwargs):
        if self._tick_queue.full():
            if not self._tick_thread_slow:
                logging.debug("Tick is slow, not running at requested rate.")
                self._tick_thread_slow = True
            return

        self._tick_counter += 1
        self._tick_queue.put((isprio and PRIO_HIGH or PRIO_LOW,
                              self._tick_counter,
                              obj, kwargs))

    def _schedule_priority_tick(self, conn, kwargs):
        self._add_obj_to_tick_queue(conn, True, **kwargs)

    def tick(self):
        for conn in self._connobjs.values():
            self._add_obj_to_tick_queue(conn, False,
                                        stats_update=True, pollvm=True)
        return 1

    def _handle_tick_error(self, msg, details):
        if self.windows <= 0:
            # This means the systray icon is running. Don't raise an error
            # here to avoid spamming dialogs out of nowhere.
            logging.debug(msg + "\n\n" + details)
            return
        self.err.show_err(msg, details=details)

    def _handle_tick_queue(self):
        while True:
            ignore1, ignore2, conn, kwargs = self._tick_queue.get()
            try:
                conn.tick_from_engine(**kwargs)
            except Exception as e:
                tb = "".join(traceback.format_exc())
                error_msg = (_("Error polling connection '%s': %s")
                    % (conn.get_uri(), e))
                self.idle_add(self._handle_tick_error, error_msg, tb)

            # Need to clear reference to make leak check happy
            conn = None
            self._tick_queue.task_done()
        return 1


    def increment_window_counter(self, src):
        ignore = src
        self.windows += 1
        logging.debug("window counter incremented to %s", self.windows)

    def decrement_window_counter(self, src):
        self.windows -= 1
        logging.debug("window counter decremented to %s", self.windows)

        self._exit_app_if_no_windows(src)

    def _can_exit(self):
        # Don't exit if system tray is enabled
        return (self.windows <= 0 and
                self._systray and
                not self._systray.is_visible())

    def _cleanup(self):
        self.err = None

        if self.timer is not None:
            GLib.source_remove(self.timer)

        if self._systray:
            self._systray.cleanup()
            self._systray = None

        self.get_manager()
        if self.windowManager:
            self.windowManager.cleanup()
            self.windowManager = None

        if self.windowConnect:
            self.windowConnect.cleanup()
            self.windowConnect = None

        if self.windowCreate:
            self.windowCreate.cleanup()
            self.windowCreate = None

        if self.windowMigrate:
            self.windowMigrate.cleanup()
            self.windowMigrate = None

        # Do this last, so any manually 'disconnected' signals
        # take precedence over cleanup signal removal
        for uri in self._connstates:
            self._cleanup_connstate(uri)
        self._connstates = {}
        vmmConnectionManager.get_instance().cleanup()

    def _exit_app_if_no_windows(self, src=None):
        def cb():
            if self._can_exit():
                logging.debug("No windows found, requesting app exit")
                self.exit_app(src or self)
        self.idle_add(cb)

    def exit_app(self, src):
        if self.err is None:
            # Already in cleanup
            return

        self.cleanup()

        if self.config.test_leak_debug:
            objs = self.config.get_objects()

            # Engine will always appear to leak
            objs.remove(self.object_key)

            if src and src.object_key in objs:
                # UI that initiates the app exit will always appear to leak
                objs.remove(src.object_key)

            for name in objs:
                logging.debug("LEAK: %s", name)

        logging.debug("Exiting app normally.")
        self._application.quit()

    def _find_error_parent_cb(self):
        """
        Search over the toplevel windows for any that are visible or have
        focus, and use that
        """
        windowlist = [self.windowManager]
        for connstate in self._connstates.values():
            windowlist.extend(list(connstate.windowDetails.values()))
            windowlist += [connstate.windowHost]

        use_win = None
        for window in windowlist:
            if not window:
                continue
            if window.topwin.has_focus():
                use_win = window
                break
            if not use_win and window.is_visible():
                use_win = window

        if use_win:
            return use_win.topwin

    def _add_conn(self, uri, probe):
        if uri in self._connstates:
            return self._connobjs[uri]

        connstate = _ConnState(uri, probe)
        conn = vmmConnectionManager.get_instance().add_conn(uri)
        conn.connect("vm-removed", self._do_vm_removed)
        conn.connect("vm-renamed", self._do_vm_renamed)
        conn.connect("state-changed", self._do_conn_changed)
        conn.connect("connect-error", self._connect_error)
        conn.connect("priority-tick", self._schedule_priority_tick)
        self._connstates[uri] = connstate
        return conn

    def _remove_conn(self, _src, uri):
        self._cleanup_connstate(uri)
        self._connstates.pop(uri)
        vmmConnectionManager.get_instance().remove_conn(uri)

    def connect_to_uri(self, uri, autoconnect=None, probe=False):
        conn = self._add_conn(uri, probe=probe)

        if autoconnect is not None:
            conn.set_autoconnect(bool(autoconnect))

        conn.open()


    def _cleanup_connstate(self, uri):
        try:
            connstate = self._connstates[uri]
            if connstate.windowHost:
                connstate.windowHost.cleanup()
            if connstate.windowClone:
                connstate.windowClone.cleanup()

            detailsmap = connstate.windowDetails
            for win in list(detailsmap.values()):
                win.cleanup()
        except Exception:
            logging.exception("Error cleaning up conn in engine")


    def _connect_error(self, conn, errmsg, tb, warnconsole):
        errmsg = errmsg.strip(" \n")
        tb = tb.strip(" \n")
        hint = ""
        show_errmsg = True

        if conn.is_remote():
            logging.debug("connect_error: conn transport=%s",
                conn.get_uri_transport())
            if re.search(r"nc: .* -- 'U'", tb):
                hint += _("The remote host requires a version of netcat/nc "
                          "which supports the -U option.")
                show_errmsg = False
            elif (conn.get_uri_transport() == "ssh" and
                  re.search(r"ssh-askpass", tb)):

                askpass = (self.config.askpass_package and
                           self.config.askpass_package[0] or
                           "openssh-askpass")
                hint += _("You need to install %s or "
                          "similar to connect to this host.") % askpass
                show_errmsg = False
            else:
                hint += _("Verify that the 'libvirtd' daemon is running "
                          "on the remote host.")

        elif conn.is_xen():
            hint += _("Verify that:\n"
                      " - A Xen host kernel was booted\n"
                      " - The Xen service has been started")

        else:
            if warnconsole:
                hint += _("Could not detect a local session: if you are "
                          "running virt-manager over ssh -X or VNC, you "
                          "may not be able to connect to libvirt as a "
                          "regular user. Try running as root.")
                show_errmsg = False
            elif re.search(r"libvirt-sock", tb):
                hint += _("Verify that the 'libvirtd' daemon is running.")
                show_errmsg = False

        connstate = self._connstates[conn.get_uri()]
        msg = _("Unable to connect to libvirt %s." % conn.get_uri())
        if show_errmsg:
            msg += "\n\n%s" % errmsg
        if hint:
            msg += "\n\n%s" % hint

        msg = msg.strip("\n")
        details = msg
        details += "\n\n"
        details += "Libvirt URI is: %s\n\n" % conn.get_uri()
        details += tb

        if connstate.probeConnection:
            msg += "\n\n"
            msg += _("Would you still like to remember this connection?")

        title = _("Virtual Machine Manager Connection Failure")
        if connstate.probeConnection:
            remember_connection = self.err.show_err(msg, details, title,
                    buttons=Gtk.ButtonsType.YES_NO,
                    dialog_type=Gtk.MessageType.QUESTION, modal=True)
            if remember_connection:
                connstate.probeConnection = False
            else:
                self.idle_add(self._do_edit_connect, self.windowManager, conn)
        else:
            if self._can_exit():
                self.err.show_err(msg, details, title, modal=True)
                self._exit_app_if_no_windows(conn)
            else:
                self.err.show_err(msg, details, title)


    ####################
    # Dialog launchers #
    ####################

    def _get_host_dialog(self, uri):
        connstate = self._connstates[uri]
        if connstate.windowHost:
            return connstate.windowHost

        conn = self._connobjs[uri]
        obj = vmmHost(conn)

        obj.connect("action-exit-app", self.exit_app)
        obj.connect("action-view-manager", self._do_show_manager)
        obj.connect("host-opened", self.increment_window_counter)
        obj.connect("host-closed", self.decrement_window_counter)

        connstate.windowHost = obj
        return connstate.windowHost

    def _do_show_host(self, src, uri):
        try:
            self._get_host_dialog(uri).show()
        except Exception as e:
            src.err.show_err(_("Error launching host dialog: %s") % str(e))


    def _get_connect_dialog(self):
        if self.windowConnect:
            return self.windowConnect

        def completed(_src, uri, autoconnect):
            self.connect_to_uri(uri, autoconnect, probe=True)

        def cancelled(src):
            if not self._connstates:
                self.exit_app(src)

        obj = vmmConnect()
        obj.connect("completed", completed)
        obj.connect("cancelled", cancelled)
        self.windowConnect = obj
        return self.windowConnect


    def _do_show_connect(self, src, reset_state=True):
        try:
            self._get_connect_dialog().show(src.topwin, reset_state)
        except Exception as e:
            src.err.show_err(_("Error launching connect dialog: %s") % str(e))

    def _do_edit_connect(self, src, connection):
        try:
            self._do_show_connect(src, False)
        finally:
            self._remove_conn(src, connection.get_uri())


    def _get_details_dialog(self, uri, connkey):
        detailsmap = self._connstates[uri].windowDetails
        if connkey in detailsmap:
            return detailsmap[connkey]

        obj = vmmDetails(self._connobjs[uri].get_vm(connkey))
        obj.connect("action-exit-app", self.exit_app)
        obj.connect("action-view-manager", self._do_show_manager)
        obj.connect("action-migrate-domain", self._do_show_migrate)
        obj.connect("action-clone-domain", self._do_show_clone)
        obj.connect("details-opened", self.increment_window_counter)
        obj.connect("details-closed", self.decrement_window_counter)

        detailsmap[connkey] = obj
        return detailsmap[connkey]

    def _show_vm_helper(self, src, uri, connkey, page, forcepage):
        try:
            details = self._get_details_dialog(uri, connkey)

            if forcepage or not details.is_visible():
                if page == DETAILS_PERF:
                    details.activate_performance_page()
                elif page == DETAILS_CONFIG:
                    details.activate_config_page()
                elif page == DETAILS_CONSOLE:
                    details.activate_console_page()
                elif page is None:
                    details.activate_default_page()

            details.show()
        except Exception as e:
            src.err.show_err(_("Error launching details: %s") % str(e))

    def _do_show_vm(self, src, uri, connkey):
        self._show_vm_helper(src, uri, connkey, None, False)

    def get_manager(self):
        if self.windowManager:
            return self.windowManager

        obj = vmmManager()
        obj.connect("action-migrate-domain", self._do_show_migrate)
        obj.connect("action-clone-domain", self._do_show_clone)
        obj.connect("action-show-domain", self._do_show_vm)
        obj.connect("action-show-create", self._do_show_create)
        obj.connect("action-show-host", self._do_show_host)
        obj.connect("action-show-connect", self._do_show_connect)
        obj.connect("action-exit-app", self.exit_app)
        obj.connect("manager-opened", self.increment_window_counter)
        obj.connect("manager-closed", self.decrement_window_counter)
        obj.connect("remove-conn", self._remove_conn)

        self.windowManager = obj
        return self.windowManager

    def _do_toggle_manager(self, ignore):
        manager = self.get_manager()
        if manager.is_visible():
            manager.close()
        else:
            manager.show()

    def _do_show_manager(self, src):
        try:
            manager = self.get_manager()
            manager.show()
        except Exception as e:
            if not src:
                raise
            src.err.show_err(_("Error launching manager: %s") % str(e))

    def _get_create_dialog(self):
        if self.windowCreate:
            return self.windowCreate

        obj = vmmCreate()
        obj.connect("action-show-domain", self._do_show_vm)
        obj.connect("create-opened", self.increment_window_counter)
        obj.connect("create-closed", self.decrement_window_counter)
        self.windowCreate = obj
        return self.windowCreate

    def _do_show_create(self, src, uri):
        try:
            self._get_create_dialog().show(src.topwin, uri)
        except Exception as e:
            src.err.show_err(_("Error launching manager: %s") % str(e))

    def _do_show_migrate(self, src, uri, connkey):
        try:
            vm = self._connobjs[uri].get_vm(connkey)

            if not self.windowMigrate:
                self.windowMigrate = vmmMigrateDialog()

            self.windowMigrate.show(src.topwin, vm)
        except Exception as e:
            src.err.show_err(_("Error launching migrate dialog: %s") % str(e))

    def _do_show_clone(self, src, uri, connkey):
        conn = self._connobjs[uri]
        connstate = self._connstates[uri]
        orig_vm = conn.get_vm(connkey)

        clone_window = connstate.windowClone
        try:
            if clone_window is None:
                clone_window = vmmCloneVM(orig_vm)
                connstate.windowClone = clone_window
            else:
                clone_window.set_orig_vm(orig_vm)

            clone_window.show(src.topwin)
        except Exception as e:
            src.err.show_err(_("Error setting clone parameters: %s") % str(e))


    ##########################################
    # Window launchers from virt-manager cli #
    ##########################################

    def _find_vm_by_cli_str(self, uri, clistr):
        """
        Lookup a VM by a string passed in on the CLI. Can be either
        ID, domain name, or UUID
        """
        if clistr.isdigit():
            clistr = int(clistr)

        for vm in self._connobjs[uri].list_vms():
            if clistr == vm.get_id():
                return vm
            elif clistr == vm.get_name():
                return vm
            elif clistr == vm.get_uuid():
                return vm

    def _cli_show_vm_helper(self, uri, clistr, page):
        src = self.get_manager()

        vm = self._find_vm_by_cli_str(uri, clistr)
        if not vm:
            src.err.show_err("%s does not have VM '%s'" %
                (uri, clistr), modal=True)
            return

        self._show_vm_helper(src, uri, vm.get_connkey(), page, True)

    def _show_manager(self):
        self._do_show_manager(None)

    def _show_host_summary(self, uri):
        self._do_show_host(self.get_manager(), uri)

    def _show_domain_creator(self, uri):
        self._do_show_create(self.get_manager(), uri)

    def _show_domain_console(self, uri, clistr):
        self._cli_show_vm_helper(uri, clistr, DETAILS_CONSOLE)

    def _show_domain_editor(self, uri, clistr):
        self._cli_show_vm_helper(uri, clistr, DETAILS_CONFIG)

    def _show_domain_performance(self, uri, clistr):
        self._cli_show_vm_helper(uri, clistr, DETAILS_PERF)

    def _launch_cli_window(self, uri, show_window, clistr):
        try:
            logging.debug("Launching requested window '%s'", show_window)
            if show_window == self.CLI_SHOW_MANAGER:
                self.get_manager().set_initial_selection(uri)
                self._show_manager()
            elif show_window == self.CLI_SHOW_DOMAIN_CREATOR:
                self._show_domain_creator(uri)
            elif show_window == self.CLI_SHOW_DOMAIN_EDITOR:
                self._show_domain_editor(uri, clistr)
            elif show_window == self.CLI_SHOW_DOMAIN_PERFORMANCE:
                self._show_domain_performance(uri, clistr)
            elif show_window == self.CLI_SHOW_DOMAIN_CONSOLE:
                self._show_domain_console(uri, clistr)
            elif show_window == self.CLI_SHOW_HOST_SUMMARY:
                self._show_host_summary(uri)
            else:
                raise RuntimeError("Unknown cli window command '%s'" %
                    show_window)
        finally:
            # In case of cli error, we may need to exit the app
            self._exit_app_if_no_windows()

    def _cli_conn_connected_cb(self, conn, uri, show_window, domain):
        try:
            ignore = conn

            if conn.is_disconnected():
                raise RuntimeError("failed to connect to cli uri=%s" % uri)

            if conn.is_active():
                self._launch_cli_window(uri, show_window, domain)
                return True

            return False
        except Exception:
            # In case of cli error, we may need to exit the app
            logging.debug("Error in cli connection callback", exc_info=True)
            self._exit_app_if_no_windows()
            return True

    def _do_handle_cli_command(self, actionobj, variant):
        ignore = actionobj
        uri = variant[0]
        show_window = variant[1]
        domain = variant[2]

        logging.debug("processing cli command uri=%s show_window=%s domain=%s",
            uri, show_window, domain)
        if not uri:
            logging.debug("No cli action requested, launching default window")
            self._show_manager()
            return

        conn = self._add_conn(uri, False)

        if conn.is_disconnected():
            # Schedule connection open
            self.idle_add(self.connect_to_uri, uri)

        if show_window:
            if conn.is_active():
                self.idle_add(self._launch_cli_window,
                    uri, show_window, domain)
            else:
                conn.connect_opt_out("state-changed",
                    self._cli_conn_connected_cb, uri, show_window, domain)
        else:
            self.get_manager().set_initial_selection(uri)
            self._show_manager()

    def _handle_cli_command(self, actionobj, variant):
        try:
            return self._do_handle_cli_command(actionobj, variant)
        except Exception:
            # In case of cli error, we may need to exit the app
            logging.debug("Error handling cli command", exc_info=True)
            self._exit_app_if_no_windows()
