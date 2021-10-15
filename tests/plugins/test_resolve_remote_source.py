"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from textwrap import dedent
import sys

from flexmock import flexmock
import pytest
import koji
import yaml

import atomic_reactor.utils.koji as koji_util
from atomic_reactor.utils.cachito import CachitoAPI
from atomic_reactor.constants import (
    PLUGIN_BUILD_ORCHESTRATE_KEY,
    REMOTE_SOURCE_TARBALL_FILENAME,
    REMOTE_SOURCE_JSON_FILENAME,
)
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.build_orchestrate_build import (
    WORKSPACE_KEY_OVERRIDE_KWARGS, OrchestrateBuildPlugin)
from atomic_reactor.plugins.pre_resolve_remote_source import ResolveRemoteSourcePlugin
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

    workflow._tmpdir.join('cert').write('')
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

    workflow._tmpdir.join('container.yaml').write(data)

    # The repo config is read when SourceConfig is initialized. Force
    # reloading here to make usage easier.
    workflow.source.config = SourceConfig(str(workflow._tmpdir))


@pytest.fixture
def workflow(tmpdir, user_params):
    workflow = DockerBuildWorkflow(source=None)

    # Stash the tmpdir in workflow so it can be used later
    workflow._tmpdir = tmpdir

    class MockSource(StubSource):

        def __init__(self, workdir):
            super(MockSource, self).__init__()
            self.workdir = workdir

    workflow.source = MockSource(str(tmpdir))
    workflow.buildstep_plugins_conf = [{'name': PLUGIN_BUILD_ORCHESTRATE_KEY}]
    workflow.user_params = {'koji_task_id': KOJI_TASK_ID}

    mock_repo_config(workflow)
    mock_reactor_config(workflow)
    mock_koji()

    return workflow


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
        .should_receive("assemble_download_url")
        .with_args(CACHITO_SOURCE_REQUEST)
        .and_return(CACHITO_REQUEST_DOWNLOAD_URL)
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("download_sources")
        .with_args(
            CACHITO_SOURCE_REQUEST,
            dest_dir=str(workflow._tmpdir),
            dest_filename="remote-source-gomod.tar.gz",
        )
        .and_return(expected_dowload_path(workflow))
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("assemble_download_url")
        .with_args(SECOND_CACHITO_SOURCE_REQUEST)
        .and_return(SECOND_CACHITO_REQUEST_DOWNLOAD_URL)
        .ordered()
    )

    (
        flexmock(CachitoAPI)
        .should_receive("download_sources")
        .with_args(
            SECOND_CACHITO_SOURCE_REQUEST,
            dest_dir=str(workflow._tmpdir),
            dest_filename="remote-source-pip.tar.gz",
        )
        .and_return(expected_dowload_path(workflow))
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
        .should_receive("get_request_env_vars")
        .with_args(SECOND_CACHITO_SOURCE_REQUEST["id"])
        .and_return(SECOND_CACHITO_ENV_VARS_JSON)
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
        .with_args(source_request, dest_dir=str(workflow._tmpdir),
                   dest_filename=REMOTE_SOURCE_TARBALL_FILENAME)
        .and_return(expected_dowload_path(workflow)))

    (flexmock(CachitoAPI)
        .should_receive('assemble_download_url')
        .with_args(source_request)
        .and_return(CACHITO_REQUEST_DOWNLOAD_URL))

    (flexmock(CachitoAPI)
        .should_receive('get_request_env_vars')
        .with_args(source_request['id'])
        .and_return(env_vars_json or CACHITO_ENV_VARS_JSON))


def mock_koji(user=KOJI_TASK_OWNER):
    koji_session = flexmock(krb_login=lambda: 'some')
    flexmock(koji, ClientSession=lambda hub, opts: koji_session)
    flexmock(koji_util).should_receive('get_koji_task_owner').and_return({'name': user})


def expected_dowload_path(workflow):
    return workflow._tmpdir.join('source.tar.gz')


def setup_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_resolve_remote_source', None)


