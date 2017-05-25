"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals
from flexmock import flexmock

import pytest
import os
import responses
import yaml

try:
    import koji as koji
except ImportError:
    import inspect
    import sys

    # Find our mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji as koji

from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins.pre_fetch_maven_artifacts import FetchMavenArtifactsPlugin
from atomic_reactor.util import ImageName
from tests.constants import MOCK_SOURCE
from tests.fixtures import docker_tasker  # noqa
from textwrap import dedent


KOJI_HUB = 'https://koji-hub.com'
KOJI_ROOT = 'https://koji-root.com'

FILER_ROOT_DOMAIN = 'filer.com'
FILER_ROOT = 'https://' + FILER_ROOT_DOMAIN


DEFAULT_KOJI_BUILD_ID = 472397


DEFAULT_KOJI_BUILD = {
    'build_id': DEFAULT_KOJI_BUILD_ID,
    'id': DEFAULT_KOJI_BUILD_ID,
    'name': 'com.sun.xml.bind.mvn-jaxb-parent',
    'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
    'release': '1',
    'version': '2.2.11.4',
}


ARCHIVE_JAXB_SUN_POM = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '697317209103338c7c841e327bb6e7b0',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.pom',
    'group_id': 'com.sun.xml.bind',
    'id': 1269850,
    'size': 15320,
    'version': '2.2.11-4'
}


ARCHIVE_JAXB_SUN_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '06bae6472e3d1635f0c3b79bd314fdf3',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.jar',
    'group_id': 'com.sun.xml.bind',
    'id': 1269849,
    'size': 252461,
    'version': '2.2.11-4'
}


ARCHIVE_JAXB_JAVADOC_SUN_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '3643ba275364b29117f2bc5f0bcf18d9',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4-javadoc.jar',
    'group_id': 'com.sun.xml.bind',
    'id': 1269848,
    'size': 819956,
    'version': '2.2.11-4'
}


ARCHIVE_JAXB_GLASSFISH_POM = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': 'cc7b7a4d1c33d83fba9adf95226af570',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.pom',
    'group_id': 'org.glassfish.jaxb',
    'id': 1269791,
    'size': 3092,
    'version': '2.2.11-4'
}


ARCHIVE_JAXB_GLASSFISH_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '2ba4912b1a3c699b09ec99e19820fb09',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4.jar',
    'group_id': 'org.glassfish.jaxb',
    'id': 1269790,
    'size': 156400,
    'version': '2.2.11-4'
}


ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR = {
    'artifact_id': 'jaxb-core',
    'build_id': 472397,
    'checksum': '69bc6de0a57dd10c7370573a8e76f0b2',
    'checksum_type': koji.CHECKSUM_TYPES['md5'],
    'filename': 'jaxb-core-2.2.11-4-javadoc.jar',
    'group_id': 'org.glassfish.jaxb',
    'id': 1269789,
    'size': 524417,
    'version': '2.2.11-4'
}


ARCHIVE_SHA1 = {
    'artifact_id': 'jaxb-sha1',
    'build_id': 472397,
    'checksum': '66bd6b88ba636993ad0fd522cc1254c9ff5f5a1c',
    'checksum_type': koji.CHECKSUM_TYPES['sha1'],
    'filename': 'jaxb-core-2.2.11-4-sha1.jar',
    'group_id': 'org.glassfish.jaxb.sha1',
    'id': 1269792,
    'size': 524417,
    'version': '2.2.11-4'
}


ARCHIVE_SHA256 = {
    'artifact_id': 'jaxb-sha256',
    'build_id': 472397,
    'checksum': 'ca52bcbda16954c9e83e4c0049277ac77f014ecc16a94ed92bc3203fa13aac7d',
    'checksum_type': koji.CHECKSUM_TYPES['sha256'],
    'filename': 'jaxb-core-2.2.11-4-sha256.jar',
    'group_id': 'org.glassfish.jaxb.sha256',
    'id': 1269792,
    'size': 524417,
    'version': '2.2.11-4'
}


# To avoid having to actually download archives during testing,
# the checksum value is based on the mocked download response,
# which is simply the "filename" and "group_id" values.
DEFAULT_ARCHIVES = [
    ARCHIVE_JAXB_SUN_POM,
    ARCHIVE_JAXB_SUN_JAR,
    ARCHIVE_JAXB_JAVADOC_SUN_JAR,
    ARCHIVE_JAXB_GLASSFISH_POM,
    ARCHIVE_JAXB_GLASSFISH_JAR,
    ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR,
    ARCHIVE_SHA1,
    ARCHIVE_SHA256,
]


