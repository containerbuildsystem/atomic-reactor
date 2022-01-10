# -*- coding: utf-8 -*-
"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os.path
import shutil
from fnmatch import fnmatch
from pathlib import Path
from textwrap import dedent

import koji
import pytest
import responses
from flexmock import flexmock

from atomic_reactor.constants import RELATIVE_REPOS_PATH, INSPECT_CONFIG, DOCKERFILE_FILENAME, \
    PLUGIN_RESOLVE_COMPOSES_KEY, PLUGIN_CHECK_AND_SET_PLATFORMS_KEY
from atomic_reactor.plugin import PluginFailedException, PreBuildPluginsRunner
from atomic_reactor.plugins.pre_inject_yum_repos import InjectYumReposPlugin
from atomic_reactor.source import VcsInfo
from atomic_reactor.util import df_parser, sha256sum, DockerfileImages
from atomic_reactor.utils.yum import YumRepo
from tests.constants import DOCKERFILE_GIT, DOCKERFILE_SHA1
from tests.util import add_koji_map_in_workflow

BUILDER_CA_BUNDLE = '/path/to/tls-ca-bundle.pem'
CA_BUNDLE_PEM = os.path.basename(BUILDER_CA_BUNDLE)
DEFAULT_DOCKERFILE = dedent("""\
    FROM fedora:33
    RUN dnf update -y
""")
KOJI_TARGET = "target"
KOJI_TARGET_NO_INFO = "target-no-info"
KOJI_TARGET_BROKEN_TAG = "target-broken"
KOJI_TARGET_BROKEN_REPO = "target-broken-repo"
KOJI_TAG = "tag"
KOJI_BROKEN_TAG = "tag-broken"
KOJI_BROKEN_REPO = "tag-broken-repo"
GET_TARGET_RESPONSE = {"build_tag_name": KOJI_TAG}
BROKEN_TAG_RESPONSE = {"build_tag_name": KOJI_BROKEN_TAG}
BROKEN_REPO_RESPONSE = {"build_tag_name": KOJI_BROKEN_REPO}
TAG_ID = "1"
BROKEN_REPO_TAG_ID = "2"
GET_TAG_RESPONSE = {"id": TAG_ID, "name": KOJI_TAG}
REPO_ID = "2"
BROKEN_REPO_ID = "3"
REPO_BROKEN_TAG_RESPONSE = {"id": BROKEN_REPO_ID, "name": KOJI_BROKEN_REPO}
GET_REPO_RESPONSE = {"id": "2"}
ROOT = "http://example.com"


# ClientSession is xmlrpc instance, we need to mock it explicitly
class MockedClientSession(object):
    def __init__(self, hub, opts=None):
        self.ca_path = None
        self.cert_path = None
        self.serverca_path = None

    def getBuildTarget(self, target):
        if target == KOJI_TARGET_NO_INFO:
            return None
        if target == KOJI_TARGET_BROKEN_TAG:
            return BROKEN_TAG_RESPONSE
        if target == KOJI_TARGET_BROKEN_REPO:
            return BROKEN_REPO_RESPONSE
        return GET_TARGET_RESPONSE

    def getTag(self, tag):
        if tag == KOJI_BROKEN_TAG:
            return None
        if tag == KOJI_BROKEN_REPO:
            return REPO_BROKEN_TAG_RESPONSE
        return GET_TAG_RESPONSE

    def getRepo(self, repo):
        if repo == BROKEN_REPO_ID:
            return None
        return GET_REPO_RESPONSE

    def ssl_login(self, cert=None, ca=None, serverca=None, proxyuser=None):
        self.ca_path = ca
        self.cert_path = cert
        self.serverca_path = serverca
        return True

    def krb_login(self, *args, **kwargs):
        return True


pytestmark = pytest.mark.usefixtures('user_params')


class MockedPathInfo(object):
    def __init__(self, topdir=None):
        self.topdir = topdir

    def repo(self, repo_id, name):
        return "{0}/repos/{1}/{2}".format(self.topdir, name, repo_id)


class MockSource(object):
    def __init__(self, build_dir: Path):
        self.dockerfile_path = str(build_dir / DOCKERFILE_FILENAME)
        self.path = str(build_dir)

    def get_build_file_path(self):
        return self.dockerfile_path, self.path

    def get_vcs_info(self):
        return VcsInfo('git', DOCKERFILE_GIT, DOCKERFILE_SHA1)


