# -*- coding: utf-8 -*-
#
# Copyright (c) 2018 Red Hat, Inc.
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
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.

"""offline_upgrade.py - DNF plugin to handle major-version system upgrades."""

from __future__ import unicode_literals
import json
import os

from dnf.cli import CliError
from dnfpluginsextras import _, logger
import dnf
import dnf.cli
import dnf.transaction


DEFAULT_DATADIR = '/var/lib/dnf/offline-upgrade'
DEFAULT_DESTDIR = '/tmp/offline-upgrade'
CMDS = ['download', 'clean', 'prepare', 'upgrade']

RELEASEVER_MSG = _(
    "Need a --releasever greater than the current system version.")
DOWNLOAD_FINISHED_MSG = _(
    "Download complete! Use 'dnf offline-upgrade prepare' to start the upgrade.\n"
    "To remove cached metadata and transaction use 'dnf offline-upgrade clean'")
CANT_RESET_RELEASEVER = _(
    "Sorry, you need to use 'download --releasever' instead of '--network'")


def clear_dir(path):
    if not os.path.isdir(path):
        return

    for entry in os.listdir(path):
        fullpath = os.path.join(path, entry)
        try:
            if os.path.isdir(fullpath):
                dnf.util.rm_rf(fullpath)
            else:
                os.unlink(fullpath)
        except OSError:
            pass


def checkReleaseVer(conf, target=None):
    if dnf.rpm.detect_releasever(conf.installroot) == conf.releasever:
        raise CliError(RELEASEVER_MSG)
    if target and target != conf.releasever:
        # it's too late to set releasever here, so this can't work.
        # (see https://bugzilla.redhat.com/show_bug.cgi?id=1212341)
        raise CliError(CANT_RESET_RELEASEVER)


class State(object):
    statefile = '/var/lib/dnf/offline-upgrade.json'

    def __init__(self):
        self._data = {}
        self._read()

    def _read(self):
        try:
            with open(self.statefile) as fp:
                self._data = json.load(fp)
        except IOError:
            self._data = {}

    def write(self):
        dnf.util.ensure_dir(os.path.dirname(self.statefile))
        with open(self.statefile, 'w') as outf:
            json.dump(self._data, outf)

    def clear(self):
        if os.path.exists(self.statefile):
            os.unlink(self.statefile)
        self._read()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.write()

    def _prop(option):
        def setprop(self, value):
            self._data[option] = value

        def getprop(self):
            return self._data.get(option)
        return property(getprop, setprop)

    download_status = _prop("download_status")
    destdir = _prop("destdir")
    target_releasever = _prop("target_releasever")
    system_releasever = _prop("system_releasever")
    gpgcheck = _prop("gpgcheck")
    upgrade_status = _prop("upgrade_status")
    distro_sync = _prop("distro_sync")
    allow_erasing = _prop("allow_erasing")
    enable_disable_repos = _prop("enable_disable_repos")
    best = _prop("best")
    exclude = _prop("exclude")
    install_packages = _prop("install_packages")


class OfflineUpgradePlugin(dnf.Plugin):
    name = 'offline-upgrade'

    def __init__(self, base, cli):
        super(OfflineUpgradePlugin, self).__init__(base, cli)
        if cli:
            cli.register_command(OfflineUpgradeCommand)