REMOTE_FILE_SPAM = {
    'url': FILER_ROOT + '/spam/spam.jar',
    'md5': 'ec61f019a3d0826c04ab20c55462aa24',
}


REMOTE_FILE_BACON = {
    'url': FILER_ROOT + '/bacon/bacon.jar',
    'md5': 'b4dbaf349d175aa5bbd5c5d076c00393',
}


REMOTE_FILE_WITH_TARGET = {
    'url': FILER_ROOT + '/eggs/eggs.jar',
    'md5': 'b1605c846e03035a6538873e993847e5',
    'target': 'sgge.jar'
}


REMOTE_FILE_SHA1 = {
    'url': FILER_ROOT + '/ham/ham.jar',
    'sha1': 'c4f8d66d78f5ed17299ae88fed9f8a8c6f3c592a',
}


REMOTE_FILE_SHA256 = {
    'url': FILER_ROOT + '/sausage/sausage.jar',
    'sha256': '0da8e7df6c45b1006b10e4d0df5e1a8d5c4dc17c2c9c0ab53c5714dadb705d1c',
}


REMOTE_FILE_MULTI_HASH = {
    'url': FILER_ROOT + '/biscuit/biscuit.jar',
    'sha256': '05892a95a8257a6c51a5ee4ba122e14e9719d7ead3b1d44e7fbea604da2fc8d1',
    'sha1': '0eb3dc253aeda45e272f07cf6e77fcc8bcf6628a',
    'md5': '24e4dec8666658ec7141738dbde951c5',
}


# To avoid having to actually download archives during testing,
# the md5 value is based on the mocked download response,
# which is simply the url.
DEFAULT_REMOTE_FILES = [REMOTE_FILE_SPAM, REMOTE_FILE_BACON, REMOTE_FILE_WITH_TARGET,
                        REMOTE_FILE_SHA1, REMOTE_FILE_SHA256, REMOTE_FILE_MULTI_HASH]


class MockSource(object):
    def __init__(self, tmpdir):
        tmpdir = str(tmpdir)
        self.dockerfile_path = os.path.join(tmpdir, 'Dockerfile')
        self.path = tmpdir

    def get_dockerfile_path(self):
        return self.dockerfile_path, self.path


class X(object):
    image_id = "xxx"
    base_image = ImageName.parse("fedora/jboss")
    set_base_image = flexmock()


class MockedPathInfo(object):
    def __init__(self, topdir=None):
        self.topdir = topdir

    def mavenbuild(self, build):
        return '{topdir}/packages/{name}/{version}/{release}/maven'.format(topdir=self.topdir,
                                                                           **build)

    def mavenfile(self, maveninfo):
        return '{group_id}/{artifact_id}/{version}/{filename}'.format(**maveninfo)


def mock_koji_session(koji_proxyuser=None, koji_ssl_certs_dir=None,
                      koji_krb_principal=None, koji_krb_keytab=None,
                      build_info=None, archives=None):

    if not build_info:
        build_info = DEFAULT_KOJI_BUILD
    if not archives:
        archives = DEFAULT_ARCHIVES

    flexmock(koji, PathInfo=MockedPathInfo)

    session = flexmock()

    (flexmock(koji)
        .should_receive('ClientSession')
        .once()
        .and_return(session))

    def mock_get_build(nvr):
        if nvr == DEFAULT_KOJI_BUILD['nvr']:
            return DEFAULT_KOJI_BUILD
        else:
            return None
    (session
        .should_receive('getBuild')
        .replace_with(mock_get_build))

    (session
        .should_receive('listArchives')
        .and_return(archives))

    return session


def mock_workflow(tmpdir):
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    mock_source = MockSource(tmpdir)
    setattr(workflow, 'builder', X)
    workflow.builder.source = mock_source
    flexmock(workflow, source=mock_source)

    return workflow


def mock_fetch_artifacts_by_nvr(tmpdir, contents=None):
    if not contents:
        contents = dedent("""\
            - nvr: com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1
            """)

    with open(os.path.join(tmpdir, FetchMavenArtifactsPlugin.NVR_REQUESTS_FILENAME), 'w') as f:
        f.write(contents)
        f.flush()


