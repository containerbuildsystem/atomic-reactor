"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Push built image to pulp registry

Several authentication schemes are possible, including
username+password and key/certificate via secrets.

However, the recommended scheme (since Origin 1.0.6) is to store a
key and certificate in a secret which the builder service account is
allowed to mount:

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

"pulp_push": {
  "pulp_registry_name": ...,
  ...
  "pulp_secret_path": "/var/run/secrets/pulp"
}
"""

from __future__ import print_function, unicode_literals

import tempfile
from tempfile import NamedTemporaryFile
import os
import subprocess

from atomic_reactor.constants import PLUGIN_PULP_SYNC_KEY, PLUGIN_PULP_PUSH_KEY
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import ImageName, are_plugins_in_order
from atomic_reactor.pulp_util import PulpHandler


class PulpPushPlugin(PostBuildPlugin):
    key = PLUGIN_PULP_PUSH_KEY
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, pulp_registry_name, load_squashed_image=None,
                 load_exported_image=None, image_names=None, pulp_secret_path=None,
                 username=None, password=None, dockpulp_loglevel=None, publish=True):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param pulp_registry_name: str, name of pulp registry to use, specified in /etc/
                                   dockpulp.conf
        :param load_squashed_image: obsolete name for load_exported_image, please don't use
        :param load_exported_image: bool, use exported tar instead of image from Docker
        :param image_names: list of additional image names
        :param pulp_secret_path: path to pulp.cer and pulp.key; $SOURCE_SECRET_PATH otherwise
        :param username: pulp username, used in preference to certificate and key
        :param password: pulp password, used in preference to certificate and key
        :param publish: Bool, whether to publish to crane or not
        """
        # call parent constructor
        super(PulpPushPlugin, self).__init__(tasker, workflow)
        self.pulp_registry_name = pulp_registry_name
        self.image_names = image_names
        if load_squashed_image is not None and load_exported_image is not None and \
                (load_squashed_image != load_exported_image):
            raise RuntimeError("Can\'t use load_squashed_image and "
                               "load_exported_image with different values")
        if load_squashed_image is not None:
            self.log.warning('load_squashed_image argument is obsolete and will be '
                             'removed in a future version; please use load_exported_image instead')
        self.load_exported_image = load_exported_image or load_squashed_image or False
        self.pulp_secret_path = pulp_secret_path
        self.username = username
        self.password = password

        self.publish = publish and not are_plugins_in_order(self.workflow.postbuild_plugins_conf,
                                                            self.key, PLUGIN_PULP_SYNC_KEY)

        self.dockpulp_loglevel = dockpulp_loglevel
        self.pulp_handler = PulpHandler(self.workflow, self.pulp_registry_name, self.log,
                                        pulp_secret_path=self.pulp_secret_path,
                                        username=self.username, password=self.password,
                                        dockpulp_loglevel=self.dockpulp_loglevel)

    def push_tar(self, filename, image_names=None, repo_prefix="redhat-"):
        # Find out how to tag this image.
        self.log.info("image names: %s", [str(image_name) for image_name in image_names])

        self.log.info("checking image before upload %s", filename)
        self.pulp_handler.check_file(filename)

        pulp_repos = self.pulp_handler.create_dockpulp_and_repos(image_names, repo_prefix)
        _, file_extension = os.path.splitext(filename)

        try:
            top_layer, layers = self.pulp_handler.get_tar_metadata(filename)
            # getImageIdsExist was introduced in rh-dockpulp 0.6+
            existing_imageids = self.pulp_handler.get_image_ids_existing(layers)
            self.log.debug("existing layers: %s", existing_imageids)

            # Strip existing layers from the tar and repack it
            remove_layers = [str(os.path.join(x, 'layer.tar')) for x in existing_imageids]

            commands = {'.xz': 'xzcat', '.gz': 'zcat', '.bz2': 'bzcat', '.tar': 'cat'}
            unpacker = commands.get(file_extension, None)
            self.log.debug("using unpacker %s for extension %s", unpacker, file_extension)
            if unpacker is None:
                raise Exception("Unknown tarball format: %s" % filename)

            with NamedTemporaryFile(prefix='strip_tar_', suffix='.gz') as outfile:
                cmd = "set -o pipefail; {0} {1} | tar --delete {2} | gzip - > {3}".format(
                    unpacker, filename, ' '.join(remove_layers), outfile.name)
                self.log.debug("running %s", cmd)
                subprocess.check_call(cmd, shell=True)
                self.log.debug("uploading %s", outfile.name)
                self.pulp_handler.upload(outfile.name)
        except:
            self.log.debug("Error on creating deduplicated layers tar", exc_info=True)
            try:
                if file_extension != '.tar':
                    raise RuntimeError("tar is already compressed")
                with NamedTemporaryFile(prefix='full_tar_', suffix='.gz') as outfile:
                    cmd = "set -o pipefail; cat {0} | gzip - > {1}".format(
                        filename, outfile.name)
                    self.log.debug("Compressing tar using '%s' command", cmd)
                    subprocess.check_call(cmd, shell=True)
                    self.log.debug("uploading %s", outfile.name)
                    self.pulp_handler.upload(outfile.name)
            except:
                self.log.info("Falling back to full tar upload")
                self.pulp_handler.upload(filename)

        for repo_id, pulp_repo in pulp_repos.items():
            for layer in layers:
                self.pulp_handler.copy(repo_id, layer)
            self.pulp_handler.update_repo(repo_id, {"tag": "%s:%s" % (",".join(pulp_repo.tags),
                                                                      top_layer)})

        # Only publish if we don't the pulp_sync plugin also configured
        if self.publish:
            self.pulp_handler.publish(pulp_repos.keys())
        else:
            self.log.info("publishing deferred until %s plugin runs", PLUGIN_PULP_SYNC_KEY)

        # Store the registry URI in the push configuration

        # We only want the hostname[:port]
        pulp_registry = self.pulp_handler.get_registry_hostname()

        self.workflow.push_conf.add_pulp_registry(self.pulp_handler.get_pulp_instance(),
                                                  pulp_registry)

        # Return the set of qualified repo names for this image
        return top_layer, [ImageName(registry=pulp_registry, repo=repodata.registry_id, tag=tag)
                for dummy_repo, repodata in pulp_repos.items()
                for tag in repodata.tags]  # noqa

    def run(self):
        image_names = self.workflow.tag_conf.images[:]
        # Add in additional image names, if any
        if self.image_names:
            self.log.info("extending image names: %s", self.image_names)
            image_names += [ImageName.parse(x) for x in self.image_names]

        if self.load_exported_image:
            if len(self.workflow.exported_image_sequence) == 0:
                raise RuntimeError('no exported image to push to pulp')
            export_path = self.workflow.exported_image_sequence[-1].get("path")
            top_layer, crane_repos = self.push_tar(export_path, image_names)
        else:
            # Work out image ID
            image = self.workflow.image
            self.log.info("fetching image %s from docker", image)
            with tempfile.NamedTemporaryFile(prefix='docker-image-', suffix='.tar') as image_file:
                image_file.write(self.tasker.d.get_image(image).data)
                # This file will be referenced by its filename, not file
                # descriptor - must ensure contents are written to disk
                image_file.flush()
                top_layer, crane_repos = self.push_tar(image_file.name, image_names)

        if self.publish:
            for image_name in crane_repos:
                self.log.info("image available at %s", str(image_name))

        return top_layer, crane_repos
