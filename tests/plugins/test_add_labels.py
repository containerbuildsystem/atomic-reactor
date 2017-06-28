"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

from collections import OrderedDict
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_add_labels_in_df import AddLabelsPlugin
from atomic_reactor.util import ImageName, df_parser
from atomic_reactor.source import VcsInfo
from atomic_reactor.constants import INSPECT_CONFIG
import re
import json
import logging
import pytest
from flexmock import flexmock
from tests.constants import MOCK_SOURCE, DOCKERFILE_GIT, DOCKERFILE_SHA1, MOCK
from tests.fixtures import docker_tasker
if MOCK:
    from tests.docker_mock import mock_docker


class MockSource(object):
    dockerfile_path = None
    path = None
    def get_vcs_info(self):
        return VcsInfo(vcs_type="git", vcs_url=DOCKERFILE_GIT, vcs_ref=DOCKERFILE_SHA1)


class X(object):
    image_id = "xxx"
    source = MockSource()
    base_image = ImageName(repo="qwe", tag="asd")

DF_CONTENT = """\
FROM fedora
RUN yum install -y python-django
CMD blabla"""
DF_CONTENT_WITH_LABELS = '''\
FROM fedora
RUN yum install -y python-django
CMD blabla
LABEL "build-date" = "docker value"
LABEL "architecture" = "docker value"
LABEL "vcs-type" = "docker value"
LABEL "vcs-url" = "docker value"
LABEL "vcs-ref" = "docker value"
LABEL "com.redhat.build-host" = "docker value"
LABEL "Build_Host" = "docker value"'''
DF_CONTENT_SINGLE_LINE = """\
FROM fedora"""
DF_CONTENT_LABEL = '''\
FROM fedora
LABEL "label2"="df value"'''
DF_CONTENT_LABELS = '''\
FROM fedora
LABEL "label1"="label1_value"
LABEL "label2"="label2_value"
LABEL "Authoritative_Registry"="authoritative-source-url_value"
LABEL "BZComponent"="com.redhat.component_value"
LABEL "Build_Host"="com.redhat.build-host_value"
LABEL "Version"="version_value"'''
LABELS_CONF_WITH_LABELS = {INSPECT_CONFIG: {"Labels": {
                                                "build-date": "base value",
                                                "architecture": "base value",
                                                "vcs-type": "base value",
                                                "vcs-url": "base value",
                                                "vcs-ref": "base value",
                                                "com.redhat.build-host": "base value",
                                                "Build_Host": "base value"}}}
LABELS_CONF_BASE = {INSPECT_CONFIG: {"Labels": {"label1": "base value"}}}
LABELS_CONF_BASE_NONE = {INSPECT_CONFIG: {"Labels": None}}
LABELS_CONF = OrderedDict({'label1': 'value 1', 'label2': 'long value'})
LABELS_CONF_ONE = {'label2': 'long value'}
LABELS_CONF_WRONG = [('label1', 'value1'), ('label2', 'value2')]
LABELS_BLANK = {}
# Can't be sure of the order of the labels, expect either
EXPECTED_OUTPUT = ["""FROM fedora
RUN yum install -y python-django
CMD blabla
LABEL "label1"="value 1" "label2"="long value"
""", """\
FROM fedora
RUN yum install -y python-django
CMD blabla
LABEL "label2"="long value" "label1"="value 1"
"""]
EXPECTED_OUTPUT2 = [r"""FROM fedora
RUN yum install -y python-django
CMD blabla
LABEL "label2"="long value"
"""]
EXPECTED_OUTPUT3 = [DF_CONTENT]
EXPECTED_OUTPUT4 = [r"""FROM fedora
LABEL "label2"="long value"
"""]
EXPECTED_OUTPUT5 = [r"""FROM fedora
LABEL "labelnew"="base value"
"""]
EXPECTED_OUTPUT6 = [r"""FROM fedora
LABEL "labelnew"="long value" "label2"="long value"
""", r"""FROM fedora
LABEL "label2"="long value" "labelnew"="long value"
"""]
EXPECTED_OUTPUT7 = [r"""FROM fedora
LABEL "label2"="df value"
LABEL "labelnew"="df value"
"""]
EXPECTED_OUTPUT8 = [r"""FROM fedora
LABEL "label1"="df value"
LABEL "label2"="df value"
""", r"""FROM fedora
LABEL "label2"="df value"
LABEL "label1"="df value"
""",
]

