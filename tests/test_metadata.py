"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import pytest

from atomic_reactor.plugin import Plugin
from atomic_reactor.metadata import (
    annotation,
    annotation_map,
)

pytestmark = pytest.mark.usefixtures('user_params')


def test_store_metadata(workflow):
    @annotation('foo')
    class BP1(Plugin):
        key = 'bp1'

        def run(self):
            return 1

    @annotation('baz')
    class BP2(Plugin):
        key = 'bp2'

        def run(self):
            return None

    p1 = BP1(workflow)
    p2 = BP2(workflow)

    assert p1.run() == 1
    assert p2.run() is None

    assert getattr(workflow.data, 'annotations') == {'foo': 1}


def test_store_metadata_map(workflow):

    @annotation_map('foo')
    @annotation_map('bar_baz', lambda result: result['bar'] + result['baz'])
    class BP1(Plugin):
        key = 'bp1'

        def run(self):
            return {'foo': 1, 'bar': 2, 'baz': 3}

    @annotation_map('spam')
    class BP2(Plugin):
        key = 'bp2'

        def run(self):
            return None

    p1 = BP1(workflow)
    p2 = BP2(workflow)

    assert p1.run() == {'foo': 1, 'bar': 2, 'baz': 3}
    assert p2.run() is None

    assert getattr(workflow.data, 'annotations') == {'foo': 1, 'bar_baz': 5}


@pytest.mark.parametrize('metadata_decorator, expected_err_msg', [
    (annotation, '[annotations] Already set: {!r}'.format('foo')),
    (annotation_map, '[annotations] Already set: {!r}'.format('foo')),
])
def test_store_metadata_conflict(metadata_decorator, expected_err_msg, workflow):
    @metadata_decorator('foo')
    class BP(Plugin):
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
    class BP(Plugin):
        key = 'bp'

        def run(self):
            return {'bar': 1, 'eggs': 2}

    p = BP(workflow)

    p.run()
    assert workflow.data.annotations == {
        'foo': {'bar': 1, 'eggs': 2},
        'bar': 1
    }
