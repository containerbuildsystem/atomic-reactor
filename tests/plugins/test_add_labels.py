"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import datetime
import json
import logging
import re
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from flexmock import flexmock

from atomic_reactor import start_time as atomic_reactor_start_time
from atomic_reactor.constants import INSPECT_CONFIG
from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_add_labels_in_df import AddLabelsPlugin
from atomic_reactor.source import VcsInfo
from atomic_reactor.types import ImageInspectionData

from tests.constants import DOCKERFILE_GIT, DOCKERFILE_SHA1
from tests.mock_env import MockEnv


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
LABELS_CONF_BASE_EXPLICIT = {INSPECT_CONFIG: {"Labels": {"version": "x", "release": "1"}}}
LABELS_CONF_BASE_NONE = {INSPECT_CONFIG: {"Labels": None}}
LABELS_CONF = OrderedDict({'label1': 'value 1', 'label2': 'long value'})
LABELS_CONF_ONE = {'label2': 'long value'}
LABELS_CONF_WRONG = [('label1', 'value1'), ('label2', 'value2')]
LABELS_CONF_EXPLICIT = {"version": "x", "release": "1"}
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
"""]
# Label order seems to be set randomly, so both possible options are added
EXPECTED_OUTPUT9 = [r"""FROM fedora
RUN yum install -y python-django
CMD blabla
LABEL "release"="1" "version"="x"
""", r"""FROM fedora
RUN yum install -y python-django
CMD blabla
LABEL "version"="x" "release"="1"
"""]


def mock_env(
    workflow,
    *,
    df_content: str,
    base_inspect: ImageInspectionData,
    # (dict[str, str] | str | None), but some tests intentionally pass the wrong type
    labels_plugin_arg: Optional[Any] = None,
    labels_reactor_conf: Optional[Dict[str, str]] = None,
    # list[list[str]], but same as above
    eq_conf: Optional[Any] = None,
    dont_overwrite: Optional[List[str]] = None,
    dont_overwrite_if_in_dockerfile: Optional[List[str]] = None,
    aliases: Optional[Dict[str, str]] = None,
    auto_labels: Optional[List[str]] = None,
) -> MockEnv:

    env = MockEnv(workflow).for_plugin("prebuild", AddLabelsPlugin.key)

    args = {
        'labels': labels_plugin_arg,
        'dont_overwrite': dont_overwrite,
        'auto_labels': auto_labels,
        'aliases': aliases,
        'equal_labels': eq_conf,
    }
    if dont_overwrite_if_in_dockerfile is not None:
        args['dont_overwrite_if_in_dockerfile'] = dont_overwrite_if_in_dockerfile

    env.set_plugin_args(args)

    df_path = Path(workflow.source.path) / "Dockerfile"
    df_path.write_text(df_content)

    workflow.build_dir.init_build_dirs(["aarch64", "x86_64"], workflow.source)

    env.set_dockerfile_images(workflow.build_dir.any_platform.dockerfile.parent_images)

    flexmock(workflow.imageutil).should_receive('base_image_inspect').and_return(base_inspect)
    flexmock(workflow.source).should_receive('get_vcs_info').and_return(
        VcsInfo(vcs_type="git", vcs_url=DOCKERFILE_GIT, vcs_ref=DOCKERFILE_SHA1)
    )

    if labels_reactor_conf is not None:
        env.reactor_config.conf["image_labels"] = deepcopy(labels_reactor_conf)
    if eq_conf is not None:
        env.reactor_config.conf["image_equal_labels"] = eq_conf

    return env


@pytest.mark.parametrize(
    'df_content, labels_conf_base, labels_conf, eq_conf, dont_overwrite,aliases, expected_output',
    [
        (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, [], [], {}, EXPECTED_OUTPUT),
        (DF_CONTENT, LABELS_CONF_BASE, json.dumps(LABELS_CONF), [], [], {}, EXPECTED_OUTPUT),
        (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF_WRONG, [], [], {}, RuntimeError()),
        (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, {'key': 'val'}, [], {}, RuntimeError()),
        (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, [], ["label1", ], {}, EXPECTED_OUTPUT2),
        (DF_CONTENT, LABELS_CONF_BASE, LABELS_BLANK, [], ["label1", ], {}, EXPECTED_OUTPUT3),
        (DF_CONTENT_SINGLE_LINE, LABELS_CONF_BASE, LABELS_CONF, [], ["label1", ], {},
         EXPECTED_OUTPUT4),
        (DF_CONTENT, LABELS_CONF_BASE, LABELS_CONF, [], [], {"not": "present"}, EXPECTED_OUTPUT),
        (DF_CONTENT_SINGLE_LINE, LABELS_CONF_BASE, LABELS_BLANK, [], [], {"label1": "labelnew"},
         EXPECTED_OUTPUT5),
        (DF_CONTENT_SINGLE_LINE, LABELS_CONF_BASE, LABELS_CONF_ONE, [], [], {"label2": "labelnew"},
         EXPECTED_OUTPUT6),
        (DF_CONTENT_LABEL, LABELS_CONF_BASE, LABELS_BLANK, [], [], {"label2": "labelnew"},
         EXPECTED_OUTPUT7),
        (DF_CONTENT_LABEL, LABELS_CONF_BASE, LABELS_BLANK, [], [], {"label2": "labelnew", "x": "y"},
         EXPECTED_OUTPUT7),
        (DF_CONTENT_LABEL, LABELS_CONF_BASE_NONE, LABELS_BLANK, [], [], {"label2": "labelnew"},
         EXPECTED_OUTPUT7),
        (DF_CONTENT_LABEL, LABELS_CONF_BASE, LABELS_BLANK, [], [], {"label2": "label1"},
         EXPECTED_OUTPUT8),
        (DF_CONTENT, LABELS_CONF_BASE_EXPLICIT, LABELS_CONF_EXPLICIT, [], [], {}, EXPECTED_OUTPUT9),
    ],
)
def test_add_labels_plugin(workflow, df_content, labels_conf_base, labels_conf, eq_conf,
                           dont_overwrite, aliases, expected_output, caplog):
    runner = mock_env(
        workflow,
        df_content=df_content,
        base_inspect=labels_conf_base,
        labels_plugin_arg=labels_conf,
        eq_conf=eq_conf,
        dont_overwrite=dont_overwrite,
        aliases=aliases,
    ).create_runner()
    df_path = workflow.build_dir.any_platform.dockerfile_path

    if isinstance(expected_output, RuntimeError):
        with pytest.raises(PluginFailedException):
            runner.run()
        assert "plugin 'add_labels_in_dockerfile' raised an exception: RuntimeError" \
            in caplog.text

    else:
        runner.run()
        assert AddLabelsPlugin.key is not None
        assert df_path.read_text() in expected_output


@pytest.mark.parametrize('use_reactor', [True, False])  # noqa
@pytest.mark.parametrize('release', [None, 'test'])
def test_add_labels(workflow, release, use_reactor):
    if use_reactor:
        labels_conf = LABELS_CONF
        labels_arg = {'release': release} if release is not None else None
    else:
        labels_conf = None
        labels_arg = LABELS_CONF.copy()
        if release is not None:
            labels_arg.update({'release': release})

    runner = mock_env(
        workflow,
        df_content=DF_CONTENT,
        base_inspect=LABELS_CONF_BASE,
        labels_plugin_arg=labels_arg,
        labels_reactor_conf=labels_conf,
    ).create_runner()

    runner.run()

    df_content = workflow.build_dir.any_platform.dockerfile_path.read_text()

    assert AddLabelsPlugin.key is not None
    assert 'label1' in df_content
    if release:
        assert 'release' in df_content
        assert release in df_content
    else:
        assert 'release' not in df_content


@pytest.mark.parametrize('auto_label, value_re_part', [  # noqa
    ('build-date', r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?'),
    ('architecture', '(x86_64|aarch64)'),
    ('vcs-type', 'git'),
    ('vcs-url', DOCKERFILE_GIT),
    ('vcs-ref', DOCKERFILE_SHA1),
    ('com.redhat.build-host', 'dummy_host'),
    ('wrong_label', None)
])
def test_add_labels_plugin_generated(workflow, auto_label, value_re_part, reactor_config_map):
    runner = mock_env(
        workflow,
        df_content=DF_CONTENT,
        base_inspect=LABELS_CONF_BASE,
        labels_reactor_conf={} if reactor_config_map else None,
        aliases={'Build_Host': 'com.redhat.build-host'},
        auto_labels=[auto_label],
    ).create_runner()

    runner.run()

    def check_labels(build_dir: BuildDir) -> None:
        df = build_dir.dockerfile

        if value_re_part:
            assert re.match(value_re_part, df.labels[auto_label])

        if auto_label == "build-date":
            utc_dt = datetime.datetime.utcfromtimestamp(atomic_reactor_start_time).isoformat()
            assert df.labels[auto_label] == utc_dt
        elif auto_label == "platform":
            assert df.labels[auto_label] == build_dir.platform

    workflow.build_dir.for_each_platform(check_labels)


@pytest.mark.parametrize('df_old_as_plugin_arg', [True, False])  # noqa
@pytest.mark.parametrize('df_new_as_plugin_arg', [True, False])
@pytest.mark.parametrize('base_old, base_new, df_old, df_new, exp_old, exp_new, exp_log', [
    (None,  None,  None,  None,  None,  None,  None),
    (None,  None,  None,  'A',   None,  'A',   None),
    (None,  None,  'A',   None,  'A',   'A',   'as an alias for label'),
    (None,  None,  'A',   'A',   'A',   'A',   'already exists'),
    (None,  None,  'A',   'B',   'B',   'B',   'as an alias for label'),
    (None,  'A',   None,  None,  None,  'A',   None),
    (None,  'A',   None,  'A',   None,  'A',   None),
    (None,  'A',   None,  'B',   None,  'B',   None),
    (None,  'A',   'A',   None,  'A',   'A',   'already exists'),
    (None,  'A',   'B',   None,  'B',   'B',   'as an alias for label'),
    (None,  'A',   'A',   'A',   'A',   'A',   'already exists'),
    (None,  'A',   'A',   'B',   'B',   'B',   'as an alias for label'),
    (None,  'A',   'B',   'A',   'A',   'A',   'as an alias for label'),
    (None,  'A',   'B',   'B',   'B',   'B',   'already exists'),
    (None,  'A',   'B',   'C',   'C',   'C',   'as an alias for label'),
    ('A',   None,  None,  None,  'A',   'A',   'as an alias for label'),
    ('A',   None,  None,  'A',   'A',   'A',   'already exists'),
    ('A',   None,  None,  'B',   'B',   'B',   'as an alias for label'),
    ('A',   None,  'A',   None,  'A',   'A',   'as an alias for label'),
    ('A',   None,  'B',   None,  'B',   'B',   'as an alias for label'),
    ('A',   None,  'A',   'A',   'A',   'A',   'already exists'),
    ('A',   None,  'A',   'B',   'B',   'B',   'as an alias for label'),
    ('A',   None,  'B',   'A',   'A',   'A',   'as an alias for label'),
    ('A',   None,  'B',   'B',   'B',   'B',   'already exists'),
    ('A',   None,  'B',   'C',   'C',   'C',   'as an alias for label'),
    ('A',   'A',   None,  None,  'A',   'A',   'already exists'),
    ('A',   'A',   None,  'A',   'A',   'A',   'already exists'),
    ('A',   'A',   None,  'B',   'B',   'B',   'as an alias for label'),
    ('A',   'A',   'A',   None,  'A',   'A',   'already exists'),
    ('A',   'A',   'B',   None,  'B',   'B',   'as an alias for label'),
    ('A',   'A',   'A',   'A',   'A',   'A',   'already exists'),
    ('A',   'A',   'A',   'B',   'B',   'B',   'as an alias for label'),
    ('A',   'A',   'B',   'A',   'A',   'A',   'as an alias for label'),
    ('A',   'A',   'B',   'B',   'B',   'B',   'already exists'),
    ('A',   'A',   'B',   'C',   'C',   'C',   'as an alias for label'),
    ('A',   'B',   None,  None,  'B',   'B',   'as an alias for label'),
    ('A',   'B',   None,  'A',   'A',   'A',   'already exists'),
    ('A',   'B',   None,  'B',   'B',   'B',   'as an alias for label'),
    ('A',   'B',   None,  'C',   'C',   'C',   'as an alias for label'),
    ('A',   'B',   'A',   None,  'A',   'A',   'as an alias for label'),
    ('A',   'B',   'B',   None,  'B',   'B',   'already exists'),
    ('A',   'B',   'C',   None,  'C',   'C',   'as an alias for label'),
    ('A',   'B',   'A',   'A',   'A',   'A',   'already exists'),
    ('A',   'B',   'A',   'B',   'B',   'B',   'as an alias for label'),
    ('A',   'B',   'A',   'C',   'C',   'C',   'as an alias for label'),
    ('A',   'B',   'B',   'A',   'A',   'A',   'as an alias for label'),
    ('A',   'B',   'B',   'B',   'B',   'B',   'already exists'),
    ('A',   'B',   'B',   'C',   'C',   'C',   'as an alias for label'),
    ('A',   'B',   'C',   'A',   'A',   'A',   'as an alias for label'),
    ('A',   'B',   'C',   'B',   'B',   'B',   'as an alias for label'),
    ('A',   'B',   'C',   'C',   'C',   'C',   'already exists'),
    ('A',   'B',   'C',   'D',   'D',   'D',   'as an alias for label'),
])
def test_add_labels_aliases(workflow, caplog, df_old_as_plugin_arg, df_new_as_plugin_arg,
                            base_old, base_new, df_old, df_new, exp_old, exp_new, exp_log,
                            reactor_config_map):
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

    runner = mock_env(
        workflow,
        df_content=df_content,
        base_inspect=base_labels,
        labels_plugin_arg=plugin_labels,
        labels_reactor_conf=plugin_labels if reactor_config_map else None,
        aliases={"label_old": "label_new"},
    ).create_runner()
    runner.run()

    df = workflow.build_dir.any_platform.dockerfile

    assert AddLabelsPlugin.key is not None
    result_old = df.labels.get("label_old") or \
        base_labels[INSPECT_CONFIG]["Labels"].get("label_old")
    result_new = df.labels.get("label_new") or \
        base_labels[INSPECT_CONFIG]["Labels"].get("label_new")
    assert result_old == exp_old
    assert result_new == exp_new

    if exp_log:
        assert exp_log in caplog.text


@pytest.mark.parametrize('base_l, df_l, expected, expected_log', [  # noqa
    ((None, None), (None, None), (None, None), None),
    ((None, None), (None, 'A'), ('A', 'A'), 'adding equal label'),
    ((None, None), ('A', None), ('A', 'A'), 'adding equal label'),
    (('A',  None), (None, None), ('A', 'A'), 'adding equal label'),
    ((None, 'A'), (None, None), ('A', 'A'), 'adding equal label'),
    (('A', 'B'), (None, None), ('A', 'B'), None),
    ((None, None), ('A', 'B'), ('A', 'B'), None),
    (('A', 'A'), (None, None), ('A', 'A'), None),
    (('A', None), ('A', None), ('A', 'A'), 'adding equal label'),
    ((None, 'A'), (None, 'A'), ('A', 'A'), 'adding equal label'),
    (('A', None), ('B', None), ('B', 'B'), 'adding equal label'),
    ((None, 'A'), (None, 'B'), ('B', 'B'), 'adding equal label'),
    (('A', 'C'), ('B', None), ('B', 'B'), 'adding equal label'),
    (('A', 'C'), (None, 'B'), ('B', 'B'), 'adding equal label'),
    (('A', 'C'), ('B', 'B'), ('B', 'B'), None),
    ((None, 'A'), ('B', 'B'), ('B', 'B'), None),
    (('A', None), ('B', 'B'), ('B', 'B'), None),
    (('A', 'A'), (None, None), ('A', 'A'), None),
    (('A', None), (None, 'A'), ('A', 'A'), 'skipping label'),
    ((None, 'A'), ('A', None), ('A', 'A'), 'skipping label'),
])
def test_add_labels_equal_aliases(workflow, caplog, base_l, df_l, expected, expected_log):
    df_content = "FROM fedora\n"
    if df_l[0]:
        df_content += 'LABEL description="{0}"\n'.format(df_l[0])
    if df_l[1]:
        df_content += 'LABEL io.k8s.description="{0}"\n'.format(df_l[1])

    base_labels = {INSPECT_CONFIG: {"Labels": {}}}
    if base_l[0]:
        base_labels[INSPECT_CONFIG]["Labels"]["description"] = base_l[0]
    if base_l[1]:
        base_labels[INSPECT_CONFIG]["Labels"]["io.k8s.description"] = base_l[1]

    runner = mock_env(
        workflow,
        df_content=df_content,
        base_inspect=base_labels,
        eq_conf=[['description', 'io.k8s.description']],
    ).create_runner()
    runner.run()

    df = workflow.build_dir.any_platform.dockerfile

    assert AddLabelsPlugin.key is not None
    result_fst = df.labels.get("description") or \
        base_labels[INSPECT_CONFIG]["Labels"].get("description")
    result_snd = df.labels.get("io.k8s.description") or \
        base_labels[INSPECT_CONFIG]["Labels"].get("io.k8s.description")
    assert result_fst == expected[0]
    assert result_snd == expected[1]

    if expected_log:
        assert expected_log in caplog.text


@pytest.mark.parametrize('base_l, df_l, expected, expected_log', [  # noqa
    ((None, None, None), (None, None, None), (None, None, None), None),
    ((None, None, None), (None, None, 'A'), ('A', 'A', 'A'), 'adding equal label'),
    (('A', 'B', 'B'), (None, None, None), ('A', 'B', 'B'), None),
    ((None, None, None), ('A', 'B', 'B'), ('A', 'B', 'B'), None),
    (('A', 'A', 'A'), (None, None, None), ('A', 'A', 'A'), None),
    (('A', None, 'A'), ('A', None, 'A'), ('A', 'A', 'A'), 'adding equal label'),
    (('A', None, None), (None, 'A', 'A'), ('A', 'A', 'A'), 'skipping label'),
    ((None, 'A', 'A'), ('A', 'A', None), ('A', 'A', 'A'), 'skipping label'),
    (('A', 'A', 'A'), ('B', 'C', None), ('B', 'C', 'B'), 'adding equal label'),
    (('A', 'A', 'A'), (None, 'C', 'D'), ('C', 'C', 'D'), 'adding equal label'),
    (('A', 'A', 'A'), ('B', None, 'D'), ('B', 'B', 'D'), 'adding equal label'),
])
def test_add_labels_equal_aliases2(workflow, caplog, base_l, df_l, expected, expected_log):
    """
    test with 3 equal labels
    """
    df_content = "FROM fedora\n"
    if df_l[0]:
        df_content += 'LABEL description="{0}"\n'.format(df_l[0])
    if df_l[1]:
        df_content += 'LABEL io.k8s.description="{0}"\n'.format(df_l[1])
    if df_l[2]:
        df_content += 'LABEL description_third="{0}"\n'.format(df_l[2])

    base_labels = {INSPECT_CONFIG: {"Labels": {}}}
    if base_l[0]:
        base_labels[INSPECT_CONFIG]["Labels"]["description"] = base_l[0]
    if base_l[1]:
        base_labels[INSPECT_CONFIG]["Labels"]["io.k8s.description"] = base_l[1]
    if base_l[2]:
        base_labels[INSPECT_CONFIG]["Labels"]["description_third"] = base_l[2]

    runner = mock_env(
        workflow,
        df_content=df_content,
        base_inspect=base_labels,
        eq_conf=[['description', 'io.k8s.description', 'description_third']],
    ).create_runner()

    if isinstance(expected_log, RuntimeError):
        with pytest.raises(PluginFailedException):
            runner.run()
    else:
        runner.run()

        df = workflow.build_dir.any_platform.dockerfile

        assert AddLabelsPlugin.key is not None
        result_fst = df.labels.get("description") or \
            base_labels[INSPECT_CONFIG]["Labels"].get("description")
        result_snd = df.labels.get("io.k8s.description") or \
            base_labels[INSPECT_CONFIG]["Labels"].get("io.k8s.description")
        result_trd = df.labels.get("description_third") or \
            base_labels[INSPECT_CONFIG]["Labels"].get("description_third")
        assert result_fst == expected[0]
        assert result_snd == expected[1]
        assert result_trd == expected[2]

        if expected_log:
            assert expected_log in caplog.text


@pytest.mark.parametrize("label_names", [  # noqa
    ("distribution-scope", ),
    ("com.redhat.license_terms", ),
    ("distribution-scope", "com.redhat.license_terms"),
])
@pytest.mark.parametrize("dont_overwrite", [True, False])
@pytest.mark.parametrize("parent_val, docker_val, result_val", [
    (None, None, "default_value"),
    ("parent_value", "docker_value", "docker_value"),
    ("parent_value", None, "default_value"),
    (None, "docker_value", "docker_value"),
    ("parent_value", "parent_value", "parent_value"),
])
def test_dont_overwrite_if_in_dockerfile(workflow, label_names, dont_overwrite,
                                         parent_val, docker_val, result_val, reactor_config_map):
    default_value = 'default_value'
    df_content = "FROM fedora\n"
    if docker_val:
        for label_name in label_names:
            df_content += 'LABEL {0}="{1}"\n'.format(label_name, docker_val)

    if parent_val:
        labels_conf_base = {INSPECT_CONFIG: {"Labels": {}}}

        for label_name in label_names:
            labels_conf_base[INSPECT_CONFIG]["Labels"][label_name] = parent_val
    else:
        labels_conf_base = {INSPECT_CONFIG: {"Labels": {}}}

    image_labels = {}
    for label_name in label_names:
        image_labels[label_name] = default_value

    runner = mock_env(
        workflow,
        df_content=df_content,
        base_inspect=labels_conf_base,
        labels_plugin_arg=image_labels,
        labels_reactor_conf=image_labels if reactor_config_map else None,
        dont_overwrite_if_in_dockerfile=label_names if dont_overwrite else None,
    ).create_runner()
    runner.run()

    df = workflow.build_dir.any_platform.dockerfile

    for label_name in label_names:
        result = df.labels.get(label_name)
        assert result == result_val


@pytest.mark.parametrize('url_format, info_url', [  # noqa
    ('url_pre {label1} {label2} url_post', 'url_pre label1_value label2_value url_post'),
    ('url_pre url_post', 'url_pre url_post'),
    ('url_pre {label1} {label2} {label3_non_existent} url_post', None),
    ('url_pre {label1} {label2} {version} url_post', 'url_pre label1_value label2_value '
     'version_value url_post'),
    ('url_pre {authoritative-source-url} {com.redhat.component} {com.redhat.build-host} url_post',
     'url_pre authoritative-source-url_value com.redhat.component_value '
     'com.redhat.build-host_value url_post'),
])
def test_url_label(workflow, url_format, info_url):
    base_labels = {INSPECT_CONFIG: {"Labels": {}}}

    env = mock_env(
        workflow,
        df_content=DF_CONTENT_LABELS,
        base_inspect=base_labels
    )
    env.reactor_config.conf['image_label_info_url_format'] = url_format

    runner = env.create_runner()

    df = workflow.build_dir.any_platform.dockerfile

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
])
@pytest.mark.parametrize('labels_docker', [
    DF_CONTENT,
    DF_CONTENT_WITH_LABELS,
])
@pytest.mark.parametrize('labels_base', [
    LABELS_CONF_BASE_NONE,
    LABELS_CONF_WITH_LABELS,
])
def test_add_labels_plugin_explicit(workflow, auto_label, labels_docker, labels_base,
                                    reactor_config_map):
    prov_labels = {auto_label: 'explicit_value'}

    runner = mock_env(
        workflow,
        df_content=labels_docker,
        base_inspect=labels_base,
        labels_plugin_arg=prov_labels,
        labels_reactor_conf=prov_labels if reactor_config_map else None,
        auto_labels=[auto_label],
        aliases={'Build_Host': 'com.redhat.build-host'},
    ).create_runner()
    runner.run()

    df = workflow.build_dir.any_platform.dockerfile

    assert df.labels[auto_label] != 'explicit_value'


@pytest.mark.parametrize('parent, should_fail', [  # noqa
    ('koji/image-build', False),
    ('scratch', False),
    ('fedora', True),
])
def test_add_labels_base_image(workflow, parent, should_fail, caplog, reactor_config_map):
    # When a 'release' label is provided by parameter and used to
    # configure the plugin, it should be set in the Dockerfile even
    # when processing base images.
    prov_labels = {'release': '5'}

    runner = mock_env(
        workflow,
        df_content=f"FROM {parent}\n",
        base_inspect={},
        labels_plugin_arg=prov_labels,
        labels_reactor_conf=prov_labels if reactor_config_map else None,
        aliases={'Build_Host': 'com.redhat.build-host'},
    ).create_runner()

    df = workflow.build_dir.any_platform.dockerfile

    if should_fail:
        with caplog.at_level(logging.ERROR):
            with pytest.raises(PluginFailedException):
                runner.run()

        msg = "base image was not inspected"
        assert msg in [x.message for x in caplog.records]
    else:
        runner.run()
        assert df.labels['release'] == '5'


@pytest.mark.parametrize('base_new, df_new, plugin_new, expected_in_df, expected_log', [  # noqa
    (None,  None,  None,  None,  None),
    (None,  'A',   'A',   'A',   None),
    (None,  'B',   'A',   'A',   'setting label'),
    (None,  'A',   'B',   'B',   None),
    (None,  None,  'A',   'A',   'setting label'),
    (None,  'A',   None,  'A',   None),
    ('A',   None,  'A',   'A',   'setting label'),
    ('B',   None,  'A',   'A',   'setting label'),
    ('A',   None,  'B',   'B',   'setting label'),
    ('A',   'A',   None,  'A',   None),
    ('A',   'B',   None,  'B',   None),
    ('B',   'A',   None,  'A',   None),
    ('A',   'A',   'A',   'A',   None),
    ('A',   'B',   'A',   'A',   'setting label'),
    ('B',   'A',   'A',   'A',   None),
    ('A',   'A',   'B',   'B',   'setting label'),
    ('A',   'B',   'B',   'B',   None),
    ('B',   'B',   'A',   'A',   'setting label'),
    ('B',   'A',   'B',   'B',   'setting label'),
    ('A',   'B',   'C',   'C',   'setting label'),
])
@pytest.mark.parametrize('release_env', ['TEST_RELEASE_VAR', None])
def test_release_label(workflow, caplog, base_new, df_new, plugin_new,
                       expected_in_df, expected_log, release_env, reactor_config_map):
    df_content = "FROM fedora\n"
    plugin_labels = {}

    if df_new:
        df_content += 'LABEL release="{0}"\n'.format(df_new)

    if plugin_new:
        plugin_labels["release"] = plugin_new

    base_labels = {INSPECT_CONFIG: {"Labels": {}}}
    if base_new:
        base_labels[INSPECT_CONFIG]["Labels"]["release"] = base_new

    env = mock_env(
        workflow,
        df_content=df_content,
        base_inspect=base_labels,
        labels_plugin_arg=plugin_labels,
        labels_reactor_conf=plugin_labels if reactor_config_map else None,
    )
    env.workflow.source.config.release_env_var = release_env
    runner = env.create_runner()

    runner.run()

    df = workflow.build_dir.any_platform.dockerfile

    assert AddLabelsPlugin.key is not None
    result_new = df.labels.get("release")
    assert result_new == expected_in_df

    if release_env and expected_in_df:
        expected = "ENV {}={}\n".format(release_env, expected_in_df)
        assert expected in df.lines

    if expected_log:
        assert expected_log in caplog.text


def test_labels_from_user_params(workflow):
    workflow.user_params["release"] = "42"

    runner = PreBuildPluginsRunner(workflow, [])
    plugin = runner.create_instance_from_plugin(AddLabelsPlugin, {})

    assert plugin.labels == {"release": "42"}
