"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from copy import deepcopy
from io import BytesIO
from textwrap import dedent
import base64
import json
import os
import responses
import tarfile
import yaml
from flexmock import flexmock

from atomic_reactor.constants import REMOTE_SOURCE_DIR, CACHITO_ENV_FILENAME, CACHITO_ENV_ARG_ALIAS
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.source import SourceConfig
from atomic_reactor.utils.cachito import CFG_TYPE_B64
from atomic_reactor.plugins.pre_download_remote_source import (
    DownloadRemoteSourcePlugin,
)
import pytest
from tests.stubs import StubSource


CFG_CONTENT = b'configContent'
CACHITO_ENV_SHEBANG = "#!/bin/bash\n"
CACHITO_ENV_VARIABLES1 = (
    "export spam=maps\n"
    "export foo='somefile; rm -rf ~'\n"
)
CACHITO_ENV_VARIABLES2 = (
    "export varkey=varval\n"
    "export fioo='newfile; rm -rf ~'\n"
)


def mock_reactor_config(workflow, insecure=False):
    data = dedent("""\
        version: 1
        koji:
            hub_url: /
            root_url: ''
            auth: {{}}
        cachito:
           api_url: 'example.com'
           insecure: {}
           auth:
               ssl_certs_dir: /some/dir
        """.format(insecure))

    config = yaml.safe_load(data)
    workflow.conf.conf = config


def mock_repo_config(workflow, tmpdir, multiple_remote_sources=False):
    data = ""

    if multiple_remote_sources:
        data = dedent("""\
            remote_sources:
            - name: first
              remote_source:
                repo: test_repo
                ref: e1be527f39ec31323f0454f7d1422c6260b00580
            """)

    if not os.path.exists(tmpdir):
        os.makedirs(tmpdir)

    with open(os.path.join(tmpdir, 'container.yaml'), 'w') as f:
        f.write(data)
        f.flush()

    class MockSource(StubSource):
        def __init__(self, workdir):
            super(MockSource, self).__init__()
            self.workdir = workdir

    workflow.source = MockSource(str(tmpdir))
    workflow.source.config = SourceConfig(str(tmpdir))


