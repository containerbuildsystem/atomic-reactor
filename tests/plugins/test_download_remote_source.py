"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

from io import BytesIO
from textwrap import dedent
import base64
import os
import responses
import tarfile

from atomic_reactor import util
from atomic_reactor.constants import REMOTE_SOURCE_DIR
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugins.pre_reactor_config import (
    ReactorConfigPlugin, WORKSPACE_CONF_KEY, ReactorConfig)
from atomic_reactor.utils.cachito import CFG_TYPE_B64
from tests.constants import TEST_IMAGE
from tests.stubs import StubInsideBuilder
from atomic_reactor.plugins.pre_download_remote_source import (
    DownloadRemoteSourcePlugin,
)
import pytest


CFG_CONTENT = b'configContent'


def mock_reactor_config(workflow, insecure=False):
    data = dedent("""\
        version: 1
        cachito:
           api_url: 'example.com'
           insecure: {}
           auth:
               ssl_certs_dir: /some/dir
        """.format(insecure))

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
    config = util.read_yaml(data, 'schemas/config.json')
    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] = ReactorConfig(config)


class TestDownloadRemoteSource(object):
    @responses.activate
    @pytest.mark.parametrize('source_url', [True, False])
    @pytest.mark.parametrize('insecure', [True, False])
    @pytest.mark.parametrize('archive_dir_exists', [True, False])
    @pytest.mark.parametrize('has_configuration', [True, False])
    @pytest.mark.parametrize('configuration_type, configuration_content', (
        [CFG_TYPE_B64, base64.b64encode(CFG_CONTENT)],
        ['unknown', 'shouldNotMatter']
        ))
    def test_download_remote_source(
        self, tmpdir, docker_tasker, source_url, archive_dir_exists,
        has_configuration, configuration_type, configuration_content, insecure
    ):
        workflow = DockerBuildWorkflow(
            TEST_IMAGE,
            source={"provider": "git", "uri": "asd"},
        )
        df_path = os.path.join(str(tmpdir), 'stub_df_path')
        workflow.builder = StubInsideBuilder().for_workflow(workflow).set_df_path(df_path)
        mock_reactor_config(workflow, insecure=insecure)
        filename = 'source.tar.gz'
        url = None
        if source_url:
            url = 'https://example.com/dir/{}'.format(filename)

        # Make a compressed tarfile with a single file 'abc'
        member = 'abc'
        abc_content = b'def'
        content = BytesIO()
        with tarfile.open(mode='w:gz', fileobj=content) as tf:
            ti = tarfile.TarInfo(name=member)
            ti.size = len(abc_content)
            tf.addfile(ti, fileobj=BytesIO(abc_content))

        # GET from the url returns the compressed tarfile
        if source_url:
            responses.add(responses.GET, url, body=content.getvalue())

        config_data = []
        config_path = 'abc.conf'
        if has_configuration:
            config_data = [
                {
                    'type': configuration_type,
                    'path': config_path,
                    'content': configuration_content
                }
            ]

        buildargs = {'spam': 'maps'}
        plugin = DownloadRemoteSourcePlugin(docker_tasker, workflow,
                                            remote_source_url=url,
                                            remote_source_build_args=buildargs,
                                            remote_source_configs=config_data)
        if archive_dir_exists and source_url:
            dest_dir = os.path.join(workflow.builder.df_dir, plugin.REMOTE_SOURCE)
            os.makedirs(dest_dir)
            with pytest.raises(RuntimeError):
                plugin.run()
            os.rmdir(dest_dir)
            return

        if source_url and has_configuration and configuration_type == 'unknown':
            with pytest.raises(ValueError):
                plugin.run()
            return

        result = plugin.run()

        if not source_url:
            assert result is None
            return

        # The return value should be the path to the downloaded archive itself
        with open(result, 'rb') as f:
            filecontent = f.read()

        assert filecontent == content.getvalue()

        # Expect a file 'abc' in the workdir
        with open(os.path.join(workflow.builder.df_dir, plugin.REMOTE_SOURCE, member), 'rb') as f:
            filecontent = f.read()

        assert filecontent == abc_content

        if has_configuration:
            injected_cfg = os.path.join(workflow.builder.df_dir, plugin.REMOTE_SOURCE, config_path)
            with open(injected_cfg, 'rb') as f:
                filecontent = f.read()

            assert filecontent == CFG_CONTENT

        # Expect buildargs to have been set
        for arg, value in buildargs.items():
            assert workflow.builder.buildargs[arg] == value
        # along with the args needed to add the sources in the Dockerfile
        assert workflow.builder.buildargs['REMOTE_SOURCE'] == plugin.REMOTE_SOURCE
        assert workflow.builder.buildargs['REMOTE_SOURCE_DIR'] == REMOTE_SOURCE_DIR
        # https://github.com/openshift/imagebuilder/issues/139
        assert not workflow.builder.buildargs['REMOTE_SOURCE'].startswith('/')
