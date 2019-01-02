"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from copy import deepcopy
import re
import subprocess

from atomic_reactor.constants import IMAGE_TYPE_DOCKER_ARCHIVE, IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.plugins.exit_remove_built_image import defer_removal
from atomic_reactor.plugins.pre_reactor_config import get_registries
from atomic_reactor.util import (get_manifest_digests, get_config_from_registry, Dockercfg)


__all__ = ('TagAndPushPlugin', )


class TagAndPushPlugin(PostBuildPlugin):
    """
    Use tags from workflow.tag_conf and push the images to workflow.push_conf
    """

    key = "tag_and_push"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, registries=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param registries: dict, keys are docker registries, values are dicts containing
                           per-registry parameters.
                           Params:
                            * "insecure" optional boolean - controls whether pushes are allowed over
                              plain HTTP.
                            * "secret" optional string - path to the secret, which stores
                              email, login and password for remote registry
        """
        # call parent constructor
        super(TagAndPushPlugin, self).__init__(tasker, workflow)

        self.registries = get_registries(self.workflow, deepcopy(registries or {}))

    def need_skopeo_push(self):
        if len(self.workflow.exported_image_sequence) > 0:
            last_image = self.workflow.exported_image_sequence[-1]
            if last_image['type'] == IMAGE_TYPE_OCI or last_image['type'] == IMAGE_TYPE_OCI_TAR:
                return True

        return False

    def push_with_skopeo(self, registry_image, insecure, docker_push_secret):
        # If the last image has type OCI_TAR, then hunt back and find the
        # the untarred version, since skopeo only supports OCI's as an
        # untarred directory
        image = [x for x in self.workflow.exported_image_sequence if
                 x['type'] != IMAGE_TYPE_OCI_TAR][-1]

        cmd = ['skopeo', 'copy']
        if docker_push_secret is not None:
            dockercfg = Dockercfg(docker_push_secret)
            dest_creds = None
            unpacked_auth_b64 = None
            try:
                unpacked_auth_b64 = dockercfg.unpack_auth_b64(registry_image.registry)
            except ValueError:
                self.log.warning("Invalid 'auth' value in '%s'", docker_push_secret)
            if unpacked_auth_b64:
                dest_creds = unpacked_auth_b64.raw_str
            else:
                credentials = dockercfg.get_credentials(registry_image.registry)
                username = credentials.get('username')
                password = credentials.get('password')
                if username and password:
                    dest_creds = username + ':' + password

            if dest_creds:
                cmd.append('--dest-creds=' + dest_creds)
            else:
                self.log.warning("No credentials found in '%s'", docker_push_secret)

        if insecure:
            cmd.append('--dest-tls-verify=false')

        if image['type'] == IMAGE_TYPE_OCI:
            source_img = 'oci:{path}:{ref_name}'.format(**image)
        elif image['type'] == IMAGE_TYPE_DOCKER_ARCHIVE:
            source_img = 'docker-archive://{path}'.format(**image)
        else:
            raise RuntimeError("Attempt to push unsupported image type %s with skopeo" %
                               image['type'])

        dest_img = 'docker://' + registry_image.to_str()

        # Make sure we don't log the credentials
        cmd += [source_img, dest_img]
        log_cmd = [re.sub(r'^--dest-creds=.*', '--dest-creds=<HIDDEN>', arg)
                   for arg in cmd]

        self.log.info("Calling: %s", ' '.join(log_cmd))
        try:
            subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            self.log.error("push failed with output:\n%s", e.output)
            e.cmd = log_cmd  # hide credentials
            raise

    def run(self):
        pushed_images = []

        if not self.workflow.tag_conf.unique_images:
            self.workflow.tag_conf.add_unique_image(self.workflow.image)

        config_manifest_digest = None
        config_manifest_type = None
        config_registry_image = None
        for registry, registry_conf in self.registries.items():
            insecure = registry_conf.get('insecure', False)
            push_conf_registry = \
                self.workflow.push_conf.add_docker_registry(registry, insecure=insecure)

            docker_push_secret = registry_conf.get('secret', None)
            self.log.info("Registry %s secret %s", registry, docker_push_secret)

            for image in self.workflow.tag_conf.images:
                if image.registry:
                    raise RuntimeError("Image name must not contain registry: %r" % image.registry)

                registry_image = image.copy()
                registry_image.registry = registry
                if self.need_skopeo_push():
                    self.push_with_skopeo(registry_image, insecure, docker_push_secret)
                else:
                    self.tasker.tag_and_push_image(self.workflow.builder.image_id,
                                                   registry_image, insecure=insecure,
                                                   force=True, dockercfg=docker_push_secret)
                    defer_removal(self.workflow, registry_image)

                pushed_images.append(registry_image)

                digests = get_manifest_digests(registry_image, registry,
                                               insecure, docker_push_secret)
                tag = registry_image.to_str(registry=False)
                push_conf_registry.digests[tag] = digests

                if not config_manifest_digest and (digests.v2 or digests.oci):
                    if digests.v2:
                        config_manifest_digest = digests.v2
                        config_manifest_type = 'v2'
                    else:
                        config_manifest_digest = digests.oci
                        config_manifest_type = 'oci'
                    config_registry_image = registry_image

            if config_manifest_digest:
                push_conf_registry.config = get_config_from_registry(
                    config_registry_image, registry, config_manifest_digest, insecure,
                    docker_push_secret, config_manifest_type)
            else:
                self.log.info("V2 schema 2 or OCI manifest is not available to get config from")

        self.log.info("All images were tagged and pushed")
        return pushed_images