def prepare(workflow, build_dir, inherited_user='', dockerfile=DEFAULT_DOCKERFILE, scratch=False,
            platforms=None, include_koji_repo=False, yum_proxy=None, koji_ssl_certs=False,
            root_url=ROOT, yum_repourls=None):
    if yum_repourls is None:
        yum_repourls = {}
    if not platforms:
        platforms = ['x86_64']
    if koji_ssl_certs:
        build_dir.joinpath("cert").write_text("cert", "utf-8")
        build_dir.joinpath("serverca").write_text("serverca", "utf-8")
    workflow.user_params['scratch'] = scratch
    workflow.data.prebuild_results[PLUGIN_CHECK_AND_SET_PLATFORMS_KEY] = platforms
    workflow.source = MockSource(build_dir)
    inspect_data = {INSPECT_CONFIG: {'User': inherited_user}}
    flexmock(workflow.imageutil).should_receive('base_image_inspect').and_return(inspect_data)
    with open(workflow.source.dockerfile_path, 'w') as f:
        f.write(dockerfile)
    workflow.build_dir.init_build_dirs(platforms, workflow.source)
    df = df_parser(str(build_dir))
    df.content = dockerfile
    workflow.dockerfile_images = DockerfileImages(df.parent_images)
    if include_koji_repo:
        session = MockedClientSession(hub='', opts=None)
        workflow.koji_session = session
        flexmock(koji,
                 ClientSession=session,
                 PathInfo=MockedPathInfo)

        workflow.conf.conf = {'version': 1, 'yum_proxy': yum_proxy}
        add_koji_map_in_workflow(workflow, hub_url='', root_url=root_url,
                                 ssl_certs_dir=str(build_dir) if koji_ssl_certs else None)
    workflow.data.prebuild_results[PLUGIN_RESOLVE_COMPOSES_KEY] = {'composes': [],
                                                                   'include_koji_repo':
                                                                   include_koji_repo,
                                                                   'yum_repourls': yum_repourls,
                                                                   }
    return workflow


@pytest.mark.parametrize(('configure_ca_bundle', 'repos'), [
    [
        True,
        [
            (
                'http://repos.host/custom.repo',
                dedent('''\
                [new-packages]
                name=repo1
                baseurl=http://repo.host/latest/$basearch/os
                '''),
                dedent(f'''\
                [new-packages]
                sslcacert=/tmp/{CA_BUNDLE_PEM}
                name=repo1
                baseurl=http://repo.host/latest/$basearch/os
                '''),
            ),
        ],
    ],
    [
        False,
        [
            (
                'http://repos.host/custom.repo',
                dedent('''\
                [new-packages]
                name=repo1
                baseurl=http://repo.host/latest/$basearch/os
                '''),
                dedent(f'''\
                [new-packages]
                sslcacert=/tmp/{CA_BUNDLE_PEM}
                name=repo1
                baseurl=http://repo.host/latest/$basearch/os
                '''),
            ),
        ],
    ]
])
@responses.activate
def test_no_base_image_in_dockerfile(workflow, build_dir, configure_ca_bundle, repos, caplog):
    workflow = prepare(workflow, build_dir, dockerfile='',
                       yum_repourls={'x86_64': [url for url, _, _ in repos]})
    workflow.conf.conf['yum_repo_allowed_domains'] = ['odcs.example.com', 'repos.host']
    if configure_ca_bundle:
        workflow.conf.conf['builder_ca_bundle'] = BUILDER_CA_BUNDLE

    # Ensure the ca_bundle PEM file is copied into build context
    flexmock(shutil).should_receive('copyfile').with_args(
        BUILDER_CA_BUNDLE,
        (workflow.build_dir.any_platform.path / CA_BUNDLE_PEM))

    for repofile_url, repofile_content, _ in repos:
        responses.add(responses.GET, repofile_url, body=repofile_content)

    runner = PreBuildPluginsRunner(workflow, [{
        'name': InjectYumReposPlugin.key,
        'args': {'target': KOJI_TARGET},
    }])

    runner.run()

    log_msg = "Skipping plugin, from scratch stage(s) can't add repos"
    assert log_msg in caplog.text


