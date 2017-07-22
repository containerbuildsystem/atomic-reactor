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
from six.moves.urllib.parse import urlparse, parse_qs

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.pre_flatpak_create_dockerfile import FlatpakCreateDockerfilePlugin
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.source import VcsInfo
from atomic_reactor.util import ImageName

from tests.constants import (MOCK_SOURCE, FLATPAK_GIT, FLATPAK_SHA1)
from tests.fixtures import docker_tasker  # noqa
from tests.flatpak import FLATPAK_APP_JSON, FLATPAK_APP_MODULEMD


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = "./"
        self.path = tmpdir

        self.flatpak_json_path = os.path.join(tmpdir, 'flatpak.json')

    def get_build_file_path(self):
        return self.flatpak_json_path, self.path

    def get_vcs_info(self):
        return VcsInfo('git', FLATPAK_GIT, FLATPAK_SHA1)


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

    with open(mock_source.flatpak_json_path, "w") as f:
        f.write(json.dumps(FLATPAK_APP_JSON))

    setattr(workflow.builder, 'df_dir', str(tmpdir))

    return workflow


PDC_URL = 'https://pdc.fedoraproject.org/rest_api/v1'
MODULE_NAME = 'eog'
MODULE_STREAM = 'f26'

ALL_VERSIONS_JSON = [{"variant_release": "20170629143459"},
                     {"variant_release": "20170629213428"}]

LATEST_VERSION = "20170629213428"
LATEST_VERSION_JSON = [{"modulemd": FLATPAK_APP_MODULEMD}]


@responses.activate  # noqa - docker_tasker fixture
@pytest.mark.parametrize('specify_version', [True, False])
def test_flatpak_create_dockerfile(tmpdir, docker_tasker, specify_version):
    workflow = mock_workflow(tmpdir)

    def handle_unreleasedvariants(request):
        query = parse_qs(urlparse(request.url).query)

        assert query['variant_id'] == [MODULE_NAME]
        assert query['variant_version'] == [MODULE_STREAM]

        if query.get('fields', None) == ['variant_release']:
            body = ALL_VERSIONS_JSON
        else:
            assert query['variant_release'] == [LATEST_VERSION]
            body = LATEST_VERSION_JSON

        return (200, {}, json.dumps(body))

    responses.add_callback(responses.GET, PDC_URL + '/unreleasedvariants/',
                           content_type='application/json',
                           callback=handle_unreleasedvariants)

    args = {
        'module_name': 'eog',
        'module_stream': 'f26',
        "compose_url": "https://git.example.com/composes/{name}-{stream}-{version}/",
        'base_image': "registry.fedoraproject.org/fedora:latest",
        'pdc_url': PDC_URL
    }
    if specify_version:
        args['module_version'] = LATEST_VERSION

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FlatpakCreateDockerfilePlugin.key,
            'args': args
        }]
    )

    runner.run()

    assert os.path.exists(workflow.builder.df_path)
    assert os.path.exists(os.path.join(workflow.builder.df_dir, 'cleanup.sh'))
