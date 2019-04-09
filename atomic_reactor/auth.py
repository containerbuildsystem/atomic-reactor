"""
Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import unicode_literals, absolute_import

from requests.auth import AuthBase, HTTPBasicAuth
from requests.cookies import extract_cookies_to_jar
from requests.utils import parse_dict_header
from six.moves.urllib.parse import urlparse
import requests
import re


class HTTPBearerAuth(AuthBase):
    """Performs Bearer authentication for the given Request object.

    username and password are optional. If provided, they will be used
    when fetching the Bearer token from realm. Otherwise, Bearer token
    is retrivied with anonymous access.

    auth_b64 may be provided for authentication (instead of username and
    password).

    Once Bearer token is retrieved, it will be cached and used in subsequent
    requests. Since tokens are specific to repositories, the token cache may
    store multiple tokens.

    Supports registry v2 API only.
    """
    BEARER_PATTERN = re.compile(r'bearer ', flags=re.IGNORECASE)
    V2_REPO_PATTERN = re.compile(r'^/v2/(.*)/(manifests|tags|blobs)/')

    def __init__(self, username=None, password=None, verify=True, access=None, auth_b64=None):
        """Initialize HTTPBearerAuth object.

        :param username: str, username to be used for authentication
        :param password: str, password to be used for authentication
        :param verify: bool, whether or not to verify server identity when
            fetching Bearer token from realm
        :param access: iter<str>, iterable (list, tuple, etc) of access to be
            requested; possible values to be included are 'pull' and/or 'push';
            defaults to ('pull',)
        :param auth_b64: str, base64 credendials as described in RFC 7617
        """
        self.username = username
        self.password = password
        self.auth_b64 = auth_b64
        self.verify = verify
        self.access = access or ('pull',)

        self._token_cache = {}

    def __call__(self, response):
        repo = self._get_repo_from_url(response.url)

        if repo in self._token_cache:
            self._set_header(response, repo)
            return response

        def handle_401_with_repo(response, **kwargs):
            return self.handle_401(response, repo, **kwargs)

        response.register_hook('response', handle_401_with_repo)
        return response

    def handle_401(self, response, repo, **kwargs):
        """Fetch Bearer token and retry."""
        if response.status_code != requests.codes.unauthorized:
            return response

        auth_info = response.headers.get('www-authenticate', '')

        if 'bearer' not in auth_info.lower():
            return response

        self._token_cache[repo] = self._get_token(auth_info, repo)

        # Consume content and release the original connection
        # to allow our new request to reuse the same one.
        # This pattern was inspired by the source code of requests.auth.HTTPDigestAuth
        response.content
        response.close()
        retry_request = response.request.copy()
        extract_cookies_to_jar(retry_request._cookies, response.request, response.raw)
        retry_request.prepare_cookies(retry_request._cookies)

        self._set_header(retry_request, repo)
        retry_response = response.connection.send(retry_request, **kwargs)
        retry_response.history.append(response)
        retry_response.request = retry_request

        return retry_response

    def _get_token(self, auth_info, repo):
        bearer_info = parse_dict_header(self.BEARER_PATTERN.sub('', auth_info, count=1))
        # If repo could not be determined, do not set scope - implies global access
        if repo:
            bearer_info['scope'] = 'repository:{}:{}'.format(repo, ','.join(self.access))
        realm = bearer_info.pop('realm')

        realm_auth = None
        if self.auth_b64:
            realm_auth = HTTPBasicAuthWithB64(self.auth_b64)
        elif self.username and self.password:
            realm_auth = HTTPBasicAuth(self.username, self.password)

        realm_response = requests.get(realm, params=bearer_info, verify=self.verify,
                                      auth=realm_auth)
        realm_response.raise_for_status()
        return realm_response.json()['token']

    def _set_header(self, response, repo):
        response.headers['Authorization'] = 'Bearer {}'.format(self._token_cache[repo])

    def _get_repo_from_url(self, url):
        url_parts = urlparse(url)
        repo = None
        v2_match = self.V2_REPO_PATTERN.search(url_parts.path)
        if v2_match:
            repo = v2_match.group(1)
        return repo


class HTTPBasicAuthWithB64(AuthBase):
    """Performs Basic authentication for the given Request object.

    As in requests.auth.HTTPBasicAuth, but instead of converting
    'username:password' to a base64 string (as per RFC 7617), this class does
    it by receiving the base64 string.
    """
    def __init__(self, auth):
        """Initialize HTTPBasicAuthWithB64 object.

        :param auth_b64: str, base64 credendials as described in RFC 7617
        """
        self.auth = auth

    def __call__(self, response):
        response.headers['Authorization'] = 'Basic {}'.format(self.auth)
        return response


class HTTPRegistryAuth(AuthBase):
    """Custom requests auth handler for constainer registries.

    Supports both Basic Auth and Bearer Auth (v2 API only).

    For v1 API requests, Basic Auth is the only supported
    authentication mechanism.

    For v2 API requests, Basic Auth is attempted first, if
    status code of response is 401, Bearer Auth is then
    attempted.
    """

    V1_URL = re.compile(r'^/v1/')
    V2_URL = re.compile(r'^/v2/')

    def __init__(self, username=None, password=None, access=None, auth_b64=None):
        self.username = username
        self.password = password
        self.access = access
        self.auth_b64 = auth_b64

        self.v1_auth = None
        self.v2_auths = []

    def __call__(self, request):
        url_parts = urlparse(request.url)
        if self.auth_b64:
            basic_auth = HTTPBasicAuthWithB64(self.auth_b64)
        elif self.username and self.password:
            basic_auth = HTTPBasicAuth(self.username, self.password)
        else:
            basic_auth = None

        if self.V1_URL.search(url_parts.path):
            if (not self.username or not self.password) and not self.auth_b64:
                # V1 API only supports basic auth which requires user/pass
                return request

            if not self.v1_auth and basic_auth:
                self.v1_auth = basic_auth

            return self.v1_auth(request)

        if self.V2_URL.search(url_parts.path):

            if not self.v2_auths:
                # It's safe to always add bearer auth handler because
                # it's only activated if indicated by www-authenticate response header
                self.v2_auths.append(HTTPBearerAuth(self.username, self.password,
                                                    access=self.access, auth_b64=self.auth_b64))

                if basic_auth:
                    self.v2_auths.append(basic_auth)

            for auth in self.v2_auths:
                request = auth(request)
                if 'authorization' in (k.lower() for k in request.headers.keys()):
                    # One of the auth handlers has a token for the request
                    break

        return request
