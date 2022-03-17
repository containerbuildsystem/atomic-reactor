"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os
from typing import Optional, Any, Dict

from atomic_reactor.config import get_koji_session
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.constants import PLUGIN_GATHER_BUILDS_METADATA_KEY
from atomic_reactor.util import is_scratch_build, map_to_user_params, get_platforms
from atomic_reactor.utils.koji import get_buildroot, get_output, KojiUploadLogger
from osbs.utils import ImageName


class GatherBuildsMetadataPlugin(PostBuildPlugin):
    """
    Gather builds metadata from platform-specific builds which are done by
    podman-remote on remote hosts. This metadata may also contain metadata
    fetched from remote hosts by running commands via ssh tunnel.

    This plugin returns a mapping from platform to the build's metadata.
    For example,

    {
        "x86_64": {...},
        "s390x": {...},
    }

    Each metadata mapping follows the format of Koji Content Generator Metadata.
    This plugin ensures it has keys ``metadata_version``, ``buildroots`` and
    ``output``.
    """

    key = PLUGIN_GATHER_BUILDS_METADATA_KEY
    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params("koji_upload_dir")

    def __init__(self, workflow, koji_upload_dir: str, blocksize: Optional[int] = None):
        super(GatherBuildsMetadataPlugin, self).__init__(workflow)
        self._koji_upload_dir = koji_upload_dir
        self._block_size = blocksize

    def _determine_image_pullspec(self) -> ImageName:
        tag_conf = self.workflow.data.tag_conf
        pullspec = None
        for image in tag_conf.unique_images:
            pullspec = image
            break
        for image in tag_conf.primary_images:
            # dash at first/last position does not count
            if '-' in image.tag[1:-1]:
                pullspec = image
                break
        if not pullspec:
            raise RuntimeError('Unable to determine pullspec_image')
        return pullspec

    def _get_build_metadata(self, platform: str):
        """
        Build the metadata needed for importing the build

        :return: tuple, the metadata and the list of Output instances
        """
        pullspec_image = self._determine_image_pullspec()
        buildroot = get_buildroot(platform)
        output_files, _ = get_output(workflow=self.workflow, buildroot_id=buildroot['id'],
                                     pullspec=pullspec_image, platform=platform,
                                     source_build=False)
        koji_metadata = {
            'metadata_version': 0,
            'buildroots': [buildroot],
            'output': [output.metadata for output in output_files],
        }

        return koji_metadata, output_files

    def _upload_file(self, session, output, serverdir):
        """
        Upload a file to koji

        :return: str, pathname on server
        """
        name = output.metadata['filename']
        self.log.debug("uploading %r to %r as %r",
                       output.file.name, serverdir, name)

        kwargs = {}
        if self._block_size is not None:
            kwargs['blocksize'] = self._block_size
            self.log.debug("using blocksize %d", self._block_size)

        upload_logger = KojiUploadLogger(self.log)
        session.uploadWrapper(output.file.name, serverdir, name=name,
                              callback=upload_logger.callback, **kwargs)
        path = os.path.join(serverdir, name)
        self.log.debug("uploaded %r", path)
        return path

    def _update_remote_host_metadata(self, platform: str, koji_metadata: Dict[str, Any]) -> None:
        """Fetch extra metadata and update them into existing metadata.

        These extra metadata may be the data that have to be fetched from the remote hosts.
        """
        # OSBS2 TBD: what extra metadata should be fetched from the remote host?

    def run(self):
        """Run the plugin."""
        metadatas: Dict[str, Dict[str, Any]] = {}

        enabled_platforms = get_platforms(self.workflow.data)
        if not enabled_platforms:
            raise ValueError("No enabled platforms.")

        for platform in enabled_platforms:
            koji_metadata, output_files = self._get_build_metadata(platform)
            self._update_remote_host_metadata(platform, koji_metadata)

            if not is_scratch_build(self.workflow):
                try:
                    session = get_koji_session(self.workflow.conf)
                    for output in output_files:
                        if output.file:
                            self._upload_file(session, output, self._koji_upload_dir)
                finally:
                    for output in output_files:
                        if output.file:
                            output.file.close()

            metadatas[platform] = koji_metadata

        return metadatas
