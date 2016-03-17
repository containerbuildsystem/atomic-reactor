"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Mounts NFS share to mountpoint,
creates a directory there and copies
exported squashed built image ($tmpdir/image.tar) into it.

Usage:
{
    'name': 'cp_built_image_to_nfs',
    'args': { 'nfs_server_path': 'server:path',
              'dest_dir': 'dest_dir',
              'mountpoint': '/tmp/mountpoint/' }

}

"""

from __future__ import unicode_literals

import os
from osbs.conf import Configuration
from osbs.api import OSBS
import shutil
import subprocess
import errno
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import get_build_json


__all__ = ('CopyBuiltImageToNFSPlugin', )

DEFAULT_MOUNTPOINT = "/atomic-reactor-nfs-mountpoint/"


def mount(server_path, mountpoint, args=None, mount_type="nfs"):
    args = args or ["nolock"]
    rendered_args = ",".join(args)
    cmd = [
        "mount",
        "-t", mount_type,
        "-o", rendered_args,
        server_path,
        mountpoint
    ]
    subprocess.check_call(cmd)


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


class CopyBuiltImageToNFSPlugin(PostBuildPlugin):
    """
    Workflow of this plugin:

    1. mount NFS
    2. create subdir (`dest_dir`)
    3. copy squashed image to $NFS/$dest_dir/
    """

    key = "cp_built_image_to_nfs"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, nfs_server_path, dest_dir=None,
                 mountpoint=DEFAULT_MOUNTPOINT, url=None, verify_ssl=True,
                 use_auth=True):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param nfs_server_path: str, [server:]path of NFS share, will use
                                node's IP address if server omitted but
                                url supplied
        :param dest_dir: this directory will be created in NFS and the built image will be copied
                         into it, if not specified, copy to root of NFS
        :param mountpoint: str, path where NFS share will be mounted
        :param url: str, URL to OSv3 instance
        :param verify_ssl: bool, verify OSv3 SSL certificate?
        :param use_auth: bool, initiate authentication with OSv3?
        """
        # call parent constructor
        super(CopyBuiltImageToNFSPlugin, self).__init__(tasker, workflow)
        self.dest_dir = dest_dir
        self.mountpoint = mountpoint
        self.absolute_dest_dir = self.mountpoint

        if ':' in nfs_server_path:
            self.nfs_server_path = nfs_server_path
        elif url:
            node = self.discover_node_ip(url, verify_ssl, use_auth)
            self.nfs_server_path = "{node}:{path}".format(node=node,
                                                          path=nfs_server_path)
        else:
            self.log.error("NFS server not specified and no url given")

        self.log.debug("NFS server path: %s", self.nfs_server_path)
        if self.dest_dir:
            self.absolute_dest_dir = os.path.join(self.mountpoint, self.dest_dir)
            self.log.debug("destination dir = %s", self.absolute_dest_dir)

    def discover_node_ip(self, url, verify_ssl, use_auth):
        build_json = get_build_json()
        namespace = build_json.get('metadata', {}).get('namespace', None)
        osbs_conf = Configuration(conf_file=None, openshift_uri=url,
                                  use_auth=use_auth, verify_ssl=verify_ssl,
                                  namespace=namespace)
        osbs = OSBS(osbs_conf, osbs_conf)
        pod = osbs.get_pod_for_build(build_json['metadata']['name'])
        return pod.get_host()

    def mount_nfs(self):
        self.log.debug("create mountpoint %s", self.mountpoint)
        mkdir_p(self.mountpoint)
        self.log.debug("mount NFS %r at %s", self.nfs_server_path, self.mountpoint)
        mount(self.nfs_server_path, self.mountpoint)

    def run(self):
        if len(self.workflow.exported_image_sequence) == 0:
            raise RuntimeError('no exported image to upload to nfs')
        source_path = self.workflow.exported_image_sequence[-1].get("path")
        if not source_path or not os.path.isfile(source_path):
            raise RuntimeError("squashed image does not exist: %s", source_path)

        self.mount_nfs()

        if self.dest_dir:
            try:
                mkdir_p(self.absolute_dest_dir)
            except (IOError, OSError) as ex:
                self.log.error("couldn't create %s: %r", self.dest_dir, ex)
                raise

        fname = os.path.basename(source_path)
        expected_image_path = os.path.join(self.absolute_dest_dir, fname)
        if os.path.isfile(expected_image_path):
            raise RuntimeError("%s already exists!" % expected_image_path)

        self.log.info("starting copying the image; this may take a while")
        try:
            shutil.copy2(source_path, self.absolute_dest_dir)
        except (IOError, OSError) as ex:
            self.log.error("couldn't copy %s into %s: %r", source_path, self.dest_dir, ex)
            raise

        if os.path.isfile(os.path.join(self.absolute_dest_dir, fname)):
            self.log.debug("CopyBuiltImagePlugin.run() success")
        else:
            self.log.error("CopyBuiltImagePlugin.run() unknown error")

        nfs_host = self.nfs_server_path.split(':', 1)[0]
        return nfs_host
