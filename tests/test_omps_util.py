"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import

from io import BytesIO
import os

import pytest

from atomic_reactor.omps_util import OMPS, OMPSError


class TestOMPS(object):

    token = "supersecrettoken"
    omps_url = 'https://omps.example.com'
    omps_namespace = 'test_namespace'

    def test_from_config(self, tmpdir):
        """Test creation of OMPS instance from config"""
        config = {
            'omps_url': self.omps_url,
            'omps_namespace': self.omps_namespace,
            'omps_secret_dir': str(tmpdir)
        }

        token_path = os.path.join(str(tmpdir), 'token')
        with open(token_path, 'w') as f:
            f.write(self.token)
            f.flush()

        omps = OMPS.from_config(config)
        assert omps
        assert omps.organization == self.omps_namespace
        assert omps.url == self.omps_url
        assert omps._token == self.token

    def test_push_archive(self, requests_mock):
        """Test pushing manifest zipfile to omps service"""
        omps_res = {'omps': 'answer'}
        requests_mock.register_uri(
            'POST',
            "{}/v2/{}/zipfile".format(self.omps_url, self.omps_namespace),
            request_headers={'Authorization': self.token},
            json=omps_res
        )
        omps = OMPS(self.omps_url, self.omps_namespace, self.token)
        fb = BytesIO(b'zip file')
        res = omps.push_archive(fb)
        assert res == omps_res

    def test_push_archive_failure(self, requests_mock):
        """Test failure when pushing manifest zipfile to omps service"""
        error = 'ErrorCode'
        emsg = 'Detailed message'
        status_code = 400
        # keep here only one entry to testing, to keep order deterministic
        # after str() call
        validation_info = {'errors': ['CSV is incorrect']}

        omps_res = {
            'error': error, 'message': emsg,
            'validation_info': validation_info}
        requests_mock.register_uri(
            'POST',
            "{}/v2/{}/zipfile".format(self.omps_url, self.omps_namespace),
            request_headers={'Authorization': self.token},
            json=omps_res,
            status_code=status_code,
        )
        omps = OMPS(self.omps_url, self.omps_namespace, self.token)
        fb = BytesIO(b'zip file')
        with pytest.raises(OMPSError) as exc:
            omps.push_archive(fb)
            assert exc.value.status_code == status_code
            assert exc.value.response == omps_res
            assert '(validation errors: {})'.format(validation_info) in str(exc.value)

    def test_push_archive_server_error(self, requests_mock):
        """Service running behind HAProxy may return 503 error without json.
        Test if this case is handled gracefully"""
        status_code = 503
        requests_mock.register_uri(
            'POST',
            "{}/v2/{}/zipfile".format(self.omps_url, self.omps_namespace),
            request_headers={'Authorization': self.token},
            text='Service unavailable',
            status_code=status_code,
        )
        omps = OMPS(self.omps_url, self.omps_namespace, self.token)
        fb = BytesIO(b'zip file')
        with pytest.raises(OMPSError) as exc:
            omps.push_archive(fb)
            assert exc.value.status_code == status_code
            assert exc.value.response == {}
