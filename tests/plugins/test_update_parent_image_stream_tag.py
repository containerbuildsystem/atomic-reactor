"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_update_parent_image_stream_tag import \
                                             UpdateParentImageStreamTagPlugin
from atomic_reactor.util import ImageName
from flexmock import flexmock
from osbs.api import OSBS
from osbs.exceptions import OsbsResponseException
from tests.constants import SOURCE, MOCK

import json
import osbs.conf
import pytest

if MOCK:
    from tests.docker_mock import mock_docker


class X(object):
    pass


class TestUpdateParentImageStreamTag(object):
    def prepare(self):
        if MOCK:
            mock_docker()
        tasker = DockerTasker()
        workflow = DockerBuildWorkflow(SOURCE, "test-image")
        setattr(workflow, 'builder', X())
        setattr(workflow.builder, 'image_id', 'asd123')
        setattr(workflow.builder, 'base_image', ImageName(repo='Fedora',
                                                          tag='22'))
        setattr(workflow.builder, 'source', X())
        setattr(workflow.builder.source, 'path', '/tmp')
        setattr(workflow.builder.source, 'dockerfile_path', None)

        return workflow, tasker

    @pytest.mark.parametrize(('status_code'), (200, 404, 500))
    @pytest.mark.parametrize(('changed'), (True, False))
    @pytest.mark.parametrize(('scheduled'), (True, False, None))
    def test_update_parent_image_stream_tag_plugin(self, monkeypatch,
                                                   status_code, changed,
                                                   scheduled):
        workflow, tasker = self.prepare()

        openshift_url = 'http://openshift-url.com'
        build_json_dir = '/usr/share/osbs'
        namespace = 'namespace1'
        image_stream_id = 'imagestream1'
        image_stream_tag_name = 'imagestreamtag1'
        image_stream_tag_id = 'imagestream1:imagestreamtag1'

        image_stream = {'kind': 'ImageStream'}
        image_stream_response = flexmock()
        image_stream_response.should_receive('json').and_return(image_stream)

        _osbs = flexmock(OSBS)

        get_image_stream = (_osbs.should_receive('get_image_stream')
                            .with_args(image_stream_id)
                            .once())

        if status_code == 200:
            get_image_stream.and_return(image_stream_response)

            (_osbs.should_receive('ensure_image_stream_tag')
             .with_args(image_stream, image_stream_tag_name, bool(scheduled))
             .once()
             .and_return(changed))

        else:
            (get_image_stream
             .and_raise(OsbsResponseException('error', status_code)))

        (flexmock(osbs.conf).should_call('Configuration')
         .with_args(
             conf_file=None,
             openshift_url=openshift_url,
             use_auth=True,
             verify_ssl=True,
             build_json_dir=build_json_dir,
             namespace=namespace))

        plugin_args = {
            'image_stream_tag': image_stream_tag_id,
            'openshift_url': openshift_url,
            'build_json_dir': build_json_dir,
        }

        if scheduled is not None:
            plugin_args['scheduled'] = scheduled

        runner = PreBuildPluginsRunner(tasker, workflow, [
            {'name': UpdateParentImageStreamTagPlugin.key, 'args': plugin_args}
        ])

        monkeypatch.setenv('BUILD', json.dumps({
            'metadata': {'namespace': namespace}}))

        if status_code == 200:
            runner.run()
            assert (workflow
                    .prebuild_results[UpdateParentImageStreamTagPlugin.key] is
                    changed)

        elif status_code == 404:
            runner.run()
            # ImageStreamTag is never changed if ImageStream is missing
            assert (workflow
                    .prebuild_results[UpdateParentImageStreamTagPlugin.key] is
                    False)

        elif status_code == 500:
            with pytest.raises(PluginFailedException):
                runner.run()
