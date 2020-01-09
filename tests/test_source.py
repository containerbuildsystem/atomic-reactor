from __future__ import absolute_import

import copy
import os

import pytest
import jsonschema

from atomic_reactor.constants import REPO_CONTAINER_CONFIG
from atomic_reactor.source import (
    Source,
    SourceConfig,
    GitSource,
    PathSource,
    get_source_instance_for,
    DummySource,
)
import atomic_reactor.source
from jsonschema import ValidationError

from tests.constants import DOCKERFILE_GIT, DOCKERFILE_OK_PATH, SOURCE_CONFIG_ERROR_PATH
from tests.util import requires_internet
import flexmock


class TestSource(object):
    def test_creates_tmpdir_if_not_passed(self):
        s = Source('git', 'foo')
        assert os.path.exists(s.tmpdir)


@requires_internet
class TestGitSource(object):
    def test_checks_out_repo(self):
        gs = GitSource('git', DOCKERFILE_GIT)
        assert os.path.exists(os.path.join(gs.path, '.git'))
        assert os.path.basename(gs.path) == 'docker-hello-world'
        assert gs.commit_id is not None
        assert len(gs.commit_id) == 40  # current git hashes are this long

        previous_commit_id = gs.commit_id
        gs.reset('HEAD~2')  # Go back two commits
        assert gs.commit_id is not None
        assert gs.commit_id != previous_commit_id
        assert len(gs.commit_id) == 40  # current git hashes are this long


class TestPathSource(object):
    def test_copies_target_dir(self, tmpdir):
        tmpdir.ensure('foo', 'bar', 'Dockerfile')
        ps = PathSource('path', 'file://' + os.path.join(str(tmpdir), 'foo'))
        path = ps.path
        assert os.path.isfile(os.path.join(path, 'bar', 'Dockerfile'))
        # make sure these are the same even on second access to ps.path/ps.get(),
        #  since second (and any subsequent) access does a bit different thing than the first one
        assert ps.get() == path


class TestGetSourceInstanceFor(object):
    @pytest.mark.parametrize('source, expected', [
        ({'provider': 'git', 'uri': 'foo'}, GitSource),
        ({'provider': 'path', 'uri': 'foo'}, PathSource),
    ])
    def test_recognizes_correct_provider(self, source, expected):
        assert isinstance(get_source_instance_for(source), expected)

    @pytest.mark.parametrize('source, error', [
        ({'provider': 'xxx', 'uri': 'foo'}, 'unknown source provider "xxx"'),
        ({'provider': 'git'}, '"source" must contain "uri" key'),
        ({'uri': 'path'}, '"source" must contain "provider" key'),
        (None, '"source" must be a dict'),
    ])
    def test_errors(self, source, error):
        with pytest.raises(ValueError) as ex:
            get_source_instance_for(source)

        assert str(ex.value) == error

    def test_retrieves_source_config_file(self):
        s = get_source_instance_for({'provider': 'path', 'uri': DOCKERFILE_OK_PATH})
        assert s.config
        assert s.config.image_build_method == 'imagebuilder'

    def test_sourceconfig_bad_build_method(self, monkeypatch):
        s = get_source_instance_for({'provider': 'path', 'uri': DOCKERFILE_OK_PATH})
        flexmock(atomic_reactor.source, CONTAINER_BUILD_METHODS=[])
        with pytest.raises(AssertionError):
            s.config    # pylint: disable=pointless-statement; is a property

    def test_broken_source_config_file(self):
        s = get_source_instance_for({'provider': 'path', 'uri': SOURCE_CONFIG_ERROR_PATH})
        with pytest.raises(ValidationError):
            s.config    # pylint: disable=pointless-statement; is a property


