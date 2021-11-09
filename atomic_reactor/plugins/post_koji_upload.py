"""
Copyright (c) 2017, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from collections import namedtuple
import os
from tempfile import NamedTemporaryFile

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.constants import PLUGIN_KOJI_UPLOAD_PLUGIN_KEY
from atomic_reactor.config import get_koji_session, get_openshift_session
from atomic_reactor.util import is_scratch_build, map_to_user_params
from atomic_reactor.utils.koji import get_buildroot, get_output, get_output_metadata

# An output file and its metadata
Output = namedtuple('Output', ['file', 'metadata'])


class KojiUploadLogger(object):
    def __init__(self, logger, notable_percent=10):
        self.logger = logger
        self.notable_percent = notable_percent
        self.last_percent_done = 0

    def callback(self, offset, totalsize, size, t1, t2):  # pylint: disable=W0613
        if offset == 0:
            self.logger.debug("upload size: %.1fMiB", totalsize / 1024 / 1024)

        if not totalsize or not t1:
            return

        percent_done = 100 * offset // totalsize
        if (percent_done >= 99 or
                percent_done - self.last_percent_done >= self.notable_percent):
            self.last_percent_done = percent_done
            self.logger.debug("upload: %d%% done (%.1f MiB/sec)",
                              percent_done, size / t1 / 1024 / 1024)


class KojiUploadPlugin(PostBuildPlugin):
    """
    Upload this build to Koji

    Note: only the image archive is uploaded to Koji at this stage.
    Metadata about this image is created and stored in a ConfigMap in
    OpenShift, ready for the orchestrator build to collect and use to
    actually create the Koji Build together with the uploaded image
    archive(s).

    Authentication is with Kerberos unless the koji_ssl_certs_dir
    configuration parameter is given, in which case it should be a
    path at which 'cert', 'ca', and 'serverca' are the certificates
    for SSL authentication.

    If Kerberos is used for authentication, the default principal will
    be used (from the kernel keyring) unless both koji_keytab and
    koji_principal are specified. The koji_keytab parameter is a
    keytab name like 'type:name', and so can be used to specify a key
    in a Kubernetes secret by specifying 'FILE:/path/to/key'.
    """

    key = PLUGIN_KOJI_UPLOAD_PLUGIN_KEY
    is_allowed_to_fail = False

    args_from_user_params = map_to_user_params("koji_upload_dir", "platform")

    def __init__(self, workflow, koji_upload_dir, blocksize=None, platform='x86_64'):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :param koji_upload_dir: str, path to use when uploading to hub
        :param blocksize: int, blocksize to use for uploading files
        :param platform: str, platform name for this build
        """
        super(KojiUploadPlugin, self).__init__(workflow)

        self.blocksize = blocksize
        self.koji_upload_dir = koji_upload_dir

        self.osbs = get_openshift_session(self.workflow.conf,
                                          self.workflow.user_params.get('namespace'))
        self.build_id = None
        self.pullspec_image = None
        self.platform = platform

    def get_logs(self):
        """
        Build the logs entry for the metadata 'output' section

        :return: list, Output instances
        """

        build_logs = NamedTemporaryFile(prefix="buildstep-%s" % self.build_id,
                                        suffix=".log",
                                        mode='wb')
        build_logs.write("\n".join(self.workflow.build_result.logs).encode('utf-8'))
        build_logs.flush()
        filename = "{platform}-build.log".format(platform=self.platform)
        return [Output(file=build_logs,
                       metadata=get_output_metadata(build_logs.name, filename))]

    def get_metadata(self):
        """
        Build the metadata needed for importing the build

        :return: tuple, the metadata and the list of Output instances
        """
        try:
            self.build_id = self.workflow.user_params['pipeline_run_name']
        except KeyError:
            self.log.error("No pipeline_run_name found")
            raise

        for image in self.workflow.tag_conf.unique_images:
            self.pullspec_image = image
            break

        for image in self.workflow.tag_conf.primary_images:
            # dash at first/last postition does not count
            if '-' in image.tag[1:-1]:
                self.pullspec_image = image
                break

        if not self.pullspec_image:
            raise RuntimeError('Unable to determine pullspec_image')

        metadata_version = 0

        buildroot = get_buildroot()
        output_files, _ = get_output(workflow=self.workflow, buildroot_id=buildroot['id'],
                                     pullspec=self.pullspec_image, platform=self.platform,
                                     source_build=False, logs=self.get_logs())

        output = [output.metadata for output in output_files]
        koji_metadata = {
            'metadata_version': metadata_version,
            'buildroots': [buildroot],
            'output': output,
        }

        return koji_metadata, output_files

    def upload_file(self, session, output, serverdir):
        """
        Upload a file to koji

        :return: str, pathname on server
        """
        name = output.metadata['filename']
        self.log.debug("uploading %r to %r as %r",
                       output.file.name, serverdir, name)

        kwargs = {}
        if self.blocksize is not None:
            kwargs['blocksize'] = self.blocksize
            self.log.debug("using blocksize %d", self.blocksize)

        upload_logger = KojiUploadLogger(self.log)
        session.uploadWrapper(output.file.name, serverdir, name=name,
                              callback=upload_logger.callback, **kwargs)
        path = os.path.join(serverdir, name)
        self.log.debug("uploaded %r", path)
        return path

    def run(self):
        """
        Run the plugin.
        """
        # Only run if the build was successful
        if self.workflow.build_process_failed:
            self.log.info("Not promoting failed build to koji")
            return

        # OSBS2 TBD
        _, output_files = self.get_metadata()

        if not is_scratch_build(self.workflow):
            try:
                session = get_koji_session(self.workflow.conf)
                for output in output_files:
                    if output.file:
                        self.upload_file(session, output, self.koji_upload_dir)
            finally:
                for output in output_files:
                    if output.file:
                        output.file.close()

        md_fragment = '{}-md'.format(self.workflow.user_params['pipeline_run_name'])
        md_fragment_key = 'metadata.json'
        annotations = {
            "metadata_fragment": "configmap/" + md_fragment,
            "metadata_fragment_key": md_fragment_key
        }

        return annotations
