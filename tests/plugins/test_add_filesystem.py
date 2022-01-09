"""
Copyright (c) 2016-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import functools
from pathlib import Path
from textwrap import dedent

from atomic_reactor.dirs import BuildDir
from flexmock import flexmock

import koji
import pytest
import responses
import logging

from atomic_reactor.plugin import (
    PreBuildPluginsRunner, PluginFailedException, BuildCanceledException)
from atomic_reactor.plugins.pre_add_filesystem import AddFilesystemPlugin
from atomic_reactor.util import df_parser, DockerfileImages
from atomic_reactor.source import VcsInfo
from atomic_reactor.constants import (DOCKERFILE_FILENAME, PLUGIN_ADD_FILESYSTEM_KEY,
                                      PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
                                      PLUGIN_RESOLVE_COMPOSES_KEY)
import atomic_reactor.utils.koji as koji_util
from tests.constants import (DOCKERFILE_GIT, DOCKERFILE_SHA1, MOCK)
if MOCK:
    from tests.retry_mock import mock_get_retry_session

KOJI_HUB = 'https://koji-hub.com'
KOJI_TARGET = 'guest-fedora-23-docker'
FILESYSTEM_TASK_ID = 1234567

DEFAULT_DOCKERFILE = dedent("""\
    FROM koji/image-build
    RUN dnf install -y python-django
    """)

DOCKERFILE_WITH_LABELS = dedent("""\
    FROM koji/image-build
    RUN dnf install -y python-django
    LABEL "com.redhat.component"="testproject" \\
          "name"="testproject_baseimage" \\
          "version"="8.0"
    """)

pytestmark = pytest.mark.usefixtures('user_params')


class MockSource(object):
    def __init__(self, build_dir: Path):
        self.dockerfile_path = str(build_dir / DOCKERFILE_FILENAME)
        self.path = str(build_dir)

    def get_build_file_path(self):
        return self.dockerfile_path, self.path

    def get_vcs_info(self):
        return VcsInfo('git', DOCKERFILE_GIT, DOCKERFILE_SHA1)


class X(object):
    def __init__(self, parent_images):
        self.image_id = "xxx"
        self.dockerfile_images = DockerfileImages(parent_images)


def mock_koji_session(scratch=False, image_task_fail=False,
                      throws_build_cancelled=False,
                      error_on_build_cancelled=False,
                      get_task_result_mock=None,
                      arches=None):

    session = flexmock()

    def _mockBuildImageOz(*args, **kwargs):
        if scratch:
            assert kwargs['opts']['scratch'] is True
        else:
            assert 'scratch' not in kwargs['opts']

        if arches:
            assert set(args[2]) == set(arches)

        return FILESYSTEM_TASK_ID

    session.should_receive('buildImageOz').replace_with(_mockBuildImageOz)

    session.should_receive('taskFinished').and_return(True)
    if image_task_fail:
        session.should_receive('getTaskInfo').and_return({
            'state': koji_util.koji.TASK_STATES['FAILED']
        })
    else:
        session.should_receive('getTaskInfo').and_return({
            'state': koji_util.koji.TASK_STATES['CLOSED']
        })

    if get_task_result_mock:
        (session.should_receive('getTaskResult')
            .replace_with(get_task_result_mock).once())

    session.should_receive('listTaskOutput').and_return([
        'fedora-23-1.0.x86_64.tar.gz',
    ])
    session.should_receive('getTaskChildren').and_return([
        {'id': 1234568},
    ])
    contents = 'tarball-contents'
    expectation = session.should_receive('downloadTaskOutput')
    for chunk in contents:
        expectation = expectation.and_return(chunk)
    # Empty content to simulate end of stream.
    expectation.and_return('')
    session.should_receive('krb_login').and_return(True)

    if throws_build_cancelled:
        task_watcher = flexmock(koji_util.TaskWatcher)

        task_watcher.should_receive('wait').and_raise(BuildCanceledException)
        task_watcher.should_receive('failed').and_return(True)

        cancel_mock_chain = session.should_receive('cancelTask').\
            with_args(FILESYSTEM_TASK_ID).once()

        if error_on_build_cancelled:
            cancel_mock_chain.and_raise(Exception("foo"))

    (flexmock(koji)
        .should_receive('ClientSession')
        .once()
        .and_return(session))


def mock_image_build_file(workflow, contents=None):
    def write_image_build_file(file_contents, build_dir: BuildDir):
        path = build_dir.path / 'image-build.conf'
        with open(path, 'w') as f:
            f.write(dedent(file_contents))
            f.flush()
        return [path]

    if contents is None:
        contents = dedent("""\
            [image-build]
            name = fedora-23
            version = 1.0
            install_tree = http://install-tree.com/$arch/fedora23/

            format = docker
            distro = Fedora-23
            repo = http://repo.com/fedora/$arch/os/

            ksurl = git+http://ksrul.com/git/spin-kickstarts.git?fedora23#b232f73e
            ksversion = FEDORA23
            kickstart = fedora-23.ks

            [factory-parameters]
            create_docker_metadata = False

            [ova-options]
            ova_option_1 = ova_option_1_value
            """)

    write_image_build_file_call = functools.partial(write_image_build_file, contents)
    return workflow.build_dir.for_all_platforms_copy(write_image_build_file_call)


def mock_workflow(workflow, build_dir: Path, dockerfile=DEFAULT_DOCKERFILE,
                  platforms=None, scratch=False):
    workflow.user_params['scratch'] = scratch
    workflow.source = MockSource(build_dir)
    if not platforms:
        platforms = ['x86_64']
    workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(platforms)
    with open(workflow.source.dockerfile_path, 'w') as f:
        f.write(dockerfile)
    workflow.build_dir.init_build_dirs(platforms, workflow.source)
    df = df_parser(str(build_dir))
    workflow.dockerfile_images = DockerfileImages(df.parent_images)
    mock_get_retry_session()


def create_plugin_instance(workflow, build_dir: Path, kwargs=None, scratch=False,  # noqa
                           platforms=None, koji_target=KOJI_TARGET,
                           dockerfile=DEFAULT_DOCKERFILE):
    mock_workflow(workflow, build_dir, dockerfile=dockerfile, scratch=scratch,
                  platforms=platforms)

    if kwargs is None:
        kwargs = {}

    make_and_store_reactor_config_map(workflow, {'root_url': kwargs.get('url', '')})

    return AddFilesystemPlugin(workflow, koji_target=koji_target, **kwargs)


def make_and_store_reactor_config_map(workflow, additional_koji=None):
    reactor_map = {
        'version': 1,
        'koji': {'hub_url': KOJI_HUB}
    }
    if additional_koji:
        reactor_map['koji'].update(additional_koji)

    workflow.conf.conf = reactor_map


@pytest.mark.parametrize('scratch', [True, False])
@pytest.mark.parametrize(('dockerfile', 'expected_dockerfile'), [
    # single-stage dockerfile with a custom image
    (dedent('''\
     FROM koji/image-build
     RUN dnf install -y python-django
     '''),
     dedent('''\
     FROM scratch
     ADD {} {}
     RUN dnf install -y python-django
     ''')
     ),
    # single-stage dockerfile with a custom image and alias
    (dedent('''\
     FROM koji/image-build AS imported_image
     RUN dnf install -y python-django
     '''),
     dedent('''\
     FROM scratch AS imported_image
     ADD {} {}
     RUN dnf install -y python-django
     ''')
     ),
    # multistage dockerfile with a custom image
    (dedent('''\
     FROM koji/image-build
     RUN dnf install -y python-django
     FROM test
     RUN dnf install -b uwlg
     '''),
     dedent('''\
     FROM scratch
     ADD {} {}
     RUN dnf install -y python-django
     FROM test
     RUN dnf install -b uwlg
     ''')
     ),
    # multistage dockerfile with multiple custom images
    (dedent('''
     FROM koji/image-build
     RUN dnf install -y python-django
     FROM koji/image-build
     RUN dnf install -y uwlg
     '''),
     dedent('''
     FROM scratch
     ADD {} {}
     RUN dnf install -y python-django
     FROM scratch
     ADD {} {}
     RUN dnf install -y uwlg
     ''')
     )
])
def test_add_filesystem_plugin_generated(workflow, build_dir, scratch, dockerfile,
                                         expected_dockerfile, caplog):
    mock_workflow(workflow, build_dir, scratch=scratch, dockerfile=dockerfile)
    mock_koji_session(scratch=scratch)
    mock_image_build_file(workflow)
    image_name = 'fedora-23-1.0.x86_64.tar.gz'
    expected_dockerfile = expected_dockerfile.replace('ADD {} {}\n',
                                                      f'ADD {image_name} '
                                                      '/\n')

    make_and_store_reactor_config_map(workflow, {'root_url': '', 'auth': {}})

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': PLUGIN_ADD_FILESYSTEM_KEY,
            'args': {}
        }]
    )

    expected_results = {
        'filesystem-koji-task-id': FILESYSTEM_TASK_ID,
    }

    results = runner.run()
    plugin_result = results[PLUGIN_ADD_FILESYSTEM_KEY]
    assert 'filesystem-koji-task-id' in plugin_result
    assert plugin_result == expected_results
    assert workflow.labels['filesystem-koji-task-id'] == FILESYSTEM_TASK_ID
    msg = f'added "{image_name}" as image filesystem'
    assert msg in caplog.text
    assert workflow.build_dir.any_platform.dockerfile.content == expected_dockerfile


@pytest.mark.parametrize('scratch', [True, False])
def test_add_filesystem_plugin_legacy(workflow, build_dir, scratch, caplog):
    mock_workflow(workflow, build_dir, scratch=scratch)
    workflow.prebuild_results[PLUGIN_RESOLVE_COMPOSES_KEY] = {'composes': []}
    mock_koji_session(scratch=scratch)
    mock_image_build_file(workflow)
    image_name = 'fedora-23-1.0.x86_64.tar.gz'
    expected_dockerfile_content = dedent(f"""\
        FROM scratch
        ADD {image_name} /
        RUN dnf install -y python-django
    """)

    make_and_store_reactor_config_map(workflow, {'root_url': '', 'auth': {}})

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': PLUGIN_ADD_FILESYSTEM_KEY,
            'args': {}
        }]
    )

    results = runner.run()
    plugin_result = results[PLUGIN_ADD_FILESYSTEM_KEY]
    assert 'filesystem-koji-task-id' in plugin_result
    assert workflow.labels['filesystem-koji-task-id'] == FILESYSTEM_TASK_ID
    msg = f'added "{image_name}" as image filesystem'
    assert msg in caplog.text
    assert workflow.build_dir.any_platform.dockerfile.content == expected_dockerfile_content


@pytest.mark.parametrize(('base_image', 'type_match'), [
    ('koji/image-build', True),
    ('KoJi/ImAgE-bUiLd  \n', True),
    ('spam/bacon', False),
    ('SpAm/BaCon  \n', False),
])
def test_base_image_type(workflow, build_dir, base_image, type_match):
    plugin = create_plugin_instance(workflow, build_dir)
    assert plugin.is_image_build_type(base_image) == type_match


def test_image_build_file_parse(workflow, build_dir):  # noqa
    plugin = create_plugin_instance(workflow, build_dir)
    file_paths = mock_image_build_file(workflow)
    image_name, config, opts = plugin.parse_image_build_config(file_paths[0])
    assert image_name == 'fedora-23'
    assert config == [
        'fedora-23',
        '1.0',
        ['x86_64'],
        'guest-fedora-23-docker',
        'http://install-tree.com/$arch/fedora23/'
    ]
    assert opts['opts'] == {
        'disk_size': 10,
        'distro': 'Fedora-23',
        'factory_parameter': [('create_docker_metadata', 'False')],
        'ova_option': ['ova_option_1=ova_option_1_value'],
        'format': ['docker'],
        'kickstart': 'fedora-23.ks',
        'ksurl': 'git+http://ksrul.com/git/spin-kickstarts.git?fedora23#b232f73e',
        'ksversion': 'FEDORA23',
        'repo': ['http://repo.com/fedora/$arch/os/'],
    }


def test_missing_yum_repourls(workflow, build_dir):  # noqa
    plugin = create_plugin_instance(workflow, build_dir, {'repos': None})
    image_build_conf = dedent("""\
        [image-build]
        version = 1.0

        distro = Fedora-23

        ksversion = FEDORA23
        """)

    file_paths = mock_image_build_file(workflow, contents=image_build_conf)
    with pytest.raises(ValueError) as exc:
        plugin.parse_image_build_config(file_paths[0])
    assert 'install_tree cannot be empty' in str(exc.value)


@pytest.mark.parametrize(('build_cancel', 'error_during_cancel'), [
    (True, False),
    (True, True),
    (False, False),
])
@pytest.mark.parametrize('raise_error', [True, False])
def test_image_task_failure(workflow, build_dir, build_cancel, error_during_cancel,
                            raise_error, caplog):
    task_result = 'task-result'

    def _mockGetTaskResult(task_id):
        if raise_error:
            raise RuntimeError(task_result)
        return task_result
    mock_workflow(workflow, build_dir)
    workflow.prebuild_results[PLUGIN_RESOLVE_COMPOSES_KEY] = {'composes': []}
    mock_koji_session(image_task_fail=True,
                      throws_build_cancelled=build_cancel,
                      error_on_build_cancelled=error_during_cancel,
                      get_task_result_mock=_mockGetTaskResult)
    mock_image_build_file(workflow)

    make_and_store_reactor_config_map(workflow, {'root_url': '', 'auth': {}})

    workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = ['x86_64']

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': PLUGIN_ADD_FILESYSTEM_KEY,
            'args': {}
        }]
    )

    with caplog.at_level(logging.INFO,
                         logger='atomic_reactor'), pytest.raises(PluginFailedException) as exc:
        runner.run()

    assert task_result in str(exc.value)
    # Also ensure getTaskResult exception message is wrapped properly
    assert 'image task,' in str(exc.value)

    if build_cancel:
        msg = "Build was canceled, canceling task %s" % FILESYSTEM_TASK_ID
        assert msg in [x.message for x in caplog.records]

        if error_during_cancel:
            # We're checking last but one message, as the last one is
            # 'plugin 'add_filesystem' raised an exception'
            assert "Exception while canceling a task (ignored): Exception: " \
                   in caplog.records[-2].message
        else:
            msg = "task %s canceled" % FILESYSTEM_TASK_ID
            assert msg in [x.message for x in caplog.records]


@pytest.mark.parametrize(('resolve_compose', 'yum_repos'), [
    ({'composes': []}, []),
    ({'composes': [{'result_repofile': 'http://odcs-compose.com/compose1.repo'}]},
     ['http://odcs-compose.com/$arch/compose1.repo']),
    ({'composes': [{'result_repofile': 'http://odcs-compose.com/compose1.repo'},
                   {'result_repofile': 'http://odcs-compose.com/compose2.repo'}]},
     ['http://odcs-compose.com/$arch/compose1.repo',
      'http://odcs-compose.com/$arch/compose2.repo']),
])
@responses.activate
def test_image_build_defaults(workflow, build_dir, resolve_compose, yum_repos):
    repos = [
        'http://install-tree.com/fedora23.repo',
        'http://repo.com/fedora/os',
    ]
    responses.add(responses.GET, 'http://install-tree.com/fedora23.repo',
                  body=dedent("""\
                    [fedora-23]
                    baseurl = http://install-tree.com/$basearch/fedora23
                    """))
    responses.add(responses.GET, 'http://repo.com/fedora/os',
                  body=dedent("""\
                    [fedora-os]
                    baseurl = http://repo.com/fedora/$basearch/os

                    [fedora-os2]
                    baseurl = http://repo.com/fedora/$basearch/os2
                    """))
    responses.add(responses.GET, 'http://odcs-compose.com/compose1.repo',
                  body=dedent("""\
                    [compose1]
                    baseurl = http://odcs-compose.com/$basearch/compose1.repo
                    """))
    responses.add(responses.GET, 'http://odcs-compose.com/compose2.repo',
                  body=dedent("""\
                    [compose2]
                    baseurl = http://odcs-compose.com/$basearch/compose2.repo
                    """))
    plugin = create_plugin_instance(workflow, build_dir, {'repos': repos})
    plugin.workflow.prebuild_results[PLUGIN_RESOLVE_COMPOSES_KEY] = resolve_compose

    image_build_conf = dedent("""\
        [image-build]
        version = 1.0

        distro = Fedora-23

        ksversion = FEDORA23
        """)

    file_paths = mock_image_build_file(workflow, contents=image_build_conf)
    plugin.update_repos_from_composes()
    image_name, config, opts = plugin.parse_image_build_config(file_paths[0])
    assert image_name == 'default-name'
    assert config == [
        'default-name',
        '1.0',
        ['x86_64'],
        'guest-fedora-23-docker',
        'http://install-tree.com/$arch/fedora23',
    ]

    all_repos = ['http://install-tree.com/$arch/fedora23',
                 'http://repo.com/fedora/$arch/os',
                 'http://repo.com/fedora/$arch/os2']
    if resolve_compose:
        all_repos.extend(yum_repos)

    assert opts['opts'] == {
        'disk_size': 10,
        'distro': 'Fedora-23',
        'factory_parameter': [('create_docker_metadata', 'False')],
        'format': ['docker'],
        'kickstart': 'kickstart.ks',
        'ksurl': '{}#{}'.format(DOCKERFILE_GIT, DOCKERFILE_SHA1),
        'ksversion': 'FEDORA23',
        'repo': all_repos,
    }


def test_image_build_dockerfile_defaults(workflow, build_dir):
    """Test if default name and version are taken from the Dockerfile"""
    image_build_conf = dedent("""\
        [image-build]

        # name and version intentionally omitted for purpose of this test

        install_tree = http://install-tree.com/$arch/fedora23/

        format = docker
        distro = Fedora-23
        repo = http://repo.com/fedora/$arch/os/

        ksurl = git+http://ksrul.com/git/spin-kickstarts.git?fedora23#b232f73e
        ksversion = FEDORA23
        kickstart = fedora-23.ks
        """)
    plugin = create_plugin_instance(workflow, build_dir, dockerfile=DOCKERFILE_WITH_LABELS)
    file_paths = mock_image_build_file(workflow, contents=image_build_conf)
    image_name, config, _ = plugin.parse_image_build_config(file_paths[0])
    assert image_name == 'testproject'  # from Dockerfile
    assert config == [
        'testproject',  # from Dockerfile
        '8.0',  # from Dockerfile
        ['x86_64'],
        'guest-fedora-23-docker',
        'http://install-tree.com/$arch/fedora23/'
    ]


@pytest.mark.parametrize('platforms', [
    None,
    ['x86_64', 'aarch64', 'ppc64le'],
])
@responses.activate
def test_image_build_overwrites(workflow, build_dir, platforms):
    repos = [
        'http://default-install-tree.com/fedora23',
        'http://default-repo.com/fedora/os.repo',
    ]
    if not platforms:
        platforms = ['i386', 'i486']
    responses.add(responses.GET, 'http://default-install-tree.com/fedora23',
                  body=dedent("""\
                    [fedora-23]
                    baseurl = http://default-install-tree.com/$basearch/fedora23
                    """))
    responses.add(responses.GET, 'http://default-repo.com/fedora/os.repo',
                  body=dedent("""\
                    [fedora-os]
                    baseurl = http://default-repo.com/fedora/$basearch/os.repo
                    """))
    plugin = create_plugin_instance(workflow, build_dir, {'repos': repos}, platforms=platforms)
    image_build_conf = dedent("""\
        [image-build]
        name = my-name
        version = 1.0
        arches = i386,i486
        target = guest-fedora-23-docker-candidate
        install_tree = http://install-tree.com/$arch/fedora23/
        format = locker,mocker
        disk_size = 20

        distro = Fedora-23
        repo = http://install-tree.com/$arch/fedora23/,http://repo.com/fedora/$arch/os/

        ksurl = http://ksurl#123
        kickstart = my-kickstart.ks
        ksversion = FEDORA23

        [factory-parameters]
        create_docker_metadata = Maybe
        """)

    file_paths = mock_image_build_file(workflow, contents=image_build_conf)
    image_name, config, opts = plugin.parse_image_build_config(file_paths[0])
    assert image_name == 'my-name'
    config_arch = platforms

    # Sort architectures for comparsion
    config[2] = sorted(config[2])
    assert config == [
        'my-name',
        '1.0',
        sorted(config_arch),
        'guest-fedora-23-docker-candidate',
        'http://install-tree.com/$arch/fedora23/',
    ]
    assert opts['opts'] == {
        'disk_size': 20,
        'distro': 'Fedora-23',
        'factory_parameter': [('create_docker_metadata', 'Maybe')],
        'format': ['locker', 'mocker'],
        'kickstart': 'my-kickstart.ks',
        'ksurl': 'http://ksurl#123',
        'ksversion': 'FEDORA23',
        'repo': [
            'http://install-tree.com/$arch/fedora23/',
            'http://repo.com/fedora/$arch/os/',
        ],
    }


@responses.activate
def test_extract_base_url_many_base_urls(workflow, build_dir):  # noqa
    repos = [
        'http://default-install-tree.com/fedora23',
        'http://default-repo.com/fedora/os.repo',
    ]
    platforms = ['x86_64']
    responses.add(responses.GET, 'http://default-install-tree.com/fedora23',
                  body=dedent("""\
                    [fedora-23]
                    baseurl = http://default-install-tree.com/$basearch/fedora23
                    [fedora-os]
                    baseurl = http://default-repo.com/fedora/$basearch/os.repo
                    [fedora-nonsense]
                    notaurl = http://default-repo.com/fedora/$basearch/os.repo
                    """))
    responses.add(responses.GET, 'http://default-repo.com/fedora/os.repo',
                  body=dedent("""\
                    [fedora-os]
                    baseurl = http://default-repo.com/fedora/$basearch/os.repo
                    [fedora-23]
                    baseurl = http://default-install-tree.com/$basearch/fedora23
                    """))
    expected_base_urls = [
        "http://default-install-tree.com/$basearch/fedora23",
        "http://default-repo.com/fedora/$basearch/os.repo"
    ]
    plugin = create_plugin_instance(workflow, build_dir, {'repos': repos}, platforms=platforms)
    for repo_url in repos:
        assert sorted(plugin.extract_base_url(repo_url)) == sorted(expected_base_urls)


@responses.activate
def test_extract_base_url_bad_repo_config(workflow, build_dir):  # noqa
    repos = [
        'http://default-install-tree.com/fedora23',
        'http://default-repo.com/fedora/os.repo',
    ]
    platforms = ['x86_64']
    responses.add(responses.GET, 'http://default-install-tree.com/fedora23',
                  body="This is not right")
    responses.add(responses.GET, 'http://default-repo.com/fedora/os.repo',
                  body="Its not even wrong")
    plugin = create_plugin_instance(workflow, build_dir, {'repos': repos}, platforms=platforms)
    for repo_url in repos:
        assert plugin.extract_base_url(repo_url) == []


def test_build_filesystem_missing_conf(workflow, build_dir):  # noqa
    plugin = create_plugin_instance(workflow, build_dir)
    with pytest.raises(RuntimeError) as exc:
        plugin.build_filesystem('image-build.conf')
    assert 'Image build configuration file not found' in str(exc.value)


@pytest.mark.parametrize(('prefix', 'suffix'), [
    ('fedora-23-spam-', '.tar'),
    ('fedora-23-spam-', '.tar.gz'),
    ('fedora-23-spam-', '.tar.bz2'),
    ('fedora-23-spam-', '.tar.xz'),
])
def test_build_filesystem_from_task_id(workflow, build_dir, prefix, suffix):
    plugin = create_plugin_instance(workflow, build_dir)
    plugin.session = flexmock()
    plugin.session.should_receive('buildImageOz').and_return(FILESYSTEM_TASK_ID)
    mock_image_build_file(workflow)
    task_id, file_name = plugin.build_filesystem('image-build.conf')
    assert task_id == FILESYSTEM_TASK_ID
    assert file_name == 'fedora-23'


@pytest.mark.parametrize(('parents', 'skip_plugin'), [
    (('koji/image-build',), False),
    (('non-custom-image',), True),
    (('scratch', 'non-custom-image'), True),
    (('non-custom-image', 'koji/image-build'), False),
    (('non-custom-image', 'koji/image-build', 'non-custom-image'), False),
    (('non-custom-image', 'koji/image-build:wont_be_used', 'koji/image-build', 'non-custom-image'),
     False),
])
@pytest.mark.parametrize('platforms', [
    (['x86_64']),
    (['x86_64']),
    (['x86_64', 'aarch64']),
    (['x86_64', 'aarch64']),
])
def test_image_download(workflow, build_dir, parents, skip_plugin,
                        platforms, caplog):
    mock_workflow(workflow, build_dir)
    workflow.dockerfile_images = DockerfileImages(parents)
    workflow.prebuild_results[PLUGIN_RESOLVE_COMPOSES_KEY] = {'composes': []}
    if not skip_plugin:
        mock_koji_session()
    mock_image_build_file(workflow)
    image_name = 'fedora-23-1.0.x86_64.tar.gz'
    expected_dockerfile_content = dedent(f"""\
        FROM scratch
        ADD {image_name} /
        RUN dnf install -y python-django
    """)

    if platforms:
        workflow.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = set(platforms)

    make_and_store_reactor_config_map(workflow, {'root_url': '', 'auth': {}})

    runner = PreBuildPluginsRunner(
        workflow,
        [{
            'name': PLUGIN_ADD_FILESYSTEM_KEY,
            'args': {}
        }]
    )

    results = runner.run()
    plugin_result = results[PLUGIN_ADD_FILESYSTEM_KEY]

    if skip_plugin:
        message = 'Nothing to do for non-custom base images'
        assert message in caplog.text
        assert plugin_result is None
        return

    assert 'filesystem-koji-task-id' in plugin_result

    assert plugin_result['filesystem-koji-task-id'] == FILESYSTEM_TASK_ID
    msg = f'added "{image_name}" as image filesystem'
    assert msg in caplog.text
    assert workflow.build_dir.any_platform.dockerfile.content == expected_dockerfile_content


@pytest.mark.parametrize('koji_target', [None, '', 'guest-fedora-23-docker'])
@responses.activate
def test_image_build_overwrites_target(workflow, build_dir, koji_target):
    plugin = create_plugin_instance(workflow, build_dir, koji_target=koji_target)
    image_build_conf = dedent("""\
        [image-build]
        name = my-name
        target = guest-fedora-23-docker-candidate
        version = 1.0
        install_tree = http://install-tree.com/$arch/fedora23/
        """)

    file_paths = mock_image_build_file(workflow, contents=image_build_conf)
    _, config, _ = plugin.parse_image_build_config(file_paths[0])
    assert config == [
        'my-name',
        '1.0',
        ['x86_64'],
        'guest-fedora-23-docker-candidate',
        'http://install-tree.com/$arch/fedora23/'
    ]


def test_no_target_set(workflow, build_dir):
    plugin = create_plugin_instance(workflow, build_dir, koji_target='')
    image_build_conf = dedent("""\
        [image-build]
        name = my-name
        version = 1.0
        install_tree = http://install-tree.com/$arch/fedora23/
        """)

    file_paths = mock_image_build_file(workflow, contents=image_build_conf)
    with pytest.raises(ValueError) as exc:
        plugin.parse_image_build_config(file_paths[0])
    assert 'target cannot be empty' in str(exc.value)
