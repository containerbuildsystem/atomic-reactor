"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from atomic_reactor.util import ImageName
from atomic_reactor.inner import PushConf

try:
    # py3
    import configparser
except ImportError:
    # py2
    import ConfigParser as configparser

try:
    import dockpulp
except (ImportError, SyntaxError):
    dockpulp = None
else:
    from atomic_reactor.plugins.post_pulp_sync import (dockpulp_config,
                                                       PulpSyncPlugin)

from flexmock import flexmock
import pytest


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
class TestDockpulpConfig(object):
    def test_config(self):
        docker_registry = 'http://registry.example.com'
        cfp = configparser.SafeConfigParser()
        with dockpulp_config(docker_registry) as config:
            env = config.env
            cfp.read(config.name)

        assert cfp.has_section('registries')
        assert cfp.has_section('filers')
        assert cfp.has_section('pulps')

        registry = cfp.get('pulps', env)
        assert registry == docker_registry


class MockPulp(object):
    """
    Mock dockpulp.Pulp object
    """

    registry = 'pulp.example.com'

    def login(self, username, password):
        pass

    def set_certs(self, cer, key):
        pass

    def syncRepo(self, env, repo, config_file=None):
        pass

    def crane(self, repos, wait=True):
        pass


@pytest.mark.skipif(dockpulp is None,
                    reason='dockpulp module not available')