class OfflineUpgradeCommand(dnf.cli.Command):
    aliases = ('offline-upgrade',)
    summary = _("Prepare system for an offline upgrade to a new release")

    def __init__(self, cli):
        super(OfflineUpgradeCommand, self).__init__(cli)
        self.state = State()

    @staticmethod
    def set_argparser(parser):
        parser.add_argument('tid', nargs=1, choices=CMDS,
                            metavar="[%s]" % "|".join(CMDS))

    def pre_configure(self):
        self._call_sub("pre_configure")

    def configure(self):
        self._call_sub("configure")
        self._call_sub("check")

    def run(self):
        self._call_sub("run")

    def run_transaction(self):
        self._call_sub("transaction")

    def _call_sub(self, name):
        subfunc = getattr(self, name + '_' + self.opts.tid[0], None)
        if callable(subfunc):
            subfunc()

    def _set_cachedir(self):
        # set download directories from json state file
        self.base.conf.cachedir = DEFAULT_DATADIR
        self.base.conf.destdir = self.state.destdir if self.state.destdir else None

    def pre_configure_download(self):
        # only download subcommand accepts --destdir command line option
        self.base.conf.cachedir = DEFAULT_DATADIR
        self.base.conf.destdir = DEFAULT_DESTDIR

    def pre_configure_clean(self):
        self._set_cachedir()

    def pre_configure_prepare(self):
        self._set_cachedir()

    def pre_configure_upgrade(self):
        self._set_cachedir()
        if self.state.enable_disable_repos:
            self.opts.repos_ed = self.state.enable_disable_repos
        self.base.conf.releasever = self.state.target_releasever

    def configure_download(self):
        self.cli.demands.root_user = True
        self.cli.demands.resolving = True
        self.cli.demands.available_repos = True
        self.cli.demands.sack_activation = True
        self.cli.demands.freshest_metadata = True
        # We want to do the depsolve / download / transaction-test, but *not*
        # run the actual RPM transaction to install the downloaded packages.
        # Setting the "test" flag makes the RPM transaction a test transaction,
        # so nothing actually gets installed.
        # (It also means that we run two test transactions in a row, which is
        # kind of silly, but that's something for DNF to fix...)
        self.base.conf.tsflags += ["test"]
        # and don't ask any questions
        self.base.conf.assumeyes = True

    def configure_clean(self):
        self.cli.demands.root_user = True

    def configure_prepare(self):
        self.cli.demands.root_user = True

    def configure_upgrade(self):
        # same as the download, but offline and non-interactive. so...
        self.cli.demands.root_user = True
        self.cli.demands.resolving = True
        self.cli.demands.available_repos = True
        self.cli.demands.sack_activation = True
        # use the saved value for --allowerasing, etc.
        self.opts.distro_sync = True
        self.cli.demands.allow_erasing = self.state.allow_erasing
        self.base.conf.gpgcheck = self.state.gpgcheck
        self.base.conf.best = self.state.best
        self.base.conf.exclude = self.state.exclude
        # don't try to get new metadata, 'cuz we're offline
        self.cli.demands.cacheonly = True
        # and don't ask any questions (we confirmed all this beforehand)
        self.base.conf.assumeyes = True

    def check_download(self):
        checkReleaseVer(self.base.conf, target=self.opts.releasever)
        dnf.util.ensure_dir(self.base.conf.cachedir)
        if self.base.conf.destdir:
            dnf.util.ensure_dir(self.base.conf.destdir)

    def check_prepare(self):
        if not self.state.download_status == 'complete':
            raise CliError(_("system is not ready for upgrade"))
        dnf.util.ensure_dir(DEFAULT_DATADIR)

    def check_upgrade(self):
        if not self.state.upgrade_status == 'ready':
            raise CliError(  # Translators: do not change "prepare" here
                _("use 'dnf offline-upgrade prepare' to begin the upgrade"))

    def run_download(self):
        self.base.distro_sync()

        with self.state as state:
            state.download_status = 'downloading'
            state.target_releasever = self.base.conf.releasever
            state.exclude = self.base.conf.exclude
            state.destdir = self.base.conf.destdir

    def run_clean(self):
        logger.info(_("Cleaning up downloaded data..."))
        clear_dir(self.base.conf.cachedir)
        if self.base.conf.destdir:
            clear_dir(self.base.conf.destdir)
        with self.state as state:
            state.download_status = None
            state.upgrade_status = None
            state.destdir = None
            state.install_packages = {}

    def run_prepare(self):
        logger.info(_("TODO: This is a place holder command by now that will setup whatever is necessary before system reboot"))

        with self.state as state:
            state.upgrade_status = 'ready'

    def run_upgrade(self):
        # change the upgrade status (so we can detect crashed upgrades later)
        with self.state as state:
            state.upgrade_status = 'incomplete'

        logger.info(_("Starting system upgrade. This will take a while."))

        # NOTE: We *assume* that depsolving here will yield the same
        # transaction as it did during the download, but we aren't doing
        # anything to *ensure* that; if the metadata changed, or if depsolving
        # is non-deterministic in some way, we could end up with a different
        # transaction and then the upgrade will fail due to missing packages.
        #
        # One way to *guarantee* that we have the same transaction would be
        # to save & restore the Transaction object, but there's no documented
        # way to save a Transaction to disk.
        #
        # So far, though, the above assumption seems to hold. So... onward!

        # add the downloaded RPMs to the sack

        errs = []

        for repo_id, pkg_spec_list in self.state.install_packages.items():
            for pkgspec in pkg_spec_list:
                try:
                    self.base.install(pkgspec, reponame=repo_id)
                except dnf.exceptions.MarkingError:
                    logger.info(_('Unable to match package: %s'), pkgspec + " " + repo_id)
                    errs.append(pkgspec)

        if errs:
            raise dnf.exceptions.MarkingError(_("Unable to match some of packages"))

    def transaction_download(self):
        downloads = self.cli.base.transaction.install_set
        install_packages = {}
        for pkg in downloads:
            install_packages.setdefault(pkg.repo.id, []).append(str(pkg))

        # Okay! Write out the state so the upgrade can use it.
        system_ver = dnf.rpm.detect_releasever(self.base.conf.installroot)
        with self.state as state:
            state.download_status = 'complete'
            state.distro_sync = True
            state.allow_erasing = self.cli.demands.allow_erasing
            state.gpgcheck = self.base.conf.gpgcheck
            state.best = self.base.conf.best
            state.system_releasever = system_ver
            state.target_releasever = self.base.conf.releasever
            state.install_packages = install_packages
            state.enable_disable_repos = self.opts.repos_ed
            state.destdir = self.base.conf.destdir
        logger.info(DOWNLOAD_FINISHED_MSG)

    def transaction_upgrade(self):
        logger.info(_("Upgrade complete! Cleaning up and rebooting..."))
        self.run_clean()