@pytest.mark.parametrize('df_content, labels_conf_base, labels_conf, dont_overwrite, aliases, expected_output', [
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, [], {}, EXPECTED_OUTPUT),
    (DF_CONTENT, LABELS_CONF_BASE, json.dumps(LABELS_CONF), [], {}, EXPECTED_OUTPUT),
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF_WRONG, [], {}, RuntimeError()),
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, ["label1", ], {}, EXPECTED_OUTPUT2),
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_BLANK, ["label1", ], {}, EXPECTED_OUTPUT3),
    (DF_CONTENT_SINGLE_LINE, LABELS_CONF_BASE, LABELS_CONF, ["label1", ], {}, EXPECTED_OUTPUT4),
    (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, [], {"not": "present"}, EXPECTED_OUTPUT),
    (DF_CONTENT_SINGLE_LINE, LABELS_CONF_BASE, LABELS_BLANK, [], {"label1": "labelnew"}, EXPECTED_OUTPUT5),
    (DF_CONTENT_SINGLE_LINE, LABELS_CONF_BASE, LABELS_CONF_ONE, [], {"label2": "labelnew"}, EXPECTED_OUTPUT6),
    (DF_CONTENT_LABEL, LABELS_CONF_BASE, LABELS_BLANK, [], {"label2": "labelnew"}, EXPECTED_OUTPUT7),
    (DF_CONTENT_LABEL, LABELS_CONF_BASE, LABELS_BLANK, [], {"label2": "labelnew", "x": "y"}, EXPECTED_OUTPUT7),
    (DF_CONTENT_LABEL, LABELS_CONF_BASE_NONE, LABELS_BLANK, [], {"label2": "labelnew"}, EXPECTED_OUTPUT7),
    (DF_CONTENT_LABEL, LABELS_CONF_BASE, LABELS_BLANK, [], {"label2": "label1"}, EXPECTED_OUTPUT8),
])
def test_add_labels_plugin(tmpdir, docker_tasker,
                           df_content, labels_conf_base, labels_conf, dont_overwrite, aliases,
                           expected_output, caplog):
    df = df_parser(str(tmpdir))
    df.content = df_content

    if MOCK:
        mock_docker()

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, base_image_inspect=labels_conf_base)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {
                'labels': labels_conf,
                'dont_overwrite': dont_overwrite,
                'auto_labels': [],
                'aliases': aliases,
            }
        }]
    )

    if isinstance(expected_output, RuntimeError):
        with pytest.raises(PluginFailedException):
            runner.run()
        assert "plugin 'add_labels_in_dockerfile' raised an exception: RuntimeError" in caplog.text()

    else:
        runner.run()
        assert AddLabelsPlugin.key is not None
        assert df.content in expected_output

@pytest.mark.parametrize('auto_label, value_re_part', [
    ('build-date', r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?'),
    ('architecture', 'x86_64'),
    ('vcs-type', 'git'),
    ('vcs-url', DOCKERFILE_GIT),
    ('vcs-ref', DOCKERFILE_SHA1),
    ('com.redhat.build-host', 'the-build-host'),
    ('Build_Host', 'the-build-host'),
])
def test_add_labels_plugin_generated(tmpdir, docker_tasker, auto_label, value_re_part):
    df = df_parser(str(tmpdir))
    df.content = DF_CONTENT

    if MOCK:
        mock_docker()

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, source=MockSource())
    flexmock(workflow, base_image_inspect=LABELS_CONF_BASE)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {'labels': {}, "dont_overwrite": [], "auto_labels": [auto_label],
                     'aliases': {'Build_Host': 'com.redhat.build-host'}}
        }]
    )

    runner.run()
    assert re.match(value_re_part, df.labels[auto_label])

