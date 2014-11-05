"""

"""
import json
import os
import shutil
import tempfile

from dock import CONTAINER_SHARE_PATH, BUILD_JSON, CONTAINER_RESULTS_JSON_PATH, RESULTS_JSON
from dock.core import DockerTasker


class PrivilegedDockerBuilder(object):
    def __init__(self, build_image_id, build_args):
        self.build_image_id = build_image_id
        self.build_args = build_args
        self.temp_dir = None

    def build(self):
        """
        build image from provided build_args

        :return:
        """
        self.temp_dir = tempfile.mkdtemp()
        try:
            with open(os.path.join(self.temp_dir, BUILD_JSON), 'w') as build_json:
                json.dump(self.build_args, build_json)
            dt = DockerTasker()
            container_id = dt.run(
                self.build_image_id,
                create_kwargs={'volumes': [self.temp_dir]},
                start_kwargs={'binds': {self.temp_dir: {'bind': CONTAINER_SHARE_PATH, 'rw': True}},
                              'privileged': True}
            )
            dt.wait(container_id)
            return self.load_results()
        finally:
            shutil.rmtree(self.temp_dir)

    def load_results(self):
        """

        :return:
        """
        if self.temp_dir:
            results_path = os.path.join(self.temp_dir, RESULTS_JSON)
            df_path = os.path.join(self.temp_dir, 'Dockerfile')
            # FIXME: race
            if not os.path.isfile(results_path):
                return None
            with open(results_path, 'r') as results_fp:
                results = json.load(results_fp)
            df = open(df_path, 'r').read()
            return results, df
