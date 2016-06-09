"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import os

try:
    import koji as koji
except ImportError:
    import inspect
    import sys

    # Find our mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji as koji

from atomic_reactor.plugins.pre_bump_release import BumpReleasePlugin
from flexmock import flexmock
from dockerfile_parse import DockerfileParser
import pytest


class TestBumpRelease(object):
    def prepare(self,
                tmpdir,
                labels=None,
                target=None):
        if labels is None:
            labels = {}

        workflow = flexmock()
        setattr(workflow, 'builder', flexmock())
        filename = os.path.join(str(tmpdir), 'Dockerfile')
        with open(filename, 'wt') as df:
            df.write('FROM base\n')
            for key, value in labels.items():
                df.write('LABEL {key}={value}\n'.format(key=key, value=value))

        setattr(workflow.builder, 'df_path', filename)
        plugin = BumpReleasePlugin(None, workflow, target, '/')
        return plugin

    def test_component_missing(self, tmpdir):
        flexmock(koji, ClientSession=lambda hub: None)
        plugin = self.prepare(tmpdir)
        with pytest.raises(RuntimeError):
            plugin.run()

    @pytest.mark.parametrize('labels', [
        {'com.redhat.component': 'component',
          'release': '1'},
        
        {'BZComponent': 'component',
         'release': '1'},

        {'com.redhat.component': 'component',
         'Release': '1'},

        {'BZComponent': 'component',
         'Release': '1'},
    ])
    def test_release_label_already_set(self, tmpdir, caplog, labels):
        flexmock(koji, ClientSession=lambda hub: None)
        plugin = self.prepare(tmpdir, labels=labels)
        plugin.run()
        assert 'not incrementing' in caplog.text()

    @pytest.mark.parametrize(('latest_builds', 'next_release', 'expected'), [
        ([], None, '1'),
        ([{}], 2, '2'),
        ([{}], '2', '2'),
    ])
    @pytest.mark.parametrize('release_label', ['release', 'Release'])
    def test_increment(self, tmpdir, release_label, latest_builds, next_release,
                       expected):
        class MockedClientSession(object):
            def __init__(self):
                pass

            def getLatestBuilds(self, target, package=None):
                return latest_builds

            def getNextRelease(self, build_info):
                return next_release

        session = MockedClientSession()
        flexmock(koji, ClientSession=session)
        plugin = self.prepare(tmpdir, labels={'com.redhat.component': 'comp'})
        plugin.run()
        parser = DockerfileParser(plugin.workflow.builder.df_path)
        assert parser.labels[release_label] == expected
