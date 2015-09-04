"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

try:  # py3 compat
    import configparser
except ImportError:
    import ConfigParser as configparser
import os

from atomic_reactor.plugin import PreBuildPlugin, AutoRebuildCanceledException
from atomic_reactor.plugins.pre_check_and_set_rebuild import is_rebuild


class StopAutorebuildIfDisabledPlugin(PreBuildPlugin):
    """If the current build is rebuild, this plugin tries to read config file like this

    [autorebuild]
    enabled=0

    to see if rebuilds are enabled or not. If they are disabled, it raises
    AutoRebuildCanceledException to stop the build process.

    `enabled` is interpreted as boolean, these values are accepted: 0, 1, false, true
    (case insensitive).
    """
    key = 'stop_autorebuild_if_disabled'
    # set is_allowed_to_fail to False, so that the actual build is skipped
    # if this plugin raises AutoRebuildCanceledException
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, config_file='.osbs-repo-config'):
        super(StopAutorebuildIfDisabledPlugin, self).__init__(tasker, workflow)
        self.config_file = config_file

    def _is_rebuild_enabled(self):
        self.log.info('reading autorebuild config from "%s"', self.config_file)
        source_path = self.workflow.source.get()
        config_path = os.path.join(source_path, self.config_file)
        cfp = configparser.SafeConfigParser()
        result = True

        if os.path.exists(config_path):
            try:
                cfp.read(config_path)
                result = cfp.getboolean('autorebuild', 'enabled')
                self.log.info('autorebuild is %s in %s',
                              'enabled' if result else 'disabled',
                              self.config_file)
            except configparser.Error:
                self.log.error('can\'t parse "%s", assuming autorebuild is enabled',
                               self.config_file)
            except ValueError as e:
                self.log.error('can\'t parse [autorebuild].enabled as bool in "%s", assuming'
                               'autorebuild is enabled (error: "%s")', self.config_file, e)
        else:
            self.log.info('no "%s", assuming autorebuild is enabled', self.config_file)

        return result

    def run(self):
        if is_rebuild(self.workflow):
            self.log.info('this is an autorebuild, determining whether to skip it')
            if not self._is_rebuild_enabled():
                self.log.info('autorebuild is disabled, %s is interrupting the build', self.key)
                raise AutoRebuildCanceledException(self.key, 'autorebuild is disabled')
            else:
                self.log.info('autorebuild is enabled, %s is doing nothing', self.key)
        else:
            self.log.info('this is not an autorebuild, %s is doing nothing', self.key)