def teardown_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_resolve_remote_source', None)


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
@pytest.mark.parametrize('env_vars_json, expected_build_args', [
    [CACHITO_ENV_VARS_JSON, CACHITO_BUILD_ARGS],
    [
        {
            'GOPATH': {'kind': 'path', 'value': 'deps/gomod'},
            'GOCACHE': {'kind': 'path', 'value': 'deps/gomod'},
        },
        {
            'GOPATH': '/remote-source/deps/gomod',
            'GOCACHE': '/remote-source/deps/gomod',
        },
    ],
    [
        {'GO111MODULE': {'kind': 'literal', 'value': 'on'}},
        {
            'GO111MODULE': 'on',
        },
    ],
])
def test_resolve_remote_source(workflow, scratch, dr_strs, dependency_replacements,
                               env_vars_json, expected_build_args):
    mock_cachito_api(workflow,
                     dependency_replacements=dependency_replacements,
                     env_vars_json=env_vars_json)
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
    expected_worker_params = [{
        'build_args': expected_build_args,
        'configs': CACHITO_REQUEST_CONFIG_URL,
        'request_id': CACHITO_REQUEST_ID,
        'url': CACHITO_REQUEST_DOWNLOAD_URL,
        'name': None,
    }]

    run_plugin_with_args(
        workflow,
        dependency_replacements=dr_strs,
        expect_error=err,
        expected_plugin_results=expected_plugin_results,
        expected_worker_params=expected_worker_params
    )


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
    expected_worker_params = [{
        'build_args': CACHITO_BUILD_ARGS,
        'configs': CACHITO_REQUEST_CONFIG_URL,
        'request_id': CACHITO_REQUEST_ID,
        'url': CACHITO_REQUEST_DOWNLOAD_URL,
        'name': None,
    }]
    run_plugin_with_args(workflow, expected_plugin_results=expected_plugin_results,
                         expected_worker_params=expected_worker_params)
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
    bad_certs_dir = str(workflow._tmpdir.join('invalid-dir'))
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
    expected_worker_params = [{
        'build_args': CACHITO_BUILD_ARGS,
        'configs': CACHITO_REQUEST_CONFIG_URL,
        'request_id': CACHITO_REQUEST_ID,
        'url': CACHITO_REQUEST_DOWNLOAD_URL,
        'name': None,
    }]

    run_plugin_with_args(workflow, expected_plugin_results=expected_plugin_results,
                         expected_worker_params=expected_worker_params)
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
        cachito_build_args = {
            'GO111MODULE': 'on',
            'GOPATH': f'/remote-source/{first_remote_source_name}/deps/gomod',
            'GOCACHE': f'/remote-source/{first_remote_source_name}/deps/gomod',
        }

        second_cachito_build_args = {
            'PIP_CERT': f'/remote-source/{second_remote_source_name}/app/package-index-ca.pem',
            'PIP_INDEX_URL': 'http://example-pip-index.url/stuff'
        }
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
                    "path": expected_dowload_path(workflow),
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
                    "path": expected_dowload_path(workflow),
                },
            },
        ]
        expected_worker_params = [
            {
                "build_args": cachito_build_args,
                "configs": CACHITO_REQUEST_CONFIG_URL,
                "request_id": CACHITO_REQUEST_ID,
                "url": CACHITO_REQUEST_DOWNLOAD_URL,
                "name": first_remote_source_name,
            },
            {
                "build_args": second_cachito_build_args,
                "configs": SECOND_CACHITO_REQUEST_CONFIG_URL,
                "request_id": SECOND_CACHITO_REQUEST_ID,
                "url": SECOND_CACHITO_REQUEST_DOWNLOAD_URL,
                "name": second_remote_source_name,
            },
        ]

        run_plugin_with_args(workflow, expected_plugin_results=expected_plugin_results,
                             expected_worker_params=expected_worker_params)


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
                         expect_result=True, expected_plugin_results=None,
                         expected_worker_params=None):
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

        # A result means the plugin was enabled and executed successfully.
        # Let's verify the expected side effects.
        orchestrator_build_workspace = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
        worker_params = orchestrator_build_workspace[WORKSPACE_KEY_OVERRIDE_KWARGS][None]

        assert worker_params["remote_sources"] == expected_worker_params

    return results
