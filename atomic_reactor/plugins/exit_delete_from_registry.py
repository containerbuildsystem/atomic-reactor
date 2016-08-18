"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from copy import deepcopy
import requests
import requests.auth
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

from atomic_reactor.plugin import ExitPlugin, PluginFailedException
from atomic_reactor.util import Dockercfg

class DeleteFromRegistryPlugin(ExitPlugin):
    """
    Delete previously pushed v2 images from a registry.
    """

    key = "delete_from_registry"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, registries):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param registries: dict, keys are docker registries, values are dicts containing per-registry
                           parameters.
                           Params:
                            * "secret" optional string - path to the secret, which stores
                              login and password for remote registry
        """
        super(DeleteFromRegistryPlugin, self).__init__(tasker, workflow)

        self.registries = deepcopy(registries)

    def run(self):
        for registry, registry_conf in self.registries.items():
            if not registry.startswith('http://') and not registry.startswith('https://'):
                registry = 'https://' + registry

            registry_noschema = urlparse(registry).netloc

            auth = None
            secret_path = registry_conf.get('secret')
            if secret_path:
                self.log.debug("registry %s secret %s", registry_noschema, secret_path)
                dockercfg = Dockercfg(secret_path).get_credentials(registry_noschema)
                try:
                    username = dockercfg['username']
                    password = dockercfg['password']
                except KeyError:
                    self.log.error("credentials for registry %s not found in %s",
                                   registry_noschema, secret_path)
                else:
                    self.log.debug("found user %s for registry %s", username, registry_noschema)
                    auth = requests.auth.HTTPBasicAuth(username, password)

            for push_conf_registry in self.workflow.push_conf.docker_registries:
                if push_conf_registry.uri == registry_noschema:
                    break
            else:
                self.log.warning("requested deleting image from %s but we haven't pushed there",
                                 registry_noschema)
                continue

            for tag, digest in push_conf_registry.digests.items():
                repo = tag.split(':')[0]
                url = registry + "/v2/" + repo + "/manifests/" + digest
                insecure = push_conf_registry.insecure
                response = requests.delete(url, verify=not insecure, auth=auth)

                if response.status_code == requests.codes.ACCEPTED:
                    self.log.info("deleted manifest %s/%s@%s", registry_noschema, repo, digest)
                elif response.status_code == requests.codes.NOT_FOUND:
                    self.log.warning("cannot delete %s/%s@%s: not found",
                                     registry_noschema, repo, digest)
                elif response.status_code == requests.codes.METHOD_NOT_ALLOWED:
                    self.log.warning("cannot delete %s/%s@%s: image deletion disabled on registry",
                                     registry_noschema, repo, digest)
                else:
                    msg = "failed to delete %s/%s@%s: %s" % (registry_noschema, repo, digest,
                                                             response.reason)
                    self.log.error("%s\n%s", msg, response.text)
                    raise PluginFailedException(msg)
