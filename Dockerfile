FROM fedora:latest
# e2fsprogs -- docker @ F20 wants it
RUN yum -y install docker-io git python-docker-py python-setuptools GitPython e2fsprogs koji python-pip
RUN mkdir /tmp/dock
ADD . /tmp/dock
RUN cd /tmp/dock && python setup.py install
CMD ["dock", "--verbose", "inside-build", "--input", "path"]
