"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


To have everything for a build in dist-git you need to fetch artefacts using 'fedpkg sources'.

This plugin should do it.
"""
import shutil
import subprocess
from pathlib import Path
from typing import List

from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import PLUGIN_DISTGIT_FETCH_KEY


class DistgitFetchArtefactsPlugin(PreBuildPlugin):
    key = PLUGIN_DISTGIT_FETCH_KEY
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :type workflow: atomic_reactor.inner.DockerBuildWorkflow
        """
        # call parent constructor
        super(DistgitFetchArtefactsPlugin, self).__init__(workflow)
        self.command = self.workflow.conf.sources_command

    def _fetch_sources(self, build_dir: BuildDir) -> List[Path]:
        """Fetch sources files.

        :param build_dir: download the sources files into this directory.
        :type build_dir: BuildDir
        :return: a list of downloaded file names.
        :rtype: list[pathlib.Path]
        """
        # Create a dedicated directory to hold the fetched sources files, from
        # where to generated the downloaded file list.
        build_dir_path = build_dir.path
        sources_outdir = build_dir_path / 'outdir'
        sources_outdir.mkdir()

        sources_cmd = self.command.split()
        sources_cmd.append('--outdir')
        sources_cmd.append(str(sources_outdir))
        self.log.debug('Fetching sources: %r', sources_cmd)
        subprocess.check_call(sources_cmd, cwd=build_dir_path)

        fetched_sources = []
        for file_name in sources_outdir.iterdir():
            shutil.move(str(file_name), str(build_dir_path))
            fetched_sources.append(file_name.relative_to(sources_outdir))
        sources_outdir.rmdir()
        return fetched_sources

    def run(self):
        """
        fetch artefacts
        """
        if not self.command:
            self.log.info('no sources command configuration, skipping plugin')
            return

        self.workflow.build_dir.for_all_platforms_copy(self._fetch_sources)
