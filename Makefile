.PHONY: build-buildimage tarball build-buildimage

clean:
	rm -vf dist/*

tarball: clean
	python setup.py sdist

build-buildimage: tarball
	cp -a dist/dock-*.tar.gz images/privileged-builder/
	cd images/privileged-builder/ && \
		docker build --rm -t buildroot-fedora .
	rm -fv images/privileged-builder/dock-*.tar.gz

q-build-buildimage: tarball
	cp -a dist/dock-*.tar.gz images/privileged-builder/
	cd images/privileged-builder/ && \
		docker build -t buildroot-fedora .
	rm -fv images/privileged-builder/dock-*.tar.gz