def mock_fetch_artifacts_by_url(tmpdir, contents=None):
    if not contents:
        contents = yaml.safe_dump(DEFAULT_REMOTE_FILES)

    with open(os.path.join(tmpdir, FetchMavenArtifactsPlugin.URL_REQUESTS_FILENAME), 'w') as f:
        f.write(contents)
        f.flush()


def mock_nvr_downloads(build_info=None, archives=None, overrides=None):
    if not build_info:
        build_info = DEFAULT_KOJI_BUILD
    if not archives:
        archives = DEFAULT_ARCHIVES
    if not overrides:
        overrides = {}

    pi = koji.PathInfo(topdir=KOJI_ROOT)

    for archive in archives:
        url = pi.mavenbuild(build_info) + '/' + pi.mavenfile(archive)
        # Use any overrides for this archive ID
        archive_overrides = overrides.get(archive['id'], {})
        status = archive_overrides.get('status', 200)
        body = archive_overrides.get('body', archive['filename'] + archive['group_id'])
        responses.add(responses.GET, url, body=body, status=status)


def mock_url_downloads(remote_files=None, overrides=None):
    if not remote_files:
        remote_files = DEFAULT_REMOTE_FILES
    if not overrides:
        overrides = {}

    for remote_file in DEFAULT_REMOTE_FILES:
        url = remote_file['url']
        # Use any overrides for this url
        remote_file_overrides = overrides.get(url, {})
        body = remote_file_overrides.get('body', url)
        status = remote_file_overrides.get('status', 200)
        responses.add(responses.GET, url, body=body, status=status)


@responses.activate  # noqa
def test_fetch_maven_artifacts(tmpdir, docker_tasker):
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(str(tmpdir))
    mock_fetch_artifacts_by_url(str(tmpdir))
    mock_nvr_downloads()
    mock_url_downloads()
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {'koji_hub': KOJI_HUB, 'koji_root': KOJI_ROOT}
        }]
    )

    results = runner.run()
    plugin_result = results[FetchMavenArtifactsPlugin.key]

    assert len(plugin_result) == len(DEFAULT_ARCHIVES) + len(DEFAULT_REMOTE_FILES)
    for download in plugin_result:
        dest = os.path.join(str(tmpdir), FetchMavenArtifactsPlugin.DOWNLOAD_DIR, download.dest)
        assert os.path.exists(dest)


@pytest.mark.parametrize(('nvr_requests', 'expected'), (  # noqa
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'filename': '*'}]
        }
    ], DEFAULT_ARCHIVES),
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'filename': '*javadoc*'}]
        }
    ], [ARCHIVE_JAXB_JAVADOC_SUN_JAR, ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR]),
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'filename': '*javadoc*', 'group_id': 'org.glassfish.jaxb'}]
        }
    ], [ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR]),
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'group_id': 'org.glassfish.jaxb'}]
        }
    ], [ARCHIVE_JAXB_GLASSFISH_JAR, ARCHIVE_JAXB_JAVADOC_GLASSFIX_JAR, ARCHIVE_JAXB_GLASSFISH_POM]),
))
@responses.activate
def test_fetch_maven_artifacts_nvr_filtering(tmpdir, docker_tasker, nvr_requests, expected):
    """Test filtering of archives in a Koji build."""
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(str(tmpdir), contents=yaml.safe_dump(nvr_requests))
    mock_nvr_downloads(archives=expected)
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {'koji_hub': KOJI_HUB, 'koji_root': KOJI_ROOT}
        }]
    )

    results = runner.run()
    plugin_result = results[FetchMavenArtifactsPlugin.key]

    assert len(plugin_result) == len(expected)
    for download in plugin_result:
        assert len(download.checksums.values()) == 1
    assert (set(list(download.checksums.values())[0] for download in plugin_result) ==
            set(expectation['checksum'] for expectation in expected))
    for download in plugin_result:
        dest = os.path.join(str(tmpdir), FetchMavenArtifactsPlugin.DOWNLOAD_DIR, download.dest)
        assert os.path.exists(dest)


