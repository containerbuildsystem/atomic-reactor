"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import os
try:
    from collections import OrderedDict
except ImportError:
    # Python 2.6
    from ordereddict import OrderedDict
from dockerfile_parse import DockerfileParser
from atomic_reactor.core import DockerTasker
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner
from atomic_reactor.plugins.pre_add_labels_in_df import AddLabelsPlugin
from atomic_reactor.util import ImageName
from tests.constants import MOCK_SOURCE
import json
import pytest


class Y(object):
    pass


class X(object):
    image_id = "xxx"
    source = Y()
    source.dockerfile_path = None
    source.path = None
    base_image = ImageName(repo="qwe", tag="asd")

DF_CONTENT = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
LABELS_CONF = OrderedDict({'label1': 'value 1', 'label2': 'long value'})
LABELS_CONF_WRONG = [('label1', 'value1'), ('label2', 'value2')]
# Can't be sure of the order of the labels, expect either
EXPECTED_OUTPUT = [r"""FROM fedora
RUN yum install -y python-django
LABEL "label1"="value 1" "label2"="long value"
CMD blabla""", r"""FROM fedora
RUN yum install -y python-django
LABEL "label2"="long value" "label1"="value 1"
CMD blabla"""]
@pytest.mark.parametrize('labels_conf, expected_output', [
    (LABELS_CONF, EXPECTED_OUTPUT),
    (json.dumps(LABELS_CONF), EXPECTED_OUTPUT),
    (LABELS_CONF_WRONG, RuntimeError())
])
def test_add_labels_plugin(tmpdir, labels_conf, expected_output):
    df = DockerfileParser(str(tmpdir))
    df.content = DF_CONTENT

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {'labels': labels_conf}
        }]
    )

    if isinstance(expected_output, RuntimeError):
        with pytest.raises(RuntimeError):
            runner.run()
    else:
        runner.run()
        assert AddLabelsPlugin.key is not None
        assert df.content in expected_output