@pytest.mark.parametrize('df_old_as_plugin_arg', [True, False])
@pytest.mark.parametrize('df_new_as_plugin_arg', [True, False])
@pytest.mark.parametrize('base_old, base_new, df_old, df_new, expected_old, expected_new, expected_log', [
    (None,  None,  None,  None,  None,  None, None                             ),
    (None,  None,  None,  'A',   None,  'A',  None                             ),
    (None,  None,  'A',   None,  'A',   'A',  'as an alias for label'          ),
    (None,  None,  'A',   'A',   'A',   'A',  'already exists'                 ),
    (None,  None,  'A',   'B',   'A',   'B',  'should probably have same value'),
    (None,  'A',   None,  None,  None,  'A',  None                             ),
    (None,  'A',   None,  'A',   None,  'A',  None                             ),
    (None,  'A',   None,  'B',   None,  'B',  None                             ),
    (None,  'A',   'A',   None,  'A',   'A',  'as an alias for label'          ),
    (None,  'A',   'B',   None,  'B',   'B',  'as an alias for label'          ),
    (None,  'A',   'A',   'A',   'A',   'A',  'already exists'                 ),
    (None,  'A',   'A',   'B',   'A',   'B',  'should probably have same value'),
    (None,  'A',   'B',   'A',   'B',   'A',  'should probably have same value'),
    (None,  'A',   'B',   'B',   'B',   'B',  'already exists'                 ),
    (None,  'A',   'B',   'C',   'B',   'C',  'should probably have same value'),
    ('A',   None,  None,  None,  'A',   'A',  'as an alias for label'          ),
    ('A',   None,  None,  'A',   'A',   'A',  'already exists'                 ),
    ('A',   None,  None,  'B',   'B',   'B',  'as an alias for label'          ),
    ('A',   None,  'A',   None,  'A',   'A',  'as an alias for label'          ),
    ('A',   None,  'B',   None,  'B',   'B',  'as an alias for label'          ),
    ('A',   None,  'A',   'A',   'A',   'A',  'already exists'                 ),
    ('A',   None,  'A',   'B',   'A',   'B',  'should probably have same value'),
    ('A',   None,  'B',   'A',   'B',   'A',  'should probably have same value'),
    ('A',   None,  'B',   'B',   'B',   'B',  'already exists'                 ),
    ('A',   None,  'B',   'C',   'B',   'C',  'should probably have same value'),
    ('A',   'A',   None,  None,  'A',   'A',  'as an alias for label'          ),
    ('A',   'A',   None,  'A',   'A',   'A',  'already exists'                 ),
    ('A',   'A',   None,  'B',   'B',   'B',  'as an alias for label'          ),
    ('A',   'A',   'A',   None,  'A',   'A',  'as an alias for label'          ),
    ('A',   'A',   'B',   None,  'B',   'B',  'as an alias for label'          ),
    ('A',   'A',   'A',   'A',   'A',   'A',  'already exists'                 ),
    ('A',   'A',   'A',   'B',   'A',   'B',  'should probably have same value'),
    ('A',   'A',   'B',   'A',   'B',   'A',  'should probably have same value'),
    ('A',   'A',   'B',   'B',   'B',   'B',  'already exists'                 ),
    ('A',   'A',   'B',   'C',   'B',   'C',  'should probably have same value'),
    ('A',   'B',   None,  None,  'B',   'B',  'as an alias for label'          ), #really?
    ('A',   'B',   None,  'A',   'A',   'A',  'already exists'                 ),
    ('A',   'B',   None,  'B',   'B',   'B',  'as an alias for label'          ),
    ('A',   'B',   None,  'C',   'C',   'C',  'as an alias for label'          ),
    ('A',   'B',   'A',   None,  'A',   'A',  'as an alias for label'          ),
    ('A',   'B',   'B',   None,  'B',   'B',  'as an alias for label'          ),
    ('A',   'B',   'C',   None,  'C',   'C',  'as an alias for label'          ),
    ('A',   'B',   'A',   'A',   'A',   'A',  'already exists'                 ),
    ('A',   'B',   'A',   'B',   'A',   'B',  'should probably have same value'),
    ('A',   'B',   'A',   'C',   'A',   'C',  'should probably have same value'),
    ('A',   'B',   'B',   'A',   'B',   'A',  'should probably have same value'),
    ('A',   'B',   'B',   'B',   'B',   'B',  'already exists'                 ),
    ('A',   'B',   'B',   'C',   'B',   'C',  'should probably have same value'),
    ('A',   'B',   'C',   'A',   'C',   'A',  'should probably have same value'),
    ('A',   'B',   'C',   'B',   'C',   'B',  'should probably have same value'),
    ('A',   'B',   'C',   'C',   'C',   'C',  'already exists'                 ),
    ('A',   'B',   'C',   'D',   'C',   'D',  'should probably have same value'),
])
def test_add_labels_aliases(tmpdir, docker_tasker, caplog,
                            df_old_as_plugin_arg, df_new_as_plugin_arg,
                            base_old, base_new, df_old, df_new, expected_old, expected_new, expected_log):
    if MOCK:
        mock_docker()

    df_content = "FROM fedora\n"
    plugin_labels = {}
    if df_old:
        if df_old_as_plugin_arg:
            plugin_labels["label_old"] = df_old
        else:
            df_content += 'LABEL label_old="{0}"\n'.format(df_old)
    if df_new:
        if df_new_as_plugin_arg:
            plugin_labels["label_new"] = df_new
        else:
            df_content += 'LABEL label_new="{0}"\n'.format(df_new)

    base_labels = {INSPECT_CONFIG: {"Labels": {}}}
    if base_old:
        base_labels[INSPECT_CONFIG]["Labels"]["label_old"] = base_old
    if base_new:
        base_labels[INSPECT_CONFIG]["Labels"]["label_new"] = base_new

    df = df_parser(str(tmpdir))
    df.content = df_content

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, base_image_inspect=base_labels)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {
                'labels': plugin_labels,
                'dont_overwrite': [],
                'auto_labels': [],
                'aliases': {"label_old": "label_new"},
            }
        }]
    )

    runner.run()
    assert AddLabelsPlugin.key is not None
    result_old = df.labels.get("label_old") or base_labels[INSPECT_CONFIG]["Labels"].get("label_old")
    result_new = df.labels.get("label_new") or base_labels[INSPECT_CONFIG]["Labels"].get("label_new")
    assert result_old == expected_old
    assert result_new == expected_new

    if expected_log:
        assert expected_log in caplog.text()

