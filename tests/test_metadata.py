"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, unicode_literals

import pytest

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import Plugin, BuildPlugin
from atomic_reactor.metadata import (
    annotation,
    annotation_map,
    label,
    label_map,
)

pytestmark = pytest.mark.usefixtures('user_params')


@pytest.mark.parametrize('metadata_decorator, metadata_attr', [
    (annotation, 'annotations'),
    (label, 'labels')
])
def test_store_metadata(metadata_decorator, metadata_attr):
    @metadata_decorator('foo')
    class BP1(BuildPlugin):
        key = 'bp1'

        def run(self):
            return 1

    @metadata_decorator('baz')
    class BP2(BuildPlugin):
        key = 'bp2'

        def run(self):
            return None

    tasker = object()
    workflow = DockerBuildWorkflow()
    p1 = BP1(tasker, workflow)
    p2 = BP2(tasker, workflow)

    assert p1.run() == 1
    assert p2.run() is None

    other_attr = 'labels' if metadata_attr == 'annotations' else 'annotations'
    assert getattr(workflow, metadata_attr) == {'foo': 1}
    assert getattr(workflow, other_attr) == {}


@pytest.mark.parametrize('metadata_map_decorator, metadata_attr', [
    (annotation_map, 'annotations'),
    (label_map, 'labels')
])
def test_store_metadata_map(metadata_map_decorator, metadata_attr):
    @metadata_map_decorator('foo', 'bar')
    class BP1(BuildPlugin):
        key = 'bp1'

        def run(self):
            return {'foo': 1, 'bar': 2, 'baz': 3}

    @metadata_map_decorator('spam')
    class BP2(BuildPlugin):
        key = 'bp2'

        def run(self):
            return None

    tasker = object()
    workflow = DockerBuildWorkflow()
    p1 = BP1(tasker, workflow)
    p2 = BP2(tasker, workflow)

    assert p1.run() == {'foo': 1, 'bar': 2, 'baz': 3}
    assert p2.run() is None

    other_attr = 'labels' if metadata_attr == 'annotations' else 'annotations'
    assert getattr(workflow, metadata_attr) == {'foo': '1', 'bar': '2'}
    assert getattr(workflow, other_attr) == {}


@pytest.mark.parametrize('metadata_decorator, expected_err_msg', [
    (annotation, '[annotations] Not a subclass of BuildPlugin'),
    (annotation_map, '[annotations] Not a subclass of BuildPlugin'),
    (label, '[labels] Not a subclass of BuildPlugin'),
    (label_map, '[labels] Not a subclass of BuildPlugin'),
])
def test_store_metadata_wrong_class(metadata_decorator, expected_err_msg):
    decorate = metadata_decorator('foo')

    with pytest.raises(TypeError) as exc_info:
        decorate(Plugin)
    assert str(exc_info.value) == expected_err_msg


@pytest.mark.parametrize('metadata_decorator, expected_err_msg', [
    (annotation_map, '[annotations] run() method did not return a dict'),
    (label_map, '[labels] run() method did not return a dict')
])
def test_store_metadata_wrong_return_type(metadata_decorator, expected_err_msg):
    @metadata_decorator('foo')
    class BP(BuildPlugin):
        key = 'bp'

        def run(self):
            return 1

    tasker = object()
    workflow = DockerBuildWorkflow()
    p = BP(tasker, workflow)

    with pytest.raises(TypeError) as exc_info:
        p.run()

    assert str(exc_info.value) == expected_err_msg


@pytest.mark.parametrize('metadata_decorator, expected_err_msg', [
    (annotation_map, '[annotations] Not found in result: {!r}'.format('bar')),
    (label_map, '[labels] Not found in result: {!r}'.format('bar'))
])
def test_store_metadata_missing_key(metadata_decorator, expected_err_msg):
    @metadata_decorator('foo', 'bar')
    class BP(BuildPlugin):
        key = 'bp'

        def run(self):
            return {'foo': 1}

    tasker = object()
    workflow = DockerBuildWorkflow()
    p = BP(tasker, workflow)

    with pytest.raises(RuntimeError) as exc_info:
        p.run()
    assert str(exc_info.value) == expected_err_msg


@pytest.mark.parametrize('metadata_decorator, expected_err_msg', [
    (annotation, '[annotations] Already set: {!r}'.format('foo')),
    (annotation_map, '[annotations] Already set: {!r}'.format('foo')),
    (label, '[labels] Already set: {!r}'.format('foo')),
    (label_map, '[labels] Already set: {!r}'.format('foo'))
])
def test_store_metadata_conflict(metadata_decorator, expected_err_msg):
    @metadata_decorator('foo')
    class BP(BuildPlugin):
        key = 'bp'

        def run(self):
            return {'foo': 1}

    tasker = object()
    workflow = DockerBuildWorkflow()
    p = BP(tasker, workflow)

    p.run()
    with pytest.raises(RuntimeError) as exc_info:
        p.run()
    assert str(exc_info.value) == expected_err_msg


def test_store_metadata_combined():
    @annotation('foo')
    @annotation_map('bar')
    @label('spam')
    @label_map('eggs')
    class BP(BuildPlugin):
        key = 'bp'

        def run(self):
            return {'bar': 1, 'eggs': 2}

    tasker = object()
    workflow = DockerBuildWorkflow()
    p = BP(tasker, workflow)

    p.run()
    assert workflow.annotations == {
        'foo': {'bar': 1, 'eggs': 2},
        'bar': '1'
    }
    assert workflow.labels == {
        'spam': {'bar': 1, 'eggs': 2},
        'eggs': '2'
    }
