"""Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Sync built image to pulp registry using Docker Registry HTTP API V2

Authentication is via a key and certificate in a secret which the
builder service account is allowed to mount:

$ oc secrets new pulp ./pulp.key ./pulp.cer
secrets/pulp
$ oc secrets add serviceaccount/builder secret/pulp --for=mount

In the BuildConfig for atomic-reactor, specify the secret in the
strategy's 'secrets' array, specifying a mount path:

"secrets": [{
  "secretSource": {
    "name": "pulp"
  },
  "mountPath": "/var/run/secrets/pulp"
}]

In the configuration for this plugin, specify the same path for
pulp_secret_path:

"pulp_sync": {
  "pulp_registry_name": ...,
  ...
  "pulp_secret_path": "/var/run/secrets/pulp"
}

"""

from __future__ import print_function, unicode_literals

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import ImageName
from contextlib import contextmanager
import dockpulp
import os
import re
from tempfile import NamedTemporaryFile


# let's silence warnings from dockpulp: there is one warning for every
# request which may result in tenths of messages: very annoying with
# "module", it just prints one warning -- this should balance security
# and UX
from warnings import filterwarnings
filterwarnings("module")


@contextmanager
def dockpulp_config(docker_registry, **kwargs):
    """
    Temporary dockpulp config pointing to the docker v2 registry

    :yields: NamedTemporaryFile instance
    """

    env = 'sync'
    template = """
[registries]
{sync}

[filers]
{sync}

[pulps]
{sync}
"""
    sync = '{env} = {url}'.format(env=env, url=docker_registry)
    with NamedTemporaryFile('wt', **kwargs) as config:
        config.write(template.format(sync=sync))
        config.flush()
        config.env = env
        yield config


class PulpSyncPlugin(PostBuildPlugin):
    key = "pulp_sync"
    is_allowed_to_fail = False

    CER = 'pulp.cer'
    KEY = 'pulp.key'

    def __init__(self, tasker, workflow,
                 pulp_registry_name,
                 docker_registry,
                 delete_from_registry=False,
                 pulp_secret_path=None,
                 username=None, password=None,
                 dockpulp_loglevel=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param pulp_registry_name: str, name of pulp registry to use,
               specified in /etc/dockpulp.conf
        :param docker_registry: str, URL of docker registry to sync from
        :param delete_from_registry: bool, whether to delete the image
               from the docker v2 registry after sync
        :param pulp_secret_path: path to pulp.cer and pulp.key
        :param username: pulp username, used in preference to
               certificate and key
        :param password: pulp password, used in preference to
               certificate and key
        """
        # call parent constructor
        super(PulpSyncPlugin, self).__init__(tasker, workflow)
        self.pulp_registry_name = pulp_registry_name
        self.docker_registry = docker_registry
        self.pulp_secret_path = pulp_secret_path
        self.username = username
        self.password = password

        if dockpulp_loglevel is not None:
            logger = dockpulp.setup_logger(dockpulp.log)
            try:
                logger.setLevel(dockpulp_loglevel)
            except (ValueError, TypeError) as ex:
                self.log.error("Can't set provided log level %r: %r",
                               dockpulp_loglevel, ex)

        if delete_from_registry:
            self.log.error("will not delete from registry as instructed: "
                           "not implemented")

    def set_auth(self, pulp):
        if self.username and self.password:
            # Use username and password if provided
            pulp.login(self.username, self.password)
        elif self.pulp_secret_path or 'SOURCE_SECRET_PATH' in os.environ:
            if self.pulp_secret_path is not None:
                path = self.pulp_secret_path
                self.log.info("using configured path %s for secrets", path)
            else:
                path = os.environ["SOURCE_SECRET_PATH"]
                self.log.info("SOURCE_SECRET_PATH=%s from environment", path)

            # Work out the pathnames for the certificate/key pair
            cer = os.path.join(path, self.CER)
            key = os.path.join(path, self.KEY)

            if not os.path.exists(cer):
                raise RuntimeError("Certificate does not exist")
            if not os.path.exists(key):
                raise RuntimeError("Key does not exist")

            # Tell dockpulp
            pulp.set_certs(cer, key)

    def run(self):
        pulp = dockpulp.Pulp(env=self.pulp_registry_name)
        self.set_auth(pulp)

        # We only want the hostname[:port]
        pulp_registry = re.sub(r'^https?://([^/]*)/?.*',
                               lambda m: m.groups()[0],
                               pulp.registry)

        # Store the registry URI in the push configuration
        self.workflow.push_conf.add_pulp_registry(self.pulp_registry_name,
                                                  pulp_registry)

        self.log.info("syncing from docker V2 registry %s",
                      self.docker_registry)

        images = []
        repos = {}  # pulp repo -> repo id
        with dockpulp_config(docker_registry=self.docker_registry) as config:
            for image in self.workflow.tag_conf.primary_images:
                if image.pulp_repo not in repos:
                    self.log.info("syncing %s", image.pulp_repo)
                    repoinfo = pulp.syncRepo(config.env, image.pulp_repo,
                                             config_file=config.name)
                    repos[image.pulp_repo] = repoinfo[0]['id']


                images.append(ImageName(registry=pulp_registry,
                                        repo=image.repo))

        self.log.info("publishing to crane")
        pulp.crane(list(repos.values()), wait=True)

        # Return the set of qualitifed repo names for this image
        return images
