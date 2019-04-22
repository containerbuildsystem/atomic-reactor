# -*- coding: utf-8 -*-
"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, unicode_literals, absolute_import

import os
import tarfile
from textwrap import dedent

import docker
from flexmock import flexmock
from six import BytesIO
from six.moves.configparser import ConfigParser

from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin, ReactorConfig,
                                                       WORKSPACE_CONF_KEY)
from atomic_reactor.plugins.pre_yum_proxy import YumProxyPlugin
from atomic_reactor.util import df_parser, ImageName

from tests.constants import SOURCE, MOCK
from tests.stubs import StubInsideBuilder


if MOCK:
    from tests.docker_mock import mock_docker


class TestYumProxyPlugin(object):

    def test_missing_config(self, tmpdir):
        df_content = dedent("""\
            FROM fedora
            RUN yum install -y python-flask
            CMD /bin/bash
            """)
        df = df_parser(str(tmpdir))
        df.content = df_content

        tasker, workflow = self.prepare(df.dockerfile_path)

        runner = PreBuildPluginsRunner(tasker, workflow, [
            {'name': YumProxyPlugin.key, 'args': {}},
        ])
        runner_results = runner.run()

        assert runner_results[YumProxyPlugin.key] is None
        # Verify Dockerfile contents have not changed
        assert df.content == df_content

    def test_add_proxy(self, tmpdir):
        df_content = dedent("""\
            FROM fedora
            RUN yum install -y python-flask
            CMD /bin/bash
            """)
        df = df_parser(str(tmpdir))
        df.content = df_content
        yum_proxies = [
            {'proxied': 'src.example.com', 'proxy': 'proxy.example.com'}
        ]
        parent_images = {
            'fedora': ImageName.parse('fedora')
        }

        yum_repo_files = {
            'spam.repo': dedent("""\
                [spam]
                name = Spam for breakfast
                baseurl = https://src.example.com/content/public/$basearch/spam/os

                [maps]
                name = Spam for breakfast
                baseurl = https://src.example.com/content/public/$basearch/maps/os
                """),
            'bacon.repo': dedent("""\
                [bacon]
                name = Bacon for breakfast
                baseurl = https://src.example.com/content/public/$basearch/bacon/os
                """),
            'non-proxied.repo': dedent("""\
                [non-proxied]
                name = Non proxied repo
                baseurl = https://alt-src.example.com/content/public/$basearch/non-proxied/os
                """),
        }

        tasker, workflow = self.prepare(
            df.dockerfile_path, yum_proxies=yum_proxies, parent_images=parent_images,
            yum_repo_files=yum_repo_files)

        runner = PreBuildPluginsRunner(tasker, workflow, [
            {'name': YumProxyPlugin.key, 'args': {}},
        ])
        runner.run()

        expected_df_content = dedent("""\
            FROM fedora
            ADD {0}/updated/fedora/bacon.repo /etc/yum.repos.d/
            ADD {0}/updated/fedora/spam.repo /etc/yum.repos.d/
            RUN yum install -y python-flask
            CMD /bin/bash
            ADD {0}/original/fedora/bacon.repo /etc/yum.repos.d/
            ADD {0}/original/fedora/spam.repo /etc/yum.repos.d/
            """.format(os.path.join(str(tmpdir), 'atomic-reactor-yum-repos')))
        assert df.content == expected_df_content

        expected_repos = {'fedora': {
            'spam.repo': {
                'spam': {
                    'baseurl': 'https://proxy.example.com/content/public/$basearch/spam/os'
                },
                'maps': {
                    'baseurl': 'https://proxy.example.com/content/public/$basearch/maps/os'
                }
            },
            'bacon.repo': {
                'bacon': {
                    'baseurl': 'https://proxy.example.com/content/public/$basearch/bacon/os'
                }
            }
        }}
        self.verify_updated_repo_files(expected_repos, workflow.source.workdir)

    def test_add_proxy_multi_stage(self, tmpdir):
        df_content = dedent("""\
            FROM python as builder
            RUN blah

            FROM fedora
            RUN yum install -y python-flask
            CMD /bin/bash
            """)
        df = df_parser(str(tmpdir))
        df.content = df_content
        yum_proxies = [
            {'proxied': 'src.example.com', 'proxy': 'proxy.example.com'}
        ]
        parent_images = {
            'fedora': ImageName.parse('fedora'),
            'python': ImageName.parse('python'),
        }

        yum_repo_files = {
            'spam.repo': dedent("""\
                [spam]
                name = Spam for breakfast
                baseurl = https://src.example.com/content/public/$basearch/spam/os
                """),
        }

        tasker, workflow = self.prepare(
            df.dockerfile_path, yum_proxies=yum_proxies, parent_images=parent_images,
            yum_repo_files=yum_repo_files)

        runner = PreBuildPluginsRunner(tasker, workflow, [
            {'name': YumProxyPlugin.key, 'args': {}},
        ])
        runner.run()

        expected_df_content = dedent("""\
            FROM python as builder
            ADD {0}/updated/python/spam.repo /etc/yum.repos.d/
            RUN blah
            ADD {0}/original/python/spam.repo /etc/yum.repos.d/

            FROM fedora
            ADD {0}/updated/fedora/spam.repo /etc/yum.repos.d/
            RUN yum install -y python-flask
            CMD /bin/bash
            ADD {0}/original/fedora/spam.repo /etc/yum.repos.d/
            """.format(os.path.join(str(tmpdir), 'atomic-reactor-yum-repos')))
        assert df.content == expected_df_content

        expected_repos = {'fedora': {
            'spam.repo': {
                'spam': {
                    'baseurl': 'https://proxy.example.com/content/public/$basearch/spam/os'
                },
            }
        }}
        self.verify_updated_repo_files(expected_repos, workflow.source.workdir)

    def prepare(self, df_path, inherited_user='', yum_proxies=None, parent_images=None,
                yum_repo_files=None):
        if MOCK:
            mock_docker()
        tasker = DockerTasker()
        workflow = DockerBuildWorkflow(SOURCE, "test-image")
        workflow.source = MockSource(df_path)
        workflow.builder = (StubInsideBuilder()
                            .for_workflow(workflow)
                            .set_df_path(df_path)
                            .set_inspection_data({
                                INSPECT_CONFIG: {
                                    'User': inherited_user,
                                },
                            }))

        if yum_proxies is not None:
            reactor_config = ReactorConfig({
                'version': 1,
                'yum_proxies': yum_proxies
            })
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {
                WORKSPACE_CONF_KEY: reactor_config
            }

        if parent_images:
            workflow.builder.parent_images = parent_images

        if yum_repo_files is not None:
            # TODO: It would be great to just generate all of this in-memory, but ATM my
            # brain can't sort that out.
            workdir = os.path.join(os.path.dirname(df_path), 'mocked')
            try:
                os.makedirs(workdir)
            except OSError:
                # Ignore if dirs have already been created. Python 2 doesn't suport exists_ok param
                pass
            archive_path = os.path.join(os.path.dirname(df_path), 'mocked.tar')
            with tarfile.open(archive_path, 'w') as archive:

                for name, content in yum_repo_files.items():
                    path = os.path.join(workdir, name)
                    with open(path, 'w') as f:
                        f.write(content)

                    archive.add(path, 'yum.repos.d/{}'.format(name))

            def mock_get_archive(cid, path, **kwargs):
                with open(archive_path) as f:
                    return BytesIO(f.read().encode('utf-8')), None

            flexmock(docker.APIClient, get_archive=mock_get_archive)

        return tasker, workflow

    def verify_updated_repo_files(self, expected_repos, workdir):
        for parent_image, repo_files in expected_repos.items():
            for repo_file, sections in repo_files.items():
                repo = ConfigParser()
                repo.read(os.path.join(
                    workdir,
                    'atomic-reactor-yum-repos/updated',
                    parent_image,
                    repo_file,
                ))
                assert set(repo.sections()) == set(sections.keys())
                for section, config in sections.items():
                    for key, value in config.items():
                        assert repo.get(section, key) == value

            # Verify no other files were written
            parent_image_files = os.path.join(workdir, 'atomic-reactor-yum-repos/updated',
                                              parent_image)
            assert set(os.listdir(parent_image_files)) == set(repo_files.keys())


class MockSource(object):
    def __init__(self, dockerfile_path):
        self.dockerfile_path = dockerfile_path
        self.path = os.path.dirname(dockerfile_path)

    def get_build_file_path(self):
        return self.dockerfile_path, self.path

    @property
    def workdir(self):
        return self.path