class TestSourceConfigSchemaValidation(object):
    """Testing parsing of configuration file and schema validation.

    Related to class source.SourceConfig
    """
    SOURCE_CONFIG_EMPTY = {
        'autorebuild': {},
        'flatpak': None,
        'compose': None,
        'go': {}
    }

    def _create_source_config(self, tmpdir, yml_config):
        tmpdir_str = str(tmpdir)
        path = os.path.join(tmpdir_str, REPO_CONTAINER_CONFIG)
        # store container configuration into expected file
        with open(path, 'w') as f:
            f.write(yml_config)
            f.flush()

        return SourceConfig(tmpdir_str)

    @pytest.mark.parametrize('yml_config, attrs_updated', [
        (
            # empty config
            """\
            """,
            {'data': {}}
        ), (
            """\
            platforms:
              only: s390x
            """,
            {'data': {'platforms': {'only': 's390x'}}}
        ), (
            """\
            platforms:
              not: s390x
            """,
            {'data': {'platforms': {'not': 's390x'}}}
        ), (
            """\
            platforms:
              not: s390x
              only: s390x
            """,
            {'data': {'platforms': {'only': 's390x', 'not': 's390x'}}}
        ), (
            """\
            platforms:
              not:
               - s390x
              only:
               - s390x
            """,
            {'data': {'platforms': {'only': ['s390x'], 'not': ['s390x']}}}
        ), (
            """\
            platforms:
            """,
            {'data': {'platforms': None}}
        ), (
            """\
            autorebuild:
              from_latest: true
            """,
            {'autorebuild': {'from_latest': True}}
        ), (
            """\
            autorebuild:
            """,
            {}
        ), (
            """\
            flatpak:
              something: random
            """,
            {'flatpak': {'something': 'random'}}
        ), (
            """\
            flatpak:
            """,
            {}
        ), (
            """\
            compose:
              packages:
                - pkg1
              pulp_repos: true
              modules:
                - module1
              signing_intent: release
            """,
            {'compose': {
                'packages': ['pkg1'], 'pulp_repos': True,
                'modules': ['module1'], 'signing_intent': 'release'
            }}
        ), (
            """\
            compose:
              include_unpublished_pulp_repos: true
            """,
            {'compose': {'include_unpublished_pulp_repos': True}}
        ), (
            """\
            compose:
            """,
            {}

        ), (
            """\
            compose:
              inherit: true
            """,
            {'compose': {}, 'inherit': True}
        ), (
            """\
            go:
              modules:
                - module: example.com/go/package
            """,
            {'go': {'modules': [{'module': 'example.com/go/package'}]}}
        ), (
            """\
            go:
              modules:
                - module: example.com/go/package
                  archive: foo
                  path: bar
            """,
            {'go': {'modules': [{'module': 'example.com/go/package',
                                 'archive': 'foo', 'path': 'bar'}]}}
        ), (
            """\
            go:
              modules:
                - module: example.com/go/package
                - module: example.com/go/package2
            """,
            {'go': {'modules': [{'module': 'example.com/go/package'},
                                {'module': 'example.com/go/package2'}]}}
        ), (
            """\
            remote_source:
              repo: https://git.example.com/team/repo.git
              ref: b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
            """,
            {'remote_source': {
                'repo': 'https://git.example.com/team/repo.git',
                'ref': 'b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a',
            }}
        ), (
            """\
            remote_source:
              repo: https://git.example.com/team/repo.git
              ref: b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
              flags:
                - enable-confeti
            """,
            {'remote_source': {
                'repo': 'https://git.example.com/team/repo.git',
                'ref': 'b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a',
                'flags': ['enable-confeti'],
            }}
        ), (
            """\
            remote_source:
              repo: https://git.example.com/team/repo.git
              ref: b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
              pkg_managers:
                - gomod
            """,
            {'remote_source': {
                'repo': 'https://git.example.com/team/repo.git',
                'ref': 'b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a',
                'pkg_managers': ['gomod'],
            }}
        ),
    ])
    def test_valid_source_config(self, tmpdir, yml_config, attrs_updated):
        source_config = self._create_source_config(tmpdir, yml_config)
        assert source_config

        attrs_expected = copy.copy(self.SOURCE_CONFIG_EMPTY)
        attrs_expected.update(attrs_updated)
        for attr_name, value in attrs_expected.items():
            assert getattr(source_config, attr_name) == value

    @pytest.mark.parametrize('yml_config', [
        """\
        platforms: not_an_object
        """,

        """\
        platforms:
          undefined_attr: s390x
        """,

        """\
        autorebuild: not_an_object
        """,

        """\
        autorebuild:
          undefined_attr: something
        """,

        """\
        autorebuild:
          from_latest: not_a_boolean
        """,

        """\
        flatpak: not_an_object
        """,

        """\
        compose: not_an_object
        """,

        """\
        go: not_an_object
        """,

        """\
        go:
          extra_key: not_allowed
        """,

        """\
        go:
        """,

        """\
        compose:
          inherit: not_a_boolean
        """,

        """\
        remote_source: not_an_object
        """,

        """\
        remote_source:
          repo: https://git.example.com/team/repo.git
        """,

        """\
        remote_source:
          ref: b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
        """,

        """\
        remote_source:
          repo: https://git.example.com/team/repo.git
          # Hash too short
          ref: b55c00f45ec3dfee0c766cea3d395d6e21cc2e5
        """,

        """\
        remote_source:
          repo: https://git.example.com/team/repo.git
          # Hash too long
          ref: b55c00f45ec3dfee0c766cea3d395d6e21cc2e5ab
        """,

        """\
        remote_source:
          repo: https://git.example.com/team/repo.git
          # Hash contains non hex
          ref: z55c00f45ec3dfee0c766cea3d395d6e21cc2e5ab
        """,

        """\
        remote_source:
          repo: https://git.example.com/team/repo.git
          ref: b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
          extra_key: not_allowed
        """,

        """\
        remote_source:
          repo: https://git.example.com/team/repo.git
          ref: b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a
          pkg_managers:
            - gomod

        go:
          modules:
            - module: example.com/go/package
        """,
    ])
    def test_invalid_source_config_validation_error(self, tmpdir, yml_config):
        with pytest.raises(jsonschema.ValidationError):
            self._create_source_config(tmpdir, yml_config)


def test_dummy_source_dockerfile():
    """Test of DummySource used for source container builds

    Test if fake Dockerfile was properly injected to meet expectations of
    inner and core codebase
    """
    ds = DummySource(None, None)
    assert ds.get()
    assert os.path.exists(os.path.join(ds.get(), 'Dockerfile'))
