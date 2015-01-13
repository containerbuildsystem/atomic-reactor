FROM fedora:latest
# e2fsprogs -- docker @ F20 wants it
RUN yum -y install docker-io git python-docker-py python-setuptools GitPython e2fsprogs koji
ADD ./dock.tar.gz /tmp/
RUN cd /tmp/dock-*/ && python setup.py install
CMD ["dock", "-v", "inside-build"]