@pytest.mark.parametrize(('nvr_requests', 'error_msg'), (  # noqa
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'filename': '*gboss'}]
        }
    ], '*gboss'),
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'group_id': 'glassfish.org'}]
        }
    ], 'glassfish.org'),
    ([
        {
            'nvr': 'com.sun.xml.bind.mvn-jaxb-parent-2.2.11.4-1',
            'archives': [{'filename': '*', 'group_id': 'glassfish.org'}]
        }
    ], 'glassfish.org'),
))
@responses.activate
def test_fetch_maven_artifacts_nvr_no_match(tmpdir, docker_tasker, nvr_requests, error_msg):
    """Err when a requested archive is not found in Koji build."""
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(str(tmpdir), contents=yaml.safe_dump(nvr_requests))
    mock_nvr_downloads()
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {'koji_hub': KOJI_HUB, 'koji_root': KOJI_ROOT}
        }]
    )

    with pytest.raises(PluginFailedException) as e:
        runner.run()

    assert 'failed to find archives' in str(e)
    assert error_msg in str(e)


@responses.activate  # noqa
def test_fetch_maven_artifacts_nvr_bad_checksum(tmpdir, docker_tasker):
    """Err when downloaded archive from Koji build has unexpected checksum."""
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(str(tmpdir))
    mock_nvr_downloads(overrides={1269850: {'body': 'corrupted-file'}})
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {'koji_hub': KOJI_HUB, 'koji_root': KOJI_ROOT}
        }]
    )

    with pytest.raises(PluginFailedException) as e:
        runner.run()

    assert 'does not match expected checksum' in str(e)


@responses.activate  # noqa
def test_fetch_maven_artifacts_nvr_bad_url(tmpdir, docker_tasker):
    """Err on download errors for artifact from Koji build."""
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(str(tmpdir))
    mock_nvr_downloads(overrides={1269850: {'status': 404}})
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {'koji_hub': KOJI_HUB, 'koji_root': KOJI_ROOT}
        }]
    )

    with pytest.raises(PluginFailedException) as e:
        runner.run()

    assert '404 Client Error' in str(e)


@responses.activate  # noqa
def test_fetch_maven_artifacts_nvr_bad_nvr(tmpdir, docker_tasker):
    """Err when given nvr is not a valid build in Koji."""
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    contents = dedent("""\
        - nvr: where-is-this-build-3.0-2
        """)
    mock_fetch_artifacts_by_nvr(str(tmpdir), contents=contents)
    mock_nvr_downloads()
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {'koji_hub': KOJI_HUB, 'koji_root': KOJI_ROOT}
        }]
    )

    with pytest.raises(PluginFailedException) as e:
        runner.run()

    assert 'Build where-is-this-build-3.0-2 not found' in str(e)


@pytest.mark.parametrize('contents', (  # noqa
    dedent("""\
        - nvo: invalid attribute
        """),

    dedent("""\

        """),

    dedent("""\
        nvr: not a list
        """),

    dedent("""\
        - nvr: foo-bar-22-9
          archives: not a list
        """),

    dedent("""\
        - nvr: foo-bar-22-9
          archives:
            - filenamo: invalid attribute
        """),

))
@responses.activate
def test_fetch_maven_artifacts_nvr_schema_error(tmpdir, docker_tasker, contents):
    """Err on invalid format for fetch-artifacts-koji.yaml"""
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    mock_fetch_artifacts_by_nvr(str(tmpdir), contents=contents)
    mock_nvr_downloads()
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {'koji_hub': KOJI_HUB, 'koji_root': KOJI_ROOT}
        }]
    )

    with pytest.raises(PluginFailedException) as e:
        runner.run()

    assert 'ValidationError' in str(e)


@responses.activate  # noqa
def test_fetch_maven_artifacts_url_with_target(tmpdir, docker_tasker):
    """Remote file is downloaded into specified filename."""
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    remote_files = [REMOTE_FILE_WITH_TARGET]
    mock_fetch_artifacts_by_url(str(tmpdir), contents=yaml.safe_dump(remote_files))
    mock_url_downloads(remote_files)
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {'koji_hub': KOJI_HUB, 'koji_root': KOJI_ROOT}
        }]
    )

    results = runner.run()
    plugin_result = results[FetchMavenArtifactsPlugin.key]

    assert len(plugin_result) == len(remote_files)

    download = plugin_result[0]
    dest = os.path.join(str(tmpdir), FetchMavenArtifactsPlugin.DOWNLOAD_DIR, download.dest)
    assert os.path.exists(dest)
    assert download.dest == REMOTE_FILE_WITH_TARGET['target']
    assert not REMOTE_FILE_WITH_TARGET['url'].endswith(download.dest)


