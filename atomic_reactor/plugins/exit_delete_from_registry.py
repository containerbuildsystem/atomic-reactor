"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from copy import deepcopy
import requests

from atomic_reactor.plugin import ExitPlugin, PluginFailedException
from atomic_reactor.util import RegistrySession, registry_hostname
from atomic_reactor.plugins.pre_reactor_config import get_registries
from atomic_reactor.constants import PLUGIN_GROUP_MANIFESTS_KEY
from requests.exceptions import HTTPError, RetryError, Timeout


class DeleteFromRegistryPlugin(ExitPlugin):
    """
    Delete previously pushed v2 images from a registry.
    """

    key = "delete_from_registry"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, registries=None):
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

        self.registries = get_registries(self.workflow, deepcopy(registries or {}))

    def request_delete(self, session, url, manifest):
        try:
            response = session.delete(url)
            response.raise_for_status()
            self.log.info("deleted manifest %s", manifest)
            return True

        except (HTTPError, RetryError, Timeout) as ex:

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

    def make_url(self, repo, digest):
        return "/v2/{repo}/manifests/{digest}".format(**vars())

    def find_registry(self, registry_noschema, workflow):
        for push_conf_registry in workflow.push_conf.docker_registries:
            if push_conf_registry.uri == registry_noschema:
                return push_conf_registry

        return None

    def handle_registry(self, session, push_conf_registry, deleted_digests):
        registry_noschema = registry_hostname(session.registry)
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
            url = self.make_url(repo, digest)
            manifest = self.make_manifest(registry_noschema, repo, digest)

            if self.request_delete(session, url, manifest):
                deleted_digests.add(digest)
                deleted = True

        return deleted

    def delete_manifest_lists(self, session, registry_noschema, deleted_digests):
        manifest_list_digests = self.workflow.postbuild_results.get(PLUGIN_GROUP_MANIFESTS_KEY)
        if not manifest_list_digests:
            return

        for repo, digest in manifest_list_digests.items():
            url = self.make_url(repo, digest.default)
            manifest = self.make_manifest(registry_noschema, repo, digest.default)
            self.request_delete(session, url, manifest)
            deleted_digests.add(digest.default)

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
                reg = registry_hostname(digest['registry'])
                worker_digests.setdefault(reg, [])
                worker_digests[reg].append(digest)

        return worker_digests

    def handle_worker_digests(self, session, worker_digests, deleted_digests):
        registry_noschema = registry_hostname(session.registry)

        if registry_noschema not in worker_digests:
            return False

        # Remove manifest list first to avoid broken lists in case an error occurs
        self.delete_manifest_lists(session, registry_noschema, deleted_digests)

        digests = worker_digests[registry_noschema]
        for digest in digests:
            if digest['digest'] in deleted_digests:
                # Manifest schema version 2 uses the same digest
                # for all tags
                self.log.info('digest already deleted %s', digest['digest'])
                return True

            url = self.make_url(digest['repository'], digest['digest'])
            manifest = self.make_manifest(registry_noschema, digest['repository'],
                                          digest['digest'])

            if self.request_delete(session, url, manifest):
                deleted_digests.add(digest['digest'])

        return True

    def run(self):
        deleted_digests = set()

        worker_digests = self.get_worker_digests()

        for registry, registry_conf in self.registries.items():
            registry_noschema = registry_hostname(registry)

            push_conf_registry = self.find_registry(registry_noschema, self.workflow)

            try:
                insecure = registry_conf['insecure']
            except KeyError:
                # 'insecure' didn't used to be set in the registry config passed to this
                # plugin - it would simply be inherited from the push_conf. To handle
                # orchestrated builds, we need to have it configured for this plugin,
                # but, if not set,  check in the push_conf for compat.
                if push_conf_registry:
                    insecure = push_conf_registry.insecure
                else:
                    insecure = False

            secret_path = registry_conf.get('secret')

            session = RegistrySession(registry, insecure=insecure, dockercfg_path=secret_path)

            # orchestrator builds use worker_digests
            orchestrator_delete = self.handle_worker_digests(session, worker_digests,
                                                             deleted_digests)

            if not push_conf_registry:
                # only warn if we're not running in the orchestrator
                if not orchestrator_delete:
                    self.log.warning("requested deleting image from %s but we haven't pushed there",
                                     registry_noschema)
                continue

            # worker node and manifests use push_conf_registry
            if self.handle_registry(session, push_conf_registry, deleted_digests):
                # delete these temp registries
                self.workflow.push_conf.remove_docker_registry(push_conf_registry)

        return deleted_digests
