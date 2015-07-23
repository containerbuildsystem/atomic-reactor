"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import gzip
try:
    import lzma
except ImportError:
    from backports import lzma
import os

from atomic_reactor.constants import EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import get_exported_image_metadata


class CompressPlugin(PostBuildPlugin):
    """Example configuration:

    "postbuild_plugins": [{
            "name": "compress",
            "args": {
                    "method": "gzip",
                    "load_squashed_image": true
            }
    }]

    Currently supported compression methods are gzip and lzma; gzip is default.
    By default, the plugin doesn't work on squashed image, you have to explicitly
    ask for it by using `load_squashed_image: true`.
    """
    key = 'compress'
    can_fail = False

    # TODO: add remove_former_image?
    def __init__(self, tasker, workflow, load_squashed_image=False, method='gzip'):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param load_squashed_image: bool, when running squash plugin with `dont_load=True`,
                                    you may load the squashed tar with this switch
        """
        super(CompressPlugin, self).__init__(tasker, workflow)
        self.load_squashed_image = load_squashed_image
        self.method = method

    def _compress_image_stream(self, stream):
        outfile = os.path.join(self.workflow.source.workdir,
                               EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE)
        if self.method == 'gzip':
            outfile = outfile.format('gz')
            fp = gzip.open(outfile, 'wb', compresslevel=6)
        elif self.method == 'lzma':
            outfile = outfile.format('xz')
            fp = lzma.open(outfile, 'wb')
        else:
            raise  # TODO

        _chunk_size = 1024**2  # 1 MB chunk size for reading/writing
        self.log.info('compressing image %s to %s using %s method',
                      self.workflow.image, outfile, self.method)
        data = stream.read(_chunk_size)
        while data != b'':
            fp.write(data)
            data = stream.read(_chunk_size)

        return outfile

    def run(self):
        if self.load_squashed_image:
            image = self.workflow.exported_squashed_image.get('path')
            self.log.info('preparing to compress image %s', image)
            with open(image, 'rb') as image_stream:
                outfile = self._compress_image_stream(image_stream)
        else:
            image = self.workflow.image
            self.log.info('fetching image %s from docker', image)
            with self.tasker.d.get_image(image) as image_stream:
                outfile = self._compress_image_stream(image_stream)
        self.workflow.exported_compressed_image.update(get_exported_image_metadata(outfile))
        print(self.workflow.exported_compressed_image)
        self.log.info('compressed image is available as %s', outfile)
