FROM fedora:latest
# e2fsprogs -- docker @ F20 wants it
RUN yum -y install docker-io git python-docker-py python-setuptools GitPython e2fsprogs koji python-pip
RUN mkdir /tmp/atomic-reactor
ADD . /tmp/atomic-reactor
RUN cd /tmp/atomic-reactor && python setup.py install
CMD ["atomic-reactor", "--verbose", "inside-build"]
