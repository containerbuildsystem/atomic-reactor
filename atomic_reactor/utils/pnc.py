"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os
import re

from atomic_reactor.util import get_retrying_requests_session


def get_scm_archive_from_build_id(api_request_url: str, build_id: str, session=None):
    """
    Return a scm archive URL and filename from PNC REST API.
    :param api_request_url: str, format string to form request url
    :param build_id: str PNC build id
    :param session: existing requests session to use
    :return str, str URL and filename of the scm archive
    :rtype str, str, URL and filename
    """
    if not session:
        session = get_retrying_requests_session()

    # This endpoint for the API is redirected to the actual source URL which
    #  we don't want to download yet, so we're disabling the redirect to just
    #  get the redirect location in response header
    response = session.get(api_request_url.format(build_id), allow_redirects=False)
    response.raise_for_status()

    url = response.headers.get('Location')
    dest_filename = os.path.basename(url)

    # Often the SCM URL will be gerrit URL which do no have the filename in URL
    # So we'll have to get the filename from the header if it's not valid.
    if not re.fullmatch(r'^[\w\-.]+$', dest_filename):
        dest_filename = session.head(url).headers.get(
            "Content-disposition").split("filename=")[1].replace('"', '')

    return url, dest_filename
