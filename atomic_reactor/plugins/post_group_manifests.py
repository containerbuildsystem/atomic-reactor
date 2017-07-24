"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

get the image manifest lists from the worker builders. If possible, group them together
and return them. if not, return the x86_64/amd64 image manifest instead after re-uploading
it for all existing image tags.
"""


from __future__ import unicode_literals
import requests
import requests.auth

from six.moves.urllib.parse import urlparse

from atomic_reactor.plugin import PostBuildPlugin, PluginFailedException
from atomic_reactor.util import Dockercfg


class GroupManifestsPlugin(PostBuildPlugin):
    key = 'group_manifests'
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, registries, group=True, goarch=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param registries: dict, keys are docker registries, values are dicts containing
                           per-registry parameters.
                           Params:
                            * "secret" optional string - path to the secret, which stores
                              login and password for remote registry
        :param group: bool, if true, create a manifest list; otherwise only add tags to
                      amd64 image manifest
        :param goarch: dict, keys are platform, values are go language platform names
        """
        # call parent constructor
        super(GroupManifestsPlugin, self).__init__(tasker, workflow)
        self.group = group
        self.goarch = goarch or {}
        self.registries = registries
        self.worker_registries = {}

    def get_worker_manifest(self, worker_data):
        worker_digests = worker_data['digests']
        worker_manifest = []

        msg = "worker_registries {0}".format(self.worker_registries)
        self.log.debug(msg)

        for registry, registry_conf in self.registries.items():
            if registry_conf.get('version') == 'v1':
                continue

            if not registry.startswith('http://') and not registry.startswith('https://'):
                registry = 'https://' + registry

            registry_noschema = urlparse(registry).netloc
            self.log.debug("evaluating registry %s", registry_noschema)

            insecure = registry_conf.get('insecure', False)
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

            if registry_noschema in self.worker_registries:
                self.log.debug("getting manifests from %s", registry_noschema)
                digest = worker_digests[0]['digest']
                repo = worker_digests[0]['repository']

                # get a v2 schemav2 response for now
                v2schema2 = 'application/vnd.docker.distribution.manifest.v2+json'
                headers = {'accept': v2schema2}
                kwargs = {'verify': not insecure, 'headers': headers, 'auth': auth}

                url = '{0}/v2/{1}/manifests/{2}'.format(registry, repo, digest)
                self.log.debug("attempting get from %s", url)
                response = requests.get(url, **kwargs)

                image_manifest = response.json()

                if image_manifest['schemaVersion'] == '1':
                    msg = 'invalid schema from {0}'.format(url)
                    raise PluginFailedException(msg)

                headers = {'Content-Type': v2schema2}
                kwargs = {'verify': not insecure, 'headers': headers, 'auth': auth}

                for image in self.workflow.tag_conf.images:
                    image_tag = image.to_str(registry=False).split(':')[1]
                    url = '{0}/v2/{1}/manifests/{2}'.format(registry, repo, image_tag)
                    self.log.debug("for image_tag %s, putting at %s", image_tag, url)
                    response = requests.put(url, json=image_manifest, **kwargs)

                    if not response.ok:
                        msg = "PUT failed: {0},\n manifest was: {1}".format(response.json(),
                                                                            image_manifest)
                        self.log.error(msg)
                    response.raise_for_status()

                worker_manifest.append(image_manifest)
                self.log.debug("appending an image_manifest")
                break

        return worker_manifest

    def run(self):
        if self.group:
            raise NotImplementedError('group=True is not supported in group_manifests')
        grouped_manifests = []

        valid = False
        all_annotations = self.workflow.build_result.annotations['worker-builds']
        for plat, annotation in all_annotations.items():
            digests = annotation['digests']
            for digest in digests:
                registry = digest['registry']
                self.worker_registries.setdefault(registry, [])
                self.worker_registries[registry].append(registry)

        for platform in all_annotations:
            if self.goarch.get(platform, platform) == 'amd64':
                valid = True
                grouped_manifests = self.get_worker_manifest(all_annotations[platform])
                break

        if valid:
            self.log.debug("found an x86_64 platform and grouped its manifest")
            return grouped_manifests
        else:
            raise ValueError('failed to find an x86_64 platform')
