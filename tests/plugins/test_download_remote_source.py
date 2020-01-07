"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

from io import BytesIO
import os
import responses
import tarfile

from atomic_reactor.constants import REMOTE_SOURCE_DIR
from atomic_reactor.inner import DockerBuildWorkflow
from tests.constants import TEST_IMAGE
from tests.stubs import StubInsideBuilder
from atomic_reactor.plugins.pre_download_remote_source import (
    DownloadRemoteSourcePlugin,
)


class TestDownloadRemoteSource(object):
    @responses.activate
    def test_download_remote_source(self, docker_tasker):
        workflow = DockerBuildWorkflow(
            TEST_IMAGE,
            source={"provider": "git", "uri": "asd"},
        )
        workflow.builder = StubInsideBuilder().for_workflow(workflow)
        filename = 'source.tar.gz'
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
        responses.add(responses.GET, url, body=content.getvalue())

        buildargs = {'spam': 'maps'}
        plugin = DownloadRemoteSourcePlugin(docker_tasker, workflow,
                                            remote_source_url=url,
                                            remote_source_build_args=buildargs)
        result = plugin.run()

        # The return value should be the path to the downloaded archive itself
        with open(result, 'rb') as f:
            filecontent = f.read()

        assert filecontent == content.getvalue()

        # Expect a file 'abc' in the workdir
        with open(os.path.join(workflow.source.workdir, plugin.REMOTE_SOURCE, member), 'rb') as f:
            filecontent = f.read()

        assert filecontent == abc_content

        # Expect buildargs to have been set
        for arg, value in buildargs.items():
            assert workflow.builder.buildargs[arg] == value
        # along with the args needed to add the sources in the Dockerfile
        assert workflow.builder.buildargs['REMOTE_SOURCE'] == plugin.REMOTE_SOURCE
        assert workflow.builder.buildargs['REMOTE_SOURCE_DIR'] == REMOTE_SOURCE_DIR
        # https://github.com/openshift/imagebuilder/issues/139
        assert not workflow.builder.buildargs['REMOTE_SOURCE'].startswith('/')
