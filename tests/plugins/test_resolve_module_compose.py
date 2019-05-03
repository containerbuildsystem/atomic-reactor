"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

from flexmock import flexmock

import json
import responses
import os
import pytest
import six

from atomic_reactor.inner import DockerBuildWorkflow
try:
    from atomic_reactor.plugins.pre_resolve_module_compose import (ResolveModuleComposePlugin,
                                                                   get_compose_info)
    MODULEMD_AVAILABLE = True
except ImportError:
    MODULEMD_AVAILABLE = False

from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor.source import VcsInfo, SourceConfig
from atomic_reactor.util import ImageName
from atomic_reactor.constants import REPO_CONTAINER_CONFIG, PLUGIN_CHECK_AND_SET_PLATFORMS_KEY

try:
    import koji
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
    import koji

from tests.constants import (MOCK_SOURCE, FLATPAK_GIT, FLATPAK_SHA1)
from tests.flatpak import FLATPAK_APP_MODULEMD, FLATPAK_APP_RPMS
from tests.retry_mock import mock_get_retry_session


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = "./"
        self.path = tmpdir
        self._config = None

        self.container_yaml_path = os.path.join(tmpdir, 'container.yaml')

    def get_build_file_path(self):
        return self.container_yaml_path, self.path

    def get_vcs_info(self):
        return VcsInfo('git', FLATPAK_GIT, FLATPAK_SHA1)

    @property
    def config(self):  # lazy load after container.yaml has been created
        self._config = self._config or SourceConfig(self.path)
        return self._config


class MockBuilder(object):
    def __init__(self):
        self.image_id = "xxx"
        self.base_image = ImageName.parse("org.gnome.eog")
        self.df_path = None

    def set_base_image(self, base_image):
        pass

    def set_df_path(self, path):
        self.df_path = path


def mock_workflow(tmpdir):
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    mock_source = MockSource(tmpdir)
    setattr(workflow, 'builder', MockBuilder())
    workflow.builder.source = mock_source
    flexmock(workflow, source=mock_source)
    workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(['x86_64', 'ppc64le'])

    setattr(workflow.builder, 'df_dir', str(tmpdir))

    return workflow


MODULE_NAME = 'eog'
MODULE_STREAM = 'f26'
MODULE_VERSION = "20170629213428"
MODULE_CONTEXT = "01234567"
MODULE_NS = MODULE_NAME + ':' + MODULE_STREAM
MODULE_NSV = MODULE_NS + ':' + MODULE_VERSION
MODULE_NSVC = MODULE_NSV + ':' + MODULE_CONTEXT
MODULE_NVR = MODULE_NAME + "-" + MODULE_STREAM + "-" + MODULE_VERSION + "." + MODULE_CONTEXT


ODCS_URL = 'https://odcs.fedoraproject.org/odcs/1'

LATEST_VERSION_JSON = [{"name": MODULE_NAME,
                        "stream": MODULE_STREAM,
                        "version": MODULE_VERSION,
                        "modulemd": FLATPAK_APP_MODULEMD,
                        "rpms": FLATPAK_APP_RPMS}]


def compose_json(state, state_name):
    return json.dumps({
        "flags": [],
        "id": 84,
        "owner": "Unknown",
        "result_repo": "http://odcs.fedoraproject.org/composes/latest-odcs-84-1/compose/Temporary",
        "source": MODULE_NSVC,
        "source_type": 2,
        "state": state,
        "state_name": state_name
    })


def mock_koji_session():
    session = flexmock()

    (session
     .should_receive('krb_login')
     .and_return(True))

    (session
     .should_receive('getBuild')
     .with_args(MODULE_NVR)
     .and_return({
         'build_id': 1138198,
         'name': MODULE_NAME,
         'version': MODULE_STREAM,
         'release': MODULE_VERSION + "." + MODULE_CONTEXT,
         'extra': {
             'typeinfo': {
                 'module': {
                     'modulemd_str': FLATPAK_APP_MODULEMD
                 }
             }
         }
     }))

    (session
     .should_receive('listArchives')
     .with_args(buildID=1138198)
     .and_return(
        [{'btype': 'module',
          'build_id': 1138198,
          'filename': 'modulemd.txt',
          'id': 147879}]))

    (session
     .should_receive('listRPMs')
     .with_args(imageID=147879)
     .and_return([
         {'arch': 'src',
          'epoch': None,
          'id': 15197182,
          'name': 'eog',
          'release': '1.module_2123+73a9ef6f',
          'version': '3.28.3'},
         {'arch': 'x86_64',
          'epoch': None,
          'id': 15197187,
          'metadata_only': False,
          'name': 'eog',
          'release': '1.module_2123+73a9ef6f',
          'version': '3.28.3'},
         {'arch': 'ppc64le',
          'epoch': None,
          'id': 15197188,
          'metadata_only': False,
          'name': 'eog',
          'release': '1.module_2123+73a9ef6f',
          'version': '3.28.3'},
     ]))

    (flexmock(koji)
        .should_receive('ClientSession')
        .and_return(session))