@pytest.mark.parametrize('base_fst, base_snd, df_fst, df_snd, expected, expected_log', [  # noqa
    (None,  None,  None,  None,  None,  None),
    (None,  None,  None,  'A',   'A',   'adding equal label'),
    (None,  None,  'A',   None,  'A',   'adding equal label'),
    ('A',   None,  None,  None,  'A',   'adding equal label'),
    (None,  'A',   None,  None,  'A',   'adding equal label'),
    ('A',   'B',   None,  None,  None,  RuntimeError()),
    (None,  None,  'A',   'B',   None,  RuntimeError()),
    ('A',   'A',   None,  None,  'A',   None),
    ('A',   None,  'A',   None,  'A',   'adding equal label'),
    (None,  'A',   None,  'A',   'A',   'adding equal label'),
    ('A',   None,  'B',   None,  'B',   'adding equal label'),
    (None,  'A',   None,  'B',   'B',   'adding equal label'),
    ('A',   'C',   'B',   None,  'B',   'adding equal label'),
    ('A',   'C',   None,  'B',   'B',   'adding equal label'),
    ('A',   'C',   'B',   'B',   'B',   None),
    (None,  'A',   'B',   'B',   'B',   None),
    ('A',   None,  'B',   'B',   'B',   None),
    ('A',   'A',   None,  None,  'A',   None),
    ('A',   None,  None,  'A',   'A',   'skipping label'),
    (None,  'A',   'A',   None,  'A',   'skipping label'),
])
def test_add_labels_equal_aliases(tmpdir, docker_tasker, caplog,
                                  base_fst, base_snd, df_fst, df_snd, expected, expected_log):
    if MOCK:
        mock_docker()

    df_content = "FROM fedora\n"
    plugin_labels = {}
    if df_fst:
        df_content += 'LABEL description="{0}"\n'.format(df_fst)
    if df_snd:
        df_content += 'LABEL io.k8s.description="{0}"\n'.format(df_snd)

    base_labels = {INSPECT_CONFIG: {"Labels": {}}}
    if base_fst:
        base_labels[INSPECT_CONFIG]["Labels"]["description"] = base_fst
    if base_snd:
        base_labels[INSPECT_CONFIG]["Labels"]["io.k8s.description"] = base_snd

    df = df_parser(str(tmpdir))
    df.content = df_content

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, base_image_inspect=base_labels)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {
                'labels': plugin_labels,
                'dont_overwrite': [],
                'auto_labels': [],
                'aliases': {},
                'equal_labels': [['description', 'io.k8s.description']]
            }
        }]
    )

    if isinstance(expected_log, RuntimeError):
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
        runner.run()
        assert AddLabelsPlugin.key is not None
        result_fst = df.labels.get("description") or base_labels[INSPECT_CONFIG]["Labels"].get("description")
        result_snd = df.labels.get("io.k8s.description") or base_labels[INSPECT_CONFIG]["Labels"].get("io.k8s.description")
        assert result_fst == expected
        assert result_snd == expected

        if expected_log:
            assert expected_log in caplog.text()

