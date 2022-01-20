# -*- coding: utf-8 -*-
"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from textwrap import dedent
import os.path
import pytest
import responses
import shutil
from atomic_reactor.constants import RELATIVE_REPOS_PATH, INSPECT_CONFIG, DOCKERFILE_FILENAME
from atomic_reactor.plugin import PluginFailedException, PreBuildPluginsRunner
from atomic_reactor.plugins.pre_add_yum_repo_by_url import AddYumRepoByUrlPlugin
from atomic_reactor.plugins.pre_inject_yum_repo import InjectYumRepoPlugin
from atomic_reactor.util import df_parser, sha256sum, DockerfileImages
from atomic_reactor.utils.yum import YumRepo
from flexmock import flexmock
from tests.stubs import StubSource

BUILDER_CA_BUNDLE = '/path/to/tls-ca-bundle.pem'
CA_BUNDLE_PEM = os.path.basename(BUILDER_CA_BUNDLE)


pytestmark = pytest.mark.usefixtures('user_params')


def prepare(workflow, df_path, df_dir, inherited_user=''):
    workflow.source = StubSource()
    inspect_data = {INSPECT_CONFIG: {'User': inherited_user}}
    flexmock(workflow, df_path=df_path)
    flexmock(workflow.imageutil).should_receive('base_image_inspect').and_return(inspect_data)
    workflow.df_dir = df_dir
    workflow.data.dockerfile_images = DockerfileImages(df_parser(df_path).parent_images)
    return workflow


def test_no_base_image_in_dockerfile(workflow, source_dir):
    dockerfile = source_dir.joinpath(DOCKERFILE_FILENAME)
    dockerfile.touch()

    workflow = prepare(workflow, str(dockerfile), str(source_dir))
    workflow.data.files = {'/etc/yum.repos.d/foo.repo': 'repo'}

    runner = PreBuildPluginsRunner(workflow, [{
        'name': InjectYumRepoPlugin.key,
        'args': {},
    }])

    with pytest.raises(PluginFailedException) as exc:
        runner.run()
    assert "No FROM line in Dockerfile" in str(exc.value)


@pytest.mark.parametrize(
    'configure_ca_bundle,inherited_user,'
    'repos,dockerfile_content,expected_final_dockerfile',
    # repos: [
    #   (repofile_url, repofile_content, expected_final_repofile)
    # ]
    [
        # normal simplest image build
        [
            True, '',
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

        # a multi-stage build, every non-scratch stage should be handled
        # every repo inside a repofile should have sslcacert set
        [
            True, '',
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

        # multi-stage build based on scratch at last
        # The last scratch stage should not have cleanup instructions
        [
            True, '',
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
            True, 'johncleese',
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
            True, 'johncleese',
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
            True, '',
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
            True, '',
            [],
            'FROM fedora:33\nRUN dnf update -y\n',
            'FROM fedora:33\nRUN dnf update -y\n',
        ],

        # Dockerfile contains continuous lines
        [
            True, '',
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
            False, '',
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
            True, '',
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
def test_inject_repos(configure_ca_bundle, inherited_user,
                      repos, dockerfile_content, expected_final_dockerfile,
                      workflow, source_dir):
    dockerfile = source_dir.joinpath(DOCKERFILE_FILENAME)
    dockerfile.write_text(dockerfile_content, "utf-8")

    workflow = prepare(workflow, str(dockerfile), str(source_dir), inherited_user)

    config = {
        'version': 1,
        # Ensure the AddYumRepoByUrlPlugin plugin is able to run
        'yum_repo_allowed_domains': ['odcs.example.com', 'repos.host'],
    }
    if configure_ca_bundle:
        config['builder_ca_bundle'] = BUILDER_CA_BUNDLE
    workflow.conf.conf = config

    # Ensure the ca_bundle PEM file is copied into build context
    flexmock(shutil).should_receive('copyfile').with_args(
        BUILDER_CA_BUNDLE,
        str(source_dir.joinpath(CA_BUNDLE_PEM)),
    )

    for repofile_url, repofile_content, _ in repos:
        responses.add(responses.GET, repofile_url, body=repofile_content)

    PreBuildPluginsRunner(workflow, [
        {
            'name': AddYumRepoByUrlPlugin.key,
            'args': {'repourls': [url for url, _, _ in repos]},
        },
        {
            'name': InjectYumRepoPlugin.key,
            'args': {},
        },
    ]).run()

    # Ensure Dockerfile is update correctly
    hashes = [sha256sum(repofile_url, abbrev_len=5) for repofile_url, _, _ in repos]
    expected = expected_final_dockerfile.format(*hashes)
    assert expected == df_parser(str(dockerfile)).content

    # Ensure the repofile is updated correctly as well
    for repofile_url, _, expected_final_repofile in repos:
        yum_repo = YumRepo(repofile_url)
        updated_repos = source_dir.joinpath(
            RELATIVE_REPOS_PATH, yum_repo.filename
        ).read_text('utf-8')
        assert expected_final_repofile == updated_repos
