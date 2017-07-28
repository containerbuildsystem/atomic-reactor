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
from atomic_reactor.util import Dockercfg, get_retrying_requests_session
from requests.exceptions import HTTPError, RetryError


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
        :param registries: dict, keys are docker registries, values are dicts containing
                           per-registry parameters.
                           Params:
                            * "secret" optional string - path to the secret, which stores
                              login and password for remote registry
        """
        super(DeleteFromRegistryPlugin, self).__init__(tasker, workflow)

        self.registries = deepcopy(registries)

    def setup_secret(self, registry, secret_path):
        auth = None

        if secret_path:
            self.log.debug("registry %s secret %s", registry, secret_path)
            dockercfg = Dockercfg(secret_path).get_credentials(registry)
            try:
                username = dockercfg['username']
                password = dockercfg['password']
            except KeyError:
                self.log.error("credentials for registry %s not found in %s",
                               registry, secret_path)
            else:
                self.log.debug("found user %s for registry %s", username, registry)
                auth = requests.auth.HTTPBasicAuth(username, password)

        return auth

    def request_delete(self, url, manifest, insecure, auth):
        session = get_retrying_requests_session()

        try:
            response = session.delete(url, verify=not insecure, auth=auth)
            response.raise_for_status()
            self.log.info("deleted manifest %s", manifest)
            return True

        except (HTTPError, RetryError) as ex:

            if ex.response.status_code == requests.codes.NOT_FOUND:
                self.log.warning("cannot delete %s: not found", manifest)
            elif ex.response.status_code == requests.codes.METHOD_NOT_ALLOWED:
                self.log.warning("cannot delete %s: image deletion disabled on registry",
                                 manifest)
            else:
                msg = "failed to delete %s: %s" % (manifest, ex.response.reason)
                self.log.error("%s\n%s", msg, ex.response.text)
                raise PluginFailedException(msg)

        return False

    def make_manifest(self, registry, repo, digest):
        return "{registry}/{repo}@{digest}".format(**vars())

    def make_url(self, registry, repo, digest):
        return "{registry}/v2/{repo}/manifests/{digest}".format(**vars())

    def make_registry_noschema(self, registry):
        return urlparse(registry).netloc

    def find_registry(self, registry_noschema, workflow):
        for push_conf_registry in workflow.push_conf.docker_registries:
            if push_conf_registry.uri == registry_noschema:
                return push_conf_registry

        return None

    def handle_registry(self, registry, push_conf_registry, auth, deleted_digests):
        registry_noschema = self.make_registry_noschema(registry)
        deleted = False

        for tag, digests in push_conf_registry.digests.items():
            digest = digests.default
            if digest in deleted_digests:
                # Manifest schema version 2 uses the same digest
                # for all tags
                self.log.info('digest already deleted %s', digest)
                deleted = True
                continue

            repo = tag.split(':')[0]
            url = self.make_url(registry, repo, digest)
            manifest = self.make_manifest(registry_noschema, repo, digest)

            # override insecure if passed
            insecure = push_conf_registry.insecure

            if self.request_delete(url, manifest, insecure, auth):
                deleted_digests.add(digest)
                deleted = True

        return deleted

    def get_worker_digests(self):
        """
         If we are being called from an orchestrator build, collect the worker
         node data and recreate the data locally.
        """
        try:
            builds = self.workflow.build_result.annotations['worker-builds']
        except(TypeError, KeyError):
            # This annotation is only set for the orchestrator build.
            # It's not present, so this is a worker build.
            return {}

        worker_digests = {}

        for plat, annotation in builds.items():
            digests = annotation['digests']
            self.log.debug("build %s has digests: %s", plat, digests)

            for digest in digests:
                reg = digest['registry']
                worker_digests.setdefault(reg, [])
                worker_digests[reg].append(digest)

        return worker_digests

    def handle_worker_digests(self, worker_digests, registry, insecure, auth, deleted_digests):
        registry_noschema = self.make_registry_noschema(registry)

        if registry_noschema not in worker_digests:
            return False

        digests = worker_digests[registry_noschema]
        for digest in digests:
            if digest['digest'] in deleted_digests:
                # Manifest schema version 2 uses the same digest
                # for all tags
                self.log.info('digest already deleted %s', digest['digest'])
                return True

            url = self.make_url(registry, digest['repository'], digest['digest'])
            manifest = self.make_manifest(registry_noschema, digest['repository'],
                                          digest['digest'])

            if self.request_delete(url, manifest, insecure, auth):
                deleted_digests.add(digest['digest'])

        return True

    def run(self):
        deleted_digests = set()

        worker_digests = self.get_worker_digests()

        for registry, registry_conf in self.registries.items():
            if not registry.startswith('http://') and not registry.startswith('https://'):
                registry = 'https://' + registry

            registry_noschema = urlparse(registry).netloc

            insecure = registry_conf.get('insecure', False)
            secret_path = registry_conf.get('secret')
            auth = self.setup_secret(registry_noschema, secret_path)

            # orchestrator builds use worker_digests
            orchestrator_delete = self.handle_worker_digests(worker_digests, registry, insecure,
                                                             auth, deleted_digests)

            push_conf_registry = self.find_registry(registry_noschema, self.workflow)
            if not push_conf_registry:
                # only warn if we're not running in the orchestrator
                if not orchestrator_delete:
                    self.log.warning("requested deleting image from %s but we haven't pushed there",
                                     registry_noschema)
                continue

            # worker node and manifests use push_conf_registry
            if self.handle_registry(registry, push_conf_registry, auth, deleted_digests):
                # delete these temp registries
                self.workflow.push_conf.remove_docker_registry(push_conf_registry)

        return deleted_digests
