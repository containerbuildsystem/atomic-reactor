"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import pytest

from atomic_reactor.plugin import BuildPlugin
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
def test_store_metadata(metadata_decorator, metadata_attr, workflow):
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

    p1 = BP1(workflow)
    p2 = BP2(workflow)

    assert p1.run() == 1
    assert p2.run() is None

    other_attr = 'labels' if metadata_attr == 'annotations' else 'annotations'
    assert getattr(workflow.data, metadata_attr) == {'foo': 1}
    assert getattr(workflow.data, other_attr) == {}


@pytest.mark.parametrize('metadata_map_decorator, metadata_attr', [
    (annotation_map, 'annotations'),
    (label_map, 'labels')
])
def test_store_metadata_map(metadata_map_decorator, metadata_attr, workflow):
    @metadata_map_decorator('foo')
    @metadata_map_decorator('bar_baz', lambda result: result['bar'] + result['baz'])
    class BP1(BuildPlugin):
        key = 'bp1'

        def run(self):
            return {'foo': 1, 'bar': 2, 'baz': 3}

    @metadata_map_decorator('spam')
    class BP2(BuildPlugin):
        key = 'bp2'

        def run(self):
            return None

    p1 = BP1(workflow)
    p2 = BP2(workflow)

    assert p1.run() == {'foo': 1, 'bar': 2, 'baz': 3}
    assert p2.run() is None

    other_attr = 'labels' if metadata_attr == 'annotations' else 'annotations'
    assert getattr(workflow.data, metadata_attr) == {'foo': 1, 'bar_baz': 5}
    assert getattr(workflow.data, other_attr) == {}


@pytest.mark.parametrize('metadata_decorator, expected_err_msg', [
    (annotation, '[annotations] Already set: {!r}'.format('foo')),
    (annotation_map, '[annotations] Already set: {!r}'.format('foo')),
    (label, '[labels] Already set: {!r}'.format('foo')),
    (label_map, '[labels] Already set: {!r}'.format('foo'))
])
def test_store_metadata_conflict(metadata_decorator, expected_err_msg, workflow):
    @metadata_decorator('foo')
    class BP(BuildPlugin):
        key = 'bp'

        def run(self):
            return {'foo': 1}

    p = BP(workflow)

    p.run()
    with pytest.raises(RuntimeError) as exc_info:
        p.run()
    assert str(exc_info.value) == expected_err_msg


def test_store_metadata_combined(workflow):
    @annotation('foo')
    @annotation_map('bar')
    @label('spam')
    @label_map('eggs')
    class BP(BuildPlugin):
        key = 'bp'

        def run(self):
            return {'bar': 1, 'eggs': 2}

    p = BP(workflow)

    p.run()
    assert workflow.data.annotations == {
        'foo': {'bar': 1, 'eggs': 2},
        'bar': 1
    }
    assert workflow.data.labels == {
        'spam': {'bar': 1, 'eggs': 2},
        'eggs': 2
    }