@pytest.mark.parametrize('base_fst, base_snd, base_trd, df_fst, df_snd, df_trd, expected, expected_log', [  # noqa
    (None,  None,  None,  None,  None,  None,  None,  None),
    (None,  None,  None,  None,  None,  'A',   'A',   'adding equal label'),
    ('A',   'B',   'B',   None,  None,  None,  None,  RuntimeError()),
    (None,  None,  None,  'A',   'B',   'B',   None,  RuntimeError()),
    ('A',   'A',   'A',   None,  None,  None,  'A',   None),
    ('A',   None,  'A',   'A',   None,  'A',   'A',   'adding equal label'),
    ('A',   None,  None,  None,  'A',   'A',   'A',   'skipping label'),
    (None,  'A',   'A',   'A',   'A',   None,  'A',   'skipping label'),
])
def test_add_labels_equal_aliases2(tmpdir, docker_tasker, caplog, base_fst, base_snd, base_trd,
                                   df_fst, df_snd, df_trd, expected, expected_log):
    """
    test with 3 equal labels
    """
    if MOCK:
        mock_docker()

    df_content = "FROM fedora\n"
    plugin_labels = {}
    if df_fst:
        df_content += 'LABEL description="{0}"\n'.format(df_fst)
    if df_snd:
        df_content += 'LABEL io.k8s.description="{0}"\n'.format(df_snd)
    if df_trd:
        df_content += 'LABEL description_third="{0}"\n'.format(df_trd)

    base_labels = {INSPECT_CONFIG: {"Labels": {}}}
    if base_fst:
        base_labels[INSPECT_CONFIG]["Labels"]["description"] = base_fst
    if base_snd:
        base_labels[INSPECT_CONFIG]["Labels"]["io.k8s.description"] = base_snd
    if base_trd:
        base_labels[INSPECT_CONFIG]["Labels"]["description_third"] = base_trd

    df = df_parser(str(tmpdir))
    df.content = df_content

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, base_image_inspect=base_labels)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {
                'labels': plugin_labels,
                'dont_overwrite': [],
                'auto_labels': [],
                'aliases': {},
                'equal_labels': [['description', 'io.k8s.description', 'description_third']]
            }
        }]
    )

    if isinstance(expected_log, RuntimeError):
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
        runner.run()
        assert AddLabelsPlugin.key is not None
        result_fst = df.labels.get("description") or base_labels[INSPECT_CONFIG]["Labels"].get("description")
        result_snd = df.labels.get("io.k8s.description") or base_labels[INSPECT_CONFIG]["Labels"].get("io.k8s.description")
        result_trd = df.labels.get("description_third") or base_labels[INSPECT_CONFIG]["Labels"].get("description_third")
        assert result_fst == expected
        assert result_snd == expected
        assert result_trd == expected

        if expected_log:
            assert expected_log in caplog.text()

@pytest.mark.parametrize("parent_scope, docker_scope, result_scope, dont_overwrite", [
    (None, None, "restricted", False),
    ("public", None, "restricted", False),
    ("private", None, "restricted", False),
    ("restricted", "public", "public", False),
    ("restricted", "restricted", "restricted", False),
    ("restricted", "private", "private", False),
    (None, None, "restricted", True),
    ("public", None, "restricted", True),
    ("private", None, "restricted", True),
    ("restricted", "public", "public", True),
    ("restricted", "restricted", "restricted", True),
    ("restricted", "private", "private", True),
    ("public", "private", "private", True)
])
def test_dont_overwrite_distribution_scope(tmpdir, docker_tasker, parent_scope,
                                           docker_scope, result_scope, dont_overwrite):
    df_content = "FROM fedora\n"
    if docker_scope:
        df_content += 'LABEL distribution-scope="{0}"'.format(docker_scope)

    if parent_scope:
        labels_conf_base = {INSPECT_CONFIG: {"Labels": {"distribution-scope": parent_scope}}}
    else:
        labels_conf_base = {INSPECT_CONFIG: {"Labels": {}}}

    df = df_parser(str(tmpdir))
    df.content = df_content

    if MOCK:
        mock_docker()

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, base_image_inspect=labels_conf_base)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    wf_args = {
        'labels': {"distribution-scope": "restricted"},
        'auto_labels': [],
        'aliases': {},
    }
    if dont_overwrite:
        wf_args["dont_overwrite_if_in_dockerfile"] = ("distribution-scope",)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': wf_args
        }]
    )

    runner.run()

    result = df.labels.get("distribution-scope")
    assert result == result_scope