@responses.activate  # noqa
def test_fetch_maven_artifacts_url_bad_checksum(tmpdir, docker_tasker):
    """Err when downloaded remote file has unexpected checksum."""
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    mock_fetch_artifacts_by_url(str(tmpdir))
    mock_url_downloads(overrides={REMOTE_FILE_SPAM['url']: {'body': 'corrupted-file'}})
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {'koji_hub': KOJI_HUB, 'koji_root': KOJI_ROOT}
        }]
    )

    with pytest.raises(PluginFailedException) as e:
        runner.run()

    assert 'does not match expected checksum' in str(e)


@responses.activate  # noqa
def test_fetch_maven_artifacts_url_bad_url(tmpdir, docker_tasker):
    """Err on download errors for remote file."""
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    mock_fetch_artifacts_by_url(str(tmpdir))
    mock_url_downloads(overrides={REMOTE_FILE_SPAM['url']: {'status': 404}})
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {'koji_hub': KOJI_HUB, 'koji_root': KOJI_ROOT}
        }]
    )

    with pytest.raises(PluginFailedException) as e:
        runner.run()

    assert '404 Client Error' in str(e)


@pytest.mark.parametrize('contents', (  # noqa
    dedent("""\
        - uru: invalid attribute
        """),

    dedent("""\

        """),

    dedent("""\
        url: not a list
        """),

    dedent("""\
        - url: missing hashing
        """),

    dedent("""\
        - url: invalid md5 checksum size
          md5: a1234
        """),

    dedent("""\
        - url: invalid sha1 checksum size
          sha1: a1234
        """),

    dedent("""\
        - url: invalid sha256 checksum size
          sha256: a1234
        """),
))
@responses.activate
def test_fetch_maven_artifacts_url_schema_error(tmpdir, docker_tasker, contents):
    """Err on invalid format for fetch-artifacts-url.yaml"""
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    mock_fetch_artifacts_by_url(str(tmpdir), contents=contents)
    mock_url_downloads()
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {'koji_hub': KOJI_HUB, 'koji_root': KOJI_ROOT}
        }]
    )

    with pytest.raises(PluginFailedException) as e:
        runner.run()

    assert 'ValidationError' in str(e)


@pytest.mark.parametrize(('domains', 'raises'), (  # noqa
    ([], False),
    ([FILER_ROOT_DOMAIN], False),
    ([FILER_ROOT_DOMAIN.upper()], False),
    ([FILER_ROOT_DOMAIN, 'spam.com'], False),
    ([
        FILER_ROOT_DOMAIN + '/spam/',
        FILER_ROOT_DOMAIN + '/bacon/',
        FILER_ROOT_DOMAIN + '/eggs/',
        FILER_ROOT_DOMAIN + '/ham/',
        FILER_ROOT_DOMAIN + '/sausage/',
        FILER_ROOT_DOMAIN + '/biscuit/',
    ], False),
    ([FILER_ROOT_DOMAIN + '/spam/'], True),
    (['spam.com'], True),
    (['spam.com', 'bacon.bz'], True),
))
@responses.activate
def test_fetch_maven_artifacts_url_allowed_domains(tmpdir, docker_tasker, domains, raises):
    """Validate URL domain is allowed when fetching remote file."""
    workflow = mock_workflow(tmpdir)
    mock_koji_session()
    mock_fetch_artifacts_by_url(str(tmpdir))
    mock_url_downloads()
    runner = PreBuildPluginsRunner(
        docker_tasker,
        workflow,
        [{
            'name': FetchMavenArtifactsPlugin.key,
            'args': {
                'koji_hub': KOJI_HUB,
                'koji_root': KOJI_ROOT,
                'allowed_domains': domains,
            }
        }]
    )

    if raises:
        with pytest.raises(PluginFailedException) as e:
            runner.run()
        assert 'is not in list of allowed domains' in str(e)

    else:
        results = runner.run()
        plugin_result = results[FetchMavenArtifactsPlugin.key]
        for download in plugin_result:
            dest = os.path.join(str(tmpdir), FetchMavenArtifactsPlugin.DOWNLOAD_DIR, download.dest)
            assert os.path.exists(dest)
