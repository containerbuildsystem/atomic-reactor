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
from atomic_reactor.util import df_parser
from flexmock import flexmock
import pytest


class TestBumpRelease(object):
    def prepare(self,
                tmpdir,
                labels=None,
                include_target=True):
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
        args = [None, workflow, 'hub']
        if include_target:
            args.append('target')
        plugin = BumpReleasePlugin(*args)
        return plugin

    def test_component_missing(self, tmpdir):
        flexmock(koji, ClientSession=lambda hub, opts=None: None)
        plugin = self.prepare(tmpdir)
        with pytest.raises(RuntimeError):
            plugin.run()

    @pytest.mark.parametrize('release_label', [
         'release',
         'Release',
    ])
    def test_release_label_already_set(self, tmpdir, caplog, release_label):
        flexmock(koji, ClientSession=lambda hub, opts=None: None)
        plugin = self.prepare(tmpdir, labels={release_label: '1'})
        plugin.run()
        assert 'not incrementing' in caplog.text()

    @pytest.mark.parametrize('labels', [
        {'com.redhat.component': 'component'},
        {'BZComponent': 'component'},
        {'version': 'version'},
        {'Version': 'version'},
        {},
    ])
    def test_missing_labels(self, tmpdir, caplog, labels):
        flexmock(koji, ClientSession=lambda hub, opts=None: None)
        plugin = self.prepare(tmpdir, labels=labels)
        with pytest.raises(RuntimeError) as exc:
            plugin.run()
        assert 'missing label' in str(exc)

    @pytest.mark.parametrize('component', [
        {'com.redhat.component': 'component1'},
        {'BZComponent': 'component2'},
    ])
    @pytest.mark.parametrize('version', [
        {'version': '7.1'},
        {'Version': '7.2'},
    ])
    @pytest.mark.parametrize('include_target', [
        True,
        False
    ])
    @pytest.mark.parametrize('next_release', [
        {'actual': '1', 'expected': '1'},
        {'actual': '1', 'expected': '2'},
    ])
    def test_increment(self, tmpdir, component, version, next_release,
                       include_target):


        class MockedClientSession(object):
            def __init__(self):
                pass

            def getNextRelease(self, build_info):
                assert build_info['name'] == list(component.values())[0]
                assert build_info['version'] == list(version.values())[0]
                return next_release['actual']

            def getBuild(self, build_info):
                assert build_info['name'] == list(component.values())[0]
                assert build_info['version'] == list(version.values())[0]
                if build_info['release'] >= next_release['expected']:
                    return None
                return True


        session = MockedClientSession()
        flexmock(koji, ClientSession=session)

        labels = {}
        labels.update(component)
        labels.update(version)

        plugin = self.prepare(tmpdir, labels=labels,
                              include_target=include_target)
        plugin.run()

        parser = df_parser(plugin.workflow.builder.df_path, workflow=plugin.workflow)
        assert parser.labels['release'] == next_release['expected']
        # Old-style spellings will be asserted only if other old-style labels are present
        if 'BZComponent' not in parser.labels.keys():
            assert 'Release' not in parser.labels
        else:
            assert parser.labels['Release'] == next_release['expected']