@responses.activate  # noqa - docker_tasker fixture
@pytest.mark.skipif(not MODULEMD_AVAILABLE,
                    reason="libmodulemd not available")
@pytest.mark.parametrize('compose_ids', (None, [], [84], [84, 2]))
@pytest.mark.parametrize('modules', (
    None,
    [],
    [MODULE_NS],
    [MODULE_NSV],
    [MODULE_NSV, 'mod_name2-mod_stream2-mod_version2'],
))
@pytest.mark.parametrize(('signing_intent', 'signing_intent_source', 'sigkeys'), [
    ('unsigned', 'default', []),
    ('release', 'container_yaml', ['R123', 'R234']),
    ('beta', 'command_line', ['R123', 'B456', 'B457']),
])
def test_resolve_module_compose(tmpdir, docker_tasker, compose_ids, modules,
                                signing_intent, signing_intent_source, sigkeys):
    secrets_path = os.path.join(str(tmpdir), "secret")
    os.mkdir(secrets_path)
    with open(os.path.join(secrets_path, "token"), "w") as f:
        f.write("green_eggs_and_ham")

    if modules is not None:
        data = "compose:\n"
        data += "    modules:\n"
        for mod in modules:
            data += "    - {}\n".format(mod)
        if signing_intent_source == 'container_yaml':
            data += '    signing_intent: ' + signing_intent
        tmpdir.join(REPO_CONTAINER_CONFIG).write(data)

    module = None
    if modules:
        module = modules[0]

    workflow = mock_workflow(tmpdir)
    mock_get_retry_session()
    mock_koji_session()

    def handle_composes_post(request):
        assert request.headers['Authorization'] == 'Bearer green_eggs_and_ham'

        if isinstance(request.body, six.text_type):
            body = request.body
        else:
            body = request.body.decode()
        body_json = json.loads(body)
        assert body_json['source']['type'] == 'module'
        assert body_json['source']['source'] == module
        assert body_json['source']['sigkeys'] == sigkeys
        assert body_json['arches'] == ['ppc64le', 'x86_64']
        return (200, {}, compose_json(0, 'wait'))

    responses.add_callback(responses.POST, ODCS_URL + '/composes/',
                           content_type='application/json',
                           callback=handle_composes_post)

    state = {'count': 1}

    def handle_composes_get(request):
        assert request.headers['Authorization'] == 'Bearer green_eggs_and_ham'

        if state['count'] == 1:
            response_json = compose_json(1, 'generating')
        else:
            response_json = compose_json(2, 'done')
        state['count'] += 1

        return (200, {}, response_json)

    responses.add_callback(responses.GET, ODCS_URL + '/composes/84',
                           content_type='application/json',
                           callback=handle_composes_get)

    args = {
        'odcs_url': ODCS_URL,
        'odcs_openidc_secret_path': secrets_path,
        'compose_ids': compose_ids
    }

    if signing_intent_source == 'command_line':
        args['signing_intent'] = signing_intent

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
        ReactorConfig({'version': 1,
                       'odcs': {'api_url': ODCS_URL,
                                'auth': {'openidc_dir': secrets_path},
                                'signing_intents': [
                                    {
                                        'name': 'unsigned',
                                        'keys': [],
                                    },
                                    {
                                        'name': 'release',
                                        'keys': ['R123', 'R234'],
                                    },
                                    {
                                        'name': 'beta',
                                        'keys': ['R123', 'B456', 'B457'],
                                    },
                                ],
                                'default_signing_intent': 'unsigned'},
                       'koji':  {'auth': {},
                                 'hub_url': 'https://koji.example.com/hub'}})

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': ResolveModuleComposePlugin.key,
            'args': args
        }]
    )

    if modules is None:
        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()
        assert '"compose" config in container.yaml is required ' in str(exc_info.value)
    elif not modules:
        with pytest.raises(PluginFailedException) as exc_info:
            runner.run()
        assert '"compose" config has no modules' in str(exc_info.value)
    else:
        runner.run()

        compose_info = get_compose_info(workflow)

        assert compose_info.compose_id == 84
        assert compose_info.base_module.name == MODULE_NAME
        assert compose_info.base_module.stream == MODULE_STREAM
        assert compose_info.base_module.version == MODULE_VERSION
        assert compose_info.base_module.mmd.props.summary == 'Eye of GNOME Application Module'
        assert compose_info.base_module.rpms == [
            'eog-0:3.28.3-1.module_2123+73a9ef6f.src.rpm',
            'eog-0:3.28.3-1.module_2123+73a9ef6f.x86_64.rpm',
            'eog-0:3.28.3-1.module_2123+73a9ef6f.ppc64le.rpm',
        ]