class TestDownloadRemoteSource(object):
    @responses.activate
    @pytest.mark.parametrize('insecure', [True, False])
    @pytest.mark.parametrize('archive_dir_exists', [True, False])
    @pytest.mark.parametrize('has_configuration', [True, False])
    @pytest.mark.parametrize('configuration_type, configuration_content', (
        [CFG_TYPE_B64, base64.b64encode(CFG_CONTENT).decode('utf-8')],
        ['unknown', 'shouldNotMatter']
        ))
    @pytest.mark.parametrize('remote_sources, env_variables, multiple_remote_sources', (
        [[{'request_id': 1, 'name': None,
           'build_args': {}}],
         [None], False],

        [[{'request_id': 1, 'name': None,
           'build_args': {'spam': 'maps', 'foo': 'somefile; rm -rf ~'}}],
         [CACHITO_ENV_VARIABLES1], False],

        [[{'request_id': 1, 'name': 'first',
           'build_args': {}}],
         [None], True],

        [[{'request_id': 1, 'name': 'first',
           'build_args': {'varkey': 'varval', 'fioo': 'newfile; rm -rf ~'}}],
         [CACHITO_ENV_VARIABLES2], True],

        [[{'request_id': 1, 'name': 'first',
           'build_args': {'varkey': 'varval', 'fioo': 'newfile; rm -rf ~'}},
          {'request_id': 2, 'name': 'second',
           'build_args': {'spam': 'maps', 'foo': 'somefile; rm -rf ~'}}],
         [CACHITO_ENV_VARIABLES2, CACHITO_ENV_VARIABLES1], True],
    ))
    def test_download_remote_source(
        self, tmpdir, docker_tasker, user_params, insecure, archive_dir_exists,
        has_configuration, configuration_type, configuration_content, remote_sources,
        env_variables, multiple_remote_sources
    ):
        remote_sources_copy = deepcopy(remote_sources)
        workflow = DockerBuildWorkflow(source=None)
        df_path = os.path.join(str(tmpdir), 'stub_df_path')
        mock_repo_config(workflow, df_path, multiple_remote_sources=multiple_remote_sources)
        workflow.df_dir = str(tmpdir)
        flexmock(workflow, df_path=df_path)
        mock_reactor_config(workflow, insecure=insecure)
        config_url = 'https://example.com/dir/configurations'
        config_data = []
        config_path = 'abc.conf'
        member_base = 'abc'
        abc_content = b'def'
        contents = []
        url = 'https://example.com/dir/source.tar.gz'

        if has_configuration:
            config_data = [
                {
                    'type': configuration_type,
                    'path': config_path,
                    'content': configuration_content
                }
            ]

        for index, remote in enumerate(remote_sources_copy):
            remote['url'] = url
            remote['configs'] = config_url

            # Make a compressed tarfile with a single file 'abc#N'
            content = BytesIO()
            member = "{}{}".format(member_base, index)

            with tarfile.open(mode='w:gz', fileobj=content) as tf:
                ti = tarfile.TarInfo(name=member)
                ti.size = len(abc_content)
                tf.addfile(ti, fileobj=BytesIO(abc_content))
            contents.append(content)

            # GET from the url returns the compressed tarfile
            responses.add(responses.GET, url, body=content.getvalue())

            responses.add(
                    responses.GET,
                    config_url,
                    content_type='application/json',
                    status=200,
                    body=json.dumps(config_data)
                    )

        plugin = DownloadRemoteSourcePlugin(docker_tasker, workflow,
                                            remote_sources=remote_sources_copy)
        if archive_dir_exists:
            for remote in remote_sources_copy:
                if multiple_remote_sources:
                    dest_dir = os.path.join(workflow.df_dir, plugin.REMOTE_SOURCE,
                                            remote['name'])
                else:
                    dest_dir = os.path.join(workflow.df_dir, plugin.REMOTE_SOURCE)
                os.makedirs(dest_dir)

            with pytest.raises(RuntimeError):
                plugin.run()
            os.rmdir(dest_dir)
            return

        if has_configuration and configuration_type == 'unknown':
            with pytest.raises(ValueError):
                plugin.run()
            return

        result = plugin.run()

        for index, remote in enumerate(remote_sources_copy):
            member = "{}{}".format(member_base, index)

            # Test content of cachito.env file
            if multiple_remote_sources:
                cachito_env_path = os.path.join(plugin.workflow.df_dir,
                                                plugin.REMOTE_SOURCE, remote['name'],
                                                CACHITO_ENV_FILENAME)
            else:
                cachito_env_path = os.path.join(plugin.workflow.df_dir,
                                                plugin.REMOTE_SOURCE,
                                                CACHITO_ENV_FILENAME)

            cachito_env_expected_content = CACHITO_ENV_SHEBANG
            if env_variables[index]:
                cachito_env_expected_content += env_variables[index]

            with open(cachito_env_path, 'r') as f:
                assert f.read() == cachito_env_expected_content

            # The return value should be the path to the downloaded archive itself
            with open(result[index], 'rb') as f:
                filecontent = f.read()
            assert filecontent == contents[index].getvalue()

            if multiple_remote_sources:
                remote_file_path = os.path.join(workflow.df_dir,
                                                plugin.REMOTE_SOURCE, remote['name'], member)
            else:
                remote_file_path = os.path.join(workflow.df_dir,
                                                plugin.REMOTE_SOURCE, member)

            # Expect a file 'abc#N' in the workdir
            with open(remote_file_path, 'rb') as f:
                filecontent = f.read()
            assert filecontent == abc_content

            if has_configuration:
                if multiple_remote_sources:
                    injected_cfg = os.path.join(workflow.df_dir, plugin.REMOTE_SOURCE,
                                                remote['name'], config_path)
                else:
                    injected_cfg = os.path.join(workflow.df_dir, plugin.REMOTE_SOURCE,
                                                config_path)

                with open(injected_cfg, 'rb') as f:
                    filecontent = f.read()
                assert filecontent == CFG_CONTENT

        # Expect buildargs to have been set
        if multiple_remote_sources:
            assert set(workflow.buildargs.keys()) == {'REMOTE_SOURCES', 'REMOTE_SOURCES_DIR'}
            assert workflow.buildargs['REMOTE_SOURCES'] == plugin.REMOTE_SOURCE
            assert workflow.buildargs['REMOTE_SOURCES_DIR'] == REMOTE_SOURCE_DIR
        else:
            for arg, value in remote_sources_copy[0]['build_args'].items():
                assert workflow.buildargs[arg] == value
            # along with the args needed to add the sources in the Dockerfile
            assert workflow.buildargs['REMOTE_SOURCE'] == plugin.REMOTE_SOURCE
            assert workflow.buildargs['REMOTE_SOURCE_DIR'] == REMOTE_SOURCE_DIR
            env_file_path = os.path.join(REMOTE_SOURCE_DIR, CACHITO_ENV_FILENAME)
            assert workflow.buildargs[CACHITO_ENV_ARG_ALIAS] == env_file_path
            # https://github.com/openshift/imagebuilder/issues/139
            assert not workflow.buildargs['REMOTE_SOURCE'].startswith('/')
