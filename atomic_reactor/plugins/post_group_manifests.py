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
from tempfile import NamedTemporaryFile
import yaml
from subprocess import check_output, CalledProcessError, STDOUT

from six.moves.urllib.parse import urlparse

from atomic_reactor.plugin import PostBuildPlugin, PluginFailedException
from atomic_reactor.util import Dockercfg, get_manifest_digests, get_retrying_requests_session
from atomic_reactor.constants import PLUGIN_GROUP_MANIFESTS_KEY


class GroupManifestsPlugin(PostBuildPlugin):
    is_allowed_to_fail = False
    key = PLUGIN_GROUP_MANIFESTS_KEY

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

    def submit_manifest_list(self, registry, registry_conf, manifest_list_spec):
        docker_secret_path = registry_conf.get('secret', None)
        with NamedTemporaryFile(prefix='manifest-list', suffix=".yml", mode='w') as fp:
            yaml.dump(manifest_list_spec, stream=fp)
            fp.flush()
            self.log.debug("Wrote to file %s with config %s", fp.name, docker_secret_path)

            # --docker-cfg may be rendundant here, but it's how the tool should work
            cmd = ['manifest-tool', '--docker-cfg=%s' % docker_secret_path,
                   'push', 'from-spec', fp.name]
            # docker always looks in $HOME for the .dockercfg, so set $HOME to the path
            try:
                check_output(cmd, stderr=STDOUT, env={'HOME': docker_secret_path})
            except CalledProcessError as exc:
                self.log.error("manifest-tool failed with %s", exc.output)
                raise
            self.log.info("Manifest list submitted for %s", registry)

    def get_grouped_manifests(self):
        grouped_manifests = []
        for registry, registry_conf in self.registries.items():
            if registry_conf.get('version') == 'v1':
                continue

            manifest_list_spec = {}
            manifest_list_spec['manifests'] = []
            all_annotations = self.workflow.build_result.annotations['worker-builds']
            for platform in all_annotations:
                worker_image = all_annotations[platform]['digests'][0]
                tag = worker_image['tag']
                repository = worker_image['repository']
                arch_entry = {
                    'image': '{0}/{1}:{2}'.format(registry, repository, tag),
                    'platform': {
                        'os': 'linux',
                        'architecture': self.goarch.get(platform, platform)
                    }
                }
                manifest_list_spec['manifests'].append(arch_entry)

            manifest_list_spec['tags'] = [image.tag for image in self.workflow.tag_conf.images]
            # use a unique image tag because manifest-tool can't accept a digest that
            # isn't in the respository yet
            registry_image = self.workflow.tag_conf.unique_images[0]
            registry_image.registry = registry
            manifest_list_spec['image'] = registry_image.to_str()
            self.log.info("Submitting manifest-list spec %s", manifest_list_spec)
            self.submit_manifest_list(registry, registry_conf, manifest_list_spec)
            insecure = registry_conf.get('insecure', False)
            secret_path = registry_conf.get('secret')

            self.log.debug('attempting get_manifest_digests from %s for %s',
                           registry, registry_image)
            manifest_list_digest = get_manifest_digests(registry_image, registry=registry,
                                                        insecure=insecure,
                                                        dockercfg_path=secret_path,
                                                        versions=('v2_list',))
            if not manifest_list_digest.v2_list:
                raise PluginFailedException('no manifest list digest for %s', registry)
            self.log.debug('Digest for registry %s is %s', registry, manifest_list_digest.v2_list)
            push_conf_registry = self.workflow.push_conf.add_docker_registry(registry,
                                                                             insecure=insecure)
            tag = registry_image.to_str(registry=False)
            push_conf_registry.digests[tag] = manifest_list_digest
            grouped_manifests.append(manifest_list_digest)

        self.log.info("Manifest lists created and collected for all repositories")
        return grouped_manifests

    def get_worker_manifest(self, worker_data):
        worker_digests = worker_data['digests']

        msg = "worker_registries {0}".format(self.worker_registries)
        self.log.debug(msg)
        session = get_retrying_requests_session()

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
                response = session.get(url, **kwargs)
                response.raise_for_status()

                if response.json()['schemaVersion'] == '1':
                    msg = 'invalid schema from {0}'.format(url)
                    raise PluginFailedException(msg)

                image_manifest = response.content
                headers = {'Content-Type': v2schema2}
                kwargs = {'verify': not insecure, 'headers': headers, 'auth': auth}

                push_conf_registry = self.workflow.push_conf.add_docker_registry(registry,
                                                                                 insecure=insecure)
                for image in self.workflow.tag_conf.images:
                    url = '{0}/v2/{1}/manifests/{2}'.format(registry, repo, image.tag)
                    self.log.debug("for image_tag %s, putting at %s", image.tag, url)
                    response = session.put(url, data=image_manifest, **kwargs)

                    if not response.ok:
                        msg = "PUT failed: {0},\n manifest was: {1}".format(response.json(),
                                                                            image_manifest)
                        self.log.error(msg)
                    response.raise_for_status()

                    # add a tag for any plugins running later that expect it
                    push_conf_registry.digests[image.tag] = digest
                break

    def run(self):
        if self.group:
            return self.get_grouped_manifests()

        all_annotations = self.workflow.build_result.annotations['worker-builds']
        for plat, annotation in all_annotations.items():
            digests = annotation['digests']
            for digest in digests:
                registry = digest['registry']
                self.worker_registries.setdefault(registry, [])
                self.worker_registries[registry].append(registry)

        for platform in all_annotations:
            if self.goarch.get(platform, platform) == 'amd64':
                self.get_worker_manifest(all_annotations[platform])
                self.log.debug("found an x86_64 platform and grouped its manifest")
                return []

        raise ValueError('failed to find an x86_64 platform')
