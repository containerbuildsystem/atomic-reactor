"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import gzip
try:
    # if we import "lzma" first, we get pyliblzma on Py2, but we want backports.lzma
    #  so first try to import backports.lzma on Py2 and then 'lzma' on Py3
    from backports import lzma
except ImportError:
    import lzma
import os

from atomic_reactor.constants import EXPORTED_COMPRESSED_IMAGE_NAME_TEMPLATE
from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import get_exported_image_metadata, human_size


class CompressPlugin(PostBuildPlugin):
    """Example configuration:

    "postbuild_plugins": [{
            "name": "compress",
            "args": {
                    "method": "gzip",
                    "load_exported_image": true
            }
    }]

    Currently supported compression methods are gzip and lzma; gzip is default.
    By default, the plugin doesn't work on exported image, you have to explicitly
    ask for it by using `load_exported_image: true`.
    """
    key = 'compress'
    is_allowed_to_fail = False

    # TODO: add remove_former_image?
    def __init__(self, tasker, workflow, load_exported_image=False, method='gzip'):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param load_exported_image: bool, when running squash plugin with `dont_load=True`,
                                    you may load the exported tar with this switch
        """
        super(CompressPlugin, self).__init__(tasker, workflow)
        self.load_exported_image = load_exported_image
        self.method = method
        self.uncompressed_size = 0

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
            raise RuntimeError('Unsupported compression format {0}'.format(self.method))

        _chunk_size = 1024**2  # 1 MB chunk size for reading/writing
        self.log.info('compressing image %s to %s using %s method',
                      self.workflow.image, outfile, self.method)
        data = stream.read(_chunk_size)
        while data != b'':
            fp.write(data)
            data = stream.read(_chunk_size)

        self.uncompressed_size = stream.tell()

        return outfile

    def run(self):
        if self.load_exported_image:
            if len(self.workflow.exported_image_sequence) == 0:
                raise RuntimeError('load_exported_image used, but no exported image')
            image = self.workflow.exported_image_sequence[-1].get('path')
            self.log.info('preparing to compress image %s', image)
            with open(image, 'rb') as image_stream:
                outfile = self._compress_image_stream(image_stream)
        else:
            image = self.workflow.image
            self.log.info('fetching image %s from docker', image)
            with self.tasker.d.get_image(image) as image_stream:
                outfile = self._compress_image_stream(image_stream)
        metadata = get_exported_image_metadata(outfile)

        if self.uncompressed_size != 0:
            metadata['uncompressed_size'] = self.uncompressed_size
            savings = 1 - metadata['size'] / float(metadata['uncompressed_size'])
            self.log.debug('uncompressed: %s, compressed: %s, ratio: %.2f %% saved',
                           human_size(metadata['uncompressed_size']),
                           human_size(metadata['size']),
                           100*savings)

        self.workflow.exported_image_sequence.append(metadata)
        self.log.info('compressed image is available as %s', outfile)
