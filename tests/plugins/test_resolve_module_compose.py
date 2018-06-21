"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from flexmock import flexmock

import json
import responses
import os
import pytest
import six
from six.moves.urllib.parse import urlparse, parse_qs

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
from atomic_reactor.constants import REPO_CONTAINER_CONFIG

from tests.constants import (MOCK_SOURCE, FLATPAK_GIT, FLATPAK_SHA1)
from tests.fixtures import docker_tasker, reactor_config_map  # noqa
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

    setattr(workflow.builder, 'df_dir', str(tmpdir))

    return workflow


MODULE_NAME = 'eog'
MODULE_STREAM = 'f26'
MODULE_VERSION = "20170629213428"
MODULE_NS = MODULE_NAME + ':' + MODULE_STREAM
MODULE_NSV = MODULE_NS + ':' + MODULE_VERSION

ODCS_URL = 'https://odcs.fedoraproject.org/odcs/1'

PDC_URL = 'https://pdc.fedoraproject.org/rest_api/v1'
LATEST_VERSION_JSON = [{"modulemd": FLATPAK_APP_MODULEMD,
                        "rpms": FLATPAK_APP_RPMS}]


def compose_json(state, state_name):
    return json.dumps({
        "flags": [],
        "id": 84,
        "owner": "Unknown",
        "result_repo": "http://odcs.fedoraproject.org/composes/latest-odcs-84-1/compose/Temporary",
        "source": MODULE_NSV,
        "source_type": 2,
        "state": state,
        "state_name": state_name
    })


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
def test_resolve_module_compose(tmpdir, docker_tasker, compose_ids, modules,  # noqa
                                reactor_config_map):
    secrets_path = os.path.join(str(tmpdir), "secret")
    os.mkdir(secrets_path)
    with open(os.path.join(secrets_path, "token"), "w") as f:
        f.write("green_eggs_and_ham")

    if modules is not None:
        data = "compose:\n"
        data += "    modules:\n"
        for mod in modules:
            data += "    - {}\n".format(mod)
        tmpdir.join(REPO_CONTAINER_CONFIG).write(data)

    module = None
    if modules:
        module = modules[0]

    workflow = mock_workflow(tmpdir)
    mock_get_retry_session()

    def handle_composes_post(request):
        assert request.headers['Authorization'] == 'Bearer green_eggs_and_ham'

        if isinstance(request.body, six.text_type):
            body = request.body
        else:
            body = request.body.decode()
        body_json = json.loads(body)
        assert body_json['source']['type'] == 'module'
        assert body_json['source']['source'] == module
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

    def handle_unreleasedvariants(request):
        query = parse_qs(urlparse(request.url).query)

        assert query['variant_id'] == [MODULE_NAME]
        assert query['variant_version'] == [MODULE_STREAM]
        assert query['variant_release'] == [MODULE_VERSION]

        return (200, {}, json.dumps(LATEST_VERSION_JSON))

    responses.add_callback(responses.GET, PDC_URL + '/unreleasedvariants/',
                           content_type='application/json',
                           callback=handle_unreleasedvariants)

    args = {
        'odcs_url': ODCS_URL,
        'odcs_openidc_secret_path': secrets_path,
        'pdc_url': PDC_URL,
        'compose_ids': compose_ids
    }

    if reactor_config_map:
        workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
        workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
            ReactorConfig({'version': 1,
                           'odcs': {'api_url': ODCS_URL,
                                    'auth': {'openidc_dir': secrets_path}},
                           'pdc': {'api_url': PDC_URL}})

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