class TestPostPulpSync(object):
    @staticmethod
    def workflow(docker_repos):
        primary_images = []
        for tag in ['1.0-1', '1.0', 'latest']:
            primary_images.extend([ImageName(repo=repo, tag=tag)
                                   for repo in docker_repos])

        tag_conf = flexmock(primary_images=primary_images)
        push_conf = PushConf()
        return flexmock(tag_conf=tag_conf,
                        push_conf=push_conf)

    def test_auth_none(self):
        docker_registry = 'http://registry.example.com'
        docker_repository = 'prod/myrepository'
        env = 'pulp'
        plugin = PulpSyncPlugin(tasker=None,
                                workflow=self.workflow([docker_repository]),
                                pulp_registry_name=env,
                                docker_registry=docker_registry)

        mockpulp = MockPulp()
        (flexmock(mockpulp)
            .should_receive('login')
            .never())
        (flexmock(mockpulp)
            .should_receive('set_certs')
            .never())
        (flexmock(mockpulp)
            .should_receive('syncRepo')
            .with_args(object,
                       'prod-myrepository',  # pulp repository name
                       config_file=object)
            .and_return([{'id': 'prefix-prod-myrepository'}])  # repo id
            .once()
            .ordered())
        (flexmock(mockpulp)
            .should_receive('crane')
            .with_args(['prefix-prod-myrepository'],  # repo id
                       wait=True)
            .once()
            .ordered())
        (flexmock(dockpulp)
            .should_receive('Pulp')
            .with_args(env=env)
            .and_return(mockpulp))

        plugin.run()

    def test_auth_password(self):
        username = 'username'
        password = 'password'
        docker_registry = 'http://registry.example.com'
        docker_repository = 'prod/myrepository'
        env = 'pulp'
        plugin = PulpSyncPlugin(tasker=None,
                                workflow=self.workflow([docker_repository]),
                                pulp_registry_name=env,
                                docker_registry=docker_registry,
                                username=username,
                                password=password)

        mockpulp = MockPulp()
        (flexmock(mockpulp)
            .should_receive('login')
            .with_args(username, password)
            .once()
            .ordered())
        (flexmock(mockpulp)
            .should_receive('set_certs')
            .never())
        (flexmock(mockpulp)
            .should_receive('syncRepo')
            .with_args(object,
                       'prod-myrepository',  # pulp repository name
                       config_file=object)
            .and_return([{'id': 'prefix-prod-myrepository'}])  # repo id
            .once()
            .ordered())
        (flexmock(mockpulp)
            .should_receive('crane')
            .with_args(['prefix-prod-myrepository'],  # repo id
                       wait=True)
            .once()
            .ordered())
        (flexmock(dockpulp)
            .should_receive('Pulp')
            .with_args(env=env)
            .and_return(mockpulp))

        plugin.run()

    @pytest.mark.parametrize('cer_exists', [True, False])
    @pytest.mark.parametrize('key_exists', [True, False])
    @pytest.mark.parametrize('source_secret', [True, False])
    def test_auth_certs(self, tmpdir, cer_exists, key_exists, source_secret, monkeypatch):
        pulp_secret_path = str(tmpdir)
        cer = pulp_secret_path + '/pulp.cer'
        key = pulp_secret_path + '/pulp.key'
        if cer_exists:
            open(cer, 'w').close()

        if key_exists:
            open(key, 'w').close()

        if source_secret:
            monkeypatch.setenv('SOURCE_SECRET_PATH', pulp_secret_path)
            pulp_secret_path = None
        else:
            monkeypatch.delenv('SOURCE_SECRET_PATH', raising=False)

        docker_registry = 'http://registry.example.com'
        docker_repository = 'prod/myrepository'
        env = 'pulp'
        plugin = PulpSyncPlugin(tasker=None,
                                workflow=self.workflow([docker_repository]),
                                pulp_registry_name=env,
                                docker_registry=docker_registry,
                                pulp_secret_path=pulp_secret_path)

        mockpulp = MockPulp()
        (flexmock(mockpulp)
            .should_receive('login')
            .never())
        if cer_exists and key_exists:
            (flexmock(mockpulp)
                .should_receive('set_certs')
                .with_args(cer, key)
                .once()
                .ordered())
            (flexmock(mockpulp)
                .should_receive('syncRepo')
                .with_args(object,
                           'prod-myrepository',  # pulp repository name
                           config_file=object)
                .and_return([{'id': 'prefix-prod-myrepository'}])  # repo id
                .once()
                .ordered())
            (flexmock(mockpulp)
                .should_receive('crane')
                .with_args(['prefix-prod-myrepository'],  # repo id
                           wait=True)
                .once()
                .ordered())
        else:
            (flexmock(mockpulp)
                .should_receive('set_certs')
                .never())
            (flexmock(mockpulp)
                .should_receive('syncRepo')
                .never())
            (flexmock(mockpulp)
                .should_receive('crane')
                .never())

        (flexmock(dockpulp)
            .should_receive('Pulp')
            .with_args(env=env)
            .and_return(mockpulp))

        if cer_exists and key_exists:
            plugin.run()
        else:
            with pytest.raises(RuntimeError):
                plugin.run()

    @pytest.mark.parametrize('fail', [False, True])
    def test_dockpulp_loglevel(self, fail, caplog):
        loglevel = 3

        mockpulp = MockPulp()
        (flexmock(mockpulp)
            .should_receive('syncRepo')
            .and_return([{'id':''}]))
        flexmock(dockpulp).should_receive('Pulp').and_return(mockpulp)
        logger = flexmock()
        expectation = (logger
                       .should_receive('setLevel')
                       .with_args(loglevel)
                       .once())
        if fail:
            expectation.and_raise(ValueError)

        (flexmock(dockpulp)
            .should_receive('setup_logger')
            .and_return(logger)
            .once())

        plugin = PulpSyncPlugin(tasker=None,
                                workflow=self.workflow(['prod/myrepository']),
                                pulp_registry_name='pulp',
                                docker_registry='http://registry.example.com',
                                username='username', password='password',
                                dockpulp_loglevel=loglevel)

        plugin.run()

        errors = [record.getMessage() for record in caplog.records()
                  if record.levelname == 'ERROR']

        if fail:
            assert len(errors) >= 1
        else:
            assert not errors

    @pytest.mark.parametrize('already_exists', [False, True])
    def test_store_registry(self, already_exists):
        docker_registry = 'http://registry.example.com'
        docker_repository = 'prod/myrepository'
        env = 'pulp'
        workflow = self.workflow([docker_repository])

        mockpulp = MockPulp()
        (flexmock(mockpulp)
            .should_receive('login')
            .never())
        (flexmock(mockpulp)
            .should_receive('set_certs')
            .never())
        (flexmock(mockpulp)
            .should_receive('syncRepo')
            .with_args(object,
                       'prod-myrepository',  # pulp repository name
                       config_file=object)
            .and_return([{'id': 'prefix-prod-myrepository'}])  # repo id
            .once()
            .ordered())
        (flexmock(mockpulp)
            .should_receive('crane')
            .with_args(['prefix-prod-myrepository'],  # repo id
                       wait=True)
            .once()
            .ordered())
        (flexmock(dockpulp)
            .should_receive('Pulp')
            .with_args(env=env)
            .and_return(mockpulp))

        if already_exists:
            workflow.push_conf.add_pulp_registry(env, mockpulp.registry)

        plugin = PulpSyncPlugin(tasker=None,
                                workflow=workflow,
                                pulp_registry_name=env,
                                docker_registry=docker_registry)

        num_registries = len(workflow.push_conf.pulp_registries)
        assert num_registries == (1 if already_exists else 0)
        plugin.run()
        assert len(workflow.push_conf.pulp_registries) == 1

    def test_delete_not_implemented(self, caplog):
        """
        Should log an error (but not raise an exception) when
        delete_from_registry is True.
        """
        mockpulp = MockPulp()
        (flexmock(mockpulp)
            .should_receive('syncRepo')
            .and_return([{'id':''}]))
        flexmock(dockpulp).should_receive('Pulp').and_return(mockpulp)
        plugin = PulpSyncPlugin(tasker=None,
                                workflow=self.workflow(['prod/myrepository']),
                                pulp_registry_name='pulp',
                                docker_registry='http://registry.example.com',
                                delete_from_registry=True,
                                username='username', password='password')

        plugin.run()

        errors = [record.getMessage() for record in caplog.records()
                  if record.levelname == 'ERROR']

        assert [message for message in errors
                if 'not implemented' in message]
