"""
Copyright (c) 2019-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import base64
import io
import sys
import tarfile
from pathlib import Path
from textwrap import dedent
from typing import Callable, Dict

from flexmock import flexmock
import pytest
import koji
import yaml

from atomic_reactor.dirs import BuildDir
import atomic_reactor.utils.koji as koji_util
from atomic_reactor.utils.cachito import CachitoAPI, CFG_TYPE_B64
from atomic_reactor.constants import (
    CACHITO_ENV_ARG_ALIAS,
    CACHITO_ENV_FILENAME,
    PLUGIN_BUILD_ORCHESTRATE_KEY,
    REMOTE_SOURCE_DIR,
    REMOTE_SOURCE_TARBALL_FILENAME,
    REMOTE_SOURCE_JSON_FILENAME,
)
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_resolve_remote_source import (
    RemoteSource,
    ResolveRemoteSourcePlugin,
)
from atomic_reactor.source import SourceConfig

from tests.stubs import StubSource


KOJI_HUB = 'http://koji.com/hub'
KOJI_TASK_ID = 123
KOJI_TASK_OWNER = 'spam'

CACHITO_URL = 'https://cachito.example.com'
CACHITO_REQUEST_ID = 98765
SECOND_CACHITO_REQUEST_ID = 98766
CACHITO_REQUEST_DOWNLOAD_URL = '{}/api/v1/{}/download'.format(CACHITO_URL, CACHITO_REQUEST_ID)
SECOND_CACHITO_REQUEST_DOWNLOAD_URL = '{}/api/v1/{}/download'.format(CACHITO_URL,
                                                                     SECOND_CACHITO_REQUEST_ID)

CACHITO_REQUEST_CONFIG_URL = '{}/api/v1/requests/{}/configuration-files'.format(
    CACHITO_URL,
    CACHITO_REQUEST_ID
)
SECOND_CACHITO_REQUEST_CONFIG_URL = '{}/api/v1/requests/{}/configuration-files'.format(
    CACHITO_URL,
    SECOND_CACHITO_REQUEST_ID
)
CACHITO_ICM_URL = '{}/api/v1/content-manifest?requests={}'.format(
    CACHITO_URL,
    CACHITO_REQUEST_ID
)
SECOND_CACHITO_ICM_URL = '{}/api/v1/content-manifest?requests={}'.format(
    CACHITO_URL,
    SECOND_CACHITO_REQUEST_ID
)

REMOTE_SOURCE_REPO = 'https://git.example.com/team/repo.git'
REMOTE_SOURCE_REF = 'b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a'
REMOTE_SOURCE_PACKAGES = [
        {
            'name': 'test-package',
            'type': 'npm',
            'version': '0.0.1'
        }
    ]
SECOND_REMOTE_SOURCE_REPO = 'https://git.example.com/other-team/other-repo.git'
SECOND_REMOTE_SOURCE_REF = 'd55c00f45ec3dfee0c766cea3d395d6e21cc2e5c'

CACHITO_SOURCE_REQUEST = {
    'id': CACHITO_REQUEST_ID,
    'repo': REMOTE_SOURCE_REPO,
    'ref': REMOTE_SOURCE_REF,
    'environment_variables': {
        'GO111MODULE': 'on',
        'GOPATH': 'deps/gomod',
        'GOCACHE': 'deps/gomod',
    },
    'flags': ['enable-confeti', 'enable-party-popper'],
    'pkg_managers': ['gomod'],
    'dependencies': [
        {
            'name': 'github.com/op/go-logging',
            'type': 'gomod',
            'version': 'v0.1.1',
        }
    ],
    'packages': [
        {
            'name': 'github.com/spam/bacon/v2',
            'type': 'gomod',
            'version': 'v2.0.3'
        }
    ],
    'configuration_files': CACHITO_REQUEST_CONFIG_URL,
    'content_manifest': CACHITO_ICM_URL,
    'extra_cruft': 'ignored',
}
SECOND_CACHITO_SOURCE_REQUEST = {
    'id': SECOND_CACHITO_REQUEST_ID,
    'repo': SECOND_REMOTE_SOURCE_REPO,
    'ref': SECOND_REMOTE_SOURCE_REF,
    'environment_variables': {
        'PIP_CERT': 'app/package-index-ca.pem',
        'PIP_INDEX_URL': 'http://example-pip-index.url/stuff'
    },
    'flags': [],
    'pkg_managers': ['pip'],
    'dependencies': [
        {
            'name': 'click',
            'type': 'pip',
            'version': '5.0',
        }
    ],
    'packages': [
        {
            'name': 'osbs/cachito-pip-with-deps',
            'type': 'pip',
            'version': '1.0.0'
        }
    ],
    'configuration_files': SECOND_CACHITO_REQUEST_CONFIG_URL,
    'content_manifest': SECOND_CACHITO_ICM_URL,
    'extra_cruft': 'ignored',
}

REMOTE_SOURCE_JSON = {
    'repo': REMOTE_SOURCE_REPO,
    'ref': REMOTE_SOURCE_REF,
    'environment_variables': {
        'GO111MODULE': 'on',
        'GOPATH': 'deps/gomod',
        'GOCACHE': 'deps/gomod',
    },
    'flags': ['enable-confeti', 'enable-party-popper'],
    'pkg_managers': ['gomod'],
    'dependencies': [
        {
            'name': 'github.com/op/go-logging',
            'type': 'gomod',
            'version': 'v0.1.1',
        }
    ],
    'packages': [
        {
            'name': 'github.com/spam/bacon/v2',
            'type': 'gomod',
            'version': 'v2.0.3'
        }
    ],
    'configuration_files': CACHITO_REQUEST_CONFIG_URL,
    'content_manifest': CACHITO_ICM_URL,
}
SECOND_REMOTE_SOURCE_JSON = {
    'repo': SECOND_REMOTE_SOURCE_REPO,
    'ref': SECOND_REMOTE_SOURCE_REF,
    'environment_variables': {
        'PIP_CERT': 'app/package-index-ca.pem',
        'PIP_INDEX_URL': 'http://example-pip-index.url/stuff'
    },
    'flags': [],
    'pkg_managers': ['pip'],
    'dependencies': [
        {
            'name': 'click',
            'type': 'pip',
            'version': '5.0',
        }
    ],
    'packages': [
        {
            'name': 'osbs/cachito-pip-with-deps',
            'type': 'pip',
            'version': '1.0.0'
        }
    ],
    'configuration_files': SECOND_CACHITO_REQUEST_CONFIG_URL,
    'content_manifest': SECOND_CACHITO_ICM_URL,
}

CACHITO_ENV_VARS_JSON = {
    'GO111MODULE': {'kind': 'literal', 'value': 'on'},
    'GOPATH': {'kind': 'path', 'value': 'deps/gomod'},
    'GOCACHE': {'kind': 'path', 'value': 'deps/gomod'},
}

# Assert this with the CACHITO_ENV_VARS_JSON
CACHITO_BUILD_ARGS = {
    'GO111MODULE': 'on',
    'GOPATH': '/remote-source/deps/gomod',
    'GOCACHE': '/remote-source/deps/gomod',
}

SECOND_CACHITO_ENV_VARS_JSON = {
    'PIP_CERT': {'kind': 'path', 'value': 'app/package-index-ca.pem'},
    'PIP_INDEX_URL': {'kind': 'literal', 'value': 'http://example-pip-index.url/stuff'},
}

# The response from CACHITO_REQUEST_CONFIG_URL
CACHITO_CONFIG_FILES = [
    {
        "path": "app/some-config.txt",
        "type": CFG_TYPE_B64,
        "content": base64.b64encode(b"gomod requests don't actually have configs").decode(),
    },
]

# The response from SECOND_CACHITO_REQUEST_CONFIG_URL
SECOND_CACHITO_CONFIG_FILES = [
    {
        "path": "app/package-index-ca.pem",
        "type": CFG_TYPE_B64,
        "content": base64.b64encode(b"-----BEGIN CERTIFICATE-----").decode(),
    },
]


def mock_reactor_config(workflow, data=None):
    if data is None:
        data = dedent("""\
            version: 1
            cachito:
               api_url: {}
               auth:
                   ssl_certs_dir: {}
            koji:
                hub_url: /
                root_url: ''
                auth: {{}}
            """.format(CACHITO_URL, workflow._tmpdir))

    workflow._tmpdir.joinpath('cert').touch()
    config = yaml.safe_load(data)
    workflow.conf.conf = config


def mock_user_params(workflow, user_params):
    if not workflow.user_params:
        workflow.user_params = user_params
    else:
        workflow.user_params.update(user_params)


def mock_repo_config(workflow, data=None):
    if data is None:
        data = dedent("""\
            remote_source:
                repo: {}
                ref: {}
            """.format(REMOTE_SOURCE_REPO, REMOTE_SOURCE_REF))

    workflow._tmpdir.joinpath('container.yaml').write_text(data, "utf-8")

    # The repo config is read when SourceConfig is initialized. Force
    # reloading here to make usage easier.
    workflow.source.config = SourceConfig(str(workflow._tmpdir))


@pytest.fixture
def workflow(workflow, source_dir):
    # Stash the tmpdir in workflow so it can be used later
    workflow._tmpdir = source_dir

    class MockSource(StubSource):

        def __init__(self, workdir):
            super(MockSource, self).__init__()
            self.workdir = workdir
            self.path = workdir

    workflow.source = MockSource(str(source_dir))
    workflow.buildstep_plugins_conf = [{'name': PLUGIN_BUILD_ORCHESTRATE_KEY}]
    workflow.user_params = {'koji_task_id': KOJI_TASK_ID}

    mock_repo_config(workflow)
    mock_reactor_config(workflow)
    mock_koji()

    workflow.build_dir.init_build_dirs(["x86_64", "ppc64le"], workflow.source)

    return workflow


def expected_build_dir(workflow) -> str:
    """The primary build_dir that the plugin is expected to work with."""
    return str(workflow.build_dir.any_platform.path)


def expected_dowload_path(workflow, remote_source_name=None) -> str:
    if remote_source_name:
        filename = f'remote-source-{remote_source_name}.tar.gz'
    else:
        filename = 'remote-source.tar.gz'

    path = Path(expected_build_dir(workflow), filename)
    return str(path)


def mock_cachito_tarball(create_at_path) -> str:
    """Create a mocked tarball for a remote source at the specified path."""
    create_at_path = Path(create_at_path)
    file_content = f"Content of {create_at_path.name}".encode("utf-8")

    readme = tarfile.TarInfo("app/README.txt")
    readme.size = len(file_content)

    with tarfile.open(create_at_path, 'w:gz') as tf:
        tf.addfile(readme, io.BytesIO(file_content))

    return str(create_at_path)


def mock_cachito_api_multiple_remote_sources(workflow, user=KOJI_TASK_OWNER):

    (
        flexmock(CachitoAPI)
        .should_receive("request_sources")
        .with_args(
            repo=REMOTE_SOURCE_REPO,
            ref=REMOTE_SOURCE_REF,
            user=user,
            dependency_replacements=None,
        )
        .and_return({"id": CACHITO_REQUEST_ID})
        .ordered()
    )
    (
        flexmock(CachitoAPI)
        .should_receive("request_sources")
        .with_args(
            repo=SECOND_REMOTE_SOURCE_REPO,
            ref=SECOND_REMOTE_SOURCE_REF,
            user=user,
            dependency_replacements=None,
        )
        .and_return({"id": SECOND_CACHITO_REQUEST_ID})
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("wait_for_request")
        .with_args({"id": CACHITO_REQUEST_ID})
        .and_return(CACHITO_SOURCE_REQUEST)
        .ordered()
    )
    (
        flexmock(CachitoAPI)
        .should_receive("wait_for_request")
        .with_args({"id": SECOND_CACHITO_REQUEST_ID})
        .and_return(SECOND_CACHITO_SOURCE_REQUEST)
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("download_sources")
        .with_args(
            CACHITO_SOURCE_REQUEST,
            dest_dir=expected_build_dir(workflow),
            dest_filename="remote-source-gomod.tar.gz",
        )
        .and_return(mock_cachito_tarball(expected_dowload_path(workflow, "gomod")))
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("get_request_env_vars")
        .with_args(CACHITO_SOURCE_REQUEST["id"])
        .and_return(CACHITO_ENV_VARS_JSON)
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("download_sources")
        .with_args(
            SECOND_CACHITO_SOURCE_REQUEST,
            dest_dir=expected_build_dir(workflow),
            dest_filename="remote-source-pip.tar.gz",
        )
        .and_return(mock_cachito_tarball(expected_dowload_path(workflow, "pip")))
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("get_request_env_vars")
        .with_args(SECOND_CACHITO_SOURCE_REQUEST["id"])
        .and_return(SECOND_CACHITO_ENV_VARS_JSON)
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("get_request_config_files")
        .with_args(CACHITO_SOURCE_REQUEST["id"])
        .and_return(CACHITO_CONFIG_FILES)
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("get_request_config_files")
        .with_args(SECOND_CACHITO_SOURCE_REQUEST["id"])
        .and_return(SECOND_CACHITO_CONFIG_FILES)
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("assemble_download_url")
        .with_args(CACHITO_REQUEST_ID)
        .and_return(CACHITO_REQUEST_DOWNLOAD_URL)
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("assemble_download_url")
        .with_args(SECOND_CACHITO_REQUEST_ID)
        .and_return(SECOND_CACHITO_REQUEST_DOWNLOAD_URL)
        .ordered()
    )


def mock_cachito_api(workflow, user=KOJI_TASK_OWNER, source_request=None,
                     dependency_replacements=None,
                     env_vars_json=None):
    if source_request is None:
        source_request = CACHITO_SOURCE_REQUEST
    (flexmock(CachitoAPI)
        .should_receive('request_sources')
        .with_args(
            repo=REMOTE_SOURCE_REPO,
            ref=REMOTE_SOURCE_REF,
            user=user,
            dependency_replacements=dependency_replacements,
         )
        .and_return({'id': CACHITO_REQUEST_ID}))

    (flexmock(CachitoAPI)
        .should_receive('wait_for_request')
        .with_args({'id': CACHITO_REQUEST_ID})
        .and_return(source_request))

    (flexmock(CachitoAPI)
        .should_receive('download_sources')
        .with_args(source_request, dest_dir=expected_build_dir(workflow),
                   dest_filename=REMOTE_SOURCE_TARBALL_FILENAME)
        .and_return(mock_cachito_tarball(expected_dowload_path(workflow))))

    (flexmock(CachitoAPI)
        .should_receive('assemble_download_url')
        .with_args(CACHITO_REQUEST_ID)
        .and_return(CACHITO_REQUEST_DOWNLOAD_URL))

    (flexmock(CachitoAPI)
        .should_receive('get_request_env_vars')
        .with_args(source_request['id'])
        .and_return(env_vars_json or CACHITO_ENV_VARS_JSON))

    (flexmock(CachitoAPI)
        .should_receive('get_request_config_files')
        .with_args(source_request['id'])
        .and_return(CACHITO_CONFIG_FILES))


def mock_koji(user=KOJI_TASK_OWNER):
    koji_session = flexmock(krb_login=lambda: 'some')
    flexmock(koji, ClientSession=lambda hub, opts: koji_session)
    flexmock(koji_util).should_receive('get_koji_task_owner').and_return({'name': user})


def check_injected_files(expected_files: Dict[str, str]) -> Callable[[BuildDir], None]:
    """Make a callable that checks expected files in a BuildDir."""

    def check_files(build_dir: BuildDir) -> None:
        """Check the presence and content of files in the unpacked_remote_sources directory."""
        unpacked_remote_sources = build_dir.path / ResolveRemoteSourcePlugin.REMOTE_SOURCE

        for path, expected_content in expected_files.items():
            abspath = unpacked_remote_sources / path
            assert abspath.read_text() == expected_content

    return check_files


def setup_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_resolve_remote_source', None)


def teardown_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_resolve_remote_source', None)


def test_source_request_to_json_missing_optional_keys(workflow):
    p = ResolveRemoteSourcePlugin(workflow)

    source_request = {
        "repo": REMOTE_SOURCE_REPO,
        "ref": REMOTE_SOURCE_REF,
        "packages": [],
    }
    # test that missing optional keys are ignored as expected
    assert p.source_request_to_json(source_request) == source_request


@pytest.mark.parametrize('scratch', (True, False))
@pytest.mark.parametrize('dr_strs, dependency_replacements',
                         ((None, None),
                          (['gomod:foo.bar/project:2'],
                           [{
                             'name': 'foo.bar/project',
                             'type': 'gomod',
                             'version': '2'}]),
                          (['gomod:foo.bar/project:2:newproject'],
                          [{
                            'name': 'foo.bar/project',
                            'type': 'gomod',
                            'new_name': 'newproject',
                            'version': '2'}]),
                          (['gomod:foo.bar/project'], None)))
def test_resolve_remote_source(workflow, scratch, dr_strs, dependency_replacements):
    mock_cachito_api(workflow, dependency_replacements=dependency_replacements)
    workflow.user_params['scratch'] = scratch
    err = None
    if dr_strs and not scratch:
        err = 'Cachito dependency replacements are only allowed for scratch builds'

    if dr_strs and any(len(dr.split(':')) < 3 for dr in dr_strs):
        err = 'Cachito dependency replacements must be'

    expected_plugin_results = [
        {
            "name": None,
            "url": CACHITO_REQUEST_DOWNLOAD_URL,
            "remote_source_json": {
                "json": REMOTE_SOURCE_JSON,
                "filename": REMOTE_SOURCE_JSON_FILENAME,
            },
            "remote_source_tarball": {
                "filename": REMOTE_SOURCE_TARBALL_FILENAME,
                "path": expected_dowload_path(workflow),
            },
        },
    ]

    run_plugin_with_args(
        workflow,
        dependency_replacements=dr_strs,
        expect_error=err,
        expected_plugin_results=expected_plugin_results,
    )

    if err:
        return

    cachito_env_content = dedent(
        """\
        #!/bin/bash
        export GO111MODULE=on
        export GOPATH=/remote-source/deps/gomod
        export GOCACHE=/remote-source/deps/gomod
        """
    )

    workflow.build_dir.for_each_platform(
        check_injected_files(
            {
                "cachito.env": cachito_env_content,
                "app/README.txt": "Content of remote-source.tar.gz",
                "app/some-config.txt": "gomod requests don't actually have configs",
            },
        )
    )

    assert workflow.buildargs == {
        **CACHITO_BUILD_ARGS,
        "REMOTE_SOURCE": ResolveRemoteSourcePlugin.REMOTE_SOURCE,
        "REMOTE_SOURCE_DIR": REMOTE_SOURCE_DIR,
        CACHITO_ENV_ARG_ALIAS: str(Path(REMOTE_SOURCE_DIR, CACHITO_ENV_FILENAME)),
    }
    # https://github.com/openshift/imagebuilder/issues/139
    assert not workflow.buildargs["REMOTE_SOURCE"].startswith("/")


@pytest.mark.parametrize(
    'env_vars_json',
    [
        {
            'GOPATH': {'kind': 'path', 'value': 'deps/gomod'},
            'GOCACHE': {'kind': 'path', 'value': 'deps/gomod'},
            'GO111MODULE': {'kind': 'literal', 'value': 'on'},
            'GOX': {'kind': 'new', 'value': 'new-kind'},
        },
    ]
)
def test_fail_build_if_unknown_kind(workflow, env_vars_json):
    mock_cachito_api(workflow, env_vars_json=env_vars_json)
    run_plugin_with_args(workflow, expect_error=r'.*Unknown kind new got from Cachito')


def test_no_koji_user(workflow, caplog):
    reactor_config = dedent("""\
        version: 1
        cachito:
           api_url: {}
           auth:
               ssl_certs_dir: {}
        koji:
            hub_url: /
            root_url: ''
            auth: {{}}
        """.format(CACHITO_URL, workflow._tmpdir))
    mock_reactor_config(workflow, reactor_config)
    mock_cachito_api(workflow, user='unknown_user')
    workflow.user_params['koji_task_id'] = 'x'
    log_msg = 'Invalid Koji task ID'

    expected_plugin_results = [
        {
            "name": None,
            "url": CACHITO_REQUEST_DOWNLOAD_URL,
            "remote_source_json": {
                "json": REMOTE_SOURCE_JSON,
                "filename": REMOTE_SOURCE_JSON_FILENAME,
            },
            "remote_source_tarball": {
                "filename": REMOTE_SOURCE_TARBALL_FILENAME,
                "path": expected_dowload_path(workflow),
            },
        },
    ]
    run_plugin_with_args(workflow, expected_plugin_results=expected_plugin_results)
    assert log_msg in caplog.text


@pytest.mark.parametrize('pop_key', ('repo', 'ref', 'packages'))
def test_invalid_remote_source_structure(workflow, pop_key):
    source_request = {
        'id': CACHITO_REQUEST_ID,
        'repo': REMOTE_SOURCE_REPO,
        'ref': REMOTE_SOURCE_REF,
        'packages': REMOTE_SOURCE_PACKAGES,
    }
    source_request.pop(pop_key)
    mock_cachito_api(workflow, source_request=source_request)
    run_plugin_with_args(workflow, expect_error='Received invalid source request')


def test_fail_when_missing_cachito_config(workflow):
    reactor_config = dedent("""\
        version: 1
        koji:
            hub_url: /
            root_url: ''
            auth: {}
        """)
    mock_reactor_config(workflow, reactor_config)

    with pytest.raises(PluginFailedException) as exc:
        run_plugin_with_args(workflow, expect_result=False)
    assert 'No Cachito configuration defined' in str(exc.value)


def test_invalid_cert_reference(workflow):
    bad_certs_dir = str(workflow._tmpdir / 'invalid-dir')
    reactor_config = dedent("""\
        version: 1
        cachito:
           api_url: {}
           auth:
               ssl_certs_dir: {}
        koji:
            hub_url: /
            root_url: ''
            auth: {{}}
        """.format(CACHITO_URL, bad_certs_dir))
    mock_reactor_config(workflow, reactor_config)
    run_plugin_with_args(workflow, expect_error="Cachito ssl_certs_dir doesn't exist")


def test_ignore_when_missing_remote_source_config(workflow):
    remote_source_config = dedent("""---""")
    mock_repo_config(workflow, remote_source_config)
    result = run_plugin_with_args(workflow, expect_result=False)
    assert result is None


@pytest.mark.parametrize(('task_id', 'log_entry'), (
    (None, 'Invalid Koji task ID'),
    ('not-an-int', 'Invalid Koji task ID'),
))
def test_bad_build_metadata(workflow, task_id, log_entry, caplog):
    workflow.user_params['koji_task_id'] = task_id
    mock_cachito_api(workflow, user='unknown_user')

    expected_plugin_results = [
        {
            "name": None,
            "url": CACHITO_REQUEST_DOWNLOAD_URL,
            "remote_source_json": {
                "json": REMOTE_SOURCE_JSON,
                "filename": REMOTE_SOURCE_JSON_FILENAME,
            },
            "remote_source_tarball": {
                "filename": REMOTE_SOURCE_TARBALL_FILENAME,
                "path": expected_dowload_path(workflow),
            },
        },
    ]

    run_plugin_with_args(workflow, expected_plugin_results=expected_plugin_results)
    assert log_entry in caplog.text
    assert 'unknown_user' in caplog.text


@pytest.mark.parametrize('allow_multiple_remote_sources', [True, False])
def test_allow_multiple_remote_sources(workflow, allow_multiple_remote_sources):
    first_remote_source_name = 'gomod'
    first_remote_tarball_filename = 'remote-source-gomod.tar.gz'
    first_remote_json_filename = 'remote-source-gomod.json'
    second_remote_source_name = 'pip'
    second_remote_tarball_filename = 'remote-source-pip.tar.gz'
    second_remote_json_filename = 'remote-source-pip.json'

    container_yaml_config = dedent(
        """\
                remote_sources:
                - name: {}
                  remote_source:
                    repo: {}
                    ref: {}
                - name: {}
                  remote_source:
                    repo: {}
                    ref: {}
                """
    ).format(
        first_remote_source_name,
        REMOTE_SOURCE_REPO,
        REMOTE_SOURCE_REF,
        second_remote_source_name,
        SECOND_REMOTE_SOURCE_REPO,
        SECOND_REMOTE_SOURCE_REF,
    )

    reactor_config = dedent("""\
                version: 1
                cachito:
                   api_url: {}
                   auth:
                       ssl_certs_dir: {}
                koji:
                    hub_url: /
                    root_url: ''
                    auth: {{}}
                allow_multiple_remote_sources: {}
                """.format(CACHITO_URL, workflow._tmpdir, allow_multiple_remote_sources))
    mock_repo_config(workflow, data=container_yaml_config)
    mock_reactor_config(workflow, reactor_config)
    mock_cachito_api_multiple_remote_sources(workflow)
    if not allow_multiple_remote_sources:
        err_msg = (
            "Multiple remote sources are not enabled, "
            "use single remote source in container.yaml"
        )
        result = run_plugin_with_args(workflow, expect_result=False, expect_error=err_msg)
        assert result is None
    else:
        expected_plugin_results = [
            {
                "name": first_remote_source_name,
                "url": CACHITO_REQUEST_DOWNLOAD_URL,
                "remote_source_json": {
                    "json": REMOTE_SOURCE_JSON,
                    "filename": first_remote_json_filename,
                },
                "remote_source_tarball": {
                    "filename": first_remote_tarball_filename,
                    "path": expected_dowload_path(workflow, "gomod"),
                },
            },
            {
                "name": second_remote_source_name,
                "url": SECOND_CACHITO_REQUEST_DOWNLOAD_URL,
                "remote_source_json": {
                    "json": SECOND_REMOTE_SOURCE_JSON,
                    "filename": second_remote_json_filename,
                },
                "remote_source_tarball": {
                    "filename": second_remote_tarball_filename,
                    "path": expected_dowload_path(workflow, "pip"),
                },
            },
        ]

        run_plugin_with_args(workflow, expected_plugin_results=expected_plugin_results)

        first_cachito_env = dedent(
            """\
            #!/bin/bash
            export GO111MODULE=on
            export GOPATH=/remote-source/gomod/deps/gomod
            export GOCACHE=/remote-source/gomod/deps/gomod
            """
        )
        second_cachito_env = dedent(
            """\
            #!/bin/bash
            export PIP_CERT=/remote-source/pip/app/package-index-ca.pem
            export PIP_INDEX_URL=http://example-pip-index.url/stuff
            """
        )

        workflow.build_dir.for_each_platform(
            check_injected_files(
                {
                    "gomod/cachito.env": first_cachito_env,
                    "gomod/app/README.txt": "Content of remote-source-gomod.tar.gz",
                    "gomod/app/some-config.txt": "gomod requests don't actually have configs",
                    "pip/cachito.env": second_cachito_env,
                    "pip/app/README.txt": "Content of remote-source-pip.tar.gz",
                    "pip/app/package-index-ca.pem": "-----BEGIN CERTIFICATE-----",
                },
            )
        )

        assert workflow.buildargs == {
            "REMOTE_SOURCES": ResolveRemoteSourcePlugin.REMOTE_SOURCE,
            "REMOTE_SOURCES_DIR": REMOTE_SOURCE_DIR,
        }
        # https://github.com/openshift/imagebuilder/issues/139
        assert not workflow.buildargs["REMOTE_SOURCES"].startswith("/")


def test_multiple_remote_sources_non_unique_names(workflow):
    container_yaml_config = dedent("""\
            remote_sources:
            - name: same
              remote_source:
                repo: https://git.example.com/team/repo.git
                ref: a55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
            - name: same
              remote_source:
                repo: https://git.example.com/team/repo.git
                ref: a55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
            - name: bit-different
              remote_source:
                repo: https://git.example.com/team/repo.git
                ref: a55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
            """)
    reactor_config = dedent("""\
                version: 1
                cachito:
                   api_url: {}
                   auth:
                       ssl_certs_dir: {}
                koji:
                    hub_url: /
                    root_url: ''
                    auth: {{}}
                allow_multiple_remote_sources: True
                """.format(CACHITO_URL, workflow._tmpdir))
    mock_repo_config(workflow, data=container_yaml_config)
    mock_reactor_config(workflow, reactor_config)

    err_msg = (
        r"Provided remote sources parameters contain non unique names: \['same'\]"
    )
    result = run_plugin_with_args(workflow, expect_result=False, expect_error=err_msg)
    assert result is None


def run_plugin_with_args(workflow, dependency_replacements=None, expect_error=None,
                         expect_result=True, expected_plugin_results=None):
    runner = PreBuildPluginsRunner(
        workflow,
        [
            {
                "name": ResolveRemoteSourcePlugin.key,
                "args": {"dependency_replacements": dependency_replacements},
            },
        ],
    )

    if expect_error:
        with pytest.raises(PluginFailedException, match=expect_error):
            runner.run()
        return

    results = runner.run()[ResolveRemoteSourcePlugin.key]

    if expect_result:
        assert results == expected_plugin_results

    return results


def test_inject_remote_sources_dest_already_exists(workflow):
    plugin = ResolveRemoteSourcePlugin(workflow)

    processed_remote_sources = [
        RemoteSource(
            id=CACHITO_REQUEST_ID,
            name=None,
            json_data={},
            build_args={},
            tarball_path=Path("/does/not/matter"),
        ),
    ]

    builddir_path = Path(expected_build_dir(workflow))
    builddir_path.joinpath(ResolveRemoteSourcePlugin.REMOTE_SOURCE).mkdir()

    err_msg = "Conflicting path unpacked_remote_sources already exists"
    with pytest.raises(RuntimeError, match=err_msg):
        plugin.inject_remote_sources(processed_remote_sources)


def test_generate_cachito_env_file_shell_quoting(workflow):
    plugin = ResolveRemoteSourcePlugin(workflow)

    dest_dir = Path(expected_build_dir(workflow))
    plugin.generate_cachito_env_file(dest_dir, {"foo": "somefile; rm -rf ~"})

    cachito_env = dest_dir / "cachito.env"
    assert cachito_env.read_text() == dedent(
        """\
        #!/bin/bash
        export foo='somefile; rm -rf ~'
        """
    )


def test_generate_cachito_config_files_unknown_type(workflow):
    plugin = ResolveRemoteSourcePlugin(workflow)

    dest_dir = Path(expected_build_dir(workflow))
    cfg_files = [{"path": "foo", "type": "unknown", "content": "does not matter"}]

    with pytest.raises(ValueError, match="Unknown cachito configuration file data type 'unknown'"):
        plugin.generate_cachito_config_files(dest_dir, cfg_files)
