"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.plugins.post_pulp_pull import (PulpPullPlugin,
                                                   CraneTimeoutError)
from atomic_reactor.inner import TagConf, PushConf
from docker.errors import NotFound
import time

from flexmock import flexmock
import pytest


class MockerTasker(object):
    def __init__(self):
        self.pulled_images = []

    def pull_image(self, image):
        self.pulled_images.append(image)
        return image.to_str()

    def inspect_image(self, image):
        pass


class TestPostPulpPull(object):
    TEST_UNIQUE_IMAGE = 'foo:unique-tag'
    CRANE_URI = 'crane.example.com'

    def workflow(self):
        tag_conf = TagConf()
        tag_conf.add_unique_image(self.TEST_UNIQUE_IMAGE)
        push_conf = PushConf()
        push_conf.add_pulp_registry('pulp', crane_uri=self.CRANE_URI)
        builder = flexmock()
        setattr(builder, 'image_id', 'sha256:(old)')
        return flexmock(tag_conf=tag_conf,
                        push_conf=push_conf,
                        builder=builder,
                        plugin_workspace={})

    def test_pull_first_time(self):
        workflow = self.workflow()
        tasker = MockerTasker()
        expected_pullspec = '%s/%s' % (self.CRANE_URI, self.TEST_UNIQUE_IMAGE)
        test_id = 'sha256:(new)'

        (flexmock(tasker)
            .should_call('pull_image')
            .and_return(expected_pullspec)
            .once()
            .ordered())

        (flexmock(tasker)
            .should_receive('inspect_image')
            .with_args(expected_pullspec)
            .and_return({'Id': test_id})
            .once())

        plugin = PulpPullPlugin(tasker, workflow)

        # Plugin return value is the new ID
        assert plugin.run() == test_id

        assert len(tasker.pulled_images) == 1
        pulled = tasker.pulled_images[0].to_str()
        assert pulled == expected_pullspec

        # Image ID is updated in workflow
        assert workflow.builder.image_id == test_id

    def test_pull_timeout(self):
        workflow = self.workflow()
        tasker = MockerTasker()
        expected_pullspec = '%s/%s' % (self.CRANE_URI, self.TEST_UNIQUE_IMAGE)

        (flexmock(tasker)
            .should_call('pull_image')
            .and_return(expected_pullspec)
            .times(3))

        (flexmock(tasker)
            .should_receive('inspect_image')
            .with_args(expected_pullspec)
            .and_raise(NotFound('message', flexmock(content=None)))
            .times(3))

        plugin = PulpPullPlugin(tasker, workflow, timeout=1, retry_delay=0.6)

        # Should raise a timeout exception
        with pytest.raises(CraneTimeoutError):
            plugin.run()

    def test_pull_retry(self):
        workflow = self.workflow()
        tasker = MockerTasker()
        expected_pullspec = '%s/%s' % (self.CRANE_URI, self.TEST_UNIQUE_IMAGE)
        test_id = 'sha256:(new)'

        (flexmock(tasker)
            .should_call('pull_image')
            .and_return(expected_pullspec)
            .times(3))

        (flexmock(tasker)
            .should_receive('inspect_image')
            .with_args(expected_pullspec)
            .and_raise(NotFound('message', flexmock(content=None)))
            .and_raise(NotFound('message', flexmock(content=None)))
            .and_return({'Id': test_id})
            .times(3))

        plugin = PulpPullPlugin(tasker, workflow, timeout=1, retry_delay=0.6)

        # Plugin return value is the new ID
        assert plugin.run() == test_id

        assert len(tasker.pulled_images) == 3
        for image in tasker.pulled_images:
            pulled = image.to_str()
            assert pulled == expected_pullspec

        # Image ID is updated in workflow
        assert workflow.builder.image_id == test_id
