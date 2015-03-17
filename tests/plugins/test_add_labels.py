from __future__ import print_function

import os
from collections import OrderedDict
from dock.core import DockerTasker
from dock.inner import DockerBuildWorkflow
from dock.plugin import PreBuildPluginsRunner
from dock.plugins.pre_add_labels_in_df import AddLabelsPlugin


class X(object):
    pass


def test_yuminject_plugin(tmpdir):
    df = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
    tmp_df = os.path.join(str(tmpdir), 'Dockerfile')
    with open(tmp_df, mode="w") as fd:
        fd.write(df)

    tasker = DockerTasker()
    workflow = DockerBuildWorkflow("asd", "test-image")
    setattr(workflow, 'builder', X)

    labels_conf = OrderedDict({'label1': 'value 1', 'label2': 'long value'})

    runner = PreBuildPluginsRunner(
        tasker,
        workflow,
        [{
             'name': AddLabelsPlugin.key,
             'args': {'labels': labels_conf}
        }]
    )
    runner.run()
    assert AddLabelsPlugin.key is not None
    with open(tmp_df, 'r') as fd:
        altered_df = fd.read()
    expected_output = r"""FROM fedora
RUN yum install -y python-django
LABEL "label1"="value 1" "label2"="long value"
CMD blabla"""
    assert expected_output == altered_df