@pytest.mark.parametrize('url_format, info_url', [
    ('url_pre {label1} {label2} url_post', 'url_pre label1_value label2_value url_post'),
    ('url_pre url_post', 'url_pre url_post'),
    ('url_pre {label1} {label2} {label3_non_existent} url_post', None),
    ('url_pre {label1} {label2} {version} url_post', 'url_pre label1_value label2_value version_value url_post'),
    ('url_pre {authoritative-source-url} {com.redhat.component} {com.redhat.build-host} url_post',
     'url_pre authoritative-source-url_value com.redhat.component_value com.redhat.build-host_value url_post'),
])
def test_url_label(tmpdir, docker_tasker, caplog, url_format, info_url):
    if MOCK:
        mock_docker()

    plugin_labels = {}
    base_labels = {INSPECT_CONFIG: {"Labels": {}}}
    df = df_parser(str(tmpdir))
    df.content = DF_CONTENT_LABELS

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, base_image_inspect=base_labels)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {
                'labels': plugin_labels,
                'dont_overwrite': [],
                'auto_labels': [],
                'info_url_format': url_format
            }
        }]
    )

    if info_url is not None:
        runner.run()
        assert df.labels.get("url") == info_url

    else:
        with pytest.raises(PluginFailedException):
            runner.run()

    assert AddLabelsPlugin.key is not None


@pytest.mark.parametrize('auto_label', [  # noqa
    'build-date',
    'architecture',
    'vcs-type',
    'vcs-url',
    'vcs-ref',
    'com.redhat.build-host',
    'Build_Host',
])
@pytest.mark.parametrize('labels_docker', [
    DF_CONTENT,
    DF_CONTENT_WITH_LABELS,
])
@pytest.mark.parametrize('labels_base', [
    LABELS_CONF_BASE_NONE,
    LABELS_CONF_WITH_LABELS,
])
def test_add_labels_plugin_explicit(tmpdir, docker_tasker, auto_label, labels_docker, labels_base):
    df = df_parser(str(tmpdir))
    df.content = labels_docker

    if MOCK:
        mock_docker()

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, source=MockSource())
    flexmock(workflow, base_image_inspect=labels_base)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    prov_labels = {}
    prov_labels[auto_label] = 'explicit_value'

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {'labels': prov_labels, "dont_overwrite": [], "auto_labels": [auto_label],
                     'aliases': {'Build_Host': 'com.redhat.build-host'}}
        }]
    )

    runner.run()

    assert df.labels[auto_label] == 'explicit_value'


@pytest.mark.parametrize('parent,should_fail', [  # noqa
    ('koji/image-build', False),
    ('fedora', True),
])
def test_add_labels_base_image(tmpdir, docker_tasker, parent, should_fail,
                               caplog):
    df = df_parser(str(tmpdir))
    df.content = "FROM {}\n".format(parent)

    if MOCK:
        mock_docker()

    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    setattr(workflow, 'builder', X)
    flexmock(workflow, source=MockSource())
    setattr(workflow.builder, 'tasker', docker_tasker)
    setattr(workflow.builder, 'df_path', df.dockerfile_path)

    # When a 'release' label is provided by parameter and used to
    # configure the plugin, it should be set in the Dockerfile even
    # when processing base images.
    prov_labels = {'release': '5'}

    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': AddLabelsPlugin.key,
            'args': {'labels': prov_labels, "dont_overwrite": [],
                     'aliases': {'Build_Host': 'com.redhat.build-host'}}
        }]
    )

    if should_fail:
        with caplog.atLevel(logging.ERROR):
            with pytest.raises(PluginFailedException):
                runner.run()

        msg = "base image was not inspected"
        assert msg in [x.message for x in caplog.records()]
    else:
        runner.run()
        assert df.labels['release'] == '5'