@pytest.mark.parametrize(
    'configure_ca_bundle,inherited_user,include_koji_repo,'
    'repos,dockerfile_content,expected_final_dockerfile',
    # repos: [
    #   (repofile_url, repofile_content, expected_final_repofile)
    # ]
    [
        # normal simplest image build
        [
            True, '', False,
            [
                (
                    'http://repos.host/custom.repo',
                    dedent('''\
                    [new-packages]
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                    dedent(f'''\
                    [new-packages]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                ),
            ],
            dedent('''\
            FROM fedora:33
            RUN dnf update -y
            '''),
            dedent(f'''\
            FROM fedora:33
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            RUN dnf update -y
            RUN rm -f '/etc/yum.repos.d/custom-{{}}.repo'
            RUN rm -f /tmp/{CA_BUNDLE_PEM}
            '''),
        ],
        [
            True, '', True,
            [
                (
                    'http://repos.host/custom.repo',
                    dedent('''\
                    [new-packages]
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                    dedent(f'''\
                    [new-packages]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                ),
            ],
            dedent('''\
            FROM fedora:33
            RUN dnf update -y
            '''),
            dedent(f'''\
            FROM fedora:33
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            RUN dnf update -y
            RUN rm -f '/etc/yum.repos.d/target-bd4b1.repo' '/etc/yum.repos.d/custom-{{}}.repo'
            RUN rm -f /tmp/{CA_BUNDLE_PEM}
            '''),
        ],

        # a multi-stage build, every non-scratch stage should be handled
        # every repo inside a repofile should have sslcacert set
        [
            True, '', False,
            [
                (
                    'http://odcs.example.com/Temporary/odcs-1234.repo',
                    dedent('''\
                    [odcs-1234]
                    name=repo1
                    baseurl=http://odcs.example.com/Temporary/$basearch/os

                    [updates]
                    name=Updates
                    baseurl=http://repos.host/updates.repo
                    '''),
                    dedent(f'''\
                    [odcs-1234]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://odcs.example.com/Temporary/$basearch/os

                    [updates]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=Updates
                    baseurl=http://repos.host/updates.repo
                    '''),
                ),
            ],
            dedent('''\
            FROM fedora:33
            RUN dnf update -y
            FROM scratch
            RUN touch /tmp/hello.txt
            FROM fedora:33
            RUN echo hello
            '''),
            dedent(f'''\
            FROM fedora:33
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            RUN dnf update -y
            FROM scratch
            RUN touch /tmp/hello.txt
            FROM fedora:33
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            RUN echo hello
            RUN rm -f '/etc/yum.repos.d/odcs-1234-{{}}.repo'
            RUN rm -f /tmp/{CA_BUNDLE_PEM}
            '''),
        ],
        [
            True, '', True,
            [
                (
                    'http://odcs.example.com/Temporary/odcs-1234.repo',
                    dedent('''\
                    [odcs-1234]
                    name=repo1
                    baseurl=http://odcs.example.com/Temporary/$basearch/os

                    [updates]
                    name=Updates
                    baseurl=http://repos.host/updates.repo
                    '''),
                    dedent(f'''\
                    [odcs-1234]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://odcs.example.com/Temporary/$basearch/os

                    [updates]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=Updates
                    baseurl=http://repos.host/updates.repo
                    '''),
                ),
            ],
            dedent('''\
            FROM fedora:33
            RUN dnf update -y
            FROM scratch
            RUN touch /tmp/hello.txt
            FROM fedora:33
            RUN echo hello
            '''),
            dedent(f'''\
            FROM fedora:33
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            RUN dnf update -y
            FROM scratch
            RUN touch /tmp/hello.txt
            FROM fedora:33
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            RUN echo hello
            RUN rm -f '/etc/yum.repos.d/target-bd4b1.repo' '/etc/yum.repos.d/odcs-1234-{{}}.repo'
            RUN rm -f /tmp/{CA_BUNDLE_PEM}
            '''),
        ],

        # multi-stage build based on scratch at last
        # The last scratch stage should not have cleanup instructions
        [
            True, '', False,
            [
                (
                    'http://repos.host/custom.repo',
                    dedent("""
                    [new-packages]
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    """),
                    dedent(f"""
                    [new-packages]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    """),
                ),
            ],
            dedent("""\
            FROM golang:1.9 AS builder1
            USER grahamchapman
            RUN build /spam/eggs
            FROM scratch
            USER somebody
            RUN build /somebody
            FROM jdk:1.8 AS builder2
            USER ericidle
            RUN yum -y update
            FROM scratch
            USER for_scratch
            RUN yum install python
            """),
            dedent(f"""\
            FROM golang:1.9 AS builder1
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            USER grahamchapman
            RUN build /spam/eggs
            FROM scratch
            USER somebody
            RUN build /somebody
            FROM jdk:1.8 AS builder2
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            USER ericidle
            RUN yum -y update
            FROM scratch
            USER for_scratch
            RUN yum install python
            """),
        ],
        [
            True, '', True,
            [
                (
                    'http://repos.host/custom.repo',
                    dedent("""
                    [new-packages]
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    """),
                    dedent(f"""
                    [new-packages]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    """),
                ),
            ],
            dedent("""\
            FROM golang:1.9 AS builder1
            USER grahamchapman
            RUN build /spam/eggs
            FROM scratch
            USER somebody
            RUN build /somebody
            FROM jdk:1.8 AS builder2
            USER ericidle
            RUN yum -y update
            FROM scratch
            USER for_scratch
            RUN yum install python
            """),
            dedent(f"""\
            FROM golang:1.9 AS builder1
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            USER grahamchapman
            RUN build /spam/eggs
            FROM scratch
            USER somebody
            RUN build /somebody
            FROM jdk:1.8 AS builder2
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            USER ericidle
            RUN yum -y update
            FROM scratch
            USER for_scratch
            RUN yum install python
            """),
        ],

        # Respect the USER from the image inspection data
        [
            True, 'johncleese', False,
            [
                (
                    'http://repos.host/custom.repo',
                    dedent('''\
                    [new-packages]
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                    dedent(f'''\
                    [new-packages]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                ),
            ],
            dedent("""\
            FROM golang:1.9 AS builder1
            RUN build /spam/eggs
            FROM base
            COPY --from=builder1 /some/stuff /bin/spam
            """),
            dedent(f"""\
            FROM golang:1.9 AS builder1
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            RUN build /spam/eggs
            FROM base
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            COPY --from=builder1 /some/stuff /bin/spam
            USER root
            RUN rm -f '/etc/yum.repos.d/custom-{{}}.repo'
            RUN rm -f /tmp/{CA_BUNDLE_PEM}
            USER johncleese
            """),
        ],

        # Respect the USER from the inpsection data even if USER is set in previous build stage.
        [
            True, 'johncleese', False,
            [
                (
                    'http://repos.host/custom.repo',
                    dedent('''\
                    [new-packages]
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                    dedent(f'''\
                    [new-packages]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                ),
            ],
            dedent("""\
            FROM golang:1.9 AS builder1
            USER grahamchapman
            RUN build /spam/eggs
            FROM base
            COPY --from=builder1 /some/stuff /bin/spam
            """),
            dedent(f"""\
            FROM golang:1.9 AS builder1
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            USER grahamchapman
            RUN build /spam/eggs
            FROM base
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            COPY --from=builder1 /some/stuff /bin/spam
            USER root
            RUN rm -f '/etc/yum.repos.d/custom-{{}}.repo'
            RUN rm -f /tmp/{CA_BUNDLE_PEM}
            USER johncleese
            """),
        ],

        # Multiple repourls
        [
            True, '', False,
            [
                (
                    'http://repos.host/custom.repo',
                    dedent('''\
                    [new-packages]
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                    dedent(f'''\
                    [new-packages]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                ),
                (
                    'http://repos.host/custom-2.repo',
                    dedent('''\
                    [new-packages]
                    name=repo1
                    baseurl=http://repos.pulphost/latest/$basearch/os
                    '''),
                    dedent(f'''\
                    [new-packages]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://repos.pulphost/latest/$basearch/os
                    '''),
                ),
            ],
            dedent('''\
            FROM fedora:33
            RUN dnf update -y
            '''),
            dedent(f'''\
            FROM fedora:33
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            RUN dnf update -y
            RUN rm -f '/etc/yum.repos.d/custom-{{}}.repo' '/etc/yum.repos.d/custom-2-{{}}.repo'
            RUN rm -f /tmp/{CA_BUNDLE_PEM}
            '''),
        ],

        # No repourls, Dockerfile should have no change.
        [
            True, '', False,
            [],
            'FROM fedora:33\nRUN dnf update -y\n',
            'FROM fedora:33\nRUN dnf update -y\n',
        ],

        # Dockerfile contains continuous lines
        [
            True, '', False,
            [
                (
                    'http://repos.host/custom.repo',
                    dedent('''\
                    [new-packages]
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                    dedent(f'''\
                    [new-packages]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                ),
            ],
            '''\
FROM fedora
RUN yum install -y httpd \
                   uwsgi
''',
            dedent(f'''\
            FROM fedora
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            RUN yum install -y httpd                    uwsgi
            RUN rm -f '/etc/yum.repos.d/custom-{{}}.repo'
            RUN rm -f /tmp/{CA_BUNDLE_PEM}
            '''),
        ],

        # builder_ca_bundle is optional. When not set, the plugin should work.
        [
            False, '', False,
            [
                (
                    'http://repos.host/custom.repo',
                    dedent('''\
                    [new-packages]
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                    dedent('''\
                    [new-packages]
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                ),
            ],
            dedent('''\
            FROM fedora:33
            RUN dnf update -y
            '''),
            dedent('''\
            FROM fedora:33
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            RUN dnf update -y
            RUN rm -f '/etc/yum.repos.d/custom-{}.repo'
            '''),

        ],

        # Reset the USER found from the last stage properly.
        # `USER 1001` should be reset after the removal commands
        [
            True, '', False,
            [
                (
                    'http://repos.host/custom.repo',
                    dedent('''\
                    [new-packages]
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                    dedent(f'''\
                    [new-packages]
                    sslcacert=/tmp/{CA_BUNDLE_PEM}
                    name=repo1
                    baseurl=http://repo.host/latest/$basearch/os
                    '''),
                ),
            ],
            dedent('''\
            FROM base
            RUN gcc main.c
            FROM fedora:33
            USER 1001
            WORKDIR /src
            '''),
            dedent(f'''\
            FROM base
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            RUN gcc main.c
            FROM fedora:33
            ADD {CA_BUNDLE_PEM} /tmp/{CA_BUNDLE_PEM}
            ADD atomic-reactor-repos/* /etc/yum.repos.d/
            USER 1001
            WORKDIR /src
            USER root
            RUN rm -f '/etc/yum.repos.d/custom-{{}}.repo'
            RUN rm -f /tmp/{CA_BUNDLE_PEM}
            USER 1001
            '''),
        ],
    ]
)
@responses.activate
def test_inject_repos(configure_ca_bundle, inherited_user, include_koji_repo, repos,
                      dockerfile_content, expected_final_dockerfile, workflow, build_dir):
    platforms = ['x86_64', 'ppc64le']
    yum_repourls = {}
    for platform in platforms:
        yum_repourls[platform] = [url for url, _, _ in repos]
    workflow = prepare(workflow, build_dir, inherited_user, dockerfile_content,
                       include_koji_repo=include_koji_repo, platforms=platforms,
                       yum_repourls=yum_repourls)
    workflow.conf.conf['yum_repo_allowed_domains'] = ['odcs.example.com', 'repos.host']
    if configure_ca_bundle:
        workflow.conf.conf['builder_ca_bundle'] = BUILDER_CA_BUNDLE

    # Ensure the ca_bundle PEM file is copied into build context
    flexmock(shutil).should_receive('copyfile').with_args(
        BUILDER_CA_BUNDLE,
        (workflow.build_dir.any_platform.path / CA_BUNDLE_PEM))
    flexmock(shutil).should_receive('copyfile').with_args(
        BUILDER_CA_BUNDLE,
        (workflow.build_dir.path / 'x86_64' / CA_BUNDLE_PEM))

    for repofile_url, repofile_content, _ in repos:
        responses.add(responses.GET, repofile_url, body=repofile_content)

    PreBuildPluginsRunner(workflow, [
        {
            'name': InjectYumReposPlugin.key,
            'args': {'target': KOJI_TARGET},
        },
    ]).run()

    # Ensure Dockerfile is update correctly
    hashes = [sha256sum(repofile_url, abbrev_len=5) for repofile_url, _, _ in repos]
    expected = expected_final_dockerfile.format(*hashes)
    assert expected == workflow.build_dir.any_platform.dockerfile.content

    # Ensure the repofile is updated correctly as well
    for repofile_url, _, expected_final_repofile in repos:
        yum_repo = YumRepo(repofile_url)
        repos_path = workflow.build_dir.any_platform.path / RELATIVE_REPOS_PATH / yum_repo.filename
        updated_repos = repos_path.read_text('utf-8')
        assert expected_final_repofile == updated_repos


@pytest.mark.parametrize('parent_images', [True, False])
@pytest.mark.parametrize('base_from_scratch', [True, False])
@pytest.mark.parametrize(('target', 'expect_success'), [
    (KOJI_TARGET, True),
    (KOJI_TARGET_NO_INFO, False),
    (KOJI_TARGET_BROKEN_TAG, False),
    (KOJI_TARGET_BROKEN_REPO, False)])
@pytest.mark.parametrize(('root',
                          'koji_ssl_certs',
                          'expected_string',
                          'proxy'), [
    # Plain http repo
    ('http://example.com',
     False,
     None,
     None),
    # Plain http repo with proxy
    ('http://example.com',
     False,
     None,
     'http://proxy.example.com'),
    # https with no cert available
    ('https://nosuchwebsiteforsure.com',
     False,
     'sslverify=0',
     None),
    # https with no cert available
    ('https://nosuchwebsiteforsure.com',
     False,
     'sslverify=0',
     'http://proxy.example.com'),
    # https with a cert for authentication
    ('https://nosuchwebsiteforsure.com',
     True,
     'sslverify=0',
     'http://proxy.example.com'),
])
def test_include_koji(workflow, build_dir, caplog, parent_images, base_from_scratch, target,
                      expect_success, root, koji_ssl_certs, expected_string, proxy):
    prepare(workflow, build_dir, include_koji_repo=True, koji_ssl_certs=koji_ssl_certs,
            yum_proxy=proxy, root_url=root)
    dockerfile_images = []
    if parent_images:
        dockerfile_images.append('parent_image:latest')
    if base_from_scratch:
        dockerfile_images.append('scratch')
    workflow.dockerfile_images = DockerfileImages(dockerfile_images)

    args = {'target': target}

    runner = PreBuildPluginsRunner(workflow, [{
        'name': InjectYumReposPlugin.key,
        'args': args,
    }])

    if target == KOJI_TARGET_NO_INFO and parent_images:
        with pytest.raises(PluginFailedException) as exc:
            runner.run()
        assert f"Provided target '{target}' doesn't exist!" in str(exc.value)
        assert f"provided target '{target}' doesn't exist" in caplog.text
    else:
        runner.run()

    if not parent_images:
        log_msg = "Skipping plugin, from scratch stage(s) can't add repos"
        assert log_msg in caplog.text
        return
    if not expect_success:
        return

    if proxy:
        assert f"Setting yum proxy to {proxy}" in caplog.text

    if koji_ssl_certs:
        for file_path, expected in [(workflow.koji_session.cert_path, 'cert'),
                                    (workflow.koji_session.serverca_path, 'serverca')]:
            assert os.path.isfile(file_path)
            with open(file_path, 'r') as fd:
                assert fd.read() == expected

    repos_path = workflow.build_dir.any_platform.path / RELATIVE_REPOS_PATH
    repofile = 'target-?????.repo'
    files = os.listdir(repos_path)
    assert len(files) == 1
    assert fnmatch(next(iter(files)), repofile)
    with open(repos_path / files[0], 'r') as f:
        content = f.read()
    assert content.startswith("[atomic-reactor-koji-target-target]\n")
    assert "gpgcheck=0\n" in content
    assert "enabled=1\n" in content
    assert "name=atomic-reactor-koji-target-target\n" in content
    assert "baseurl=%s/repos/tag/2/$basearch\n" % root in content

    if proxy:
        assert "proxy=%s" % proxy in content

    if expected_string:
        assert expected_string in content


@pytest.mark.parametrize('target, include_repo', [
    ('target', False),
    ('target', True),
    (None, False),
    (None, True),
])
def test_include_koji_without_target(workflow, build_dir, caplog, target, include_repo):
    prepare(workflow, build_dir, include_koji_repo=include_repo)
    args = {'target': target}

    add_koji_map_in_workflow(workflow, hub_url='', root_url='http://example.com')

    runner = PreBuildPluginsRunner(workflow, [{
        'name': InjectYumReposPlugin.key,
        'args': args,
    }])

    runner.run()

    if not include_repo or not target:
        if not include_repo:
            log_msg = f"'include_koji_repo parameter is set to '{include_repo}', " \
                      f"not including koji repo"
        else:
            log_msg = 'no target provided, not adding koji repo'
    else:
        log_msg = "injected yum repo: /etc/yum.repos.d/target-bd4b1.repo for 'x86_64' platform"
    assert log_msg in caplog.text


@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
def test_no_repourls(inject_proxy, workflow, build_dir):
    workflow = prepare(workflow, build_dir, yum_repourls={'x86_64': []})
    runner = PreBuildPluginsRunner(workflow, [{
        'name': InjectYumReposPlugin.key,
        'args': {'inject_proxy': inject_proxy}}])
    runner.run()
    assert InjectYumReposPlugin.key is not None
    assert not (workflow.build_dir.any_platform.path / RELATIVE_REPOS_PATH).exists()


@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
@pytest.mark.parametrize(('repourl', 'repo_filename'),
                         [
                             ('http://example.com/example%20repo.repo', 'example repo-a8b44.repo'),
                         ])
@responses.activate
def test_single_repourl(workflow, build_dir, inject_proxy, repourl, repo_filename):
    workflow = prepare(workflow, build_dir, yum_repourls={'x86_64': [repourl]})
    repo_content = '''[repo]\n'''
    responses.add(responses.GET, repourl, body=repo_content)
    runner = PreBuildPluginsRunner(workflow, [{
        'name': InjectYumReposPlugin.key,
        'args': {'inject_proxy': inject_proxy}}])
    runner.run()

    repos_path = workflow.build_dir.any_platform.path / RELATIVE_REPOS_PATH
    files = os.listdir(repos_path)
    assert len(files) == 1
    assert fnmatch(next(iter(files)), repo_filename)
    with open(repos_path / files[0], 'r') as f:
        content = f.read()
    if inject_proxy:
        assert 'proxy = %s\n\n' % inject_proxy in content
    else:
        assert 'proxy' not in content


@pytest.mark.parametrize('base_from_scratch', [True, False])
@pytest.mark.parametrize('parent_images', [True, False])
@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
@pytest.mark.parametrize(('repos', 'filenames'), (
    (['http://example.com/a/b/c/myrepo.repo', 'http://example.com/repo-2.repo'],
     ['myrepo-b9003.repo', 'repo-2-e5f47.repo']),
    (['http://example.com/spam/myrepo.repo', 'http://example.com/bacon/myrepo.repo'],
     ['myrepo-91be9.repo', 'myrepo-c8e02.repo']),
))
@responses.activate
def test_multiple_repourls(workflow, build_dir, caplog, base_from_scratch, parent_images,
                           inject_proxy, repos, filenames):
    workflow = prepare(workflow, build_dir, yum_repourls={'x86_64': repos})

    dockerfile_images = []
    if parent_images:
        dockerfile_images.append('parent_image:latest')
    if base_from_scratch:
        dockerfile_images.append('scratch')
    workflow.dockerfile_images = DockerfileImages(dockerfile_images)
    repo_content = '''[repo]\n'''

    for repofile_url in repos:
        responses.add(responses.GET, repofile_url, body=repo_content)
    runner = PreBuildPluginsRunner(workflow, [{
        'name': InjectYumReposPlugin.key,
        'args': {'inject_proxy': inject_proxy}}])
    runner.run()

    repos_path = workflow.build_dir.any_platform.path / RELATIVE_REPOS_PATH

    if not parent_images:
        assert InjectYumReposPlugin.key is not None
        assert not repos_path.exists()
    else:
        files = os.listdir(repos_path)
        assert len(files) == 2
        for filename in filenames:
            with open(repos_path / filename, 'r') as f:
                content = f.read()
            if inject_proxy:
                assert 'proxy = %s\n\n' % inject_proxy in content
            else:
                assert 'proxy' not in content


@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
@responses.activate
def test_single_repourl_no_suffix(inject_proxy, workflow, build_dir):
    repofile_url = 'http://example.com/example%20repo'
    workflow = prepare(workflow, build_dir, yum_repourls={'x86_64': [repofile_url]})
    repo_content = '''[repo]\n'''

    responses.add(responses.GET, repofile_url, body=repo_content)
    runner = PreBuildPluginsRunner(workflow, [{
        'name': InjectYumReposPlugin.key,
        'args': {'inject_proxy': inject_proxy}}])
    runner.run()

    repo_filename = 'example repo-?????.repo'
    repos_path = workflow.build_dir.any_platform.path / RELATIVE_REPOS_PATH
    files = os.listdir(repos_path)
    assert len(files) == 1
    assert fnmatch(next(iter(files)), repo_filename)
    with open(repos_path / files[0], 'r') as f:
        content = f.read()
    if inject_proxy:
        assert 'proxy = %s\n\n' % inject_proxy in content
    else:
        assert 'proxy' not in content


@pytest.mark.parametrize('inject_proxy', [None, 'http://proxy.example.com'])
@pytest.mark.parametrize(('repos', 'patterns'), (
    (['http://example.com/a/b/c/myrepo', 'http://example.com/repo-2.repo'],
     ['myrepo-?????.repo', 'repo-2-?????.repo']),
    (['http://example.com/a/b/c/myrepo', 'http://example.com/repo-2'],
     ['myrepo-?????.repo', 'repo-2-?????.repo']),
    (['http://example.com/a/b/c/myrepo.repo', 'http://example.com/repo-2'],
     ['myrepo-?????.repo', 'repo-2-?????.repo']),
    (['http://example.com/spam/myrepo.repo', 'http://example.com/bacon/myrepo'],
     ['myrepo-?????.repo', 'myrepo-?????.repo']),
    (['http://example.com/a/b/c/myrepo.repo?blab=bla', 'http://example.com/a/b/c/repo-2?blab=bla'],
     ['myrepo-?????.repo', 'repo-2-?????.repo']),
    (['http://example.com/a/b/c/myrepo', 'http://example.com/a/b/c/myrepo.repo'],
     ['myrepo-?????.repo', 'myrepo-?????.repo']),
))
@responses.activate
def test_multiple_repourls_no_suffix(workflow, build_dir, inject_proxy, repos, patterns):
    workflow = prepare(workflow, build_dir, yum_repourls={'x86_64': repos})
    repo_content = '''[repo]\n'''

    for repofile_url in repos:
        responses.add(responses.GET, repofile_url, body=repo_content)
    runner = PreBuildPluginsRunner(workflow, [{
        'name': InjectYumReposPlugin.key,
        'args': {'inject_proxy': inject_proxy}}])
    runner.run()

    repos_path = workflow.build_dir.any_platform.path / RELATIVE_REPOS_PATH
    files = os.listdir(repos_path)
    assert len(files) == 2

    for pattern in patterns:
        for filename in files:
            with open(repos_path / files[0], 'r') as f:
                content = f.read()
            if fnmatch(filename, pattern):
                if inject_proxy:
                    assert 'proxy = %s\n\n' % inject_proxy in content
                else:
                    assert 'proxy' not in content


def test_invalid_repourl(workflow, build_dir):
    """Plugin should raise RuntimeError with repo details when invalid URL
       is used
    """
    wrong_repo_url = "http://example.com/nope/repo"
    workflow = prepare(workflow, build_dir, yum_repourls={'x86_64': [wrong_repo_url]})
    runner = PreBuildPluginsRunner(workflow, [{
        'name': InjectYumReposPlugin.key,
        'args': {'inject_proxy': None}}])

    (flexmock(YumRepo)
        .should_receive('fetch')
        .and_raise(Exception, 'Oh noes, repo is not working!'))

    with pytest.raises(PluginFailedException) as exc:
        runner.run()

    msg = "Failed to fetch yum repo {repo}".format(repo=wrong_repo_url)
    assert msg in str(exc.value)


@pytest.mark.parametrize('scratch', [True, False])
@pytest.mark.parametrize(('allowed_domains', 'repo_urls', 'will_raise'), (
    (None, ['http://example.com/repo'], False),
    ([], ['http://example.com/repo'], False),
    (['foo.redhat.com', 'bar.redhat.com'], ['http://foo.redhat.com/some/repo'], False),
    (['foo.redhat.com', 'bar.redhat.com'], ['http://bar.redhat.com/some/repo'], False),
    (['foo.redhat.com', 'bar.redhat.com'],
     ['http://foo.redhat.com/some/repo', 'http://bar.redhat.com/some/repo'], False),
    (['foo.redhat.com', 'bar.redhat.com'], ['http://pre.foo.redhat.com/some/repo'], True),
    (['foo.redhat.com', 'bar.redhat.com'], ['http://foo.redhat.com.post/some/repo'], True),
    (['foo.redhat.com', 'bar.redhat.com'], ['http://foor.redhat.com.post/some/repo'], True),
    (['foo.redhat.com', 'bar.redhat.com'], ['http://baar.redhat.com.post/some/repo'], True),
    (['foo.redhat.com', 'bar.redhat.com'],
     ['http://foo.redhat.com/some/repo', 'http://wrong.bar.redhat.com/some/repo'], True),
    (['foo.redhat.com', 'bar.redhat.com'],
     ['http://wrong.foo.redhat.com/some/repo', 'http://bar.redhat.com/some/repo'], True),
    (['foo.redhat.com', 'bar.redhat.com'],
     ['http://wrong.foo.redhat.com/some/repo', 'http://wrong.bar.redhat.com/some/repo'], True),
))
@responses.activate
def test_allowed_domains(build_dir, allowed_domains, repo_urls, will_raise, scratch, workflow):
    workflow = prepare(workflow, build_dir, yum_repourls={'x86_64': repo_urls})
    workflow.user_params['scratch'] = scratch
    reactor_map = {'version': 1}

    if allowed_domains is not None:
        reactor_map['yum_repo_allowed_domains'] = allowed_domains

    workflow.conf.conf = reactor_map

    for repofile_url in repo_urls:
        responses.add(responses.GET, repofile_url)

    runner = PreBuildPluginsRunner(workflow, [{
        'name': InjectYumReposPlugin.key,
        'args': {'inject_proxy': None}}])

    if will_raise and not scratch:
        with pytest.raises(PluginFailedException) as exc:
            runner.run()

        msg = 'Errors found while checking yum repo urls'
        assert msg in str(exc.value)
    else:
        runner.run()
